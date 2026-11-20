import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, tripRow, pctClass, setUpdated, $ } from "../ui.js";
import { renderHeat, renderTrend, renderDist, renderCauses, renderTrains } from "../charts.js";

const routeId = Number(location.pathname.split("/").pop());
const p0 = getParams();
const state = {
  period: p0.from ? "custom" : (p0.period || "30d"), from: p0.from || "", to: p0.to || "",
  status: p0.status || "", q: "", offset: 0, chart: null,
};

function rangeParams() {
  return state.period === "custom" ? { from: state.from, to: state.to } : { period: state.period };
}

function kpiTile(label, value, sub, cls = "") {
  return `<div class="card kpi ${cls}"><div class="label">${esc(label)}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function renderHead(d) {
  const r = d.route;
  document.title = `${r.origin_name} → ${r.dest_name} – Pendelmonitor`;
  $("#title").innerHTML = `<a href="/station/${encodeURIComponent(r.origin_eva)}">${esc(r.origin_name)}</a><span class="arrow">→</span><a href="/station/${encodeURIComponent(r.dest_eva)}">${esc(r.dest_name)}</a>`;
  $("#period-label").textContent = d.label;
  const rev = $("#reverse");
  if (r.reverse_id) {
    rev.classList.remove("hidden");
    rev.href = `/route/${r.reverse_id}${location.search}`;
    const rp = d.reverse_kpis?.punctuality;
    rev.textContent = `⇄ Gegenrichtung${rp !== null && rp !== undefined ? ` (${pct(rp)})` : ""}`;
  }
}

function renderKpis(k) {
  $("#kpis").innerHTML = [
    kpiTile("Pünktlich ab", pct(k.punctuality), `${num(k.punctual)} von ${num(k.measured)} Fahrten`, pctClass(k.punctuality)),
    kpiTile("Pünktlich an", pct(k.arr_punctuality), k.arr_measured ? `Ankunft am Ziel · ${num(k.arr_measured)} Fahrten` : "noch keine Ankunftsdaten", pctClass(k.arr_punctuality)),
    kpiTile("Ø Verspätung", mins(k.avg_delay), `Ankunft Ø ${mins(k.arr_avg_delay)}`),
    kpiTile("Ausfälle", pct(k.cancel_rate), `${num(k.cancelled + k.partial)} Fahrten`, k.cancel_rate > 5 ? "poor" : k.cancel_rate > 2 ? "mid" : ""),
    kpiTile("Für Reisende", pct(k.traveller_punctuality), "Ausfälle zählen als unpünktlich", pctClass(k.traveller_punctuality)),
    kpiTile("Fahrten", num(k.planned), `max. Verspätung ${mins(k.max_delay)}`),
  ].join("");
  const notes = [];
  if (k.partial) notes.push(`<span><b>${num(k.partial)}</b> davon Ziel entfallen</span>`);
  if (k.replaced) notes.push(`<span><b>${num(k.replaced)}</b> durch Ersatzzug ersetzt</span>`);
  if (k.sev) notes.push(`<span><b>${num(k.sev)}</b> Ersatzverkehr (Bus)</span>`);
  if (k.extra) notes.push(`<span><b>${num(k.extra)}</b> Zusatzfahrten</span>`);
  if (k.platform_changed) notes.push(`<span><b>${num(k.platform_changed)}</b> Gleiswechsel</span>`);
  if (k.unknown) notes.push(`<span><b>${num(k.unknown)}</b> ohne Ist-Daten (nicht gewertet)</span>`);
  $("#notes").innerHTML = notes.join("");
}

async function loadLive() {
  try {
    const d = await api(`/api/routes/${routeId}/live`);
    const rows = [...d.past.map((t) => tripRow(t, { past: true })), `<li class="row now-sep">Jetzt</li>`, ...d.next.map((t) => tripRow(t))];
    $("#live").innerHTML = d.past.length || d.next.length ? rows.join("") : `<li class="empty">Keine Fahrten in den nächsten Stunden.</li>`;
  } catch (e) { errorBox($("#live"), e); }
}

const FILTERS = [["", "Alle"], ["late", "Verspätet"], ["failed", "Ausfälle"], ["replaced", "Ersatzzug"], ["sev", "Bus-Ersatz"], ["unknown", "Ohne Ist-Daten"]];

async function loadTrips(append = false) {
  if (!append) state.offset = 0;
  const list = $("#trips");
  try {
    const d = await api(`/api/routes/${routeId}/trips`, { ...rangeParams(), status: state.status, q: state.q, limit: 50, offset: state.offset });
    const html = d.trips.map((t) => tripRow(t, { showDate: true })).join("");
    if (append) list.insertAdjacentHTML("beforeend", html);
    else list.innerHTML = html || `<li class="empty">Keine Fahrten gefunden.</li>`;
    state.offset += d.trips.length;
    $("#trips-total").textContent = `${num(d.total)} Fahrten`;
    $("#more").classList.toggle("hidden", state.offset >= d.total);
  } catch (e) { errorBox(list, e); }
}

async function load() {
  try {
    const d = await api(`/api/routes/${routeId}`, rangeParams());
    renderHead(d); renderKpis(d.kpis); renderHeat(d.heatmap); renderTrend(d); renderDist(d); renderCauses(d.causes, state.period); renderTrains(d.trains);
  } catch (e) { errorBox($("#kpis"), e); }
  loadTrips();
}

// ---------- controls
const form = $("#daterange");
periodChips($("#periods"), state.period, (p) => {
  if (p === "custom") { form.classList.remove("hidden"); form.elements.from.focus(); return; }
  form.classList.add("hidden");
  state.period = p; setParams({ period: p === "30d" ? null : p, from: null, to: null }); load();
}, { custom: true });
if (state.period === "custom") { form.classList.remove("hidden"); form.elements.from.value = state.from; form.elements.to.value = state.to; }
form.addEventListener("submit", (e) => {
  e.preventDefault();
  state.period = "custom"; state.from = form.elements.from.value; state.to = form.elements.to.value;
  setParams({ period: null, from: state.from, to: state.to }); load();
});
$("#trip-filter").innerHTML = FILTERS.map(([v, l]) => `<button type="button" class="chip" data-v="${v}" aria-pressed="${v === state.status}">${l}</button>`).join("");
$("#trip-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $("#trip-filter").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  state.status = b.dataset.v; setParams({ status: state.status }); loadTrips();
});
let qTimer;
$("#trip-q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = e.target.value.trim(); loadTrips(); }, 300); });
$("#more").addEventListener("click", () => loadTrips(true));

load();
loadLive();
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
setInterval(() => { if (document.visibilityState === "visible") loadLive(); }, 60000);
