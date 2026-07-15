// FloodNowcasting.eu 2.0 - static MapLibre frontend (multi-level LOD).
// Coarse HydroBASINS level when zoomed out, finer as you zoom in:
//   L6 = continental first view, L7 large (accumulation only),
//   L8 & L9 = the "relevant" fine levels that also carry SEVERITY.
// Geometry per level is static and LAZY-LOADED (only fetched when you first zoom
// into its band); the tiny per-level alerts.json (rewritten by the cron) is
// joined client-side. A "View" selector switches basin colouring between
// severity and rainfall accumulated over a chosen fixed window (2/4/8/12 h).
// Live radar from RainViewer. Zoom is capped so tiles never blank.

const REFRESH_MS = 120000;
const NONE_COLOR = "rgba(0,0,0,0)";
const geos = {};          // level -> base GeoJSON once loaded
const loaded = {};        // level -> bool
const alertsCache = {};   // level -> alerts object
let manifest = null, LV = [];
let ACC = { windows: [], default: 4, ramp: [], min: 0.3 };   // accumulation config (from manifest)
let SEV = new Set();      // levels that carry severity
const state = { mode: "severity", window: 4 };               // current View selection

(async function init() {
  manifest = await (await fetch("data/levels.json", { cache: "no-store" })).json();
  LV = manifest.levels.slice().sort((a, b) => a.minzoom - b.minzoom);
  SEV = new Set(manifest.severity_levels || []);
  if (manifest.acc) {
    ACC = { windows: manifest.acc.windows_h, default: manifest.acc.default_h,
            ramp: manifest.acc.ramp, min: manifest.acc.min_mm };
    state.window = ACC.default;
  }
  const [w, s, e, n] = manifest.domain;

  const map = new maplibregl.Map({
    container: "map",
    minZoom: manifest.map.minzoom, maxZoom: manifest.map.maxzoom,   // cap zoom → no blank tiles
    style: {
      version: 8,
      sources: { carto: {
        type: "raster", tileSize: 256, maxzoom: 19,
        tiles: ["https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
                "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"],
        attribution: "© OpenStreetMap contributors © CARTO" } },
      layers: [{ id: "carto", type: "raster", source: "carto" }],
    },
    bounds: [[w, s], [e, n]], fitBoundsOptions: { padding: 20 },
  });
  map.addControl(new maplibregl.NavigationControl(), "top-right");
  window._map = map;

  buildViewControl(map);

  map.on("load", () => {
    // add empty sources + zoom-banded layers for every level (geometry filled lazily).
    // fill-color / fill-opacity are read from per-feature props set by styleFeature().
    LV.forEach((L, i) => {
      const zmin = Math.max(L.minzoom, manifest.map.minzoom);
      const zmax = i + 1 < LV.length ? LV[i + 1].minzoom : 24;
      map.addSource("basins" + L.level, { type: "geojson", data: emptyFC() });
      map.addLayer({ id: "fill" + L.level, type: "fill", source: "basins" + L.level,
        minzoom: zmin, maxzoom: zmax,
        paint: { "fill-color": ["get", "color"], "fill-opacity": ["get", "op"] } });
      map.addLayer({ id: "line" + L.level, type: "line", source: "basins" + L.level,
        minzoom: zmin, maxzoom: zmax,
        paint: { "line-color": "#5b6b77", "line-width": 0.4, "line-opacity": 0.5 } });
      wirePopup(map, "fill" + L.level);
    });

    // alert pins (severity levels only) — coloured dot at each alerting basin's
    // centre, sized by level, visible at every zoom so L9 flash-floods show from afar.
    const rad = manifest.pins.radius;
    const radiusExpr = ["match", ["get", "level"]];
    Object.entries(rad).forEach(([lv, r]) => radiusExpr.push(Number(lv), r));
    radiusExpr.push(5);
    map.addSource("pins", { type: "geojson", data: emptyFC() });
    map.addLayer({
      id: "pins", type: "circle", source: "pins",
      paint: { "circle-radius": radiusExpr, "circle-color": ["get", "color"],
               "circle-stroke-color": "#333", "circle-stroke-width": 1, "circle-opacity": 0.9 } });
    wirePins(map);

    wireRadarToggle(map);
    map.on("zoomend", () => { ensureVisibleLevels(map); updateLevelBadge(map); });
    ensureVisibleLevels(map);
    updateLevelBadge(map);
    renderLegend();
    refresh(map);
    setInterval(() => refresh(map), REFRESH_MS);
  });
})();

const emptyFC = () => ({ type: "FeatureCollection", features: [] });

