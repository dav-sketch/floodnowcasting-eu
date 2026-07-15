"""FloodNowcasting.eu 2.0 - operational pipeline (calibrated RainViewer radar).

Flow per cycle:
  RainViewer radar tiles -> decode to mm/h -> CALIBRATION factor -> per-catchment
  areal-mean mm for each *new* frame -> rolling store (deduped by timestamp,
  pruned to WINDOW_H) -> accumulate each basin over ITS OWN response time ->
  ARF-reduced 10-y comparison (De Michele 2001) -> severity = return period
  (Ceresetti et al. 2012) -> colours + outputs.

Radar sees convective cells (unlike a smoothing model); CAL_FACTOR fixes the tile
over-read. The store grows across runs, so 6-10 h windows build up over time even
though RainViewer exposes only ~2 h of past frames per call. Run via run.py.
"""
import io, math, json
from pathlib import Path
import numpy as np, pandas as pd, requests
import geopandas as gpd
from shapely.geometry import box
from PIL import Image

import config as C

# ----------------------------------------------------------------------
# Poschlod 10-year return levels (threshold)  - shared cache with notebook
# ----------------------------------------------------------------------
ZENODO = "https://zenodo.org/records/3878888/files/ReturnLevel_10year_{d}.txt?download=1"
DURATIONS = ["1h", "3h", "6h", "12h", "24h"]
_DUR_H = np.array([1, 3, 6, 12, 24], float)
_RL, _trees = {}, {}

def _load_returnlevels():
    for d in DURATIONS:
        f = C.CACHE / f"returnlevel_10y_{d}.txt"
        if not f.exists():
            print("  downloading Poschlod", d)
            r = requests.get(ZENODO.format(d=d), timeout=180); r.encoding = "utf-8"
            f.write_text(r.text, encoding="utf-8")
        df = pd.read_csv(f); df.columns = ["q5", "median", "q95", "lat", "lon"]
        _RL[d] = df

def _tree(d):
    if d not in _trees:
        from scipy.spatial import cKDTree
        df = _RL[d]
        _trees[d] = (cKDTree(np.c_[df.lon.values, df.lat.values]), df["median"].values)
    return _trees[d]

def threshold_point_mm(lon, lat, D_h):
    """Point 10-y level (mm) at arbitrary duration, log-log interpolated."""
    depths = []
    for d in DURATIONS:
        tree, vals = _tree(d)
        _, i = tree.query([lon, lat]); depths.append(vals[i])
    depths = np.array(depths)
    D = min(max(D_h, _DUR_H[0]), _DUR_H[-1])
    return float(np.exp(np.interp(np.log(D), np.log(_DUR_H), np.log(depths))))

# ----------------------------------------------------------------------
# hydrology / severity helpers
# ----------------------------------------------------------------------
def response_time_h(area_km2):
    return float(max(0.5, 0.9 * (max(area_km2, 1.0) ** 0.38)))

def arf(area_km2, D_h):
    """De Michele-Kottegoda-Rosso (2001) areal reduction factor in (0,1]."""
    A_star = max(area_km2 - C.ARF_A0_KM2, 0.0)
    if A_star <= 0.0:
        return 1.0
    base = 1.0 + C.ARF_VARPI * (A_star**C.ARF_Z / max(D_h, 0.05))**C.ARF_B
    return float(min(1.0, base**(-C.ARF_V / C.ARF_B)))

_ra = np.array([a for a, _ in C.RP_ANCHORS]); _rlnT = np.log([t for _, t in C.RP_ANCHORS])
def est_return_period(ratio):
    if ratio <= 0: return 0.0
    if ratio < _ra[0]:
        s = (_rlnT[1]-_rlnT[0])/(_ra[1]-_ra[0]); lnT = _rlnT[0]+s*(ratio-_ra[0])
    elif ratio > _ra[-1]:
        s = (_rlnT[-1]-_rlnT[-2])/(_ra[-1]-_ra[-2]); lnT = _rlnT[-1]+s*(ratio-_ra[-1])
    else:
        lnT = np.interp(ratio, _ra, _rlnT)
    return float(np.exp(lnT))

def classify(T, ratio, acc):
    if T >= C.RP_COLORS[2][0]: return C.RP_COLORS[2][1], C.RP_COLORS[2][2]
    if T >= C.RP_COLORS[1][0]: return C.RP_COLORS[1][1], C.RP_COLORS[1][2]
    if T >= C.RP_COLORS[0][0]: return C.RP_COLORS[0][1], C.RP_COLORS[0][2]
    if ratio >= C.WATCH_RATIO: return "#c7e9b4", "watch"
    col = None
    for mm, c in C.RAIN_TIERS:          # wet but below watch -> rainfall shading
        if acc >= mm: col = c
    if col: return col, "rain"
    return "#2b8cbe22", "none"

