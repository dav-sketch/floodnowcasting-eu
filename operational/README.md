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
   **De Michele–Kottegoda–Rosso (2001)** ARF `[1+ϖ(A*^z/T)^b]^(−v/b)`.
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
| `ARF_*` | De Michele (2001) ARF params — UK/NERC default; Milan & Cévennes (Ceresetti 2012) alternatives commented |
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
