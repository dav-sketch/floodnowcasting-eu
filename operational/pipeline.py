"""FloodNowcasting.eu 2.0 - operational pipeline (OPERA radar + RainViewer gap-fill).

Flow per cycle:
  anchor T = newest OPERA 1-h accumulation end time
  -> for each of the last WINDOW_H hour slots (T-(k+1)h, T-kh]:
       OPERA ACRR(T-kh) per-basin areal mean   (downloaded once, cached in the store;
                                                  re-downloadable for 24 h -> gap-proof)
       else RainViewer 10-min frames in that slot (only basins OPERA does not see)
       else MISSING (coverage < 1, never a silent 0)
  -> fixed-window sums (2/4/8/12 h) and each basin's OWN response-time sum
  -> ARF-reduced 10-y comparison (De Michele 2001) -> severity = return period
     (Ceresetti et al. 2012) -> colours + outputs.

Accumulation is in whole-hour slots anchored on the data time, not on the run
time, so the result does not depend on when / how irregularly the runs happen.
Run via run.py.
"""
import io, math, json, time
from pathlib import Path
import numpy as np, pandas as pd, requests
import geopandas as gpd
from shapely.geometry import box
from PIL import Image

import config as C
import qpe as Q

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
    """Point 10-y level (mm) at arbitrary duration, log-log interpolated.
    (Poschlod-based; RETAINED for reference - severity now uses the DDF fit below.)"""
    depths = []
    for d in DURATIONS:
        tree, vals = _tree(d)
        _, i = tree.query([lon, lat]); depths.append(vals[i])
    depths = np.array(depths)
    D = min(max(D_h, _DUR_H[0]), _DUR_H[-1])
    return float(np.exp(np.interp(np.log(D), np.log(_DUR_H), np.log(depths))))

# ----------------------------------------------------------------------
# DDF 10-year thresholds (user log-log fit; REPLACES Poschlod for severity)
# ----------------------------------------------------------------------
# Per EURO-CORDEX grid point the file gives a (slope) & b (intercept) of a
# log10(depth) vs log10(duration) line fit on the 3-24 h 10-y levels. The 10-y
# depth at the catchment response time D (h) is then
#     depth(mm) = 10 ** (a * log10(D) + b)   ==   (10**b) * D**a .
# a,b are fit on 3-24 h, so D is clamped to [DDF_D_MIN_H, DDF_D_MAX_H].
_ddf = {}
def _load_ddf():
    if _ddf: return
    f = C.DDF_FILE
    if not f.exists():
        raise FileNotFoundError(
            f"DDF parameter file not found: {f}\n"
            "It is required at precompute time (bakes the severity thresholds).")
    d = np.genfromtxt(f, skip_header=1, delimiter="\t")   # cols: lat lon a b 1h_10y 24h_10y
    d = d[~np.isnan(d).any(axis=1)]
    from scipy.spatial import cKDTree
    _ddf["tree"] = cKDTree(np.c_[d[:, 1], d[:, 0]])       # (lon, lat)
    _ddf["a"], _ddf["b"] = d[:, 2], d[:, 3]
    print(f"  loaded DDF: {len(d)} grid points "
          f"(lat {d[:,0].min():.1f}..{d[:,0].max():.1f}, "
          f"lon {d[:,1].min():.1f}..{d[:,1].max():.1f})")

def ddf_depth_mm(lons, lats, D_h):
    """Vectorised 10-y depth (mm) at per-point duration D_h from the nearest DDF
    grid point. Accepts scalars or arrays; returns a float or np.ndarray."""
    _load_ddf()
    scalar = np.isscalar(lons)
    lons = np.atleast_1d(np.asarray(lons, float)); lats = np.atleast_1d(np.asarray(lats, float))
    D = np.clip(np.atleast_1d(np.asarray(D_h, float)), C.DDF_D_MIN_H, C.DDF_D_MAX_H)
    _, idx = _ddf["tree"].query(np.c_[lons, lats])
    depth = 10.0 ** (_ddf["a"][idx] * np.log10(D) + _ddf["b"][idx])
    return float(depth[0]) if scalar else depth

# ----------------------------------------------------------------------
# hydrology / severity helpers
# ----------------------------------------------------------------------
def response_time_h(area_km2):
    return float(max(0.5, 0.9 * (max(area_km2, 1.0) ** 0.38)))

