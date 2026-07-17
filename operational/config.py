"""FloodNowcasting.eu 2.0 - operational pipeline configuration.

Mirrors the science knobs of the PoC notebook. Edit DOMAIN_BBOX / UPDATE_MIN /
WINDOW_H for your run. All paths are derived from the project root.
"""
from pathlib import Path

# --- paths ------------------------------------------------------------
OPS   = Path(__file__).resolve().parent
BASE  = OPS.parent                                   # project root
HYBAS_DIR = BASE / "hybas" / "hybas_lake_eu_lev01-12_v1c"
CACHE = BASE / "_cache"                               # shared with the notebook (Poschlod files)
STATE = OPS / "state"                                # rolling store lives here
OUT   = OPS / "out"                                  # debug: alerts.geojson / map.html
WEB     = OPS / "web"                                 # static site (deploy this folder)
WEBDATA = WEB / "data"                               # catchments.geojson (static) + alerts.json (dynamic)
for _d in (CACHE, STATE, OUT, WEBDATA):
    _d.mkdir(parents=True, exist_ok=True)

# Per-level web simplification (deg); finer level = smaller basins, so use a
# smaller tolerance to avoid collapsing them. L6 coarse/large -> simplify hard;
# L9 many small basins -> only a "slight" simplification (kept crisp but trimmed).
SIMPLIFY = {6: 0.020, 7: 0.012, 8: 0.007, 9: 0.005}
COORD_PRECISION = 4                                   # geojson coord decimals (~11 m); big filesize win

# --- domain & cadence -------------------------------------------------
DOMAIN_BBOX = (-12.0, 34.0, 45.0, 72.0)  # (lon_min,lat_min,lon_max,lat_max) - all Europe
TILE_Z      = 6                          # radar tile zoom (must stay fixed once the store exists)

# Level-of-detail: coarse level when zoomed out, finer as you zoom in.
# L6 = continental first view; L7 = large; L8/L9 = the "relevant" fine levels.
LEVELS      = [6, 7, 8, 9]                # HydroBASINS levels COMPUTED each cycle
POLY_LEVELS = [6, 7, 8, 9]                # levels whose polygons are served as zoom layers (all served now)
SEVERITY_LEVELS = [8, 9]                  # only these get severity classification; L6/L7 are accumulation-only
LEVEL_MINZOOM = {6: 0, 7: 5, 8: 7, 9: 9}  # a poly level's polygons show from this map zoom until the next takes over
MAP_MINZOOM   = 3                         # prevent zooming out past continent
MAP_MAXZOOM   = 11                        # allow enough zoom for L9 (finest) to have a real band
RADAR_MAXZOOM = 7                         # radar raster native maxzoom; MapLibre overzooms beyond (no blank tiles)

# Alert pins: every alerting basin (severity levels only) drops a coloured dot at
# its centre, visible at all zooms so flash-flood (L9) alerts are spottable from afar.
PIN_LABELS = ["watch", "~10y", "~30y", ">=100y"]   # which severities get a pin
PIN_RADIUS = {8: 7, 9: 4}                          # circle radius (px) by level: L8 larger, L9 small
WINDOW_H    = 12.0                        # max rolling window retained (h); must be >= max(ACC_WINDOWS_H)
UPDATE_MIN  = 30                          # loop cadence; clamped to [15, 120] by run.py
FRAME_INTERVAL_MIN = 10                   # RainViewer past-frame spacing

# --- fixed accumulation windows (UI selector) -------------------------
# Besides the severity computation (over each basin's own response time), every
# served level also reports rainfall accumulated over these FIXED trailing
# windows. The frontend lets the user pick one and colours basins by the
# ACC_RAMP below. L6/L7 (no severity) are always coloured this way.
ACC_WINDOWS_H = [2, 4, 8, 12]             # windows offered in the "Rain" view (hours)
ACC_DEFAULT_H = 4                         # window used by default / when severity mode hits an accum-only level
ACC_RAMP = [                              # (accumulated mm >=, colour), sequential blues
    (0.5, "#d7ecf7"), (2.0, "#9ecae1"), (5.0, "#6baed6"), (10.0, "#4292c6"),
    (20.0, "#2171b5"), (40.0, "#08519c"), (80.0, "#08306b"),
]

