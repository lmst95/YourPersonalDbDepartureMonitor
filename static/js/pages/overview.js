import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, time, delayBadge, trainLabel,
  sparkline, pctColor, setUpdated, $ } from "../ui.js";
import { WD, f1, miniBox, sortTable } from "../cockpit.js";

const state = { period: getParams().period || "7d", view: getParams().view || "list", q: getParams().q || "", data: null };
let map = null, layers = null, fitPending = true;

// ---------- map styles (same OSM tiles, different CSS filter; choice stored per browser)
const MAP_STYLES = [["standard", "Standard"], ["gray", "Grau"], ["dark", "Dunkel"]];
function storedStyle() {
  try { const v = localStorage.getItem("mapStyle"); if (MAP_STYLES.some(([k]) => k === v)) return v; } catch { /* private mode */ }
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "standard";
}
function applyStyle(style) {
  const el = document.getElementById("map");
  MAP_STYLES.forEach(([k]) => el.classList.toggle(`map-${k}`, k === style));
  el.querySelectorAll(".map-style button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.style === style));
  try { localStorage.setItem("mapStyle", style); } catch { /* ignore */ }
}
function styleControl() {
  const Ctl = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const div = L.DomUtil.create("div", "map-style leaflet-bar");
      div.setAttribute("role", "group");
      div.setAttribute("aria-label", "Kartenstil");
      div.innerHTML = MAP_STYLES.map(([k, l]) => `<button type="button" data-style="${k}" aria-pressed="false" title="Kartenstil: ${l}">${l}</button>`).join("");
      L.DomEvent.disableClickPropagation(div);
      div.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) applyStyle(b.dataset.style); });
      return div;
    },
  });
  return new Ctl();
}

// ---------- search
const norm = (s) => (s || "").toLocaleLowerCase("de").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
function matches(c) {
  const words = norm(state.q).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = norm(c.dirs.map((d) => `${d.origin_name} ${d.dest_name}`).join(" "));
  return words.every((w) => hay.includes(w));
}

function corridors(routes) {
  // group A->B and B->A into one corridor, keep a stable order
  const out = [], byKey = new Map();
  for (const r of routes) {
    const key = [r.origin_eva, r.dest_eva].sort().join("|");
    if (!byKey.has(key)) { byKey.set(key, { key, dirs: [] }); out.push(byKey.get(key)); }
    byKey.get(key).dirs.push(r);
  }
  for (const c of out) {
    c.dirs.sort((a, b) => (a.origin_name || "").localeCompare(b.origin_name || ""));
    const a = c.dirs[0];
    c.title = c.dirs.length > 1 ? [a.origin_name, a.dest_name] : [a.origin_name, a.dest_name];
    c.both = c.dirs.length > 1;
    const ps = c.dirs.map((d) => d.kpis.punctuality).filter((p) => p !== null);
    c.worst = ps.length ? Math.min(...ps) : null;
  }
  return out;
}

function renderJump(all) {
  const sel = $("#route-jump");
  sel.innerHTML = `<option value="">Direkt zu Strecke …</option>` + all.map((c) =>
    `<optgroup label="${esc(c.title[0])} ${c.both ? "⇄" : "→"} ${esc(c.title[1])}">${c.dirs.map((d) =>
      `<option value="${d.id}">${esc(d.origin_name)} → ${esc(d.dest_name)} (${pct(d.kpis.punctuality)})</option>`).join("")}</optgroup>`).join("");
}

const routeMatches = (r) => matches({ dirs: [r] });
let table = null;