function activeLevel(z) {
  return LV.slice().reverse().find(L => z >= L.minzoom) || LV[0];
}
async function ensureVisibleLevels(map) {
  const L = activeLevel(map.getZoom());
  if (loaded[L.level]) return;
  loaded[L.level] = true;                       // guard against double-fetch
  try {
    const geo = await (await fetch("data/" + L.geometry, { cache: "no-store" })).json();
    geo.features.forEach(f => { f.properties.color = NONE_COLOR; f.properties.op = 0.03; });
    geos[L.level] = geo;
    applyAlerts(map, L.level);                  // colour with cached alerts if present
  } catch (e) { loaded[L.level] = false; }
}

async function refresh(map) {
  let radarMeta = null, total = 0, when = "?";
  for (const L of LV) {
    let data;
    try { data = await (await fetch("data/" + L.alerts, { cache: "no-store" })).json(); }
    catch (e) { continue; }
    alertsCache[L.level] = data.alerts || {};
    applyAlerts(map, L.level);
    if (SEV.has(L.level))                        // count only genuine severity alerts, not wet basins
      total += Object.values(alertsCache[L.level]).filter(a => a.label && a.label !== "none" && a.label !== "rain").length;
    if (!radarMeta && data.meta) radarMeta = data.meta;
    if (data.meta && data.meta.generated_unix)
      when = new Date(data.meta.generated_unix * 1000).toUTCString().replace("GMT", "UTC");
  }
  try {
    const pins = await (await fetch("data/" + manifest.pins.file, { cache: "no-store" })).json();
    map.getSource("pins").setData(pins);
  } catch (e) { /* no pins yet */ }
  if (radarMeta) updateRadar(map, radarMeta);
  setStatus(`${total} basin alert${total === 1 ? "" : "s"} · updated ${when}`);
}

function wirePins(map) {
  map.on("click", "pins", e => {
    const p = e.features[0].properties;
    new maplibregl.Popup().setLngLat(e.lngLat).setHTML(
      `<b>Alert · level ${p.level}</b><br>Severity: <b>${sevLabel(p.label)}</b>`
      + (p.T > 0 ? `<br>Est. return period: <b>${p.T} y</b>` : "")
      + `<br>Rain ${p.acc_mm} mm · 10-y thr ${p.thr_mm} mm<br>Basin ${p.HYBAS_ID}`
    ).addTo(map);
    map.flyTo({ center: e.lngLat, zoom: Math.max(map.getZoom(), p.level >= 9 ? 10 : p.level >= 8 ? 9 : 7) });
  });
  map.on("mouseenter", "pins", () => map.getCanvas().style.cursor = "pointer");
  map.on("mouseleave", "pins", () => map.getCanvas().style.cursor = "");
}

// ---- per-feature styling: severity vs rain-accumulation ---------------
function colorForAcc(mm) {
  let c = null;
  for (const [t, col] of ACC.ramp) if (mm >= t) c = col;   // ramp is ascending; take highest tier reached
  return c;
}
function styleFeature(f, level) {
  const p = f.properties;
  if (state.mode === "severity" && SEV.has(level)) {       // severity colouring (L8/L9 only)
    const lab = p.label || "none";
    if (lab !== "none") {
      p.color = p.sevcolor; p.op = lab === "rain" ? 0.5 : 0.62;
    } else { p.color = NONE_COLOR; p.op = 0.03; }
    return;
  }
  // rain-accumulation colouring: rain mode, OR an accumulation-only level (L6/L7),
  // OR a severity level while severity mode has no meaning here.
  const mm = p["acc_" + state.window] || 0;
  const col = colorForAcc(mm);
  if (col) { p.color = col; p.op = 0.55; }
  else { p.color = NONE_COLOR; p.op = 0.03; }
}

function applyAlerts(map, level) {
  const geo = geos[level], alerts = alertsCache[level];
  if (!geo || !alerts) return;
  geo.features.forEach(f => {
    const a = alerts[f.properties.HYBAS_ID];
    const acc = a && a.acc ? a.acc : {};
    ACC.windows.forEach(w => { f.properties["acc_" + w] = acc[String(w)] ?? 0; });
    f.properties.label = a && a.label ? a.label : "none";
    f.properties.sevcolor = a && a.color ? a.color : NONE_COLOR;
    f.properties.T = a && a.T != null ? a.T : 0;
    f.properties.acc_mm = a && a.acc_mm != null ? a.acc_mm : 0;
    f.properties.coverage = a && a.coverage != null ? a.coverage : null;
    styleFeature(f, level);
  });
  map.getSource("basins" + level).setData(geo);
}

// Recolour every loaded level in place (no refetch) — used when the View changes.
function restyle(map) {
  Object.keys(geos).forEach(k => {
    const level = Number(k), geo = geos[level];
    if (!geo) return;
    geo.features.forEach(f => styleFeature(f, level));
    if (map.getSource("basins" + level)) map.getSource("basins" + level).setData(geo);
  });
}