# ----------------------------------------------------------------------
# fixed domain grid + RainViewer decode (grid deterministic from bbox+z)
# ----------------------------------------------------------------------
def _deg2tile(lon, lat, z):
    n = 2**z; lr = math.radians(lat)
    return ((lon+180)/360*n, (1-math.log(math.tan(lr)+1/math.cos(lr))/math.pi)/2*n)

def domain_grid():
    z = C.TILE_Z; b = C.DOMAIN_BBOX
    x0, _ = _deg2tile(b[0], b[3], z); _, y0 = _deg2tile(b[0], b[3], z)
    x1, _ = _deg2tile(b[2], b[1], z); _, y1 = _deg2tile(b[2], b[1], z)
    xr = range(int(x0), int(x1)+1); yr = range(int(y0), int(y1)+1)
    W, H = 256*len(xr), 256*len(yr)
    tx0, ty0 = int(list(xr)[0]), int(list(yr)[0])
    jj, ii = np.meshgrid(np.arange(W), np.arange(H))
    n = 2**z
    lon = (tx0 + (jj+0.5)/256)/n*360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi*(1-2*(ty0+(ii+0.5)/256)/n))))
    return xr, yr, lon, lat

_pal_rgb = np.array([c for _, c in C.PALETTE], float)
_pal_dbz = np.array([d for d, _ in C.PALETTE], float)
def _decode(img):
    rgb, a = img[..., :3].astype(float), img[..., 3]
    d2 = ((rgb[..., None, :] - _pal_rgb[None, None, :])**2).sum(-1)
    dbz = np.clip(_pal_dbz[np.argmin(d2, -1)], 0.0, C.DBZ_MAX)
    rate = (10.0**(dbz/10.0)/C.ZR_A)**(1.0/C.ZR_B)     # mm/h (uncalibrated)
    rate[a < C.RAIN_ALPHA_MIN] = 0.0
    return rate

_BLANK = np.zeros((256, 256, 4), np.uint8)
def _get_tile(url, tries=3):
    """Fetch one radar tile; transient errors -> blank (dry) tile, never crash."""
    for k in range(tries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                return np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))
            return _BLANK
        except requests.RequestException:
            if k == tries - 1:
                return _BLANK
    return _BLANK

def fetch_frame_rate(host, path, xr, yr):
    cols = []
    for tx in xr:
        rows = [_get_tile(f"{host}{path}/256/{C.TILE_Z}/{tx}/{ty}/2/0_0.png") for ty in yr]
        cols.append(np.concatenate(rows, axis=0))
    return _decode(np.concatenate(cols, axis=1))

def rainviewer_frames():
    j = requests.get(C.RV_JSON, timeout=30).json()
    return j["host"], j["radar"]["past"]

# ----------------------------------------------------------------------
# precompute: catchments (+thresholds) and radar-pixel -> catchment map
# ----------------------------------------------------------------------
def _level_paths(lv):
    return {"attrs": C.STATE / f"attrs_L{lv}.csv",       # small per-cycle table (no geometry)
            "pmap":  C.STATE / f"pixel_cat_L{lv}.npz",    # compressed grid->basin map
            "store": C.STATE / f"frames_L{lv}.csv",
            "geo":   C.WEBDATA / f"catchments_L{lv}.geojson",
            "alerts": C.WEBDATA / f"alerts_L{lv}.json"}

