# FloodNowcasting.eu 2.0 — operational pipeline

Live extreme-rainfall early-warning over Europe, on free data. Each catchment is
coloured by its **severity** = the estimated rainfall **return period** at its own
space–time scale (area, response time), following the severity-diagram framework
of Ceresetti et al. (2012, *Weather and Forecasting*).

## Method (one cycle)

1. **Quantitative rain** — [RainViewer](https://rainviewer.com) radar tiles decoded
   to mm/h (palette→dBZ→Marshall-Palmer) × a **calibration factor** `CAL_FACTOR`.
   Radar is used (not a model) because it *sees convective cells*; the model-based
   Open-Meteo smooths/misses them (~10× low), which is the fatal failure mode for
   flash floods. `CAL_FACTOR≈0.20` fixes the tile over-read (tuned on Bucharest
   convective cells and Swiss stratiform — both gave ~0.2). Frames are stored in a
   **rolling per-catchment table**, so 6–10 h windows build up over successive runs
   (RainViewer exposes only ~2 h per call). Tiles (~156/frame) are downloaded
   **concurrently** (`TILE_WORKERS`) — this was the dominant per-cycle cost.
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
| `CAL_FACTOR` | radar calibration multiplier (~0.20); tune against local gauges |
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

## Notes & next steps

- **QPE choice.** Calibrated RainViewer sees convective cells with realistic
  magnitude, but the calibration is a single empirical factor. For gauge-adjusted
  radar millimetres (no calibration guesswork) swap in EUMETNET **OPERA** QPE
  (needs registration). Open-Meteo (`OM_URL`) is kept only as a stratiform
  cross-check / for the forecast-lead-time extension — it must not be the trigger.
- **Scale to all Europe.** Widen `DOMAIN_BBOX`; keep API calls bounded with a
  sensible `GRID_DEG`, or only query grid cells flagged wet by a coarse pre-scan.
- **Static webapp.** Precompute thresholds/geometry as PMTiles; publish the small
  `alerts.json` from a cron job to GitHub/Cloudflare Pages + MapLibre GL ($0).
- **Lead time.** Extend with ECMWF open-data IFS precip beyond the radar horizon;
  IFS soil moisture for initial abstraction.
