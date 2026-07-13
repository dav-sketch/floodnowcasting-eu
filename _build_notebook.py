"""Builds FloodNowcasting_PoC.ipynb from ordered (type, source) cells.
Kept in the repo so the notebook is regenerable/diff-able."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "FloodNowcasting_PoC.ipynb")

cells = []
def md(src):  cells.append(("markdown", src))
def code(src): cells.append(("code", src))

# ------------------------------------------------------------------ intro
md(r"""# FloodNowcasting.eu 2.0 — Proof of Concept

**Live extreme-rainfall early-warning over Europe, on free data.**

This notebook proves the scientific loop end-to-end:

1. **Threshold** — load the *10-year return level* of sub-daily rainfall
   (Poschlod et al. 2021, ESSD; 1/3/6/12/24 h; ~12.5 km grid; the exact
   dataset staged in `IDF/`). This is what we alert against.
2. **Catchments** — load HydroBASINS (levels 6–12, staged in `hybas/`) and give
   each catchment a **response/lag time** from its upstream area. The lag time
   picks which rainfall *duration* matters and how much *warning* we can offer.
3. **Live rain** — pull the latest radar frames from the free **RainViewer**
   API, decode them to rain rate, and **accumulate** over the relevant window.
4. **Alert** — per catchment, accumulate rain over its **response-time** window,
   apply an **areal reduction factor**, and turn the ratio to the 10-y level into
   an estimated **return period**: ~10 y (yellow) → ~30 y (orange) → ≥100 y (red).
5. **Map** — render coloured catchments over the live radar.

### Honest framing (read this)
- This is an **extreme-*rainfall*** warning, **not** a discharge forecast.
  For extreme events (T ≫ 10 y) heavy rain reliably becomes a flood, which is
  what makes the shortcut useful for *time-to-shelter*.
- **The weak link is quantitative radar (QPE).** RainViewer serves *coloured
  PNG tiles*, not mm. We decode colour→dBZ→mm/h (Marshall–Palmer). That decode
  is a **tunable approximation** (see the `ZR_*` / palette config). Operationally
  we'd swap in EUMETNET **OPERA gauge-adjusted QPE** for true millimetres.
- RainViewer's public API only exposes ~**2 h** of past frames, so live
  accumulation here maxes out around 2 h. That's already enough for **small,
  fast catchments** (levels 10–12) — which is exactly where nowcasting saves
  lives. Large slow basins need a rolling accumulation store (see final notes).
