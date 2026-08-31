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
# WRR 37(12):3247-3252 Eq. 14 == Eq. 3 of Ceresetti et al. 2012 (WAF 27:162-179),
# the severity-diagram paper this app implements:
#
#       ARF(A, D) = [ 1 + varpi * A**a / D**b ] ** (-v / b)
#
# A = area (km2) the rainfall is averaged over, D = duration (h); `a` is the
# area-decay exponent, `b` the duration exponent, v the point-rainfall time
# exponent. Both papers ALSO quote the dynamic-scaling exponent z = a / b
# (i.e. a = z*b) - an easy trap, because De Michele's UK table quotes z = 0.70
# with b = 0.40, which means the area exponent is only a = 0.28.
#
# Published parameter sets, as (varpi, a, b, v):
ARF_SETS = {
    # De Michele et al. 2001 fit to the NERC Flood Studies Report (UK).
    # Fitted across 1 min-25 days and 1-18000 km2, BUT the FSR tables only pair
    # large areas with long durations, so at this app's scales (1-12 h over
    # 1e2-1e4 km2) it extrapolates and barely reduces anything: ARF 0.86-0.98,
    # i.e. an almost inert areal correction. Kept for reference/comparison.
    "uk_nerc":           (0.011,   0.28,  0.40,  0.70),
    # De Michele et al. 2001 fit to the Milan gauge network: 0.25-300 km2 and
    # 20 min-6 h, z = 1 so a = b = 0.540. Very steep - small urban areas only.
    "milan_urban":       (0.0905,  0.540, 0.540, 0.484),
    # Ceresetti et al. 2012 Table 1, Cevennes-Vivarais radar+gauge, fitted on
    # areas >= 50 km2 and durations >= 2 h - exactly the scales used here, and
    # the parameters of the severity-diagram method itself. DEFAULT.
    "cevennes_flat":     (0.00632, 0.55,  0.34,  0.84),
    "cevennes_mountain": (0.00234, 0.52,  0.14,  0.64),
}
ARF_SET = "cevennes_flat"
# Sanity: at A = 5000 km2, D = 12 h this gives ARF 0.53 (uk_nerc gave 0.93), so a
# large-basin event is scored roughly twice as severe as before - which is the
# behaviour Ceresetti et al. 2012 Fig. 4 shows. Print pipeline.arf_table() to
# see the full grid, or set ARF_SET = "uk_nerc" to reproduce the old numbers.

# Which HydroBASINS area column feeds the ARF. The severity test is "areal
# observation over area A vs areal 10-y quantile over the SAME A", so this must
# be the area the radar accumulation is actually averaged over - the basin
# polygon, i.e. SUB_AREA. (UP_AREA is the whole upstream drainage: ~4x larger
# than SUB_AREA at level 9 on average and up to 1.4e6 km2 on the Danube/Volga
# trunks, where a properly steep ARF collapses to ~0.01 and would manufacture
# permanent false alarms. See README "Known limitations" for the flip side:
# the accumulation is local-polygon only, while t_lag still uses UP_AREA.)
ARF_AREA_FIELD = "SUB_AREA"

# The DDF/Poschlod "point" value is not a point: it is a 0.11 deg (~12.5 km,
# ~156 km2) grid-cell mean, so it already carries some areal averaging.
# De Michele's Eq. 2 defines the ARF relative to that reference area, so we use
#       ARF_used(A, D) = min( ARF(A, D) / ARF(A_ref, D), 1 )
# which correctly returns 1.0 for a basin the size of one grid cell. (The old
# code instead subtracted the reference area, A* = A - A0, which is the paper's
# rain-gauge-orifice form and is not the right reduction at 156 km2.)
ARF_REF_AREA_KM2 = 156.0

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
