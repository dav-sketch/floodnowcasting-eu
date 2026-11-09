"""FloodNowcasting.eu 2.0 - precipitation sources (QPE) on the common domain grid.

Two sources, both resampled onto the SAME Web-Mercator domain grid that the
committed pixel->basin maps (state/pixel_cat_L*.npz) are built on, so no
shapefile / precompute is needed to add a source:

* OPERA NIMBUS 1-h accumulation (ACRR), EUMETNET, CC BY 4.0. Public S3 bucket,
  last 24 h, ODIM-HDF5, 2 km LAEA grid. Key time = END of the hour, e.g.
  OPERA@20261109T1000@0@ACRR.h5 = rain 09:00-10:00 UTC. A new file every 15 min.
* RainViewer 10-min radar tiles (colour -> dBZ -> mm/h), rate-limited.

Every field comes back as float arrays with NaN = NOT OBSERVED (outside radar
coverage, failed download, low quality) and 0 = observed dry. Keeping that
distinction is the whole point: a failed download must never look like "no rain".
"""
import io, re, time, threading, collections
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import numpy as np, requests
from PIL import Image

import config as C

UA = {"User-Agent": "FloodNowcasting.eu/2.0 (+https://dav-sketch.github.io/floodnowcasting-eu/)"}
_session = requests.Session(); _session.headers.update(UA)

# ======================================================================
# OPERA
# ======================================================================
_KEY_RE = re.compile(r"OPERA@(\d{8}T\d{4})@0@(\w+)\.h5$")

def _utc(t):  return datetime.fromtimestamp(int(t), timezone.utc)

def opera_list(now_unix=None, product=None):
    """{end_time_unix: object_key} of the OPERA composites currently in the 24-h
    cache (today + yesterday prefixes). Raises on network/HTTP failure."""
    product = product or C.OPERA_PRODUCT
    now = _utc(now_unix or time.time())
    out = {}
    for day in (now - timedelta(days=1), now):
        prefix = day.strftime("%Y/%m/%d/OPERA/COMP/")
        token = None
        while True:
            params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token: params["continuation-token"] = token
            r = _session.get(C.OPERA_S3, params=params, timeout=30); r.raise_for_status()
            for key in re.findall(r"<Key>([^<]+)</Key>", r.text):
                m = _KEY_RE.search(key)
                if m and m.group(2) == product:
                    t = datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
                    out[int(t.timestamp())] = key
            m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", r.text)
            if "<IsTruncated>true</IsTruncated>" in r.text and m:
                token = m.group(1)
            else:
                break
    return out

def _opera_read(content):
    """ODIM-HDF5 bytes -> (mm float32 2-D with NaN = not observed, where-attrs)."""
    import h5py
    with h5py.File(io.BytesIO(content), "r") as f:
        g = f["dataset1/data1"]
        w = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in g["what"].attrs.items()}
        raw = g["data"][:].astype(np.float64)
        nodata, undetect = w.get("nodata"), w.get("undetect")
        val = raw * float(w.get("gain", 1.0)) + float(w.get("offset", 0.0))
        val[raw == undetect] = 0.0                     # radar looked, no echo = dry
        val[raw == nodata] = np.nan                    # outside coverage
        if "quality1" in g and C.OPERA_QI_MIN > 0:
            qw = dict(g["quality1/what"].attrs)
            q = g["quality1/data"][:] * float(qw.get("gain", 1.0)) + float(qw.get("offset", 0.0))
            val[(q < C.OPERA_QI_MIN) & ~np.isnan(val)] = np.nan
        val[val < 0] = np.nan                          # any other negative flag
        where = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f["where"].attrs.items()}
    return val.astype(np.float32), where

def opera_fetch(key, tries=3):
    """Download + decode one composite. Returns (mm 2-D, where) or None on failure."""
    url = f"{C.OPERA_S3}/{key}"
    for k in range(tries):
        try:
            r = _session.get(url, timeout=60)
            if r.status_code == 200:
                return _opera_read(r.content)
            if r.status_code == 404:
                return None
        except (requests.RequestException, OSError):
            pass
        time.sleep(2 * (k + 1))
    return None