""")

# ------------------------------------------------------------------ config
code(r'''# === Configuration ===================================================
from pathlib import Path

BASE   = Path.cwd()                      # run from FloodNowCasting.eu_2.0/
IDF_DIR   = BASE / "IDF"
HYBAS_DIR = BASE / "hybas" / "hybas_lake_eu_lev01-12_v1c"
CACHE  = BASE / "_cache"; CACHE.mkdir(exist_ok=True)

# --- Area of interest -------------------------------------------------
# If AUTO_LOCATE, we scan Europe and centre the demo on the rainiest spot
# right now, so the PoC always shows something. Set False to pin a BBOX.
AUTO_LOCATE = True
BBOX = (5.5, 45.0, 8.0, 47.0)            # (lon_min, lat_min, lon_max, lat_max) fallback
HYBAS_LEVEL = 9                           # 6 (large) .. 12 (small). 9 = good regional detail.
TILE_Z = 7                                # radar tile zoom for the AOI (higher = finer, more tiles)

# --- Colours = estimated rainfall return period -----------------------
# ratio = areal accumulation / (10-y areal level). Map ratio -> return period T
# via a growth curve anchored on (ratio, years). Defaults are Geneva-like
# (30y ~ 1.2x the 10y level, 100y ~ 1.4x); tune per region.
RP_ANCHORS  = [(1.0, 10), (1.2, 30), (1.4, 100)]
RP_COLORS   = [(10, "#ffeda0", "~10y"), (30, "#feb24c", "~30y"), (100, "#f03b20", ">=100y")]
WATCH_RATIO = 0.8                          # below the 10-y level but worth watching (green)

# --- Areal Reduction Factor: De Michele, Kottegoda & Rosso (2001) -----
# WRR 37(12):3247-3252, scaling-based ARF (their Eq. 14):
#   ARF(A,T) = [ 1 + varpi * (A*^z / T)^b ] ^ (-v/b)
# A* = max(A - A0, 0) km2  (A0 = reference "point" area), T in h. Areal thr =
# point IDF * ARF. Default = UK/NERC fit (areas 1-18000 km2, wide durations).
ARF_VARPI, ARF_B, ARF_Z, ARF_V = 0.011, 0.40, 0.70, 0.70    # UK/NERC fit
# ARF_VARPI, ARF_B, ARF_Z, ARF_V = 0.0905, 0.540, 1.0, 0.484  # Milan (urban) fit
ARF_A0_KM2 = 156.0        # Poschlod cells ~12.5 km (~156 km2): already areal

# --- RainViewer decode (THE calibration knobs) ------------------------
RAIN_ALPHA_MIN = 120     # ignore tile pixels more transparent than this (haze/no-data)
ZR_A, ZR_B = 200.0, 1.6  # Marshall-Palmer  Z = A * R^B   (Z in mm^6/m^3, R in mm/h)
DBZ_MAX = 53.0           # cap reflectivity (hail contamination) -> caps rate ~130 mm/h

# Universal-Blue (RainViewer colour scheme 2) approx anchors: dBZ -> (R,G,B).
# Light cyan = weak echo ... magenta/pink = extreme. Tune against known events.
PALETTE = [
    (5 , (150,230,240)), (10,(108,209,235)), (15,( 54,186,229)),
    (20,(  0,163,224)), (25,(  0,136,191)), (30,(  0,119,170)),
    (35,( 60,190, 90)), (40,(240,240, 60)), (45,(250,180, 40)),
    (50,(235, 90, 40)), (55,(190, 30, 30)), (60,(220, 60,180)),
    (65,(240,150,230)),
]
print("Config OK. Level", HYBAS_LEVEL, "| auto-locate:", AUTO_LOCATE)
''')

# ------------------------------------------------------------------ imports
code(r'''import numpy as np, pandas as pd, requests, io, math, itertools
import geopandas as gpd
from shapely.geometry import box, Point
from PIL import Image

try:
    from scipy.spatial import cKDTree
    HAVE_KDTREE = True
except Exception:
    HAVE_KDTREE = False
print("geopandas", gpd.__version__, "| KDTree:", HAVE_KDTREE)
''')

# ------------------------------------------------------------------ step 1
md("## 1 — Load the 10-year return-level threshold (Poschlod et al. 2021)")
code(r'''ZENODO = "https://zenodo.org/records/3878888/files/ReturnLevel_10year_{d}.txt?download=1"
DURATIONS = ["1h","3h","6h","12h","24h"]

def load_returnlevels():
    out = {}
    for d in DURATIONS:
        f = CACHE / f"returnlevel_10y_{d}.txt"
        if not f.exists():
            print("downloading", d, "...")
            r = requests.get(ZENODO.format(d=d), timeout=180); r.encoding="utf-8"
            f.write_text(r.text, encoding="utf-8")
        df = pd.read_csv(f)
        df.columns = ["q5","median","q95","lat","lon"]   # header is "# Pr (mm) Q5, ..."
        out[d] = df
    return out

RL = load_returnlevels()
print({d: RL[d].shape for d in DURATIONS})
RL["3h"].head(3)
''')
code(r'''# Nearest-neighbour lookup of the 10-y level (mm) at a point, per duration.
_trees = {}
def _tree(d):
    if d not in _trees:
        df = RL[d]
        pts = np.c_[df.lon.values, df.lat.values]
        _trees[d] = (cKDTree(pts) if HAVE_KDTREE else pts, df["median"].values)
    return _trees[d]

def threshold_mm(lon, lat, d):
    tree, vals = _tree(d)
    if HAVE_KDTREE:
        _, i = tree.query([lon, lat])
    else:
        i = int(np.argmin((tree[:,0]-lon)**2 + (tree[:,1]-lat)**2))
    return float(vals[i])

_DUR_H = np.array([1,3,6,12,24], float)
def threshold_point_mm(lon, lat, D_h):
    """Point 10-y level (mm) at arbitrary duration D_h, log-log interpolated."""
    depths = np.array([threshold_mm(lon,lat,f"{int(d)}h") for d in _DUR_H])
    D = min(max(D_h, _DUR_H[0]), _DUR_H[-1])
    return float(np.exp(np.interp(np.log(D), np.log(_DUR_H), np.log(depths))))

# sanity: 3h 10-y level near Genoa (~8.9E,44.4N) should be a few tens of mm
print("3h 10-y level near Genoa:", round(threshold_mm(8.9,44.4,"3h"),1), "mm")
print("2.2h interp near Genoa  :", round(threshold_point_mm(8.9,44.4,2.2),1), "mm")
''')

# ------------------------------------------------------------------ step 2
md(r"""## 2 — Catchments: response time, test duration & areal reduction

