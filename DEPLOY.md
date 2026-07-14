# Going live — free, always-on FloodNowcasting.eu 2.0

The site is 100% static files (`operational/web/`). A GitHub Actions cron runs one
pipeline cycle every 15 min and redeploys the page with fresh alerts. Total cost: €0.

You need: a GitHub account and `git` installed. (Optional but easier: the `gh` CLI.)

---

## Step 0 — precompute locally (once)

You already did this. It builds the committed artifacts:

```bash
cd operational
python run.py --precompute
```

Creates (small, meant to be committed):
- `state/attrs_L*.csv`         (per-level basin thresholds/centroids; L7/L8/L9)
- `state/pixel_cat_L*.npz`     (compressed grid→basin map; L7/L8/L9)
- `web/data/catchments_L7.geojson`, `..._L8.geojson`  (served polygon geometry)
- `web/data/levels.json`       (LOD + pins manifest)

L9 is computed for the alert **pins** only (its 41k polygons are never served), so
it has `attrs_L9`/`pixel_cat_L9` but no geojson. The dynamic outputs
(`alerts_L*.json`, `pins.geojson`, `frames_L*.csv`) are git-ignored and produced
each cycle.

> Redo this only when you change `DOMAIN_BBOX`, `LEVELS`, `TILE_Z` or `SIMPLIFY`.

---

## Step 1 — put the project on GitHub  ← the part you asked about

From the project root (`FloodNowCasting.eu_2.0/`):

```bash
git init
git add .
git commit -m "FloodNowcasting.eu 2.0"
```

The included `.gitignore` already excludes the big raw inputs (`hybas/`, `_cache/`,
`IDF/`) and the machine-generated `frames_L*.csv` / `alerts_L*.json`, while **keeping**
the committed precompute artifacts above. Sanity-check the commit is small:

```bash
git count-objects -vH        # expect ~20–25 MB, not hundreds
```

Create the GitHub repo and push. Easiest with the `gh` CLI:

```bash
gh repo create floodnowcasting-eu --public --source=. --push
```

…or manually: create an empty **public** repo on github.com, then:

```bash
git remote add origin https://github.com/<YOUR_USER>/floodnowcasting-eu.git
git branch -M main
git push -u origin main
```

---

## Step 2 — turn on GitHub Pages

On github.com: **repo → Settings → Pages → Build and deployment → Source: “GitHub
Actions”.** (No branch/folder to pick — the workflow publishes the site.)

---

## Step 3 — run the workflow

**repo → Actions**. If prompted, click *“I understand… enable workflows”*. Open the
**nowcast** workflow → **Run workflow** (manual trigger) to start immediately; after
that the 15-min cron takes over.

- The **first run takes ~4 min** (it fetches all ~13 radar frames to warm the store).
- Later runs are quick (only new frames).
- When it finishes, your live site is at:
  **`https://<YOUR_USER>.github.io/floodnowcasting-eu/`**

The 6–10 h accumulation window fills in gradually as the store grows over ~40 runs
(≈10 h). Alerts appear only when rain over a basin passes its return-period thresholds.

---

## Changing the region / levels later

1. Edit `operational/config.py` (`DOMAIN_BBOX`, `LEVELS`, …).
2. `cd operational && python run.py --precompute`
3. `git add -A && git commit -m "reconfigure" && git push`

The next cron run picks it up.

---

## Alternative — run it on your own always-on machine

If you'd rather not use CI (or want the full 10 h window without cache limits), run
the loop on any always-on box (mini-PC, Raspberry Pi, cheap VM):

```bash
cd operational
python run.py --loop 15          # updates web/data/ every 15 min
```

Then serve/publish `operational/web/` with any static host (Netlify drop, Cloudflare
Pages, `python -m http.server`, nginx, …) pointed at that folder.

---

## Notes & limits

- **GitHub Actions cron** is best-effort; under load runs may be delayed a few minutes.
- **Actions cache** (rolling store) can be evicted after ~7 days of inactivity; the
  window simply rebuilds from the last 2 h on the next run.
- **Accuracy**: rainfall is calibrated RainViewer radar (`CAL_FACTOR`); tune it against
  local gauges. Swap in EUMETNET OPERA QPE for gauge-adjusted millimetres.
- **Finer detail everywhere** (level 9 all-Europe): generate PMTiles with `tippecanoe`
  instead of GeoJSON — see `operational/README.md`.
