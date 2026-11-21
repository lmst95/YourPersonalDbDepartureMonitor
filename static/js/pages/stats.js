// Comparison: routes as matrix route × departure hour (click an hour to sort by it), small multiples with
// the daily profile of every route on the same scale, and a sortable KPI table. Stations: sortable table.
import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, pctColor, setUpdated, textMatches, $ } from "../ui.js";
import { f1, tint, chart, pill, miniBox, sortTable } from "../cockpit.js";

const p0 = getParams();
const state = {
  tab: p0.tab === "stations" ? "stations" : "routes",
  period: p0.from ? "custom" : (p0.period || "30d"), from: p0.from || "", to: p0.to || "",
  metric: ["punct", "p50", "p90"].includes(p0.metric) ? p0.metric : "punct", sortBy: "all", dir: 1,
  q: p0.q || "", data: null,
};
const range = () => (state.period === "custom" ? { from: state.from, to: state.to } : { period: state.period });
const qs = () => new URLSearchParams(range()).toString();
let table = null;

// value of one cell for the chosen metric; colour on the punctuality scale (minutes mapped onto it)
const val = (s) => (!s || s.m < 5 ? null : state.metric === "punct" ? s.punctuality : s[state.metric]);
const colour = (s) => (state.metric === "punct" ? pctColor(s.punctuality) : pctColor(100 - (state.metric === "p50" ? s.p50 * 4 : s.p90 * 1.6)));
const fmt = (v) => (v === null ? "" : state.metric === "punct" || v >= 10 ? Math.round(v) : (Math.round(v * 10) / 10).toLocaleString("de-DE"));
const better = () => (state.metric === "punct" ? -1 : 1); // worse first: low punctuality, high minutes

function routesShown() { return state.data.routes.filter((r) => textMatches(`${r.origin_name} ${r.dest_name}`, state.q)); }

function renderMatrix(rows) {
  const used = new Set(), byH = new Map();
  for (const r of rows) { const m = new Map(r.hours.map((h) => [h.h, h])); byH.set(r.id, m); for (const h of r.hours) if (h.m >= 5) used.add(h.h); }
  const H = [...used].sort((a, b) => a - b);
  const key = (r) => (state.sortBy === "all" ? val({ ...r.all, punctuality: r.all.punctuality }) : val(byH.get(r.id).get(state.sortBy)));
  const sorted = [...rows].sort((a, b) => {
    const x = key(a), y = key(b);
    if (x === null) return y === null ? 0 : 1; if (y === null) return -1;
    return (x - y) * -better() * state.dir;
  });
  let s = `<div class="mx" style="grid-template-columns:minmax(150px,240px) repeat(${H.length},minmax(26px,1fr)) 8px 52px"><span></span>`;
  for (const h of H) s += `<button type="button" class="h${state.sortBy === h ? " on" : ""}" data-s="${h}" aria-label="Nach ${h} Uhr sortieren">${h}</button>`;
  s += `<span></span><button type="button" class="h${state.sortBy === "all" ? " on" : ""}" data-s="all">Gesamt</button>`;
  for (const r of sorted) {
    s += `<a class="rn" href="/route/${r.id}?${qs()}" title="${esc(r.origin_name)} → ${esc(r.dest_name)}">${esc(r.origin_name)} → ${esc(r.dest_name)}</a>`;
    for (const h of H) {
      const c = byH.get(r.id).get(h), v = val(c);
      s += v === null ? `<span class="c none" title="${esc(`${r.origin_name} → ${r.dest_name}, ${h} Uhr: zu wenige Fahrten`)}"></span>`
        : `<span class="c" style="background:${tint(state.metric === "punct" ? c.punctuality : 100 - (state.metric === "p50" ? c.p50 * 4 : c.p90 * 1.6), 0.85)}" title="${esc(`${r.origin_name} → ${r.dest_name}, ${h}–${h + 1} Uhr: ${pct(c.punctuality)} pünktlich, Median ${f1(c.p50)} min, 90 % ${f1(c.p90)} min, ${c.n} Fahrten`)}">${fmt(v)}</span>`;
    }
    const t = r.all, tv = val(t);
    s += `<span></span><span class="c tot${tv === null ? " none" : ""}"${tv === null ? "" : ` style="background:${colour(t)}"`}>${fmt(tv)}</span>`;
  }
  $("#matrix").innerHTML = H.length ? `${s}</div>` : `<p class="muted">Keine Fahrten im Zeitraum.</p>`;
}