Each HydroBASINS polygon gets a **response / lag time** from its upstream area
`UP_AREA` (km²): `t_lag ≈ 0.9 · A^0.38` h. It drives three things:

- **Accumulation duration.** Rain is accumulated & compared over a window equal
  to the response time (`D_test = min(t_lag, radar window)`): shorter than that
  and the basin isn't fully contributing; much longer and discharge plateaus.
- **Warning lead time** we can advertise = `t_lag`.
- The point 10-y IDF (log-log interpolated to `D_test`) becomes a **catchment-areal**
  threshold via the **Areal Reduction Factor** of De Michele, Kottegoda & Rosso
  (2001), `ARF = [1 + ϖ(A*^z/T)^b]^(−v/b) ≤ 1`, so `thr_areal = thr_point · ARF`.
  `A* = A − A₀` with `A₀ ≈ 156 km²` (a Poschlod cell is already areal, ~12.5 km).

`coverage = radar window / t_lag` shows how much of the basin's response the ~2-h
radar record spans — small for big basins until we add a live accumulation store.""")
code(r'''def response_time_h(area_km2):
    # coarse regional scaling; ~1h at 10 km2 .. ~24h at ~1e4 km2
    return float(max(0.5, 0.9 * (max(area_km2, 1.0) ** 0.38)))

def arf(area_km2, D_h):
    """De Michele-Kottegoda-Rosso (2001) areal reduction factor in (0,1]."""
    A_star = max(area_km2 - ARF_A0_KM2, 0.0)     # <= A0 -> point/grid scale -> 1
    if A_star <= 0.0:
        return 1.0
    base = 1.0 + ARF_VARPI * (A_star**ARF_Z / max(D_h, 0.05))**ARF_B
    return float(min(1.0, base**(-ARF_V/ARF_B)))

_ra = np.array([a for a,_ in RP_ANCHORS]); _rlnT = np.log([t for _,t in RP_ANCHORS])
def est_return_period(ratio):
    """ratio (areal acc / 10-y areal level) -> estimated return period (years)."""
    if ratio <= 0: return 0.0
    if ratio < _ra[0]:                     # linear-in-lnT extrapolation below
        s=(_rlnT[1]-_rlnT[0])/(_ra[1]-_ra[0]); lnT=_rlnT[0]+s*(ratio-_ra[0])
    elif ratio > _ra[-1]:                  # ... and above
        s=(_rlnT[-1]-_rlnT[-2])/(_ra[-1]-_ra[-2]); lnT=_rlnT[-1]+s*(ratio-_ra[-1])
    else:
        lnT = np.interp(ratio, _ra, _rlnT)
    return float(np.exp(lnT))

def load_catchments(level, bbox, window_h):
    shp = HYBAS_DIR / f"hybas_lake_eu_lev{level:02d}_v1c.shp"
    aoi = box(*bbox)
    g = gpd.read_file(shp, bbox=bbox)          # bbox filter = fast, avoids loading all EU
    g = g[g.intersects(aoi)].copy()
    g["t_lag_h"]  = g["UP_AREA"].apply(response_time_h)          # response = warning lead time
    # accumulate/compare over the response time, but not beyond what radar gives us
    g["D_test_h"] = np.minimum(g["t_lag_h"], window_h)
    g["coverage"] = (window_h / g["t_lag_h"]).clip(upper=1.0)    # fraction of response observed
    rp = g.geometry.representative_point()            # NB: g.cx is GeoPandas' coord indexer, not a column
    g["cen_x"] = rp.x.values
    g["cen_y"] = rp.y.values
    g["arf"]    = [arf(a,d) for a,d in zip(g["UP_AREA"], g["D_test_h"])]
    g["thr_pt"] = [threshold_point_mm(x,y,d) for x,y,d in zip(g["cen_x"],g["cen_y"],g["D_test_h"])]
    g["thr_mm"] = g["thr_pt"] * g["arf"]              # areal 10-y level
    return g.reset_index(drop=True)

print("catchment loader ready (response-time accumulation + ARF)")
''')

# ------------------------------------------------------------------ step 3
md("## 3 — Live radar: fetch RainViewer frames & decode to rain rate")
code(r'''RV_JSON = "https://api.rainviewer.com/public/weather-maps.json"

def rainviewer_frames():
    j = requests.get(RV_JSON, timeout=30).json()
    host = j["host"]
    past = j["radar"]["past"]
    now  = j["radar"].get("nowcast", []) or []
    return host, past, now

# --- Web-Mercator tile math -------------------------------------------
def deg2tile(lon, lat, z):
    n = 2**z
    x = (lon+180.0)/360.0*n
    lr = math.radians(lat)
    y = (1 - math.log(math.tan(lr)+1/math.cos(lr))/math.pi)/2*n
    return x, y
def tilexy_range(bbox, z):
    x0,_ = deg2tile(bbox[0], bbox[3], z); _,y0 = deg2tile(bbox[0], bbox[3], z)
    x1,_ = deg2tile(bbox[2], bbox[1], z); _,y1 = deg2tile(bbox[2], bbox[1], z)
    return range(int(x0), int(x1)+1), range(int(y0), int(y1)+1)
def pix2lonlat(px, py, z):
    n = 2**z
    lon = px/n*360.0-180.0
    lat = math.degrees(math.atan(math.sinh(math.pi*(1-2*py/n))))
    return lon, lat

# --- colour -> dBZ -> mm/h --------------------------------------------
_pal_rgb = np.array([c for _,c in PALETTE], float)
_pal_dbz = np.array([d for d,_ in PALETTE], float)
def rgb_to_dbz(rgb):                       # rgb: (...,3) float
    d2 = ((rgb[...,None,:]-_pal_rgb[None,None,:])**2).sum(-1)
    return _pal_dbz[np.argmin(d2, -1)]
def dbz_to_rate(dbz):                       # mm/h via Z = A R^B
    Z = 10.0**(dbz/10.0)
    return (Z/ZR_A)**(1.0/ZR_B)

def fetch_frame_grid(host, path, bbox, z):
    """Return (lon2d, lat2d, rate_mm_h) for one radar frame over bbox."""
    xr, yr = tilexy_range(bbox, z)
    cols=[]
    for tx in xr:
        rows=[]
        for ty in yr:
            url=f"{host}{path}/256/{z}/{tx}/{ty}/2/0_0.png"   # scheme 2, no smoothing
            r=requests.get(url, timeout=30)
            im=(np.zeros((256,256,4),np.uint8) if r.status_code!=200
                else np.array(Image.open(io.BytesIO(r.content)).convert("RGBA")))
            rows.append(im)
        cols.append(np.concatenate(rows, axis=0))   # stack in y
    img = np.concatenate(cols, axis=1)               # stack in x
    H,W,_ = img.shape
    rgb, a = img[...,:3].astype(float), img[...,3]
    dbz  = np.clip(rgb_to_dbz(rgb), 0.0, DBZ_MAX)    # clip extreme (hail) tail
    rate = dbz_to_rate(dbz)
    rate[a < RAIN_ALPHA_MIN] = 0.0                   # mask haze / no-data
    # pixel-centre lon/lat
    tx0, ty0 = int(list(xr)[0]), int(list(yr)[0])
    jj, ii = np.meshgrid(np.arange(W), np.arange(H))
    gpx = tx0 + (jj+0.5)/256.0
    gpy = ty0 + (ii+0.5)/256.0
    n=2**z
    lon = gpx/n*360.0-180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi*(1-2*gpy/n))))
    return lon, lat, rate

print("RainViewer helpers ready")
''')

# ------------------------------------------------------------------ step 3b auto-locate + accumulate
code(r'''def find_rainy_bbox(host, path, z=5):
    """Scan mid-latitude Europe tiles at low zoom; return bbox of wettest tile."""
    best=None; best_n=-1
    for tx in range(15, 21):          # ~ -10E..30E band at z=5
        for ty in range(9, 13):       # ~ 35N..60N band
            url=f"{host}{path}/256/{z}/{tx}/{ty}/2/0_0.png"
            r=requests.get(url, timeout=20)
            if r.status_code!=200: continue
            a=np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))[...,3]
            n=int((a>=RAIN_ALPHA_MIN).sum())
            if n>best_n: best_n, best = n, (tx,ty)
    tx,ty=best
    lon0,lat1 = pix2lonlat(tx, ty, z)
    lon1,lat0 = pix2lonlat(tx+1, ty+1, z)
    print(f"wettest z{z} tile {best} ({best_n} wet px) -> bbox lon[{lon0:.2f},{lon1:.2f}] lat[{lat0:.2f},{lat1:.2f}]")
    return (lon0, lat0, lon1, lat1)

host, past, now = rainviewer_frames()
print(f"RainViewer: {len(past)} past frames, {len(now)} nowcast frames")
frames = past                                    # accumulate all available past (~2h)
window_min = round((frames[-1]["time"]-frames[0]["time"])/60) + 10
print("accumulation window ~", window_min, "min")

if AUTO_LOCATE:
    BBOX = find_rainy_bbox(host, past[-1]["path"])
print("AOI BBOX:", tuple(round(v,3) for v in BBOX))
''')
code(r'''# Accumulate rain depth (mm) across frames: rate(mm/h) * interval(h), summed.
interval_h = 10/60.0
acc=None; LON=LAT=None
for k,fr in enumerate(frames):
    lon, lat, rate = fetch_frame_grid(host, fr["path"], BBOX, TILE_Z)
    if acc is None:
        acc=np.zeros_like(rate); LON,LAT=lon,lat
    acc += rate*interval_h
    print(f"  frame {k+1}/{len(frames)}  max rate {rate.max():5.1f} mm/h  cum max {acc.max():6.1f} mm", end="\r")
print(f"\nAccumulated {window_min} min. grid {acc.shape}, max {acc.max():.1f} mm, wet px {(acc>0.1).sum()}")
''')

# ------------------------------------------------------------------ step 4 zonal + alert
md(r"""## 4 — Zonal statistics → estimated return period per catchment

