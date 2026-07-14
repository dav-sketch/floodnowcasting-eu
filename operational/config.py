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

SIMPLIFY = {7: 0.010, 8: 0.007}                       # per-level web simplification (deg); finer = crisper but bigger
COORD_PRECISION = 4                                   # geojson coord decimals (~11 m); big filesize win

# --- domain & cadence -------------------------------------------------
DOMAIN_BBOX = (-12.0, 34.0, 45.0, 72.0)  # (lon_min,lat_min,lon_max,lat_max) - all Europe
TILE_Z      = 6                          # radar tile zoom (must stay fixed once the store exists)

# Level-of-detail: coarse level when zoomed out, finer as you zoom in.
LEVELS      = [7, 8, 9]                   # HydroBASINS levels COMPUTED each cycle (severity + pins)
POLY_LEVELS = [7, 8]                      # levels whose polygons are served as zoom layers (L9 too big whole-EU)
LEVEL_MINZOOM = {7: 0, 8: 8}             # a poly level's polygons show from this map zoom until the next takes over
MAP_MINZOOM   = 3                         # prevent zooming out past continent
MAP_MAXZOOM   = 10                        # prevent excessive zoom (avoids missing-tile ugliness)
RADAR_MAXZOOM = 7                         # radar raster native maxzoom; MapLibre overzooms beyond (no blank tiles)

# Alert pins: every alerting basin (any level) drops a coloured dot at its centre,
# visible at all zooms so flash-flood (L9) alerts are spottable from far out.
PIN_LABELS = ["watch", "~10y", "~30y", ">=100y"]   # which severities get a pin
PIN_RADIUS = {7: 9, 8: 6, 9: 4}                    # circle radius (px) by level: L7 large ... L9 small
WINDOW_H    = 10.0                        # max rolling window retained in the store (hours)
UPDATE_MIN  = 30                          # loop cadence; clamped to [15, 120] by run.py
FRAME_INTERVAL_MIN = 10                   # RainViewer past-frame spacing

# --- Quantitative rainfall: RainViewer radar (sees convective cells) ---
# Colour PNG tiles -> dbZ -> mm/h (Marshall-Palmer), then a CALIBRATION factor.
# CAL_FACTOR was tuned so decoded cell depths match observations/gauges:
# Bucharest convective cells -> 0.22, Swiss stratiform vs Open-Meteo -> 0.21.
# A single ~0.20 multiplier fixes the systematic tile over-read in both regimes.
RV_JSON = "https://api.rainviewer.com/public/weather-maps.json"
CAL_FACTOR = 0.20                         # <-- radar calibration (tune vs local gauges)
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

# Rainfall shading for WET-but-below-watch basins, so ordinary rain is visible
# (not just extreme alerts). (accumulated mm >=, colour), pale -> blue.
RAIN_MIN_MM = 0.3
RAIN_TIERS  = [(0.3, "#d7ecf7"), (3.0, "#9ecae1"), (10.0, "#4292c6")]