def _build_level(lv, glon, glat):
    print(f"  building level {lv} ...")
    shp = C.HYBAS_DIR / f"hybas_lake_eu_lev{lv:02d}_v1c.shp"
    aoi = box(*C.DOMAIN_BBOX)
    g = gpd.read_file(shp, bbox=C.DOMAIN_BBOX)
    g = g[g.intersects(aoi)].copy().reset_index(drop=True)
    g["HYBAS_ID"] = g["HYBAS_ID"].astype("int64").astype(str)
    g["t_lag_h"]  = g["UP_AREA"].apply(response_time_h)
    g["D_test_h"] = np.minimum(g["t_lag_h"], C.WINDOW_H)
    rp = g.geometry.representative_point()
    g["cen_x"], g["cen_y"] = rp.x.values, rp.y.values
    g["arf"]    = [arf(a, d) for a, d in zip(g["UP_AREA"], g["D_test_h"])]
    g["thr_pt"] = [threshold_point_mm(x, y, d) for x, y, d in zip(g.cen_x, g.cen_y, g.D_test_h)]
    g["thr_mm"] = g["thr_pt"] * g["arf"]

    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(glon.ravel(), glat.ravel()), crs="EPSG:4326")
    j = gpd.sjoin(pts, g[["geometry"]].reset_index(), predicate="within", how="left")
    j = j[~j.index.duplicated(keep="first")].sort_index()
    pixel_cat = np.where(j["index"].isna(), -1, j["index"]).astype(np.int32)

    p = _level_paths(lv)
    g.drop(columns="geometry").to_csv(p["attrs"], index=False)   # tiny table CI reads each cycle
    np.savez_compressed(p["pmap"], pixel_cat=pixel_cat)
    if lv in C.POLY_LEVELS:                                       # L9 geometry too big to serve whole
        _export_web_geometry(g, lv, p["geo"])
    print(f"    L{lv}: {len(g)} basins, {(pixel_cat>=0).sum()} px inside"
          f"{'' if lv in C.POLY_LEVELS else ' (pins only)'}")

def _level_ready(lv):
    need = ["attrs", "pmap"] + (["geo"] if lv in C.POLY_LEVELS else [])
    return all(_level_paths(lv)[k].exists() for k in need)

def precompute(force=False):
    xr, yr, glon, glat = domain_grid()
    todo = [lv for lv in C.LEVELS if force or not _level_ready(lv)]
    if todo:
        print("precompute: loading Poschlod ...")
        _load_returnlevels()
        print(f"precompute: grid {glon.shape}; building levels {todo} ...")
        for lv in todo:
            _build_level(lv, glon, glat)
    _write_manifest()

def load_level(lv):
    p = _level_paths(lv)
    df = pd.read_csv(p["attrs"]); df["HYBAS_ID"] = df["HYBAS_ID"].astype(str)
    return df, np.load(p["pmap"])["pixel_cat"]

def _write_manifest():
    man = {"levels": [{"level": lv, "minzoom": C.LEVEL_MINZOOM.get(lv, 0),
                       "severity": lv in C.SEVERITY_LEVELS,
                       "geometry": f"catchments_L{lv}.geojson", "alerts": f"alerts_L{lv}.json"}
                      for lv in C.POLY_LEVELS],
           "pins": {"file": "pins.geojson", "radius": {str(k): v for k, v in C.PIN_RADIUS.items()}},
           "acc": {"windows_h": C.ACC_WINDOWS_H, "default_h": C.ACC_DEFAULT_H,
                   "ramp": [[mm, c] for mm, c in C.ACC_RAMP], "min_mm": C.RAIN_MIN_MM},
           "severity_levels": C.SEVERITY_LEVELS,
           "map": {"minzoom": C.MAP_MINZOOM, "maxzoom": C.MAP_MAXZOOM},
           "radar_maxzoom": C.RADAR_MAXZOOM, "domain": C.DOMAIN_BBOX}
    (C.WEBDATA / "levels.json").write_text(json.dumps(man, indent=1))

def _export_web_geometry(g, lv, out):
    """Static, simplified catchment polygons for the web frontend (once per level)."""
    w = g[["HYBAS_ID", "UP_AREA", "t_lag_h", "D_test_h", "thr_mm", "arf", "geometry"]].copy()
    w["geometry"] = w.geometry.simplify(C.SIMPLIFY.get(lv, 0.01), preserve_topology=True)
    for c in ["UP_AREA", "t_lag_h", "D_test_h", "thr_mm"]:
        w[c] = w[c].round(1)
    w["arf"] = w["arf"].round(2)
    if out.exists(): out.unlink()
    try:
        w.to_file(out, driver="GeoJSON", COORDINATE_PRECISION=C.COORD_PRECISION)
    except TypeError:
        w.to_file(out, driver="GeoJSON")
    print(f"    web geometry -> {out.name} ({out.stat().st_size//1024} KB)")

# ----------------------------------------------------------------------
# rolling store (per level): table [frame_time x HYBAS_ID] of calibrated mm
# ----------------------------------------------------------------------
def load_store(lv):
    p = _level_paths(lv)["store"]
    if not p.exists(): return pd.DataFrame()
    df = pd.read_csv(p, index_col=0); df.index = df.index.astype("int64")
    df.columns = [str(c) for c in df.columns]; return df
def save_store(lv, df): df.to_csv(_level_paths(lv)["store"])

