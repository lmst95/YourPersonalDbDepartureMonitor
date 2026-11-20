import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, pctColor, setUpdated, textMatches, $ } from "../ui.js";

const p0 = getParams();
const state = {
  tab: p0.tab === "stations" ? "stations" : "routes",
  period: p0.from ? "custom" : (p0.period || "30d"), from: p0.from || "", to: p0.to || "",
  sort: p0.sort || "punctuality", q: p0.q || "", data: null,
};

// [key, label, value(item), direction]
const SORTS = {
  routes: [
    ["punctuality", "Pünktlichkeit", (r) => r.kpis.punctuality ?? 999, 1],
    ["arr_punctuality", "Ankunft", (r) => r.kpis.arr_punctuality ?? 999, 1],
    ["avg_delay", "Ø Verspätung", (r) => r.kpis.avg_delay ?? -1, -1],
    ["cancel_rate", "Ausfälle", (r) => r.kpis.cancel_rate ?? -1, -1],
    ["planned", "Fahrten", (r) => r.kpis.planned ?? 0, -1],
  ],
  stations: [
    ["punctuality", "Pünktlichkeit", (s) => s.dep.punctuality ?? 999, 1],
    ["arr_punctuality", "Ankunft", (s) => s.arr.punctuality ?? 999, 1],
    ["avg_delay", "Ø Verspätung", (s) => s.dep.avg_delay ?? -1, -1],
    ["cancel_rate", "Ausfälle", (s) => s.dep.cancel_rate ?? -1, -1],
    ["planned", "Züge/Tag", (s) => s.per_day ?? 0, -1],
  ],
};

const qs = () => (state.period === "custom" ? `?from=${state.from}&to=${state.to}` : `?period=${state.period}`);
const items = () => (state.tab === "stations" ? state.data.stations : state.data.routes);
const label = (x) => (state.tab === "stations" ? x.name : `${x.origin_name} → ${x.dest_name}`);
const href = (x) => (state.tab === "stations" ? `/station/${encodeURIComponent(x.eva)}${qs()}` : `/route/${x.id}${qs()}`);
const kpis = (x) => (state.tab === "stations" ? x.dep : x.kpis);

function rowHtml(x) {
  const k = kpis(x);
  const arr = state.tab === "stations" ? x.arr.punctuality : x.kpis.arr_punctuality;
  const amount = state.tab === "stations" ? ["Züge/Tag", num(x.per_day)] : ["Fahrten", num(k.planned)];
  const flag = state.tab === "stations" && !x.routes ? ' <span class="flag">nur Bahnhof</span>' : "";
  return `<li><a href="${href(x)}">
    <span class="name">${esc(label(x))}${flag}</span>
    <span class="badge" style="background:${pctColor(k.punctuality)};color:#111;justify-self:end">${pct(k.punctuality)}</span>
    <span class="vals">
      <span><span class="lbl">Ankunft </span><b>${pct(arr)}</b></span>
      <span><span class="lbl">Ø </span><b>${mins(k.avg_delay)}</b></span>
      <span><span class="lbl">Ausfälle </span><b>${pct(k.cancel_rate)}</b></span>
      <span><span class="lbl">${amount[0]} </span><b>${amount[1]}</b></span>
    </span>
    <div class="bar" aria-hidden="true"><span style="width:${k.punctuality ?? 0}%;background:${pctColor(k.punctuality)}"></span></div>
  </a></li>`;
}

function render() {
  const sorts = SORTS[state.tab];
  const [, , key, dir] = sorts.find((s) => s[0] === state.sort) || sorts[0];
  const all = items();
  const rows = all.filter((x) => textMatches(label(x), state.q)).sort((a, b) => dir * (key(a) - key(b)));
  const noun = state.tab === "stations" ? "Bahnhöfen" : "Strecken";
  $("#route-count").textContent = state.q ? `${rows.length} von ${all.length} ${noun}` : "";
  const sorted = [...all].sort((a, b) => label(a).localeCompare(label(b), "de"));
  $("#route-jump").innerHTML = `<option value="">Direkt zu ${state.tab === "stations" ? "Bahnhof" : "Strecke"} …</option>` +
    sorted.map((x) => `<option value="${esc(href(x))}">${esc(label(x))} (${pct(kpis(x).punctuality)})</option>`).join("");
  const head = `<li class="head" aria-hidden="true"><span>${state.tab === "stations" ? "Bahnhof" : "Strecke"}</span><span>Pünktlich ab</span><span>Pünktlich an</span><span>Ø Verspätung</span><span>Ausfälle</span><span>${state.tab === "stations" ? "Züge/Tag" : "Fahrten"}</span><span></span></li>`;
  $("#cmp").innerHTML = rows.length ? head + rows.map(rowHtml).join("") : `<li class="empty">Nichts passt zu „${esc(state.q)}“.</li>`;
}

function renderControls() {
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tab === state.tab));
  $("#title").textContent = state.tab === "stations" ? "Vergleich aller Bahnhöfe" : "Vergleich aller Strecken";
  $("#route-q").placeholder = state.tab === "stations" ? "Bahnhof suchen" : "Strecke oder Bahnhof suchen";
  $("#sort").innerHTML = SORTS[state.tab].map(([v, l]) => `<button type="button" class="chip" data-v="${v}" aria-pressed="${v === state.sort}">${l}</button>`).join("");
  $("#explain").textContent = state.tab === "stations"
    ? "Alle Züge an den beobachteten Bahnhöfen (seit Beginn der OpenBoards-Daten). Pünktlich = weniger als 6 min Verspätung."
    : "Pünktlich = weniger als 6 min Verspätung. Ausfälle umfassen ausgefallene Fahrten und Fahrten, deren Halt am Ziel entfällt.";
}

async function load() {
  try {
    const range = state.period === "custom" ? { from: state.from, to: state.to } : { period: state.period };
    state.data = await api("/api/compare", { kind: state.tab, ...range });
    $("#period-label").textContent = state.data.label;
    render();
  } catch (e) { errorBox($("#cmp"), e); }
}

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
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b || b.dataset.tab === state.tab) return;
  state.tab = b.dataset.tab; state.data = null; state.sort = "punctuality";
  setParams({ tab: state.tab === "routes" ? null : state.tab, sort: null });
  renderControls(); $("#cmp").innerHTML = `<li class="empty">Lade…</li>`; load();
});
$("#sort").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $("#sort").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  state.sort = b.dataset.v; setParams({ sort: state.sort === "punctuality" ? null : state.sort }); if (state.data) render();
});
const qInput = $("#route-q");
qInput.value = state.q;
let qTimer;
qInput.addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { state.q = qInput.value.trim(); setParams({ q: state.q }); if (state.data) render(); }, 150);
});
$("#route-jump").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v.startsWith("/route/") || v.startsWith("/station/")) location.href = v;
});
renderControls();
load();
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
