"""Builds Poschlod2021_PointRainfall.ipynb from ordered (type, source) cells.

Same convention as _build_notebook.py: the notebook is a build artifact, so edit
THIS file and re-run `python _build_poschlod_notebook.py`, never the .ipynb.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "Poschlod2021_PointRainfall.ipynb")

cells = []
def md(src):  cells.append(("markdown", src))
def code(src): cells.append(("code", src))

# ------------------------------------------------------------------ intro
md(r"""# Rainfall intensity at any point of Europe — Poschlod et al. (2021)

**What this notebook does.** Given *any* latitude/longitude in Europe and *any*
accumulation duration, return the **10-year rainfall return level** — as a depth
(mm) and as an intensity (mm h⁻¹) — with its uncertainty band, plus the
**areal** value for a catchment of a given size.

**The source.** Poschlod, Hundhausen, Ludwig, Zhu & Kotlarski (2021),
*"Ten-year return levels of sub-daily extreme precipitation over Europe"*,
[ESSD 13:983–1003](https://doi.org/10.5194/essd-13-983-2021); data at
[Zenodo 3878888](https://zenodo.org/records/3878888). Return levels were obtained
by fitting a GEV to **1500 model-years** of hourly precipitation (a 50-member
CRCM5 large ensemble × 30 years) on the 0.11° (~12.5 km) EURO-CORDEX grid.
Each file gives, per grid cell, the **q5 / median / q95** of the 10-year level
across the 50 GEV fits — so the band is *fitting/ensemble* uncertainty.

**What the dataset does and does not give you:**

| | |
|---|---|
| Durations | **1, 3, 6, 12, 24 h** only — five anchors |
| Return period | **10 years only** — there is no 2-y or 100-y file |
| Grid | 0.11° rotated-pole EURO-CORDEX, 78 400 cells, lat 30.4–64.7, lon −25.0–37.9 |
| Value | a **grid-cell mean** over ~156 km², *not* a true point value |

So "rainfall intensity at any point, at any duration" needs three explicit
modelling steps, each isolated in its own function below:

1. **In space** — the grid is rotated-pole, so it is *not* a regular lat/lon
   mesh; we use nearest-cell or inverse-distance weighting over the k nearest
   cells, with true great-circle distances (§2).
2. **In duration** — either log–log interpolation between the five anchors, or a
   fitted **DDF power law** `depth = 10^b · D^a` that also extrapolates (§3).
   This is the same model as the project's `IDF/IDF_loglogParameters.txt`, which
   we reproduce and validate in §4.
3. **In space *extent*** — the areal reduction factor turns the cell value into a
   catchment value (§7).

Everything else — return periods other than 10 y (§8) — is flagged as
extrapolation *beyond* Poschlod, because it is.

> Run this notebook from the repository root (`FloodNowCasting.eu_2.0/`). The five
> Poschlod files are cached in `_cache/`; if absent they are downloaded (~50 MB).
""")

# ------------------------------------------------------------------ config
md("## 1 — Setup and data load")
code(r'''# === Configuration ===================================================
from pathlib import Path
import numpy as np, pandas as pd, requests

BASE  = Path.cwd()                     # run from FloodNowCasting.eu_2.0/
CACHE = BASE / "_cache"; CACHE.mkdir(exist_ok=True)
IDF   = BASE / "IDF"                   # holds the project's own DDF fit file

ZENODO = "https://zenodo.org/records/3878888/files/ReturnLevel_10year_{d}.txt?download=1"
DURATIONS_H = np.array([1.0, 3.0, 6.0, 12.0, 24.0])       # the five Poschlod anchors
DUR_TAGS    = ["1h", "3h", "6h", "12h", "24h"]

# Duration range used to FIT the DDF power law. Poschlod themselves report the
# 1 h field as the weakest (biased low, especially over complex terrain: -16.3 %
# mean bias vs +8.2 % at 24 h), so the fit is made on 3-24 h and the 1 h anchor
# is treated as a diagnostic, not a constraint. Change to (1., 24.) to include it.
DDF_FIT_RANGE = (3.0, 24.0)

# Spatial interpolation defaults
IDW_K     = 4          # number of nearest grid cells
IDW_POWER = 2.0        # inverse-distance weight exponent
MAX_KM    = 40.0       # refuse to answer further than this from the nearest cell

# --- Areal Reduction Factor (mirrors operational/config.py) ------------
# De Michele, Kottegoda & Rosso (2001) Eq. 14 == Ceresetti et al. (2012) Eq. 3:
#     ARF(A, D) = [1 + varpi * A**a / D**b] ** (-v/b)
# NOTE: `a` is the AREA exponent. The papers also quote the dynamic-scaling
# exponent z = a/b, so a = z*b -- De Michele's UK row reads z=0.70, b=0.40,
# i.e. a = 0.28, which is why that set barely decays with area.
ARF_SETS = {                        # (varpi, a, b, v)
    "uk_nerc":           (0.011,   0.28,  0.40,  0.70),
    "milan_urban":       (0.0905,  0.540, 0.540, 0.484),
    "cevennes_flat":     (0.00632, 0.55,  0.34,  0.84),
    "cevennes_mountain": (0.00234, 0.52,  0.14,  0.64),
}
ARF_SET          = "cevennes_flat"
ARF_REF_AREA_KM2 = 156.0            # one 0.11 deg cell -- the "point" reference

# --- Plot styling (data-viz conventions used throughout) ---------------
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]     # fixed categorical order, never cycled
BLUE_RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
             "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
INK, INK2, GRIDC = "#0b0b0b", "#52514e", "#e3e2de"
print("Config OK.")
''')

code(r'''def load_poschlod():
    """Load the five 10-year return-level files into one dict of (5, N) arrays.

    Returns {"lat": (N,), "lon": (N,), "q5"/"median"/"q95": (5, N)} with rows
    ordered as DURATIONS_H. The five files share an identical cell ordering
    (asserted below), so one lat/lon pair serves all durations.
    """
    frames = {}
    for tag in DUR_TAGS:
        f = CACHE / f"returnlevel_10y_{tag}.txt"
        if not f.exists():
            print("  downloading", tag, "...")
            r = requests.get(ZENODO.format(d=tag), timeout=300); r.encoding = "utf-8"
            f.write_text(r.text, encoding="utf-8")
        df = pd.read_csv(f)                     # header is "# Pr (mm) Q5, ..."
        df.columns = ["q5", "median", "q95", "lat", "lon"]
        frames[tag] = df

    ref = frames[DUR_TAGS[0]]
    for tag in DUR_TAGS[1:]:                    # same cells, same order?
        assert np.allclose(frames[tag].lat, ref.lat) and \
               np.allclose(frames[tag].lon, ref.lon), f"grid mismatch in {tag}"

    out = {"lat": ref.lat.to_numpy(), "lon": ref.lon.to_numpy()}
    for stat in ("q5", "median", "q95"):
        out[stat] = np.vstack([frames[t][stat].to_numpy() for t in DUR_TAGS])
    return out

POS = load_poschlod()
N = POS["lat"].size
print(f"{N} grid cells | lat {POS['lat'].min():.2f}..{POS['lat'].max():.2f}"
      f" | lon {POS['lon'].min():.2f}..{POS['lon'].max():.2f}")
print(f"unique latitudes: {np.unique(POS['lat'].round(5)).size}  "
      f"-> rotated-pole grid, NOT a regular lat/lon mesh")
pd.DataFrame(POS["median"].T, columns=[f"{t} median mm" for t in DUR_TAGS]).describe().round(1)
''')

# ------------------------------------------------------------------ step 2
md(r"""## 2 — Interpolation in space

The EURO-CORDEX grid is **rotated-pole**: cell centres are regular in the rotated
frame but irregular in plain lat/lon (78 400 cells, 77 464 distinct latitudes).
Bilinear interpolation on a lat/lon mesh is therefore not available, and Euclidean
distance in degrees is anisotropic (a degree of longitude is ~11 km at 60 °N but
~19 km further south).

Both problems go away by putting the cell centres on the **unit sphere** and
building the k-d tree in 3-D Cartesian coordinates: nearest neighbours are then
exact, and the chord length converts to a true great-circle distance.

Two estimators are offered:

- `method="nearest"` — the containing cell. Honest: the dataset *is* a cell mean,
  so this returns exactly the published number with no invention.
- `method="idw"` — inverse-distance weighting over the `IDW_K` nearest cells.
  Smoother across cell boundaries, at the cost of blending neighbours.
""")
code(r'''from scipy.spatial import cKDTree

R_EARTH_KM = 6371.0088

def _xyz(lat, lon):
    """(lat, lon) in degrees -> unit-sphere Cartesian (..., 3)."""
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la)*np.cos(lo), np.cos(la)*np.sin(lo), np.sin(la)], axis=-1)

def _chord_to_km(chord):
    """Unit-sphere chord length -> great-circle distance in km."""
    return 2.0 * R_EARTH_KM * np.arcsin(np.clip(chord/2.0, 0.0, 1.0))

TREE = cKDTree(_xyz(POS["lat"], POS["lon"]))

# median centre-to-centre spacing, as a sanity check on the quoted 0.11 deg
_d, _ = TREE.query(_xyz(POS["lat"][:2000], POS["lon"][:2000]), k=2)
print(f"median cell spacing: {np.median(_chord_to_km(_d[:, 1])):.2f} km"
      f"   (0.11 deg ~ 12.2 km)")
''')

code(r'''def cell_weights(lat, lon, method="idw", k=IDW_K, power=IDW_POWER, max_km=MAX_KM):
    """Grid cells contributing to a point, with weights summing to 1.

    Returns (idx, w, dist_km) where idx indexes POS. Raises if the point is more
    than `max_km` from any cell (i.e. outside the EURO-CORDEX domain / at sea).
    """
    kq = 1 if method == "nearest" else k
    chord, idx = TREE.query(_xyz(float(lat), float(lon)), k=kq)
    chord, idx = np.atleast_1d(chord), np.atleast_1d(idx)
    dist = _chord_to_km(chord)
    if dist[0] > max_km:
        raise ValueError(f"({lat}, {lon}) is {dist[0]:.0f} km from the nearest "
                         f"Poschlod cell (limit {max_km:.0f} km) - outside the domain?")
    if method == "nearest" or dist[0] < 1e-9:      # sitting on a cell centre
        w = np.zeros_like(dist); w[0] = 1.0
    else:
        w = dist**(-power); w /= w.sum()
    return idx, w, dist

def point_levels(lat, lon, **kw):
    """10-year return level at a point, at the five Poschlod anchor durations.

    Returns a DataFrame indexed by duration (h) with depth q5/median/q95 (mm)
    and the corresponding mean intensities (mm/h).
    """
    idx, w, dist = cell_weights(lat, lon, **kw)
    rows = {stat: POS[stat][:, idx] @ w for stat in ("q5", "median", "q95")}
    df = pd.DataFrame(rows, index=pd.Index(DURATIONS_H, name="duration_h"))
    df.columns = ["depth_q5_mm", "depth_median_mm", "depth_q95_mm"]
    for c in ("q5", "median", "q95"):
        df[f"intensity_{c}_mmh"] = df[f"depth_{c}_mm"] / df.index.to_numpy()
    df.attrs["nearest_km"] = float(dist[0])
    df.attrs["n_cells"] = int(np.sum(w > 0))
    return df

# demo: Geneva
GVA = (46.20, 6.15)
_t = point_levels(*GVA)
print(f"Geneva {GVA} - nearest cell {_t.attrs['nearest_km']:.1f} km, "
      f"{_t.attrs['n_cells']} cells blended")
_t.round(2)
''')

# ------------------------------------------------------------------ step 3
md(r"""## 3 — Interpolation in duration: the DDF model

Two ways to get from five anchors to *any* duration:

**(a) log–log interpolation.** Straight-line interpolation of `log(depth)` against
`log(D)` between the anchors. Passes exactly through all five published values and
is what `operational/pipeline.py::threshold_point_mm` does — but it cannot be
trusted outside 1–24 h and inherits the 1 h low bias verbatim.

**(b) DDF power law.** Fit

$$\text{depth}(D) = 10^{\,b}\,D^{\,a} \qquad\Longleftrightarrow\qquad
\log_{10}\text{depth} = a\log_{10} D + b$$

by least squares over `DDF_FIT_RANGE` (3–24 h by default). One smooth curve, it
extrapolates, and — because the fit ignores the biased 1 h anchor — it *raises*
the short-duration end back to a plausible level. `a` is the DDF slope (~0.34
typically, i.e. depth grows roughly as `D^{1/3}`) and `10^b` is the depth at
`D = 1 h`. This is exactly the model behind `IDF/IDF_loglogParameters.txt`, which
§4 checks.

Note the intensity follows for free: `i(D) = depth(D)/D = 10^b · D^(a-1)`, so the
intensity decays as `D^(a-1)` ≈ `D^-0.66`.
""")
code(r'''def fit_ddf(durations, depths, fit_range=DDF_FIT_RANGE):
    """Least-squares fit of log10(depth) = a*log10(D) + b.

    `depths` may be (n_dur,) or (n_dur, M) -- the fit is vectorised over the
    trailing axis, so the whole 78 400-cell grid fits in one call.
    Returns (a, b), scalars or (M,) arrays.
    """
    m = (durations >= fit_range[0]) & (durations <= fit_range[1])
    if m.sum() < 2:
        raise ValueError(f"need >= 2 anchor durations inside {fit_range}")
    x = np.log10(durations[m])
    y = np.log10(np.asarray(depths, float)[m])
    xc = x - x.mean()
    a = (xc @ y) / (xc @ xc)                      # slope, broadcasts over columns
    b = y.mean(axis=0) - a * x.mean()
    return a, b

def ddf_depth(a, b, D_h):
    """Depth (mm) from DDF parameters at duration(s) D_h."""
    return 10.0 ** (a * np.log10(np.asarray(D_h, float)) + b)

# Per-cell DDF parameters for the WHOLE grid (one vectorised fit).
A_GRID, B_GRID = fit_ddf(DURATIONS_H, POS["median"])
print(f"grid DDF slope a:      p5 {np.percentile(A_GRID,5):.3f} | "
      f"median {np.median(A_GRID):.3f} | p95 {np.percentile(A_GRID,95):.3f}")
print(f"grid DDF intercept b:  p5 {np.percentile(B_GRID,5):.3f} | "
      f"median {np.median(B_GRID):.3f} | p95 {np.percentile(B_GRID,95):.3f}")
print(f"  -> median depth at D=1h from the fit: {10**np.median(B_GRID):.1f} mm "
      f"(raw Poschlod 1h median {np.median(POS['median'][0]):.1f} mm)")
''')

code(r'''def rain_depth_mm(lat, lon, D_h, model="ddf", stat="median",
                  fit_range=DDF_FIT_RANGE, **kw):
    """**Main entry point.** 10-year rainfall DEPTH (mm) at a point and duration.

    lat, lon : degrees (WGS84)
    D_h      : duration in hours; scalar or array
    model    : "ddf"    -> fitted power law (extrapolates; recommended)
               "interp" -> log-log interpolation between the 5 anchors,
                           clamped to [1, 24] h
    stat     : "median" | "q5" | "q95"  (GEV-fit ensemble percentile)
    **kw     : passed to cell_weights (method="nearest"/"idw", k, power)
    """
    idx, w, _ = cell_weights(lat, lon, **kw)
    anchors = POS[stat][:, idx] @ w                      # (5,) depths at this point
    D = np.asarray(D_h, float)
    if model == "ddf":
        a, b = fit_ddf(DURATIONS_H, anchors, fit_range)
        out = ddf_depth(a, b, D)
    elif model == "interp":
        Dc = np.clip(D, DURATIONS_H[0], DURATIONS_H[-1])
        out = np.exp(np.interp(np.log(Dc), np.log(DURATIONS_H), np.log(anchors)))
    else:
        raise ValueError(f"model must be 'ddf' or 'interp', got {model!r}")
    return out if np.ndim(out) else float(out)

def rain_intensity_mmh(lat, lon, D_h, **kw):
    """10-year rainfall INTENSITY (mm/h) = depth(D) / D."""
    return rain_depth_mm(lat, lon, D_h, **kw) / np.asarray(D_h, float)

# --- worked example ---------------------------------------------------
for name, (la, lo) in {"Geneva": GVA, "Valencia": (39.47, -0.38),
                       "Ahrweiler": (50.54, 7.11), "Bergen (NO)": (60.39, 5.32)}.items():
    d2  = rain_depth_mm(la, lo, 2.0)
    d12 = rain_depth_mm(la, lo, 12.0)
    print(f"{name:<12} 10-y level:  2 h {d2:5.1f} mm ({d2/2:5.1f} mm/h)"
          f"   |  12 h {d12:5.1f} mm ({d12/12:4.1f} mm/h)")
''')

# ------------------------------------------------------------------ step 4
md(r"""## 4 — Validation against the project's own DDF file

`IDF/IDF_loglogParameters.txt` holds a pre-computed `(a, b)` pair per grid cell and
is what the operational pipeline reads (`pipeline._load_ddf`). Two checks:

1. Do the `(a, b)` in the file match a fit re-done here from the Poschlod files?
2. How far does the power law sit from each published anchor?
""")
code(r'''ddf_file = IDF / "IDF_loglogParameters.txt"
if not ddf_file.exists():
    print("IDF/IDF_loglogParameters.txt not present - skipping the cross-check")
else:
    raw = np.genfromtxt(ddf_file, skip_header=1, delimiter="\t")   # lat lon a b 1h 24h
    raw = raw[~np.isnan(raw).any(axis=1)]
    f_lat, f_lon, f_a, f_b, f_c1, f_c24 = raw.T
    # align the file's cells to ours (they are the same grid)
    chord, fidx = TREE.query(_xyz(f_lat, f_lon), k=1)
    print(f"file rows {len(raw)} | our cells {N} | "
          f"max match distance {_chord_to_km(chord).max()*1000:.1f} m")

    rep = pd.DataFrame({
        "file a": [np.median(f_a)], "our a": [np.median(A_GRID[fidx])],
        "file b": [np.median(f_b)], "our b": [np.median(B_GRID[fidx])],
    }, index=["median"]).round(4)
    print("\n(1) parameters, file vs re-fit here:"); print(rep.to_string())
    print(f"    max |a_file - a_ours| = {np.abs(f_a - A_GRID[fidx]).max():.4f}"
          f" | max |b_file - b_ours| = {np.abs(f_b - B_GRID[fidx]).max():.4f}")

    print("\n(2) DDF power law vs each published Poschlod anchor (ratio fit/Poschlod):")
    for i, D in enumerate(DURATIONS_H):
        r = ddf_depth(f_a, f_b, D) / POS["median"][i, fidx]
        flag = "  <- 1 h anchor deliberately excluded from the fit" if D == 1 else ""
        print(f"    D = {D:4.0f} h   median {np.median(r):5.3f}"
              f"   (p5 {np.percentile(r,5):.3f}, p95 {np.percentile(r,95):.3f}){flag}")

    # The file carries two extra columns, headed "1h_10ans" / "24h_10ans".
    # NEITHER is reproducible from (a, b), and neither equals the Poschlod field
    # it is named after -- so their provenance is unknown. Nothing reads them
    # (pipeline._load_ddf takes only columns a and b), but do not reuse them.
    print("\n(3) the file's two trailing columns - both UNEXPLAINED:")
    for D, col, name in [(1.0, f_c1, "1h_10ans"), (24.0, f_c24, "24h_10ans")]:
        r_fit = ddf_depth(f_a, f_b, D) / col
        r_pos = col / POS["median"][int(np.where(DURATIONS_H == D)[0][0]), fidx]
        print(f"    '{name:<9}'  fit(D={D:g}h) / column = {np.median(r_fit):5.3f}"
              f"   |  column / Poschlod {D:g}h = {np.median(r_pos):5.2f}"
              f"   -> neither is 1.0")
    print("    (Row 0 happens to match at 24 h, which is a coincidence - the")
    print("     medians above are over all 78 400 cells.) Only a and b are used,")
    print("     and only a and b should be trusted.")
''')

md(r"""**Reading the output.** The re-fit reproduces the file's `(a, b)` to within
rounding, which confirms both the formula
`depth = 10**(a*log10(D) + b)` and the 3–24 h fitting window. The power law then
lands within a few per cent of the published 3, 6, 12 and 24 h levels and sits
~40 % **above** the 1 h level — the intended de-biasing, given Poschlod report the
1 h field as their weakest (−16.3 % mean bias, worse over the Alps).

The one loose end is the file's **two trailing columns** (`1h_10ans`,
`24h_10ans`): neither can be reproduced from `(a, b)`, and neither matches the
Poschlod field it is named after. `pipeline._load_ddf` reads only columns `a` and
`b`, so nothing downstream is affected — but those two columns should be
re-derived before anyone relies on them.
""")

# ------------------------------------------------------------------ step 5
md("## 5 — The DDF curve at a point")
code(r'''import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.ticker import ScalarFormatter

CMAP_RAIN = LinearSegmentedColormap.from_list("rain_blue", BLUE_RAMP)

def _tidy(ax):
    ax.grid(True, which="both", color=GRIDC, lw=0.6, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"): ax.spines[s].set_visible(False)
    for s in ("left", "bottom"): ax.spines[s].set_color(GRIDC)
    ax.tick_params(colors=INK2, labelsize=9, length=3)

def plot_ddf(lat, lon, title=None, area_km2=None, ax=None, fit_range=DDF_FIT_RANGE):
    """Depth and intensity vs duration at one point.

    Two panels rather than one plot with two y-axes: depth (mm) and intensity
    (mm/h) are different measures, and a twin axis would let the reader compare
    two arbitrary scales.
    """
    lv = point_levels(lat, lon)
    a, b = fit_ddf(DURATIONS_H, lv.depth_median_mm.to_numpy(), fit_range)
    Dg = np.logspace(np.log10(0.5), np.log10(48.0), 200)
    fit = ddf_depth(a, b, Dg)
    arf_v = arf(area_km2, Dg) if area_km2 else None

    axes = ax if ax is not None else plt.subplots(1, 2, figsize=(11, 4.1))[1]
    for k, (axx, ylab, div) in enumerate(
            [(axes[0], "10-y depth  (mm)", 1.0), (axes[1], "10-y intensity  (mm h⁻¹)", Dg)]):
        axx.fill_between(DURATIONS_H,
                         lv.depth_q5_mm / (1 if k == 0 else DURATIONS_H),
                         lv.depth_q95_mm / (1 if k == 0 else DURATIONS_H),
                         color=SERIES[0], alpha=0.16, lw=0, zorder=1,
                         label="Poschlod q5–q95 (GEV-fit spread)")
        axx.plot(DURATIONS_H, lv.depth_median_mm / (1 if k == 0 else DURATIONS_H),
                 "o-", color=SERIES[0], lw=2, ms=8, zorder=3,
                 label="Poschlod 10-y median (5 anchors)")
        axx.plot(Dg, fit / div, "--", color=SERIES[1], lw=2, zorder=4,
                 label=f"DDF fit  $10^{{{b:.3f}}}\\,D^{{{a:.3f}}}$")
        if arf_v is not None:
            axx.plot(Dg, fit * arf_v / div, ":", color=SERIES[2], lw=2, zorder=4,
                     label=f"areal, A = {area_km2:g} km² (ARF)")
        axx.set_xscale("log"); axx.set_yscale("log")
        axx.set_xlabel("accumulation duration  D  (h)", color=INK2, fontsize=9)
        axx.set_ylabel(ylab, color=INK2, fontsize=9)
        axx.set_xticks([0.5, 1, 3, 6, 12, 24, 48])
        axx.set_xticklabels(["0.5", "1", "3", "6", "12", "24", "48"])
        # log axes default to 10^n labels; rainfall depths read better as numbers
        axx.yaxis.set_major_formatter(ScalarFormatter())
        axx.yaxis.set_minor_formatter(ScalarFormatter())
        axx.ticklabel_format(axis="y", style="plain")
        axx.axvspan(fit_range[0], fit_range[1], color=SERIES[1], alpha=0.05, zorder=0)
        _tidy(axx)
    if ax is None:
        ttl = title or f"10-year rainfall at {lat:.3f}°N, {lon:.3f}°E"
        fig = axes[0].figure
        # one legend for both panels, below them: 3-4 entries would otherwise sit
        # on top of the data in the left panel
        h, l = axes[0].get_legend_handles_labels()
        fig.legend(h, l, frameon=False, fontsize=9, labelcolor=INK2,
                   loc="upper center", bbox_to_anchor=(0.5, 0.03),
                   ncol=len(l), handlelength=2.2, columnspacing=1.6)
        fig.suptitle(ttl + f"   ·   nearest cell {lv.attrs['nearest_km']:.1f} km"
                     f"   ·   shaded band = DDF fit window",
                     color=INK, fontsize=11, y=1.02)
        fig.tight_layout()
    else:
        axes[0].legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper left")
    return a, b

# ARF needs defining before the plot can show the areal curve -> see section 7;
# a thin forward declaration keeps this cell self-contained.
_AW, _AA, _AB, _AV = ARF_SETS[ARF_SET]
def _arf_raw(A, D):
    A = np.maximum(np.asarray(A, float), 0.0); D = np.maximum(np.asarray(D, float), 0.05)
    return (1.0 + _AW * A**_AA / D**_AB) ** (-_AV/_AB)
def arf(A, D):
    """ARF normalised to one grid cell: A <= 156 km2 -> 1.0. See section 7."""
    out = np.minimum(_arf_raw(A, D) / _arf_raw(ARF_REF_AREA_KM2, D), 1.0)
    return out if np.ndim(out) else float(out)

plot_ddf(*GVA, title="10-year rainfall near Geneva", area_km2=2000)
plt.show()
''')

# ------------------------------------------------------------------ step 6
md("## 6 — The whole field: a map at any duration")
code(r'''# The 78 400 cells are a 280 x 280 rotated-pole mesh in row-major order (verified:
# neighbouring cells are <= 0.25 deg apart along a row, <= 0.114 deg down a column),
# so the field can be drawn as a gapless CURVILINEAR pcolormesh instead of a
# scatter of dots -- a scatter leaves white gaps between the fanned-out northern
# rows, which reads as a moire pattern rather than as data.
GRID_SHAPE = (280, 280)
_ok = POS["lat"].size == GRID_SHAPE[0] * GRID_SHAPE[1]
LAT2D = POS["lat"].reshape(GRID_SHAPE) if _ok else None
LON2D = POS["lon"].reshape(GRID_SHAPE) if _ok else None

def grid_depth(D_h):
    """10-y depth (mm) for every grid cell at duration D_h, from the per-cell DDF fit."""
    return ddf_depth(A_GRID, B_GRID, D_h)

def plot_map(D_h=6.0, mark=None, vmin_pct=1.0, vmax_pct=99.0):
    """Choropleth of the 10-y level over Europe. Sequential single hue (blue),
    light -> dark = low -> high, which is what a magnitude encoding requires."""
    z = grid_depth(D_h)
    norm = Normalize(np.percentile(z, vmin_pct), np.percentile(z, vmax_pct))
    fig, ax = plt.subplots(figsize=(9.5, 7.6))
    if LAT2D is not None:
        sc = ax.pcolormesh(LON2D, LAT2D, z.reshape(GRID_SHAPE),
                           cmap=CMAP_RAIN, norm=norm, shading="nearest", rasterized=True)
    else:                                    # fallback if the reshape ever breaks
        sc = ax.scatter(POS["lon"], POS["lat"], c=z, s=2.5, marker="s",
                        cmap=CMAP_RAIN, norm=norm, linewidths=0)
    if mark:
        ax.plot(mark[1], mark[0], "o", ms=10, mfc="none", mec=SERIES[1], mew=2.4, zorder=5)
        ax.annotate(f"{rain_depth_mm(*mark, D_h):.0f} mm", (mark[1], mark[0]),
                    xytext=(12, 10), textcoords="offset points", color=SERIES[1],
                    fontsize=10, fontweight="bold", va="center", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=SERIES[1], lw=1.1))
    cb = fig.colorbar(sc, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label(f"10-year rainfall depth over {D_h:g} h  (mm)", color=INK2, fontsize=9)
    cb.outline.set_visible(False); cb.ax.tick_params(colors=INK2, labelsize=9)
    ax.set_xlabel("longitude (°E)", color=INK2, fontsize=9)
    ax.set_ylabel("latitude (°N)", color=INK2, fontsize=9)
    ax.set_title(f"Poschlod et al. (2021) 10-year return level · D = {D_h:g} h"
                 f" · {N} EURO-CORDEX cells", color=INK, fontsize=11)
    ax.set_aspect(1/np.cos(np.radians(50)))       # crude but honest at this latitude
    _tidy(ax); ax.grid(False)
    plt.tight_layout()

plot_map(6.0, mark=GVA)
plt.show()
''')

md(r"""The Mediterranean coasts (Liguria, Cévennes, Dalmatia, Valencia), the Alpine
south side and the Norwegian west coast carry the highest sub-daily extremes,
while the continental interior and Iberian plateau are lowest — the expected
pattern, and a useful smell test that the point query is wired up correctly.
""")

# ------------------------------------------------------------------ step 7
md(r"""## 7 — From a grid cell to a catchment: the areal reduction factor

A Poschlod value is a **~156 km² cell mean**, and a catchment of area `A` never
receives its 10-year point rainfall uniformly. De Michele, Kottegoda & Rosso
(2001, Eq. 14) — identical to Eq. 3 of Ceresetti et al. (2012), the
severity-diagram paper this project follows — gives

$$\mathrm{ARF}(A, D) = \Bigl[\,1 + \varpi\,\frac{A^{a}}{D^{b}}\Bigr]^{-v/b}$$

Two traps, both of which this project had fallen into:

1. **`a` is the area exponent, not `z`.** Both papers *also* quote the
   dynamic-scaling exponent `z = a/b`. De Michele's UK/NERC row reads
   `z = 0.70, b = 0.40`, so `a = z·b = 0.28` — an ARF that barely decays with
   area (0.93 at 5000 km² / 12 h). The NERC Flood Studies Report tables only pair
   *large* areas with *long* durations, so that fit is pure extrapolation at
   1–12 h over 10²–10⁴ km². The **Cévennes** fit (Ceresetti et al. 2012 Table 1)
   was made on `A ≥ 50 km²` and `D ≥ 2 h` — exactly these scales — and gives
   **0.53** there. That is the default here.
2. **The reference is a cell, not a point.** Since the "point" value already
   averages ~156 km², the ARF is normalised as
   `ARF(A,D) / ARF(156 km², D)`, following De Michele's Eq. 2. A catchment the
   size of one cell then correctly gets `ARF = 1`.
""")
code(r'''def areal_depth_mm(lat, lon, D_h, area_km2, **kw):
    """10-y rainfall depth (mm) averaged over a catchment of `area_km2`."""
    return rain_depth_mm(lat, lon, D_h, **kw) * arf(area_km2, D_h)

# How much the parameter set matters, at this project's scales:
areas = [156, 500, 1000, 2000, 5000, 10000, 50000]
tab = {}
for name in ARF_SETS:
    _AW, _AA, _AB, _AV = ARF_SETS[name]
    tab[name] = [arf(A, 12.0) for A in areas]
_AW, _AA, _AB, _AV = ARF_SETS[ARF_SET]          # restore the active set
print("ARF at D = 12 h, normalised to one 156 km² cell:")
print(pd.DataFrame(tab, index=pd.Index(areas, name="A km²")).round(3).to_string())

print(f"\nGeneva, D = 12 h, active set '{ARF_SET}':")
for A in (156, 1000, 5000):
    print(f"  A = {A:>5d} km²   point {rain_depth_mm(*GVA, 12.0):.1f} mm"
          f"   ->  areal {areal_depth_mm(*GVA, 12.0, A):.1f} mm"
          f"   (ARF {arf(A, 12.0):.3f})")
''')

md(r"""**Why this matters for the warning.** The severity of an observed event is
`observed areal rain / areal 10-y level`. Halving the areal 10-y level for a
large basin **doubles** the computed severity — an event that scored "below the
10-year level" under the UK/NERC parameters can score as a 30-year event under
the Cévennes ones. That is the correction applied in `operational/config.py`
(`ARF_SET = "cevennes_flat"`), together with feeding the ARF the basin's own
`SUB_AREA` rather than its upstream drainage `UP_AREA`.

**Caveat, stated plainly:** the Cévennes fit is a Mediterranean-convective
climate and was fitted over a 160 × 200 km window, so applying it pan-Europe is
itself an extrapolation — just a far smaller one than using an area exponent of
0.28. Regionalising the ARF is the proper next step.
""")

# ------------------------------------------------------------------ step 8
md(r"""## 8 — Return periods other than 10 years — *extrapolation, not Poschlod*

Poschlod publishes **only** the 10-year level. The project's frontend nonetheless
speaks of ~30-y and ≥100-y, using the growth curve from
`operational/config.py::RP_ANCHORS`, anchored on ratios to the 10-y level
(Geneva-like: 30 y ≈ 1.2×, 100 y ≈ 1.4×). Reproduced here so the assumption is
visible and tunable — it is a **regional assumption, not data**. Replace it with
local GEV parameters wherever you have them (Poschlod's own GEV `µ, σ, ξ` fields
are in their Supplement and would let this be derived properly).
""")
code(r'''RP_ANCHORS = [(1.0, 10), (1.2, 30), (1.4, 100)]     # (depth / 10-y depth, years)
_ra   = np.array([r for r, _ in RP_ANCHORS])
_rlnT = np.log([t for _, t in RP_ANCHORS])

def depth_for_return_period(lat, lon, D_h, T_years, **kw):
    """Depth (mm) at return period T, by INVERTING the RP_ANCHORS growth curve.
    Beyond Poschlod - the growth curve is a regional assumption."""
    ratio = np.interp(np.log(T_years), _rlnT, _ra)      # log-linear in T
    return rain_depth_mm(lat, lon, D_h, **kw) * ratio

def return_period_for_depth(lat, lon, D_h, depth_mm_obs, **kw):
    """Estimated return period (y) of an observed depth. Mirrors
    pipeline.est_return_period, including the log-linear extrapolation
    outside the anchors."""
    ref = rain_depth_mm(lat, lon, D_h, **kw)
    if ref <= 0 or depth_mm_obs <= 0: return 0.0
    ratio = depth_mm_obs / ref
    if ratio < _ra[0]:
        s = (_rlnT[1]-_rlnT[0])/(_ra[1]-_ra[0]);  lnT = _rlnT[0]+s*(ratio-_ra[0])
    elif ratio > _ra[-1]:
        s = (_rlnT[-1]-_rlnT[-2])/(_ra[-1]-_ra[-2]); lnT = _rlnT[-1]+s*(ratio-_ra[-1])
    else:
        lnT = np.interp(ratio, _ra, _rlnT)
    return float(np.exp(lnT))

print("Geneva, D = 6 h  (10-y level "
      f"{rain_depth_mm(*GVA, 6.0):.1f} mm):")
for T in (10, 30, 100):
    print(f"  T = {T:3d} y  ->  {depth_for_return_period(*GVA, 6.0, T):5.1f} mm"
          "   [growth-curve assumption]")
for obs in (45.0, 60.0, 70.0, 95.0):
    T = return_period_for_depth(*GVA, 6.0, obs)
    note = "  <-- ratio > 1.4: log-linear extrapolation, do not quote this number" \
           if obs / rain_depth_mm(*GVA, 6.0) > _ra[-1] else ""
    print(f"  observed {obs:5.1f} mm / 6 h at Geneva  ->  T ~ {T:7.0f} y{note}")
''')

# ------------------------------------------------------------------ step 9
md(r"""## 9 — Interactive explorer

Move the sliders to query any point, duration and catchment area. If `ipywidgets`
is unavailable the cell falls back to a static example, so the notebook still runs
top-to-bottom.
""")
code(r'''PRESETS = {
    "Geneva (CH)":        (46.20,  6.15),
    "Ahrweiler (DE)":     (50.54,  7.11),
    "Valencia (ES)":      (39.47, -0.38),
    "Genoa (IT)":         (44.41,  8.93),
    "Nimes / Cevennes":   (43.84,  4.36),
    "Bergen (NO)":        (60.39,  5.32),
    "Bucharest (RO)":     (44.43, 26.10),
    "London (UK)":        (51.51, -0.13),
}

def report(lat, lon, D_h, area_km2, method="idw"):
    lv = point_levels(lat, lon, method=method)
    a, b = fit_ddf(DURATIONS_H, lv.depth_median_mm.to_numpy())
    d  = ddf_depth(a, b, D_h)
    dl = ddf_depth(*fit_ddf(DURATIONS_H, lv.depth_q5_mm.to_numpy()), D_h)
    du = ddf_depth(*fit_ddf(DURATIONS_H, lv.depth_q95_mm.to_numpy()), D_h)
    f  = arf(area_km2, D_h)
    print(f"({lat:.3f}°N, {lon:.3f}°E)   nearest cell {lv.attrs['nearest_km']:.1f} km"
          f"   ·  DDF: depth = 10^{b:.3f} · D^{a:.3f}")
    print(f"  D = {D_h:g} h,  10-year POINT level")
    print(f"      depth      {d:7.1f} mm      (q5–q95  {dl:.1f} – {du:.1f})")
    print(f"      intensity  {d/D_h:7.2f} mm/h    (q5–q95  {dl/D_h:.2f} – {du/D_h:.2f})")
    print(f"  A = {area_km2:g} km²,  AREAL level  (ARF {f:.3f}, set '{ARF_SET}')")
    print(f"      depth      {d*f:7.1f} mm")
    print(f"      intensity  {d*f/D_h:7.2f} mm/h")
    plot_ddf(lat, lon, area_km2=area_km2)
    plt.show()

try:
    import ipywidgets as W
    from IPython.display import display

    w_lat = W.FloatSlider(value=GVA[0], min=30.5, max=64.5, step=0.05,
                          description="lat °N", readout_format=".2f", continuous_update=False)
    w_lon = W.FloatSlider(value=GVA[1], min=-24.5, max=37.5, step=0.05,
                          description="lon °E", readout_format=".2f", continuous_update=False)
    w_dur = W.FloatLogSlider(value=6.0, base=10, min=np.log10(0.5), max=np.log10(48),
                             step=0.02, description="D (h)", readout_format=".2f",
                             continuous_update=False)
    w_area = W.FloatLogSlider(value=1000.0, base=10, min=np.log10(1), max=np.log10(50000),
                              step=0.02, description="A (km²)", readout_format=".0f",
                              continuous_update=False)
    w_meth = W.ToggleButtons(options=[("nearest cell", "nearest"), ("IDW k=4", "idw")],
                             value="idw", description="space")
    w_city = W.Dropdown(options=["-- pick a city --"] + list(PRESETS), value="-- pick a city --",
                        description="preset")

    def _jump(ch):
        if ch["new"] in PRESETS:
            w_lat.value, w_lon.value = PRESETS[ch["new"]]
    w_city.observe(_jump, names="value")

    display(W.VBox([w_city, W.HBox([w_lat, w_lon]), W.HBox([w_dur, w_area]), w_meth,
                    W.interactive_output(report, {"lat": w_lat, "lon": w_lon, "D_h": w_dur,
                                                  "area_km2": w_area, "method": w_meth})]))
except ImportError:
    print("ipywidgets not installed - static example instead:\n")
    report(*GVA, 6.0, 1000.0)
''')

# ------------------------------------------------------------------ notes
md(r"""## 10 — Caveats, in order of how much they should worry you

1. **The values are model-derived, not observed.** They come from a GEV fit to
   1500 years of the CRCM5 large ensemble, not from rain gauges. Poschlod validate
   against national datasets: Spearman ρ > 0.76, and the model **underestimates**
   observed intensities over 60 % of the area at 1 h (77–83 % at 3–24 h), with a
   mean bias of −16.3 % (1 h) to +8.2 % (24 h).
2. **The 1 h field is the weak one**, especially over complex terrain such as the
   Alps. That is why `DDF_FIT_RANGE = (3, 24)` h excludes it and the power law
   sits ~40 % above it. If you need true 1 h design values, prefer national IDF
   data where it exists.
3. **A cell is not a point.** Every number is a ~156 km² mean; a true point value
   at the same return period would be higher. §7 handles the opposite direction
   (cell → larger catchment) but cannot recover the sub-cell peak.
4. **Only 10 years is data.** Everything in §8 is a growth-curve assumption.
5. **Extrapolating in duration.** The `"ddf"` model happily evaluates at 0.5 h or
   48 h; it was fitted on 3–24 h. Treat anything outside that window as indicative.
6. **The ARF parameters are regional.** See the caveat at the end of §7.

## Reusing these functions elsewhere

`rain_depth_mm(lat, lon, D_h)` / `rain_intensity_mmh(...)` / `areal_depth_mm(...)`
are self-contained: they need only `numpy`, `pandas`, `scipy` and the five cached
Poschlod files. The operational pipeline's equivalents are
`pipeline.ddf_depth_mm` (pre-fitted `(a, b)` from `IDF/IDF_loglogParameters.txt`,
faster for bulk queries) and `pipeline.threshold_point_mm` (the `"interp"` model).
""")

# ------------------------------------------------------------------ emit
nb = {
    "cells": [
        {"cell_type": t, "metadata": {}, "source": s.splitlines(keepends=True),
         **({"outputs": [], "execution_count": None} if t == "code" else {})}
        for t, s in cells
    ],
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.13"},
    },
    "nbformat": 4, "nbformat_minor": 5,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(nb, f, indent=1, ensure_ascii=False)
print(f"wrote {OUT} with {len(cells)} cells")