def areal_mean_mm(rate, pixel_cat, n_cat):
    """Per-catchment mean CALIBRATED mm for one frame."""
    mm = rate.ravel() * (C.FRAME_INTERVAL_MIN/60.0) * C.CAL_FACTOR
    valid = pixel_cat >= 0
    idx = pixel_cat[valid]; w = mm[valid]
    tot = np.bincount(idx, weights=w, minlength=n_cat)
    cnt = np.bincount(idx, minlength=n_cat)
    return np.divide(tot, cnt, out=np.zeros(n_cat), where=cnt > 0)

# ---- rolling-store algebra (the no-double-count guarantee lives here) ----
# The store is keyed by each radar frame's UNIX timestamp. merge_frames dedupes
# on that key, so a given 10-min frame is stored exactly once no matter how many
# overlapping 2-h fetches (at any cadence) observe it. window_accumulate then
# sums each stored frame once -> the 6-10 h total is cadence-independent and is
# never inflated by re-seeing the same frame.
def merge_frames(store, rows):
    if rows:
        parts = ([store] if len(store) else []) + [r.to_frame().T for r in rows]
        store = pd.concat(parts)
    if len(store):
        store = store[~store.index.duplicated(keep="last")].sort_index()   # dedupe by timestamp
    return store

def prune_store(store, window_h):
    if not len(store): return store
    now_t = int(store.index.max())
    return store[store.index >= now_t - window_h*3600]

def window_accumulate(store, hids, d_test):
    """Per-basin rainfall sum over each basin's trailing D_test window (mm),
    plus (span_h, now_t). Each stored frame contributes exactly once."""
    times = store.index.to_numpy(dtype=float)
    now_t = times.max(); age_h = (now_t - times) / 3600.0; span_h = (now_t - times.min()) / 3600.0
    acc = np.zeros(len(hids))
    for i, (hid, dt) in enumerate(zip(hids, d_test)):
        col = store[hid].to_numpy(dtype=float)
        acc[i] = float(np.nansum(col[age_h <= dt]))
    return acc, span_h, now_t

def fixed_window_accum(store, hids, windows_h):
    """Per-basin rainfall sum over each FIXED trailing window (mm).
    Returns {window_h: np.array over hids}. Each stored frame contributes once."""
    times = store.index.to_numpy(dtype=float)
    now_t = times.max(); age_h = (now_t - times) / 3600.0
    arr = store.reindex(columns=hids).to_numpy(dtype=float)   # rows=frames, cols=basins (hids order)
    return {w: np.nan_to_num(arr[age_h <= w, :]).sum(axis=0) for w in windows_h}

# ----------------------------------------------------------------------
# one cycle - decode each new frame ONCE, map it to every level
# ----------------------------------------------------------------------
def run_once(verbose=True):
    precompute()
    xr, yr, _, _ = domain_grid()
    host, past = rainviewer_frames()
    levels = {lv: load_level(lv) for lv in C.LEVELS}
    stores = {lv: load_store(lv) for lv in C.LEVELS}
    have = {lv: set(int(t) for t in stores[lv].index) for lv in C.LEVELS}
    new_rows = {lv: [] for lv in C.LEVELS}

    todo = [f for f in past if any(int(f["time"]) not in have[lv] for lv in C.LEVELS)]
    if verbose: print(f"cycle: {len(past)} frames available, {len(todo)} new to decode (shared across levels)")
    for f in todo:
        rate = fetch_frame_rate(host, f["path"], xr, yr)
        t = int(f["time"])
        for lv in C.LEVELS:
            if t in have[lv]: continue
            cat, pmap = levels[lv]
            new_rows[lv].append(pd.Series(areal_mean_mm(rate, pmap, len(cat)),
                                          index=cat["HYBAS_ID"].tolist(), name=t))

    out = {}; pins = []
    for lv in C.LEVELS:
        st = prune_store(merge_frames(stores[lv], new_rows[lv]), C.WINDOW_H)
        if len(st): save_store(lv, st)
        summary, lv_pins = _severity_for_level(levels[lv][0], st, host, past, lv)
        out[lv] = summary; pins += lv_pins
    _write_pins(pins)
    if verbose:
        for lv in C.LEVELS:
            r = out[lv]
            print(f"  L{lv}: {r['wet']} wet | {r['nalert']} >=10y | "
                  f"max acc {r['maxacc']:.1f} mm | max T {r['maxT']:.0f} y")
        print(f"  pins: {len(pins)} alerting basins across levels")
    return out

