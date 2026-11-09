# FloodNowcasting.eu 2.0 — operational pipeline

Live extreme-rainfall early-warning over Europe, on free data. Each catchment is
coloured by its **severity** = the estimated rainfall **return period** at its own
space–time scale (area, response time), following the severity-diagram framework
of Ceresetti et al. (2012, *Weather and Forecasting*).

## Method (one cycle)

1. **Quantitative rain (Nov-2026: OPERA primary, RainViewer gap-fill)** — see
   "Rainfall data & merging" below. Hourly per-basin rain over the last
   `WINDOW_H` hours, merged per basin and per hour from:
   - **EUMETNET OPERA NIMBUS 1-h accumulation** (`ACRR`, 2 km, QC'd pan-European
     composite, CC BY 4.0) from the public S3 cache that keeps **24 h** → any
     late/missed run is backfilled exactly;
   - **RainViewer** 10-min tiles (colour → dBZ by exact "Universal Blue" lookup →
     Marshall-Palmer), only where OPERA has no radar, rescaled to OPERA's level.
2. **Accumulate** each basin's areal-average rain over **its own response time**
   `t_lag ≈ 0.9·UP_AREA^0.38 h`, capped at `WINDOW_H` and **rounded to whole hours**
   (`D_ddf_h`); the same window is used for the observation and the threshold.
3. **Threshold** — 10-y **Depth-Duration-Frequency** level at the basin's duration
   from the user log-log DDF fit (`IDF/IDF_loglogParameters.txt`, nearest EURO-CORDEX
   grid point): `depth(mm) = 10**(a·log10(D_h)+b)`. `a,b` are fit on the 3–24 h
   10-y levels (de-biases the raw 1 h). Reduced to catchment-areal by the
   **De Michele–Kottegoda–Rosso (2001, Eq. 14)** ARF, also Eq. 3 of Ceresetti
   et al. (2012): `ARF(A,D) = [1 + ϖ·A^a / D^b]^(−v/b)`, evaluated on the
   **basin's own area** (`SUB_AREA`, the area the radar mean is taken over) and
   normalised by `ARF(156 km², D)` because a Poschlod/DDF cell is itself a
   ~156 km² areal mean. See **ARF** below.
4. **Severity** — ratio (areal rain / areal 10-y level) → return period via a
   growth curve (`RP_ANCHORS`, Geneva-like default). Colours: ~10 y → ~30 y → ≥100 y.
   Computed only for basins with ≥ `SEVERITY_MIN_MM` in the longest fixed window
   (that window bounds the response-time accumulation, so no alert is missed).
5. **Outputs** — `out/alerts.geojson`, `out/alerts.json`, `out/map.html`
   (basins over the RainViewer radar visual layer).

## Usage

```bash
cd operational
python run.py --precompute      # once: build catchment + grid-mapping cache (state/)
python run.py --once            # single cycle now
python run.py --loop 30         # run every 30 min (clamped to 15–120)
```

For unattended operation, call `python run.py --once` from Task Scheduler / cron /
a GitHub Actions cron. State is only a cache; each cycle is self-contained.

## Configuration (`config.py`)

| Knob | Meaning |
|------|---------|
| `DOMAIN_BBOX` | area of interest `(lon_min,lat_min,lon_max,lat_max)`; default all Europe |
| `LEVELS` / `POLY_LEVELS` | HydroBASINS levels computed / served as zoom polygons (LOD); default `[6, 7, 8, 9]`. L6 = continental first view, L7 large, L8/L9 fine |
| `SEVERITY_LEVELS` | levels that get severity classification; default `[8, 9]`. Others (L6/L7) are **accumulation-only** (severity is not meaningful for such large basins) |
| `LEVEL_MINZOOM` | map zoom at which each level's polygons appear (coarse→fine); default `{6:0, 7:5, 8:7, 9:9}` |
| `MAP_MINZOOM` / `MAP_MAXZOOM` | zoom caps (prevents blank-tile ugliness); default 3 / 11 |
| `RADAR_MAXZOOM` | radar raster native maxzoom; MapLibre overzooms beyond it |
| `WINDOW_H` | max rolling window retained (h); default 12 (≥ longest `ACC_WINDOWS_H`) |
| `ACC_WINDOWS_H` / `ACC_DEFAULT_H` / `ACC_RAMP` | fixed rainfall-accumulation windows offered in the "View" selector (`[2,4,8,12]` h), the default window, and the blue colour ramp |
| `UPDATE_MIN` | loop cadence (15–120) |
| `CAL_FACTOR` | radar calibration multiplier (default 1.0 = raw Marshall-Palmer); tune against local gauges |
| `TILE_Z` | radar tile zoom (keep fixed once the store exists) |
| `ARF_SET` / `ARF_SETS` | which published ARF parameter set `(ϖ, a, b, v)` to use; default `"cevennes_flat"` (Ceresetti et al. 2012 Table 1). Also `"uk_nerc"`, `"milan_urban"`, `"cevennes_mountain"` |
| `ARF_AREA_FIELD` | HydroBASINS area column fed to the ARF; **`SUB_AREA`** (the area the rain is averaged over) |
| `ARF_REF_AREA_KM2` | reference "point" area = one DDF grid cell (156 km²); ARF is normalised by `ARF(A_ref, D)` |
| `RP_ANCHORS` | ratio→return-period growth curve (tune per region) |