function renderList() {
  const el = $("#table");
  const all = corridors(state.data.routes);
  renderJump(all);
  const rows = state.data.routes.filter(routeMatches).map((r) => ({
    ...r, name: `${r.origin_name} → ${r.dest_name}`,
    box: { m: r.dist?.m || 0, punct: r.kpis.punctuality, p10: r.dist?.p10, p25: r.dist?.p25, p50: r.dist?.p50, p75: r.dist?.p75, p90: r.dist?.p90 },
  }));
  $("#route-count").textContent = state.q ? `${rows.length} von ${state.data.routes.length} Strecken` : "";
  if (!state.data.routes.length) {
    el.innerHTML = `<div class="empty">Noch keine Strecken konfiguriert. Auf dem Server mit <code>dbmon add</code> hinzufügen.</div>`;
    table = null;
    return;
  }
  const href = (r) => `/route/${r.id}?period=${encodeURIComponent(state.period)}`;
  const cfg = {
    rows, key: "punct", dir: "asc", empty: `Keine Strecke passt zu „${state.q}“.`, id: (r) => r.id,
    cols: [
      { key: "name", label: "Strecke", render: (r) => `<a class="rname" href="${href(r)}">${esc(r.origin_name)} <span class="muted">→</span> ${esc(r.dest_name)}</a>` },
      { key: "punct", label: "Pünktlich", num: true, asc: true, sort: (r) => r.kpis.punctuality, render: (r) => `<span class="pbar"><i><s style="width:${r.kpis.punctuality ?? 0}%;background:${pctColor(r.kpis.punctuality)}"></s></i><b>${pct(r.kpis.punctuality)}</b></span>` },
      { key: "p50", label: "Median", num: true, sort: (r) => r.dist?.p50, render: (r) => f1(r.dist?.p50) },
      { key: "p90", label: "90 %", num: true, tip: "9 von 10 Fahrten sind höchstens so spät (min)", sort: (r) => r.dist?.p90, render: (r) => `<span class="${r.dist?.p90 > 15 ? "t-bad" : r.dist?.p90 >= 6 ? "t-warn" : ""}">${f1(r.dist?.p90)}</span>` },
      { key: "box", label: "Verteilung", sort: (r) => r.dist?.p75, render: (r) => miniBox(r.box, 100) },
      { key: "avg", label: "Ø", num: true, sort: (r) => r.kpis.avg_delay, render: (r) => mins(r.kpis.avg_delay) },
      { key: "fail", label: "Ausfälle", num: true, sort: (r) => r.kpis.cancel_rate, render: (r) => pct(r.kpis.cancel_rate) },
      { key: "n", label: "Fahrten", num: true, sort: (r) => r.kpis.planned, render: (r) => num(r.kpis.planned) },
      { key: "trend", label: "30 Tage", tip: "Pünktlichkeit je Tag, sortiert nach dem letzten Tag", sort: (r) => (r.spark || []).filter((x) => x.p !== null).at(-1)?.p, render: (r) => sparkline(r.spark || [], { w: 90, h: 24 }) },
      { key: "worst", label: "Schlechteste Stunde", asc: true, sort: (r) => r.worst?.punctuality, render: (r) => (r.worst ? `${WD[(r.worst.wd)]} ${r.worst.h}–${r.worst.h + 1} Uhr <span class="muted">${Math.round(r.worst.punctuality)} %</span>` : "–") },
      { key: "next", label: "Nächste Züge", sort: (r) => r.next?.[0]?.planned, render: (r) => (r.next || []).map((t) => `<span class="dep"><time>${time(t.planned)}</time>${trainLabel(t)}${delayBadge(t)}</span>`).join("") || `<span class="muted">–</span>` },
    ],
  };
  if (table) table.set(rows); else table = sortTable(el, cfg);
}