function ribbon(w, hours, yMax = 30) {
  const h = 74, pl = 2, pr = 2, pt = 4, pb = 13, iw = w - pl - pr, ih = h - pt - pb;
  const ok = hours.filter((g) => g.m >= 4 && g.p50 !== null);
  if (ok.length < 2) return `<svg width="${w}" height="${h}" aria-hidden="true"><text x="${w / 2}" y="${h / 2}" font-size="10" text-anchor="middle">zu wenige Daten</text></svg>`;
  const x = (hh) => pl + (hh / 23) * iw, y = (v) => pt + ih - ((Math.max(-1, Math.min(yMax, v)) + 1) / (yMax + 1)) * ih;
  const line = (k) => ok.map((g, i) => `${i ? "L" : "M"}${x(g.h).toFixed(1)},${y(g[k]).toFixed(1)}`).join("");
  const band = `${line("p75")}${[...ok].reverse().map((g) => `L${x(g.h).toFixed(1)},${y(g.p25).toFixed(1)}`).join("")}Z`;
  let s = `<svg width="${w}" height="${h}" aria-hidden="true"><line x1="${pl}" x2="${w - pr}" y1="${y(5.5)}" y2="${y(5.5)}" class="limit"/>`;
  s += `<path d="${band}" class="band"/><path d="${line("p90")}" class="p90l"/><path d="${line("p50")}" class="medl"/>`;
  for (const t of [6, 12, 18]) s += `<text x="${x(t)}" y="${h - 2}" font-size="9" text-anchor="middle">${t}</text>`;
  return `${s}</svg>`;
}
function renderCards(rows) {
  const sorted = [...rows].sort((a, b) => (a.all.punctuality ?? 999) - (b.all.punctuality ?? 999));
  $("#cards").innerHTML = sorted.map((r) => `<a class="sm" href="/route/${r.id}?${qs()}"><div class="top"><span class="nm" title="${esc(r.origin_name)} → ${esc(r.dest_name)}">${esc(r.origin_name)} → ${esc(r.dest_name)}</span><span class="pv" style="color:${pctColor(r.all.punctuality)}">${r.all.punctuality === null ? "–" : `${Math.round(r.all.punctuality)} %`}</span></div>
    <div class="chart" data-r="${r.id}"></div><div class="sub"><span>Median ${f1(r.all.p50)}</span><span>90 % ${f1(r.all.p90)}</span><span>Ausfälle ${pct(r.kpis.cancel_rate)}</span></div></a>`).join("");
  for (const r of sorted) chart($(`#cards [data-r="${r.id}"]`), (w) => ribbon(w, r.hours));
}