## Static webapp (`web/`)

A dependency-light [MapLibre GL](https://maplibre.org) site. Per-level catchment
geometry (`web/data/catchments_L{6,7,8,9}.geojson`, static, simplified) is
**lazy-loaded** as you zoom into each band, and the tiny per-level
`alerts_L*.json` (rewritten each cycle) is joined client-side. A **View** selector
switches basin colouring between **severity** (L8/L9 only) and **rainfall
accumulated over a chosen fixed window** (2/4/8/12 h, all levels), with the live
RainViewer radar as an independent overlay. Nothing but static files → $0 hosting.

**Preview locally:**
```bash
cd operational/web
python -m http.server 8777      # open http://localhost:8777
```

**Deploy free (GitHub Pages + cron):** see the step-by-step in the repo root
`DEPLOY.md`. In short: precompute locally, commit the static artifacts, enable
GitHub Pages on `operational/web/`, and the included `.github/workflows/nowcast.yml`
runs a cycle every 15 min and commits the fresh per-level `alerts_L*.json`.

(Level 9 across all Europe is ~10 MB gzipped as GeoJSON — too heavy. For finer
zoom-in detail everywhere, generate **PMTiles** via `tippecanoe` so the geometry
is one cacheable vector-tile file instead of megabytes of JSON.)

## Areal Reduction Factor (revised)

`ARF(A,D) = [1 + ϖ·A^a / D^b]^(−v/b)` — De Michele, Kottegoda & Rosso (2001)
Eq. 14, identical to Eq. 3 of Ceresetti et al. (2012). `a` is the **area**
exponent, `b` the duration exponent. Both papers also quote the *dynamic-scaling*
exponent `z = a/b`, which is where this went wrong before:

(ARF column below is the **raw** published formula, so it can be compared with
the papers' figures; the pipeline additionally divides by `ARF(156 km², D)`,
which lifts the default 5000 km²/12 h value from 0.53 to 0.59.)

| set | ϖ | a | b | v | fitted on | raw ARF at 5000 km², 12 h |
|-----|---|---|---|---|-----------|----------------------|
| `uk_nerc` | 0.011 | **0.28** (`z=0.70`×`b`) | 0.40 | 0.70 | NERC FSR, 1 min–25 d, 1–18 000 km² | **0.93** |
| `milan_urban` | 0.0905 | 0.540 | 0.540 | 0.484 | Milan gauges, 0.25–300 km², 20 min–6 h | 0.34 |
| **`cevennes_flat`** (default) | 0.00632 | **0.55** | 0.34 | 0.84 | Cévennes, **A ≥ 50 km², D ≥ 2 h** | **0.53** |
| `cevennes_mountain` | 0.00234 | 0.52 | 0.14 | 0.64 | Cévennes highlands, same limits | 0.55 |

The old default (`uk_nerc`) has an area exponent of only 0.28, so it barely
decays with area: it returned ARF 0.86–0.98 across this app's whole domain, i.e.
**an essentially inert areal correction**. The NERC FSR tables only pair large
areas with *long* durations, so at 1–12 h over 10²–10⁴ km² that fit is pure
extrapolation. The Cévennes fit was made on exactly the scales used here and is
the ARF of the severity-diagram method itself, so it is now the default. Net
effect: a large-basin event is scored **~2× more severe** than before.

Two related corrections came with it:

- **Area.** The ARF is now evaluated on `SUB_AREA` (the polygon the radar mean is
  taken over), not `UP_AREA` (upstream drainage). With a properly steep ARF,
  `UP_AREA` would have driven the Danube/Volga trunk basins (up to 1.4·10⁶ km²)
  to ARF ≈ 0.01 and manufactured permanent false alarms.
- **Reference "point".** A Poschlod/DDF value is a 0.11° (~12.5 km, ~156 km²)
  cell mean, not a point. Following De Michele's Eq. 2 the ARF is normalised,
  `ARF_used = min(ARF(A,D)/ARF(156 km², D), 1)`, so a basin the size of one cell
  gets exactly 1.0. (The old code subtracted areas, `A* = A − 156`, which is the
  paper's rain-gauge-orifice form and is not the right reduction at 156 km².)

Print the active grid with:

```bash
cd operational && python -c "import pipeline; pipeline.arf_table()"
```

**Changing any ARF knob requires re-baking the thresholds**, because `thr_mm` is
precomputed into the committed artifacts: `python run.py --precompute` (already
forces a rebuild; needs the `hybas/` shapefiles, and rewrites
`state/attrs_L*.csv` and `web/data/catchments_L*.geojson`).

### Known limitation (unchanged by this revision)

The rainfall is accumulated over the **local polygon only**, while `t_lag` still
comes from `UP_AREA`. So a basin gets a response-time window sized for its whole
upstream catchment but rain averaged over just its own sub-basin. The two biases
partly cancel (longer `D` → higher threshold *and* more accumulated rain), but the
clean fix is to route accumulation downstream through `NEXT_DOWN` so the
observation really is the upstream-area mean. Left as future work.

## Rainfall data & merging (Nov-2026)

**Why it changed.** In a rainy week the app raised no alert. Two bugs together
kept every run on a 2-hour window: (1) the Actions cache restored the key prefix
`nowcast-store-v2-` but saved `nowcast-store-<id>`, so the store was never
restored; (2) RainViewer now limits tiles to **500 per minute** (HTTP 429), and
a cold start asks for about 2000. Each 429 was decoded as a *dry* tile and stored,
and a stored frame was never fetched again. The live site showed
`store_span_h = 2.0` and identical 2 h and 12 h totals.

**Sources** (`qpe.py`, both resampled to the existing Web-Mercator domain grid,
so the committed `pixel_cat_L*.npz` maps are reused; no shapefiles needed):

| | OPERA NIMBUS `ACRR` (primary) | RainViewer (gap-fill) |
|---|---|---|
| what | 1-h accumulation, new file every 15 min | 10-min radar frames (PNG tiles) |
| where | `https://s3.waw3-1.cloudferro.com/openradar-24h/YYYY/MM/DD/OPERA/COMP/OPERA@YYYYMMDDTHHMM@0@ACRR.h5` (no key; key time = END of hour) | `api.rainviewer.com/public/weather-maps.json` |
| history | **24 h** | ~2 h |
| grid | 1900×2200 × 2 km LAEA (`where/projdef`); nodata = outside coverage, undetect = dry; QI layer | Web-Mercator z6 |
| coverage | most of EU incl. IT/AT/CH/ES/RO; **not** Greece, Ukraine, Turkey, Belarus/Russia | everywhere |
| licence | CC BY 4.0 — attribution shown on the map | RainViewer terms |

**Merge rule**, per basin and per hour slot `(T-(k+1)h, T-kh]`, where the anchor
`T` = the newest OPERA end time (or the newest RainViewer frame time if OPERA lags
it by more than `OPERA_MAX_LAG_MIN`):
1. OPERA `ACRR(T-kh)` if ≥ `MIN_VALID_FRAC` of the basin's pixels are observed
   (pixels with QI < `OPERA_QI_MIN` are dropped, which removes the 40–200 mm/h
   clutter spikes);
2. else RainViewer: `6 × mean` of the frames in the slot (≥ `RV_MIN_FRAMES_PER_H`),
   × `k`, the RainViewer→OPERA factor. `k = Σ OPERA / Σ RainViewer` over the
   basin-hours both sources observed, smoothed in log space across runs
   (`state/rv_bias.json`). First live estimate: **k ≈ 0.56** (1745 wet L8 basins,
   correlation 0.78), i.e. RainViewer reads ~1.75× OPERA;
3. else **missing**: the hour adds 0 to the sum but lowers the basin's `coverage`
   (written to `alerts_L*.json` as `cov` per window and shown in the popup with
   a `*`). A failed download can no longer pass for "no rain".

Because the slots are tied to the *data* time, totals do not depend on when or
how often the job runs. `python test_accumulation.py` replays a synthetic
timeline at 15-min, hourly, GitHub-jitter (gaps up to 9 h), single-run-after-20-h
and repeated same-minute cadences: OPERA basins are exact (error < 1e-14 mm) in
every case, RainViewer hours seen in full are exact, and partially seen hours are
rescaled estimates.

**Operational limits.**
- OPERA covers any cadence up to a 24 h gap. Outside OPERA, RainViewer still
  needs runs less than 2 h apart; GitHub's best-effort cron leaves holes there,
  and those holes show up as coverage < 1.
- RainViewer is paced at `RV_MAX_TILES_PER_MIN = 400`. Only tiles touching
  OPERA-blind basins are fetched (`RV_MODE="gapfill"`, ~96 of 156). If any OPERA
  hour is missing, the whole domain is fetched for that run.
- OPERA NIMBUS is **not gauge-adjusted** (Marshall-Palmer, no bias correction).
  `k` harmonises RainViewer to OPERA, not to gauges.
- Runtime: about 4–5 min on a cold start (RainViewer pacing), about 40 s afterwards.

## Notes & next steps

- **QPE choice.** OPERA is now the primary source (see above). A gauge
  adjustment would be the next step (e.g. the Netatmo-adjusted OPERA work by
  Overeem et al.). Open-Meteo (`OM_URL`) is kept only as a stratiform
  cross-check / for the forecast-lead-time extension — it must not be the trigger.
- **Scale to all Europe.** Widen `DOMAIN_BBOX`; keep API calls bounded with a
  sensible `GRID_DEG`, or only query grid cells flagged wet by a coarse pre-scan.
- **Static webapp.** Precompute thresholds/geometry as PMTiles; publish the small
  `alerts.json` from a cron job to GitHub/Cloudflare Pages + MapLibre GL ($0).
- **Lead time.** Extend with ECMWF open-data IFS precip beyond the radar horizon;
  IFS soil moisture for initial abstraction.