_AW, _AA, _AB, _AV = C.ARF_SETS[C.ARF_SET]          # (varpi, a, b, v)

def arf_raw(area_km2, D_h):
    """De Michele-Kottegoda-Rosso (2001) Eq. 14 == Ceresetti et al. (2012) Eq. 3:
        ARF(A,D) = [1 + varpi * A**a / D**b] ** (-v/b),   A in km2, D in h.
    Vectorised; the areal exponent is `a` (NOT the dynamic-scaling z = a/b)."""
    A = np.maximum(np.asarray(area_km2, float), 0.0)
    D = np.maximum(np.asarray(D_h, float), 0.05)
    return (1.0 + _AW * A**_AA / D**_AB) ** (-_AV / _AB)

def arf(area_km2, D_h):
    """ARF normalised to the DDF grid-cell area (config.ARF_REF_AREA_KM2), since
    the 10-y "point" level is itself a ~156 km2 cell mean: a basin no bigger than
    one cell gets 1.0, larger basins get a real reduction. Returns (0,1]."""
    out = np.minimum(arf_raw(area_km2, D_h) / arf_raw(C.ARF_REF_AREA_KM2, D_h), 1.0)
    return out if np.ndim(out) else float(out)

def arf_table(areas=(50, 160, 500, 1000, 2000, 5000, 10000, 50000),
              durations=(2, 4, 8, 12, 24)):
    """Diagnostic: print the active ARF set over an area x duration grid."""
    print(f"ARF set '{C.ARF_SET}' (varpi={_AW}, a={_AA}, b={_AB}, v={_AV}), "
          f"referenced to {C.ARF_REF_AREA_KM2:.0f} km2, area from {C.ARF_AREA_FIELD}")
    print("  A km2 | " + " ".join(f"{d:>6}h" for d in durations))
    for A in areas:
        print(f"  {A:6d} | " + " ".join(f"{arf(A, d):7.3f}" for d in durations))

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

# Exact colour -> dBZ lookup in RainViewer's official Universal Blue table.
# Each RGBA is packed into one uint32 key; unsmoothed tiles use only these colours.
def _pack(rgba):
    rgba = np.asarray(rgba, np.uint32)
    return (rgba[..., 0] << 24) | (rgba[..., 1] << 16) | (rgba[..., 2] << 8) | rgba[..., 3]

_lut = {}
for _k, _h in enumerate(C.RV_UNIVERSAL_BLUE):
    _rgba = tuple(int(_h[i:i+2], 16) for i in (0, 2, 4, 6))
    _lut.setdefault(_rgba, C.RV_UNIVERSAL_BLUE_DBZ0 + _k)   # first dBZ wins (65+ all white)
_lut_key = _pack(np.array(list(_lut.keys())))
_order = np.argsort(_lut_key)
_lut_key = _lut_key[_order]
_lut_rgba = np.array(list(_lut.keys()), float)[_order]
_lut_dbz = np.array(list(_lut.values()), float)[_order]

def _decode(img):
    """RGBA tile mosaic -> rain rate (mm/h, uncalibrated)."""
    flat = img.reshape(-1, 4)
    dbz = np.full(len(flat), -np.inf)
    echo = flat[:, 3] > 0                                   # transparent = no echo
    px = flat[echo]
    key = _pack(px)
    pos = np.clip(np.searchsorted(_lut_key, key), 0, len(_lut_key) - 1)
    hit = _lut_key[pos] == key
    d = _lut_dbz[pos]
    if not hit.all():                                       # stray colour -> nearest RGBA entry
        miss = px[~hit].astype(float)
        d[~hit] = _lut_dbz[np.argmin(((miss[:, None, :] - _lut_rgba[None]) ** 2).sum(-1), 1)]
    dbz[echo] = d
    rate = (10.0**(np.minimum(dbz, C.DBZ_MAX)/10.0)/C.ZR_A)**(1.0/C.ZR_B)   # mm/h
    rate[dbz < C.DBZ_MIN] = 0.0
    return rate.reshape(img.shape[:2])

def fetch_frame_rate(host, path, xr, yr, tiles=None):
    """Decode one RainViewer frame over the domain grid (rate-limited download).
    Returns (mm/h with NaN where a tile was not requested / not obtained, n_failed)."""
    return Q.rv_fetch_frame(host, path, xr, yr, _decode, tiles)