function renderTable() {
  const routes = state.tab === "routes";
  const rows = routes ? routesShown().map((r) => ({ ...r, name: `${r.origin_name} → ${r.dest_name}`, k: r.kpis, box: { ...r.all, punct: r.all.punctuality } }))
    : state.data.stations.filter((s) => textMatches(s.name, state.q)).map((s) => ({ ...s, k: s.dep }));
  const href = (r) => (routes ? `/route/${r.id}?${qs()}` : `/station/${encodeURIComponent(r.eva)}?${qs()}`);
  const cols = [
    { key: "name", label: routes ? "Strecke" : "Bahnhof", render: (r) => `<a class="rname" href="${href(r)}">${esc(r.name)}</a>${!routes && !r.routes ? ' <span class="flag">nur Bahnhof</span>' : ""}` },
    { key: "p", label: "Pünktlich ab", num: true, asc: true, sort: (r) => r.k.punctuality, render: (r) => pill(r.k.punctuality) },
    { key: "pa", label: "Pünktlich an", num: true, asc: true, sort: (r) => (routes ? r.k.arr_punctuality : r.arr.punctuality), render: (r) => pct(routes ? r.k.arr_punctuality : r.arr.punctuality) },
  ];
  if (routes) cols.push(
    { key: "p50", label: "Median", num: true, sort: (r) => r.all.p50, render: (r) => f1(r.all.p50) },
    { key: "p90", label: "90 %", num: true, sort: (r) => r.all.p90, render: (r) => f1(r.all.p90) },
    { key: "box", label: "Verteilung", sort: (r) => r.all.p75, render: (r) => miniBox(r.box, 100) },
  );
  cols.push(
    { key: "avg", label: "Ø", num: true, sort: (r) => r.k.avg_delay, render: (r) => mins(r.k.avg_delay) },
    { key: "f", label: "Ausfälle", num: true, sort: (r) => r.k.cancel_rate, render: (r) => pct(r.k.cancel_rate) },
    routes ? { key: "n", label: "Fahrten", num: true, sort: (r) => r.k.planned, render: (r) => num(r.k.planned) }
      : { key: "pd", label: "Züge/Tag", num: true, sort: (r) => r.per_day, render: (r) => num(r.per_day) },
  );
  table = null;
  table = sortTable($("#table"), { rows, cols, key: "p", dir: "asc", empty: `Nichts passt zu „${state.q}“.` });
  const total = routes ? state.data.routes.length : state.data.stations.length;
  $("#count").textContent = state.q ? `${rows.length} von ${total} ${routes ? "Strecken" : "Bahnhöfen"}` : "";
  $("#explain").textContent = routes
    ? "Pünktlich = weniger als 6 min Verspätung. Ausfälle umfassen ausgefallene Fahrten und Fahrten, deren Halt am Ziel entfällt."
    : "Alle Züge an den beobachteten Bahnhöfen (seit Beginn der OpenBoards-Daten). Pünktlich = weniger als 6 min Verspätung.";
}

function render() {
  const routes = state.tab === "routes";
  $("#matrix-box").classList.toggle("hidden", !routes);
  $("#cards-box").classList.toggle("hidden", !routes);
  $("#metric").classList.toggle("hidden", !routes);
  $("#title").textContent = routes ? "Vergleich aller Strecken" : "Vergleich aller Bahnhöfe";
  if (routes) { const rows = routesShown(); renderMatrix(rows); renderCards(rows); }
  renderTable();
}

async function load() {
  try {
    state.data = state.tab === "routes" ? await api("/api/compare/hourly", range()) : await api("/api/compare", { kind: "stations", ...range() });
    $("#period-label").textContent = state.data.label;
    render();
  } catch (e) { errorBox($("#table"), e); }
}

// ---------- controls
document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tab === state.tab));
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b || b.dataset.tab === state.tab) return;
  state.tab = b.dataset.tab; state.data = null;
  document.querySelectorAll("#tabs button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  setParams({ tab: state.tab === "routes" ? null : state.tab });
  $("#table").innerHTML = `<div class="skeleton" style="min-height:200px"></div>`; load();
});
$("#metric").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === state.metric));
$("#metric").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]"); if (!b) return;
  $("#metric").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  state.metric = b.dataset.v; setParams({ metric: state.metric === "punct" ? null : state.metric });
  if (state.data && state.tab === "routes") renderMatrix(routesShown());
});
$("#matrix").addEventListener("click", (e) => {
  const b = e.target.closest("button.h"); if (!b) return;
  const k = b.dataset.s === "all" ? "all" : Number(b.dataset.s);
  state.dir = state.sortBy === k ? -state.dir : 1; state.sortBy = k;
  renderMatrix(routesShown());
});
const form = $("#daterange");
periodChips($("#periods"), state.period, (p) => {
  if (p === "custom") { form.classList.remove("hidden"); form.elements.from.focus(); return; }
  form.classList.add("hidden"); state.period = p; setParams({ period: p === "30d" ? null : p, from: null, to: null }); load();
}, { custom: true });
if (state.period === "custom") { form.classList.remove("hidden"); form.elements.from.value = state.from; form.elements.to.value = state.to; }
form.addEventListener("submit", (e) => {
  e.preventDefault(); state.period = "custom"; state.from = form.elements.from.value; state.to = form.elements.to.value;
  setParams({ period: null, from: state.from, to: state.to }); load();
});
const qInput = $("#q");
qInput.value = state.q;
let t;
qInput.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { state.q = qInput.value.trim(); setParams({ q: state.q }); if (state.data) render(); }, 150); });

load();
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