// ---- View selector (Severity | Rain 2h/4h/8h/12h) ---------------------
function buildViewControl(map) {
  const box = document.getElementById("view-opts");
  const opts = [{ v: "sev", txt: "Severity" }]
    .concat(ACC.windows.map(w => ({ v: "rain:" + w, txt: "Rain " + w + "h" })));
  box.innerHTML = opts.map(o =>
    `<label class="vopt"><input type="radio" name="viewmode" value="${o.v}"` +
    `${o.v === "sev" ? " checked" : ""}/> ${o.txt}</label>`).join("");
  box.querySelectorAll("input[name=viewmode]").forEach(el =>
    el.addEventListener("change", ev => {
      const v = ev.target.value;
      if (v === "sev") state.mode = "severity";
      else { state.mode = "rain"; state.window = Number(v.split(":")[1]); }
      restyle(map); renderLegend(); updateLevelBadge(map);
    }));
}

// ---- legend adapts to the current View --------------------------------
function renderLegend() {
  const el = document.getElementById("legend");
  if (state.mode === "severity") {
    el.innerHTML =
      `<div class="lg"><i style="background:#f03b20"></i> ≥ 100-y rainfall</div>` +
      `<div class="lg"><i style="background:#feb24c"></i> ~ 30-y</div>` +
      `<div class="lg"><i style="background:#ffeda0"></i> ~ 10-y</div>` +
      `<div class="lg"><i style="background:#c7e9b4"></i> watch (&lt; 10-y)</div>` +
      `<div class="lg"><i style="background:#4292c6"></i> rain (over response time)</div>`;
  } else {
    const rows = ACC.ramp.slice().reverse().map(([mm, col]) =>
      `<div class="lg"><i style="background:${col}"></i> ≥ ${mm} mm</div>`).join("");
    el.innerHTML = `<div class="lg lg-h">Rain accumulated over ${state.window} h</div>` + rows;
  }
}

function updateRadar(map, meta) {
  if (!meta.rainviewer_host) return;
  const url = `${meta.rainviewer_host}${meta.rainviewer_path}/256/{z}/{x}/{y}/2/1_1.png`;
  if (map.getLayer("radar")) { map.removeLayer("radar"); map.removeSource("radar"); }
  map.addSource("radar", { type: "raster", tileSize: 256, maxzoom: manifest.radar_maxzoom, tiles: [url] });
  map.addLayer({ id: "radar", type: "raster", source: "radar", paint: { "raster-opacity": 0.45 } },
               "fill" + LV[0].level);
  const on = document.getElementById("radar").checked;
  map.setLayoutProperty("radar", "visibility", on ? "visible" : "none");
}
function wireRadarToggle(map) {
  document.getElementById("radar").addEventListener("change", e => {
    if (map.getLayer("radar"))
      map.setLayoutProperty("radar", "visibility", e.target.checked ? "visible" : "none");
  });
}

function wirePopup(map, layer) {
  map.on("click", layer, e => {
    const p = e.features[0].properties;
    const accLine = ACC.windows.map(w => `${w}h: <b>${(+p["acc_" + w] || 0).toFixed(1)}</b>`).join(" · ");
    let html = `<b>Basin ${p.HYBAS_ID}</b><br>`;
    if (p.label && p.label !== "none" && p.T !== undefined) {   // severity level with a classification
      html += `Severity: <b>${sevLabel(p.label)}</b><br>`
        + (p.T > 0 ? `Est. return period: <b>${p.T} y</b><br>` : "")
        + `Rain (areal, over response time): ${p.acc_mm ?? 0} mm · 10-y thr ${p.thr_mm} mm<br>`;
    }
    html += `Rain accumulation — ${accLine} mm<br>`
      + `Warning lead ${p.t_lag_h} h · test ${p.D_test_h} h<br>`
      + `Area ${p.UP_AREA} km² · ARF ${p.arf}`
      + (p.coverage != null && p.coverage < 1 ? `<br><i>coverage ${p.coverage} (partial window)</i>` : "");
    new maplibregl.Popup().setLngLat(e.lngLat).setHTML(html).addTo(map);
  });
  map.on("mouseenter", layer, () => map.getCanvas().style.cursor = "pointer");
  map.on("mouseleave", layer, () => map.getCanvas().style.cursor = "");
}

function updateLevelBadge(map) {
  const L = activeLevel(map.getZoom());
  const el = document.getElementById("level");
  const kind = SEV.has(L.level) ? "severity" : "accumulation only";
  if (el) el.textContent = `basins: level ${L.level} (${kind})`;
  const note = document.getElementById("view-note");
  if (note) {
    note.textContent = state.mode === "severity" && !SEV.has(L.level)
      ? `Level ${L.level} has no severity — showing rain over ${state.window} h.`
      : "";
  }
}
function sevLabel(l) {
  return { ">=100y": "≥100-y", "~30y": "~30-y", "~10y": "~10-y", watch: "watch", rain: "rain", none: "none" }[l] || l;
}
function setStatus(s) { document.getElementById("status").textContent = s; }