_opera_idx = {}
def opera_index(where, glon, glat):
    """Flat OPERA pixel index for every domain-grid pixel (-1 = outside OPERA).
    Built from the file's own ODIM `where` (projdef + corners), cached per grid."""
    sig = (where["projdef"], int(where["xsize"]), int(where["ysize"]),
           float(where["xscale"]), float(where["yscale"]), glon.shape)
    if sig not in _opera_idx:
        import pyproj
        t = pyproj.Transformer.from_crs("EPSG:4326", where["projdef"], always_xy=True)
        x0, y0 = t.transform(float(where["UL_lon"]), float(where["UL_lat"]))   # UL corner
        x, y = t.transform(glon, glat)
        col = np.floor((x - x0) / sig[3]).astype(np.int64)
        row = np.floor((y0 - y) / sig[4]).astype(np.int64)
        ok = (col >= 0) & (col < sig[1]) & (row >= 0) & (row < sig[2])
        _opera_idx[sig] = np.where(ok, row * sig[1] + col, -1).astype(np.int64)
    return _opera_idx[sig]

def opera_on_grid(field, where, glon, glat):
    """Resample an OPERA field (nearest neighbour) onto the domain grid (NaN outside)."""
    idx = opera_index(where, glon, glat)
    out = np.full(glon.shape, np.nan, np.float32)
    ok = idx >= 0
    out[ok] = field.ravel()[idx[ok]]
    return out

def opera_fetch_many(items, glon, glat):
    """items: {t: key}. Yields (t, grid_mm) as files are decoded (None if failed)."""
    with ThreadPoolExecutor(max_workers=max(1, C.OPERA_WORKERS)) as ex:
        futs = {t: ex.submit(opera_fetch, k) for t, k in items.items()}
        for t in sorted(futs):
            res = futs[t].result()
            yield t, (None if res is None else opera_on_grid(res[0], res[1], glon, glat))

# ======================================================================
# RainViewer (rate-limited; failures are NaN, never "dry")
# ======================================================================
class _RateLimiter:
    """Evenly paced: one call every 60/n s (shared across threads). Pacing rather
    than a 60-s budget also respects RainViewer's burst limit (300)."""
    def __init__(self, n):
        self.dt, self.next, self.lock = 60.0 / max(1, int(n)), 0.0, threading.Lock()
    def wait(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.dt
        if slot > now:
            time.sleep(slot - now)

_limiter = _RateLimiter(C.RV_MAX_TILES_PER_MIN)
_stats = collections.Counter()

def rv_stats():
    return dict(_stats)

def _get_tile(url, tries=4):
    """One RainViewer tile as RGBA uint8, or None if it could not be obtained.
    200 -> tile; 404 -> None (frame expired); 429 -> honour Retry-After, retry."""
    for k in range(tries):
        _limiter.wait()
        try:
            r = _session.get(url, timeout=30)
        except requests.RequestException:
            _stats["neterr"] += 1; time.sleep(1 + k); continue
        if r.status_code == 200:
            _stats["ok"] += 1
            return np.array(Image.open(io.BytesIO(r.content)).convert("RGBA"))
        _stats[f"http{r.status_code}"] += 1
        if r.status_code == 429:
            time.sleep(min(65.0, float(r.headers.get("retry-after", 30) or 30)))
            continue
        if r.status_code >= 500:
            time.sleep(2 + 2 * k); continue
        return None                                    # 403/404...: not retryable
    return None

def rainviewer_frames():
    j = _session.get(C.RV_JSON, timeout=30).json()
    return j["host"], j["radar"]["past"]

def rv_fetch_frame(host, path, xr, yr, decode, tiles=None):
    """Decode one RainViewer frame over the domain grid -> mm/h with NaN where a
    tile was not requested (`tiles` subset) or could not be downloaded.
    Returns (rate, n_failed_tiles)."""
    xr, yr = list(xr), list(yr)
    want = [(tx, ty) for tx in xr for ty in yr if tiles is None or (tx, ty) in tiles]
    urls = [f"{host}{path}/256/{C.TILE_Z}/{tx}/{ty}/2/0_0.png" for tx, ty in want]
    with ThreadPoolExecutor(max_workers=max(1, C.TILE_WORKERS)) as ex:
        got = dict(zip(want, ex.map(_get_tile, urls)))
    H, W = 256 * len(yr), 256 * len(xr)
    rate = np.full((H, W), np.nan, np.float32)
    failed = 0
    for (tx, ty), img in got.items():
        if img is None:
            failed += 1; continue
        i, j = (ty - yr[0]) * 256, (tx - xr[0]) * 256
        rate[i:i + 256, j:j + 256] = decode(img)
    return rate, failed

def tiles_needing_rv(xr, yr, need_mask):
    """Set of (tx, ty) tiles that contain at least one pixel of `need_mask`."""
    xr, yr = list(xr), list(yr)
    H, W = need_mask.shape
    blk = need_mask.reshape(H // 256, 256, W // 256, 256).any(axis=(1, 3))
    return {(xr[c], yr[r]) for r, c in zip(*np.nonzero(blk))}