Wet radar pixels are spatial-joined to catchments; we take the **areal-average**
accumulation and divide by the **areal** 10-y level. That **ratio** maps to an
estimated **return period** through the growth curve `RP_ANCHORS` (Geneva-like
default: 1.0→10 y, 1.2→30 y, 1.4→100 y). Colours follow the estimated T.""")
code(r'''cat = load_catchments(HYBAS_LEVEL, BBOX, window_min/60.0)
print(len(cat), "catchments at level", HYBAS_LEVEL, "in AOI")

# radar pixels -> points (only wet ones, for speed), spatial-join to catchments
wet = acc > 0.1
pts = gpd.GeoDataFrame(
    {"acc": acc[wet]},
    geometry=gpd.points_from_xy(LON[wet], LAT[wet]),
    crs="EPSG:4326")

if len(pts):
    j = gpd.sjoin(pts, cat[["geometry"]].reset_index(), predicate="within", how="inner")
    agg = j.groupby("index")["acc"].agg(["mean","max","count"])
else:
    agg = pd.DataFrame(columns=["mean","max","count"])

cat["acc_mean"] = cat.index.map(agg["mean"]).fillna(0.0)   # areal-average (matches ARF)
cat["acc_max"]  = cat.index.map(agg["max"]).fillna(0.0)
cat["n_px"]     = cat.index.map(agg["count"]).fillna(0).astype(int)

# ratio = catchment-areal accumulation / areal 10-y level -> estimated return period
cat["ratio"]   = (cat["acc_mean"] / cat["thr_mm"].replace(0, np.nan)).fillna(0.0)
cat["T_years"] = cat["ratio"].apply(est_return_period)

def classify(T, ratio):
    lvl, col, lab = 0, "#2b8cbe22", "none"
    if ratio >= WATCH_RATIO and T < RP_COLORS[0][0]:
        lvl, col, lab = 0, "#c7e9b4", "watch"          # approaching 10y
    for i,(tt,c,l) in enumerate(RP_COLORS, start=1):
        if T >= tt: lvl, col, lab = i, c, l
    return pd.Series({"alert_lvl":lvl, "color":col, "label":lab})
cls = pd.DataFrame([classify(T,r) for T,r in zip(cat["T_years"], cat["ratio"])], index=cat.index)
cat = cat.join(cls)

cols=["HYBAS_ID","UP_AREA","t_lag_h","D_test_h","coverage","arf","thr_mm","acc_mean","ratio","T_years","label"]
cat.sort_values("ratio", ascending=False)[cols].round(2).head(12)
''')

# ------------------------------------------------------------------ step 5 map
md("## 5 — Interactive map: coloured catchments over live radar")
code(r'''import folium
c = [ (BBOX[1]+BBOX[3])/2, (BBOX[0]+BBOX[2])/2 ]
m = folium.Map(location=c, zoom_start=TILE_Z, tiles="CartoDB positron")

# live radar overlay (RainViewer tiles, smoothed for display)
folium.TileLayer(
    tiles=f"{host}{past[-1]['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png",
    attr="RainViewer", name="Radar (latest)", opacity=0.6, overlay=True).add_to(m)

def style(feat):
    p=feat["properties"]
    on = p["label"]!="none"
    return {"fillColor":p["color"], "color":"#555", "weight":0.6,
            "fillOpacity":0.55 if on else 0.06}

gj = cat.copy()
for cc in ["UP_AREA","t_lag_h","D_test_h","thr_mm","acc_mean"]:
    gj[cc]=gj[cc].round(1)
gj["arf"]=gj["arf"].round(2); gj["coverage"]=gj["coverage"].round(2)
gj["ratio"]=gj["ratio"].round(2); gj["T_years"]=gj["T_years"].round(0)
folium.GeoJson(
    gj.to_json(), name="Catchments", style_function=style,
    tooltip=folium.GeoJsonTooltip(
        fields=["HYBAS_ID","label","T_years","t_lag_h","D_test_h","coverage","thr_mm","acc_mean","arf","ratio"],
        aliases=["Basin","Alert","Est. T (y)","Warning (h)","Test dur (h)","Coverage",
                 "Areal 10-y thr (mm)","Areal rain (mm)","ARF","Ratio"])
).add_to(m)
folium.LayerControl().add_to(m)

n_alert=(cat["alert_lvl"]>0).sum()
print(f"{n_alert} catchments in watch/alert. Max ratio {cat['ratio'].max():.2f}")
m.save(str(BASE/"floodnowcast_map.html"))
m
''')

# ------------------------------------------------------------------ notes
md(r"""## From PoC to operational (the path to a free live webapp)