def rainviewer_frames():
    return Q.rainviewer_frames()

# ----------------------------------------------------------------------
# precompute: catchments (+thresholds) and radar-pixel -> catchment map
# ----------------------------------------------------------------------
def _level_paths(lv):
    return {"attrs": C.STATE / f"attrs_L{lv}.csv",       # small per-cycle table (no geometry)
            "pmap":  C.STATE / f"pixel_cat_L{lv}.npz",    # compressed grid->basin map
            "opera": C.STATE / f"opera_L{lv}.csv",       # [ACRR end time x basin] hourly mm
            "rv":    C.STATE / f"rv_L{lv}.csv",          # [frame time x basin] 10-min mm (NaN = unseen)
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
    # DDF duration = response time ROUNDED to whole hours (>=1, capped by WINDOW_H).
    # This same window is used for the accumulation, so obs and threshold match.
    g["D_ddf_h"]  = np.clip(np.round(g["D_test_h"]), 1.0, C.WINDOW_H)
    rp = g.geometry.representative_point()
    g["cen_x"], g["cen_y"] = rp.x.values, rp.y.values
    if C.ARF_AREA_FIELD not in g.columns:
        raise KeyError(f"config.ARF_AREA_FIELD = {C.ARF_AREA_FIELD!r} is not a column of "
                       f"{shp.name}; available: {sorted(g.columns)}")
    # ARF over the area the radar accumulation is averaged over (the polygon).
    g["arf"]    = arf(g[C.ARF_AREA_FIELD].to_numpy(), g["D_ddf_h"].to_numpy())
    g["thr_pt"] = ddf_depth_mm(g["cen_x"].to_numpy(), g["cen_y"].to_numpy(), g["D_ddf_h"].to_numpy())
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
        print("precompute: loading DDF thresholds ...")
        _load_ddf()
        arf_table(areas=(160, 500, 2000, 10000), durations=(2, 6, 12))
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
    w = g[["HYBAS_ID", "UP_AREA", "SUB_AREA", "t_lag_h", "D_test_h", "D_ddf_h",
           "thr_mm", "arf", "geometry"]].copy()
    w["geometry"] = w.geometry.simplify(C.SIMPLIFY.get(lv, 0.01), preserve_topology=True)
    for c in ["UP_AREA", "SUB_AREA", "t_lag_h", "D_test_h", "thr_mm"]:
        w[c] = w[c].round(1)
    w["D_ddf_h"] = w["D_ddf_h"].round(0).astype(int)
    w["arf"] = w["arf"].round(2)
    if out.exists(): out.unlink()
    try:
        w.to_file(out, driver="GeoJSON", COORDINATE_PRECISION=C.COORD_PRECISION)
    except TypeError:
        w.to_file(out, driver="GeoJSON")
    print(f"    web geometry -> {out.name} ({out.stat().st_size//1024} KB)")


# ----------------------------------------------------------------------
# rolling stores (per level, per source). Pure CACHES: OPERA hours can always be
# re-downloaded for 24 h, so losing a store only costs download time.
#   opera_L*.csv : index = ACRR end time (unix), value = basin mean mm over the hour
#   rv_L*.csv    : index = RainViewer frame time, value = basin mean mm over 10 min
#   NaN = basin not (sufficiently) observed by that source at that time.
# ----------------------------------------------------------------------
def load_store(lv, kind):
    p = _level_paths(lv)[kind]
    if not p.exists(): return pd.DataFrame()
    df = pd.read_csv(p, index_col=0); df.index = df.index.astype("int64")
    df.columns = [str(c) for c in df.columns]; return df

def save_store(lv, kind, df):
    df.to_csv(_level_paths(lv)[kind], float_format="%.4g")

def areal_mean(field_mm, pixel_cat, n_cat):
    """Per-basin mean of a grid field (mm) over OBSERVED pixels only.
    NaN where < MIN_VALID_FRAC of the basin's pixels are observed."""
    v = field_mm.ravel()
    inb = pixel_cat >= 0
    obs = inb & ~np.isnan(v)
    tot = np.bincount(pixel_cat[obs], weights=v[obs], minlength=n_cat)
    nobs = np.bincount(pixel_cat[obs], minlength=n_cat)
    nall = np.bincount(pixel_cat[inb], minlength=n_cat)
    out = np.full(n_cat, np.nan)
    ok = (nall > 0) & (nobs >= C.MIN_VALID_FRAC * np.maximum(nall, 1))
    out[ok] = tot[ok] / nobs[ok]
    return out

def areal_mean_mm(rate, pixel_cat, n_cat):
    """RainViewer frame (mm/h) -> per-basin mean mm over one 10-min frame."""
    return areal_mean(rate * (C.FRAME_INTERVAL_MIN / 60.0) * C.CAL_FACTOR, pixel_cat, n_cat)

# ---- store algebra: dedupe by timestamp -> a frame/hour counts exactly once ----
def merge_frames(store, rows):
    if rows:
        parts = ([store] if len(store) else []) + [r.to_frame().T for r in rows]
        store = pd.concat(parts)
    if len(store):
        store = store[~store.index.duplicated(keep="last")].sort_index()
    return store

def prune_store(store, oldest_t):
    if not len(store): return store
    return store[store.index > oldest_t - 3600]

def hourly_slots(op_store, rv_store, hids, T, n_h, rv_scale=1.0):
    """Merged hourly rainfall [n_h x basins], slot k = (T-(k+1)h, T-kh].
    Returns (mm with NaN = missing, src with 0=missing / 1=OPERA / 2=RainViewer).
    RainViewer fill is multiplied by rv_scale (OPERA-harmonisation factor).
    Anchored on the DATA time T, so it does not depend on when runs happen."""
    n = len(hids)
    arr = np.full((n_h, n), np.nan); src = np.zeros((n_h, n), np.int8)
    if len(op_store):
        op = op_store.reindex(columns=list(hids))
    if len(rv_store):
        rv = rv_store.reindex(columns=list(hids))
        rv_t = rv.index.to_numpy(np.int64); rv_a = rv.to_numpy(float)
    per_h = 60.0 / C.FRAME_INTERVAL_MIN
    for k in range(n_h):
        te = int(T) - k * 3600
        if len(op_store) and te in op.index:
            v = op.loc[te].to_numpy(float); ok = ~np.isnan(v)
            arr[k, ok] = v[ok]; src[k, ok] = 1
        if len(rv_store):
            sel = (rv_t > te - 3600) & (rv_t <= te)
            if sel.any():
                sub = rv_a[sel]; nok = (~np.isnan(sub)).sum(axis=0)
                fill = np.isnan(arr[k]) & (nok >= C.RV_MIN_FRAMES_PER_H)
                if fill.any():
                    arr[k, fill] = np.nansum(sub[:, fill], axis=0) / nok[fill] * per_h * rv_scale
                    src[k, fill] = 2
    return arr, src

def rv_bias_sample(op_store, rv_store, T, n_h):
    """(sum OPERA, sum RainViewer, n basins) over basin-hours seen by BOTH sources
    (RainViewer with all 6 frames) and wet in either. Raw RainViewer, no scaling."""
    if not (len(op_store) and len(rv_store)): return 0.0, 0.0, 0
    cols = op_store.columns.intersection(rv_store.columns)
    rv_t = rv_store.index.to_numpy(np.int64); rv_a = rv_store[cols].to_numpy(float)
    so = sr = 0.0; nb = 0
    for k in range(n_h):
        te = int(T) - k * 3600
        if te not in op_store.index: continue
        sel = (rv_t > te - 3600) & (rv_t <= te)
        if sel.sum() < 6: continue
        o = op_store.loc[te, cols].to_numpy(float)
        sub = rv_a[sel]; r = sub.sum(axis=0)              # NaN if any frame unseen
        both = ~np.isnan(o) & ~np.isnan(r) & ((o > 0.2) | (r > 0.2))
        so += o[both].sum(); sr += r[both].sum(); nb += int(both.sum())
    return so, sr, nb

def rv_bias_update(sample):
    """Log-space EMA of k = OPERA / RainViewer, persisted in state/rv_bias.json."""
    f = C.STATE / "rv_bias.json"
    st = json.loads(f.read_text()) if f.exists() else {"k": 1.0, "n_updates": 0}
    so, sr, nb = sample
    st["last_sample"] = {"opera_mm": round(so, 1), "rv_mm": round(sr, 1), "basins": nb}
    if C.RV_BIAS_ADJUST and nb >= C.RV_BIAS_MIN_BASINS and so >= C.RV_BIAS_MIN_MM and sr > 0:
        lo, hi = C.RV_BIAS_BOUNDS
        k_new = float(np.clip(so / sr, lo, hi))
        a = C.RV_BIAS_ALPHA if st["n_updates"] else 1.0     # first estimate taken as is
        st["k"] = float(np.exp(a * np.log(k_new) + (1 - a) * np.log(st["k"])))
        st["n_updates"] += 1; st["last_k"] = round(k_new, 3)
    f.write_text(json.dumps(st, indent=1))
    return st["k"] if C.RV_BIAS_ADJUST else 1.0, st

def window_sum(arr, w):
    """(sum mm, coverage 0-1) over the first w hourly slots. A missing hour adds
    nothing to the sum but lowers the coverage, which is shown to the user."""
    w = int(w); sub = arr[:w]
    return np.nansum(sub, axis=0), (~np.isnan(sub)).sum(axis=0) / float(w)

def response_window_sum(arr, d_h):
    """Per-basin sum over its own integer response time d_h (h) + coverage."""
    d_h = np.asarray(d_h).astype(int)
    acc = np.zeros(arr.shape[1]); cov = np.zeros(arr.shape[1])
    for d in np.unique(d_h):
        cols = np.where(d_h == d)[0]
        a, c = window_sum(arr[:, cols], d); acc[cols] = a; cov[cols] = c
    return acc, cov

# ----------------------------------------------------------------------
# one cycle
# ----------------------------------------------------------------------
def choose_anchor(op_avail, past):
    """T = newest OPERA hour, unless OPERA lags RainViewer by > OPERA_MAX_LAG_MIN."""
    t_rv = int(past[-1]["time"]) if past else None
    t_op = max(op_avail) if op_avail else None
    if t_op and (t_rv is None or t_rv - t_op <= C.OPERA_MAX_LAG_MIN * 60):
        return t_op, "opera"
    if t_rv is None:
        raise RuntimeError("no OPERA and no RainViewer data available")
    return (t_rv // 900) * 900, "rainviewer (OPERA stale or unavailable)"

def run_once(verbose=True):
    t_start = time.time()
    log = print if verbose else (lambda *a, **k: None)
    precompute()
    xr, yr, glon, glat = domain_grid()
    n_h = int(round(C.WINDOW_H))

    # -- what is available right now --------------------------------------
    host, past = rainviewer_frames()
    op_avail = {}
    if C.USE_OPERA:
        try:
            op_avail = Q.opera_list()
        except Exception as e:
            log(f"  OPERA listing failed ({type(e).__name__}: {e}) -> RainViewer only")
    T, anchor_src = choose_anchor(op_avail, past)
    oldest = T - n_h * 3600
    log(f"cycle: anchor T = {Q._utc(T):%Y-%m-%d %H:%M} UTC ({anchor_src}); "
        f"{len(op_avail)} OPERA hours listed, {len(past)} RainViewer frames")

    levels = {lv: load_level(lv) for lv in C.LEVELS}
    op_st = {lv: prune_store(load_store(lv, "opera"), oldest) for lv in C.LEVELS}
    rv_st = {lv: prune_store(load_store(lv, "rv"), oldest) for lv in C.LEVELS}

    # -- 1. OPERA: the hour slots ending at T, T-1h, ... --------------------
    want = [T - k * 3600 for k in range(n_h)]
    op_missing = {t for t in want if t not in op_avail}
    todo = {t: op_avail[t] for t in want
            if t in op_avail and any(t not in op_st[lv].index for lv in C.LEVELS)}
    log(f"  OPERA: {n_h - len(op_missing)}/{n_h} hour slots on the server, {len(todo)} to download")
    rows = {lv: [] for lv in C.LEVELS}
    for t, grid in Q.opera_fetch_many(todo, glon, glat):
        if grid is None:
            op_missing.add(t); log(f"    OPERA {Q._utc(t):%H:%M} download failed"); continue
        for lv in C.LEVELS:
            cat, pmap = levels[lv]
            rows[lv].append(pd.Series(areal_mean(grid, pmap, len(cat)),
                                      index=cat["HYBAS_ID"].tolist(), name=t))
    for lv in C.LEVELS:
        op_st[lv] = merge_frames(op_st[lv], rows[lv])
        if len(op_st[lv]): save_store(lv, "opera", op_st[lv])

    # -- 2. RainViewer: only where OPERA is blind; whole domain if an OPERA
    #       hour is missing or OPERA is down ------------------------------
    full = (C.RV_MODE == "full") or bool(op_missing) or not C.USE_OPERA
    tiles = None
    if not full:
        need = np.zeros(glon.size, bool)
        for lv in C.LEVELS:
            cat, pmap = levels[lv]
            blind = np.isnan(op_st[lv].loc[T].reindex(cat["HYBAS_ID"]).to_numpy(float))
            need |= (pmap >= 0) & blind[np.maximum(pmap, 0)]
        tiles = Q.tiles_needing_rv(xr, yr, need.reshape(glon.shape))
    n_tiles = len(list(xr)) * len(list(yr)) if tiles is None else len(tiles)
    fr_todo = [f for f in past if int(f["time"]) > oldest - 3600
               and any(int(f["time"]) not in rv_st[lv].index for lv in C.LEVELS)]
    mode = "full domain" if full else "gap-fill"
    log(f"  RainViewer: {mode}, {n_tiles} tiles/frame, {len(fr_todo)} new frames")
    rows = {lv: [] for lv in C.LEVELS}; n_skip = 0
    for f in (fr_todo if n_tiles else []):
        rate, failed = fetch_frame_rate(host, f["path"], xr, yr, tiles)
        if failed:                         # incomplete frame: don't store, retry next run
            n_skip += 1; continue
        t = int(f["time"])
        for lv in C.LEVELS:
            cat, pmap = levels[lv]
            rows[lv].append(pd.Series(areal_mean_mm(rate, pmap, len(cat)),
                                      index=cat["HYBAS_ID"].tolist(), name=t))
    if n_skip:
        log(f"    {n_skip} RainViewer frame(s) incomplete -> not stored, retried next run")
    for lv in C.LEVELS:
        rv_st[lv] = merge_frames(rv_st[lv], rows[lv])
        if len(rv_st[lv]): save_store(lv, "rv", rv_st[lv])

    # -- 3. harmonise RainViewer to OPERA, merge, severity -----------------
    ref = C.SEVERITY_LEVELS[0] if C.SEVERITY_LEVELS else C.LEVELS[-1]
    rv_k, bias = rv_bias_update(rv_bias_sample(op_st[ref], rv_st[ref], T, n_h))
    log(f"  RainViewer->OPERA factor k = {rv_k:.2f} (this run: {bias.get('last_sample')})")
    meta_src = {"rv_scale": round(rv_k, 3), "anchor": anchor_src, "opera_hours": n_h - len(op_missing),
                "window_hours": n_h, "rv_mode": mode, "rv_tiles_per_frame": n_tiles,
                "rv_http": Q.rv_stats()}
    out = {}; pins = []
    for lv in C.LEVELS:
        cat = levels[lv][0]
        arr, src = hourly_slots(op_st[lv], rv_st[lv], cat["HYBAS_ID"].tolist(), T, n_h, rv_k)
        summary, lv_pins = _severity_for_level(cat, arr, src, T, host, past, lv, meta_src)
        out[lv] = summary; pins += lv_pins
    _write_pins(pins)
    if verbose:
        for lv in C.LEVELS:
            r = out[lv]
            print(f"  L{lv}: {r['wet']} wet | {r['nalert']} >=10y | max acc {r['maxacc']:.1f} mm "
                  f"| max T {r['maxT']:.0f} y | basin-hours OPERA {r['f_opera']:.0%} "
                  f"RV {r['f_rv']:.0%} missing {r['f_miss']:.0%}")
        print(f"  pins: {len(pins)} alerting basins | RainViewer HTTP {Q.rv_stats()} "
              f"| {time.time() - t_start:.0f} s")
    return out

def _severity_for_level(cat, arr, src, T, host, past, lv, meta_src):
    is_sev = lv in C.SEVERITY_LEVELS
    cat = cat.copy(); n = len(cat)
    # data-source shares, over basins seen by some radar at least once
    seen = (src > 0).any(axis=0)
    share = lambda code: float((src[:, seen] == code).mean()) if seen.any() else 0.0
    stats = {"f_opera": share(1), "f_rv": share(2), "f_miss": share(0)}

    for w in C.ACC_WINDOWS_H:
        a, c = window_sum(arr, w)
        cat[f"acc_{w}h"] = np.round(a, 1); cat[f"cov_{w}h"] = np.round(c, 2)

    acc = np.zeros(n); ratio = np.zeros(n); T_y = np.zeros(n); cov = np.zeros(n)
    color = np.full(n, "#2b8cbe22", dtype=object); label = np.full(n, "none", dtype=object)
    if is_sev:
        # severity over each basin's OWN response time (whole hours) = the same
        # duration as its DDF threshold (Ceresetti method).
        dtest = cat["D_ddf_h"].to_numpy(); thr = cat["thr_mm"].to_numpy()
        acc, cov = response_window_sum(arr, dtest)
        # WET GATE: the longest fixed window bounds the response-time sum from above.
        wet = np.where(cat[f"acc_{C.ACC_WINDOWS_H[-1]}h"].to_numpy() >= C.SEVERITY_MIN_MM)[0]
        if wet.size:
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio[wet] = np.where(thr[wet] > 0, acc[wet] / thr[wet], 0.0)
            T_y[wet] = [est_return_period(r) for r in ratio[wet]]
            for i in wet:
                color[i], label[i] = classify(T_y[i], ratio[i], acc[i])
    cat["acc_mm"] = acc; cat["coverage"] = np.round(cov, 2)
    cat["ratio"] = ratio; cat["T_years"] = T_y; cat["color"] = color; cat["label"] = label

    if lv in C.POLY_LEVELS:
        _write_level_alerts(cat, int(T), host, past, lv, meta_src, stats)

    pins = [{"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [round(r.cen_x, 4), round(r.cen_y, 4)]},
             "properties": {"level": lv, "color": r.color, "label": r.label,
                            "T": round(r.T_years), "acc_mm": round(r.acc_mm, 1),
                            "thr_mm": round(r.thr_mm, 1), "D_ddf_h": int(r.D_ddf_h),
                            "coverage": r.coverage, "HYBAS_ID": r.HYBAS_ID}}
            for r in cat.itertuples() if r.label in C.PIN_LABELS] if is_sev else []

    longest = cat[f"acc_{C.ACC_WINDOWS_H[-1]}h"]
    return ({"wet": int((longest > 0.2).sum()),
             "nalert": int(cat["label"].isin(["~10y", "~30y", ">=100y"]).sum()),
             "maxacc": float(longest.max()), "maxT": float(cat["T_years"].max()), **stats}, pins)

def _write_pins(features):
    (C.WEBDATA / "pins.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}))

def _write_level_alerts(cat, T, host, past, lv, meta_src, stats):
    is_sev = lv in C.SEVERITY_LEVELS
    meta = {"generated_unix": int(T), "data_end_unix": int(T), "run_unix": int(time.time()),
            "window_h": int(round(C.WINDOW_H)), "store_span_h": float(round(C.WINDOW_H)),
            "level": lv, "severity": is_sev, "acc_windows_h": C.ACC_WINDOWS_H,
            "domain": C.DOMAIN_BBOX,
            "qpe": "OPERA NIMBUS 1-h accumulation (EUMETNET, CC BY 4.0) + RainViewer gap-fill",
            "sources": meta_src, "basin_hours": {k: round(v, 3) for k, v in stats.items()},
            "cal_factor": C.CAL_FACTOR, "rainviewer_host": host,
            "rainviewer_path": past[-1]["path"] if past else None}
    # Include a basin if it has measurable rain in any window or a severity label.
    # acc/cov = {window_h: value}; cov only written when some window is incomplete.
    alerts = {}
    for r in cat.itertuples():
        acc = {str(w): getattr(r, f"acc_{w}h") for w in C.ACC_WINDOWS_H}
        label = getattr(r, "label", "none")
        if not (max(acc.values()) >= C.RAIN_MIN_MM or (is_sev and label != "none")):
            continue
        entry = {"acc": acc}
        cov = {str(w): getattr(r, f"cov_{w}h") for w in C.ACC_WINDOWS_H}
        if min(cov.values()) < 1:
            entry["cov"] = cov
        if is_sev:
            entry.update({"T": round(r.T_years), "ratio": round(r.ratio, 2),
                          "acc_mm": round(r.acc_mm, 1), "color": r.color,
                          "label": label, "coverage": r.coverage})
        alerts[r.HYBAS_ID] = entry
    _level_paths(lv)["alerts"].write_text(json.dumps({"meta": meta, "alerts": alerts}, indent=1))