# --- Quantitative rainfall: RainViewer radar (sees convective cells) ---
# Colour PNG tiles -> dbZ -> mm/h (Marshall-Palmer), then a CALIBRATION factor.
# CAL_FACTOR was tuned so decoded cell depths match observations/gauges:
# Bucharest convective cells -> 0.22, Swiss stratiform vs Open-Meteo -> 0.21.
# A single ~0.20 multiplier fixes the systematic tile over-read in both regimes.
# --- Depth-Duration-Frequency (DDF) 10-year thresholds ----------------
# User-provided log-log DDF fit on the EURO-CORDEX grid (IDF/ folder). Per grid
# point: a (slope) & b (intercept), with 10-y depth(mm) = 10**(a*log10(D_h)+b).
# a,b were fit on the 3-24 h Poschlod 10-y levels (this de-biases the raw 1 h).
# This REPLACES the Poschlod point threshold for the severity computation
# (nearest grid point; ARF is still applied). Read only at precompute time -
# the baked thr_mm lives in the committed attrs/geometry, so cycles don't need it.
DDF_FILE = BASE / "IDF" / "IDF_loglogParameters.txt"
DDF_D_MIN_H, DDF_D_MAX_H = 1.0, 24.0     # clamp response time to the DDF fit domain

RV_JSON = "https://api.rainviewer.com/public/weather-maps.json"
CAL_FACTOR = 0.20                         # <-- radar calibration (tune vs local gauges)
TILE_WORKERS = 16                         # concurrent radar-tile downloads per frame
                                          # (the domain is ~156 tiles/frame; sequential
                                          # fetch was the main runtime cost). 0/1 = serial.
RAIN_ALPHA_MIN = 120
ZR_A, ZR_B = 200.0, 1.6                   # Marshall-Palmer Z = A*R^B
DBZ_MAX = 53.0                            # clip hail tail
PALETTE = [
    (5 , (150,230,240)), (10,(108,209,235)), (15,( 54,186,229)),
    (20,(  0,163,224)), (25,(  0,136,191)), (30,(  0,119,170)),
    (35,( 60,190, 90)), (40,(240,240, 60)), (45,(250,180, 40)),
    (50,(235, 90, 40)), (55,(190, 30, 30)), (60,(220, 60,180)),
    (65,(240,150,230)),
]

# Open-Meteo (model mm) - kept only as an optional stratiform cross-check /
# for the future forecast-lead-time extension. NOT the primary trigger
# (it smooths/misses convective cells - see README).
OM_URL = "https://api.open-meteo.com/v1/forecast"

# --- Areal Reduction Factor: De Michele, Kottegoda & Rosso (2001) ------
# ARF(A,T) = [1 + varpi*(A*^z / T)^b]^(-v/b), A* = max(A-A0,0) km2, T in h.
# (= Eq. 3 of Ceresetti et al. 2012, WAF, the severity-diagram paper.)
# z = a/b where a is the area-decay exponent. Pick the fit for your region:
ARF_VARPI, ARF_B, ARF_Z, ARF_V = 0.011,   0.40, 0.70,        0.70    # UK/NERC (broad, pan-EU default)
# ARF_VARPI, ARF_B, ARF_Z, ARF_V = 0.0905,  0.540, 1.0,        0.484   # Milan (urban)
# ARF_VARPI, ARF_B, ARF_Z, ARF_V = 0.00632, 0.34,  0.55/0.34,  0.84    # Cevennes flat  (Ceresetti 2012 T.1)
# ARF_VARPI, ARF_B, ARF_Z, ARF_V = 0.00234, 0.14,  0.52/0.14,  0.64    # Cevennes mountain (Ceresetti 2012 T.1)
ARF_A0_KM2 = 156.0

# --- Severity: ratio -> return period (Ceresetti et al. 2012) ---------
# The colour of each basin IS its "severity" = return period at its own
# space-time scale (A = catchment area, D = response time). Growth curve
# anchored on (ratio, years); Geneva-like default, tune per region.
RP_ANCHORS  = [(1.0, 10), (1.2, 30), (1.4, 100)]
RP_COLORS   = [(10, "#ffeda0", "~10y"), (30, "#feb24c", "~30y"), (100, "#f03b20", ">=100y")]
WATCH_RATIO = 0.8
# Skip the (per-basin) response-time severity math for basins whose LONGEST fixed
# window holds less than this much rain. The longest window (>= any response time)
# bounds the response-time accumulation from above, so this never hides an alert:
# a basin needs tens of mm to reach WATCH_RATIO of its 10-y level. Big speed-up
# because the dry majority skips the classification loop entirely.
SEVERITY_MIN_MM = 10.0

# Rainfall shading for WET-but-below-watch basins, so ordinary rain is visible
# (not just extreme alerts). (accumulated mm >=, colour), pale -> blue.
RAIN_MIN_MM = 0.3
RAIN_TIERS  = [(0.3, "#d7ecf7"), (3.0, "#9ecae1"), (10.0, "#4292c6")]