function renderMap() {
  if (!window.L) return;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  if (!map) {
    map = L.map("map", { zoomControl: true, attributionControl: true, tap: true });
    map.addControl(styleControl());
    applyStyle(storedStyle());
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      // OSM's tile policy requires a Referer; the page itself uses "same-origin", so allow the origin here
      referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    layers = L.layerGroup().addTo(map);
  }
  layers.clearLayers();
  const bounds = [], allBounds = [];
  const stations = new Map();
  for (const c of corridors(state.data.routes)) {
    const a = c.dirs[0];
    if (a.origin_lat == null || a.dest_lat == null) continue;
    // follow the typical stop sequence if known (fall back to a straight line)
    const pts = (a.path && a.path.length >= 2) ? a.path.map((p) => [p.lat, p.lon]) : [[a.origin_lat, a.origin_lon], [a.dest_lat, a.dest_lon]];
    const hitSearch = matches(c);
    allBounds.push(...pts);
    if (hitSearch) bounds.push(...pts);
    const line = L.polyline(pts, { color: pctColor(c.worst), weight: hitSearch ? 6 : 4, opacity: hitSearch ? .9 : .25, lineCap: "round" });
    const links = c.dirs.map((d) => `<div><a href="/route/${d.id}">${esc(d.origin_name)} → ${esc(d.dest_name)}</a><br><span class="muted">${pct(d.kpis.punctuality)} pünktlich · Ø ${mins(d.kpis.avg_delay)}</span></div>`).join('<hr style="border:0;border-top:1px solid var(--border);margin:8px 0">');
    line.bindPopup(links);
    // wider invisible line = easier to tap on phones
    const hit = L.polyline(pts, { color: "#000", opacity: 0, weight: 24 }).bindPopup(links);
    layers.addLayer(line); layers.addLayer(hit);
    for (const d of c.dirs) {
      stations.set(d.origin_eva, { name: d.origin_name, lat: d.origin_lat, lon: d.origin_lon });
      stations.set(d.dest_eva, { name: d.dest_name, lat: d.dest_lat, lon: d.dest_lon });
    }
  }
  for (const [eva, s] of stations) {
    if (s.lat == null) continue;
    const m = L.circleMarker([s.lat, s.lon], { radius: 8, color: dark ? "#eceef2" : "#16191f", weight: 2, fillColor: dark ? "#181b21" : "#fff", fillOpacity: 1 });
    m.bindPopup(`<strong>${esc(s.name)}</strong><br><a href="/station/${encodeURIComponent(eva)}">Abfahrten und Strecken →</a>`);
    m.bindTooltip(esc(s.name), { direction: "top", offset: [0, -8] });
    layers.addLayer(m);
  }
  // only re-fit when the user changed something, not on the periodic refresh
  if (fitPending && (bounds.length || allBounds.length)) {
    map.fitBounds(bounds.length ? bounds : allBounds, { padding: [30, 30], maxZoom: 12 });
    fitPending = false;
  }
  setTimeout(() => map.invalidateSize(), 50);
}

async function load() {
  try {
    state.data = await api("/api/overview", { period: state.period });
    $("#period-label").textContent = `${state.data.label} · ${state.data.routes.length} Strecken`;
    setUpdated(state.data.last_update);
    renderList();
    if (state.view === "map" || matchMedia("(min-width: 1000px)").matches) renderMap();
  } catch (e) { errorBox($("#table"), e); }
}

function setView(v) {
  state.view = v;
  setParams({ view: v === "list" ? null : v });
  document.querySelectorAll(".view-toggle button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === v));
  $("#overview").classList.toggle("show-map", v === "map");
  if (v === "map" && state.data) renderMap();
}

periodChips($("#periods"), state.period, (p) => { state.period = p; setParams({ period: p === "7d" ? null : p }); load(); });
const qInput = $("#route-q");
qInput.value = state.q;
let qTimer;
qInput.addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => {
    state.q = qInput.value.trim();
    setParams({ q: state.q });
    fitPending = true;
    if (state.data) { renderList(); if (map) renderMap(); }
  }, 150);
});
$("#route-jump").addEventListener("change", (e) => {
  if (e.target.value) location.href = `/route/${encodeURIComponent(e.target.value)}?period=${encodeURIComponent(state.period)}`;
});
document.querySelector(".view-toggle").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView(b.dataset.view); });
setView(state.view);
load();
setInterval(() => { if (document.visibilityState === "visible") load(); }, 120000);