**What this notebook already proves:** threshold grid → catchments with lag
times → live radar accumulation → per-catchment 10-y-return alerts → map. All on
free data, no API keys.

**To go live & stay free:**
1. **Own accumulation store.** RainViewer gives only ~2 h. Run a **GitHub Actions
   cron (every 10–15 min)** that appends each radar frame to a rolling store, so
   6/12/24 h windows become available for large basins.
2. **Rain-masking for scale.** Never zonal-stat all EU level-12 (~1 M polygons).
   Only touch catchments intersecting wet radar pixels — the cron job stays tiny.
3. **Precompute once.** Per-catchment 10-y thresholds, lag times, and geometry →
   **PMTiles**. The cron only writes a small JSON of `{HYBAS_ID: alert_lvl}`.
4. **Static frontend.** MapLibre GL + PMTiles on GitHub/Cloudflare Pages ($0).
5. **Better QPE.** Replace tile-decode with **EUMETNET OPERA** gauge-adjusted
   composite (true mm) and calibrate against known events (Ahr 2021, Genoa,
   Valencia 2024).
6. **Extend lead time.** Blend **ECMWF open-data IFS** precip forecast beyond the
   radar horizon, and use IFS **soil moisture** for initial-abstraction.

**Data sources:** Poschlod et al. 2021 (ESSD, Zenodo 3878888) · HydroBASINS v1c ·
RainViewer API · EUMETNET OPERA · ECMWF open data.
""")

nb = {
    "cells": [
        ({"cell_type":"markdown","metadata":{},"source":src.splitlines(keepends=True)}
         if t=="markdown" else
         {"cell_type":"code","metadata":{},"execution_count":None,"outputs":[],
          "source":src.splitlines(keepends=True)})
        for (t,src) in cells
    ],
    "metadata": {"kernelspec":{"display_name":"Python 3","language":"python","name":"python3"},
                 "language_info":{"name":"python","version":"3.13"}},
    "nbformat":4, "nbformat_minor":5,
}
with open(OUT,"w",encoding="utf-8") as f:
    json.dump(nb, f, ensure_ascii=False, indent=1)
print("wrote", OUT, "with", len(cells), "cells")