def _severity_for_level(cat, store, host, past, lv):
    is_sev = lv in C.SEVERITY_LEVELS
    if not len(store):
        if lv in C.POLY_LEVELS:
            empty = cat.assign(**{f"acc_{w}h": 0.0 for w in C.ACC_WINDOWS_H})
            empty = empty.assign(acc_mm=0.0, coverage=0.0, ratio=0.0, T_years=0.0,
                                 color="#2b8cbe22", label="none")
            _write_level_alerts(empty, int(past[-1]["time"]), 0.0, 0, host, past, lv)
        return {"wet": 0, "nalert": 0, "maxacc": 0.0, "maxT": 0.0}, []
    hids = cat["HYBAS_ID"].tolist()
    cat = cat.copy()

    # fixed-window accumulation (all served levels, drives the "Rain" view selector)
    accw = fixed_window_accum(store, hids, C.ACC_WINDOWS_H)
    for w in C.ACC_WINDOWS_H:
        cat[f"acc_{w}h"] = np.round(accw[w], 1)

    if is_sev:
        # severity over each basin's OWN response time (the Ceresetti diagram method)
        dtest = cat["D_test_h"].to_numpy()
        acc, span_h, now_t = window_accumulate(store, hids, dtest)
        cov = np.minimum(span_h, dtest) / dtest
        cat["acc_mm"] = acc; cat["coverage"] = np.round(cov, 2)
        cat["ratio"] = (cat["acc_mm"] / cat["thr_mm"].replace(0, np.nan)).fillna(0.0)
        cat["T_years"] = cat["ratio"].apply(est_return_period)
        cc = [classify(T, r, a) for T, r, a in zip(cat["T_years"], cat["ratio"], cat["acc_mm"])]
        cat["color"] = [c for c, _ in cc]; cat["label"] = [l for _, l in cc]
    else:
        # accumulation-only level (L6/L7): no severity classification
        times = store.index.to_numpy(dtype=float)
        now_t = times.max(); span_h = (now_t - times.min()) / 3600.0
        cat["acc_mm"] = 0.0; cat["coverage"] = 0.0; cat["ratio"] = 0.0
        cat["T_years"] = 0.0; cat["color"] = "#2b8cbe22"; cat["label"] = "none"

    if lv in C.POLY_LEVELS:
        _write_level_alerts(cat, int(now_t), span_h, len(store), host, past, lv)

    pins = [{"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [round(r.cen_x, 4), round(r.cen_y, 4)]},
             "properties": {"level": lv, "color": r.color, "label": r.label,
                            "T": round(r.T_years), "acc_mm": round(r.acc_mm, 1),
                            "thr_mm": round(r.thr_mm, 1), "HYBAS_ID": r.HYBAS_ID}}
            for r in cat.itertuples() if r.label in C.PIN_LABELS] if is_sev else []

    maxacc = float(cat[f"acc_{C.ACC_WINDOWS_H[-1]}h"].max())      # longest-window peak accumulation
    return ({"wet": int((cat[f"acc_{C.ACC_WINDOWS_H[-1]}h"] > 0.2).sum()),
             "nalert": int(cat["label"].isin(["~10y", "~30y", ">=100y"]).sum()),
             "maxacc": maxacc, "maxT": float(cat["T_years"].max())}, pins)

def _write_pins(features):
    (C.WEBDATA / "pins.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}))

def _write_level_alerts(cat, now_t, span_h, n_frames, host, past, lv):
    is_sev = lv in C.SEVERITY_LEVELS
    meta = {"generated_unix": int(now_t), "store_span_h": round(span_h, 2), "n_frames": int(n_frames),
            "level": lv, "severity": is_sev, "acc_windows_h": C.ACC_WINDOWS_H,
            "domain": C.DOMAIN_BBOX, "qpe": "rainviewer-calibrated",
            "cal_factor": C.CAL_FACTOR, "rainviewer_host": host, "rainviewer_path": past[-1]["path"]}
    # Include a basin if it has any measurable rain in any fixed window, or (on a
    # severity level) it carries a severity label. acc = {window_h: mm}.
    alerts = {}
    for r in cat.itertuples():
        acc = {str(w): getattr(r, f"acc_{w}h") for w in C.ACC_WINDOWS_H}
        label = getattr(r, "label", "none")
        if not (max(acc.values()) >= C.RAIN_MIN_MM or (is_sev and label != "none")):
            continue
        entry = {"acc": acc}
        if is_sev:
            entry.update({"T": round(r.T_years), "ratio": round(r.ratio, 2),
                          "acc_mm": round(r.acc_mm, 1), "color": r.color,
                          "label": label, "coverage": r.coverage})
        alerts[r.HYBAS_ID] = entry
    _level_paths(lv)["alerts"].write_text(json.dumps({"meta": meta, "alerts": alerts}, indent=1))
