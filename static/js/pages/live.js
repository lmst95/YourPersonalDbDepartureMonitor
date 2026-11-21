// Live page: departure board in the look of the station displays (blue, white lines), platform display,
// and the next trains of the routes starting at the chosen station. The only page with auto refresh.
import { api, esc, errorBox, getParams, setParams, time, relTime, setUpdated, $ } from "../ui.js";

const p0 = getParams();
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const state = {
  eva: p0.eva || store.get("live.eva") || "", view: p0.view === "gleis" ? "gleis" : "board",
  mine: store.get("live.mine") === "1", gl: null, data: null,
};

const FAILED = new Set(["cancelled", "partial"]);
const trainText = (t) => (t.line && !/^\d+$/.test(t.line) && t.category !== "ICE" && t.category !== "IC"
  ? t.line.replace(/^([A-Z]+)(\d)/, "$1 $2") : `${t.category || ""} ${t.number || ""}`.trim());
const plat = (t) => t.platform || t.planned_platform || "";

function chip(t) {
  if (t.status === "cancelled") return `<span class="ca">fällt aus</span>`;
  if (t.status === "partial") return `<span class="ca">Ziel entfällt</span>`;
  if (t.status === "sev") return `<span class="ca">Bus</span>`;
  if (t.delay_min > 0) return `<span class="ca">ca. +${t.delay_min}</span>`;
  return "";
}
function note(t) {
  const n = [];
  if (t.status === "cancelled") n.push(`Fahrt fällt aus${t.replacement_label ? `. Ersatz: ${t.replacement_label}` : ""}`);
  if (t.status === "partial") n.push("Fällt auf einem Teil der Strecke aus");
  if (t.status === "sev") n.push("Ersatzverkehr mit Bus");
  if (t.status === "replaced") n.push(`Wird ersetzt durch ${t.replacement_label || "einen Ersatzzug"}`);
  if (!FAILED.has(t.status) && t.delay_min >= 6) n.push(`Verspätung ca. ${t.delay_min} Minuten`);
  if (t.platform_changed) n.push(`Heute von Gleis ${plat(t)}`);
  return n.join(" · ");
}
const lauf = (txt, size) => (txt ? `<div class="lauf${txt.length > size ? " long" : ""}"><span>${esc(txt)}</span></div>` : "");

function rowsShown() {
  const rows = state.data.board;
  return state.mine ? rows.filter((r) => r.mine) : rows;
}

function boardView(name) {
  const rows = rowsShown().slice(0, 10);
  const body = rows.length ? rows.map((t) => `<div class="dbb-row${FAILED.has(t.status) ? " x" : ""}">
      <div><div class="t">${time(t.planned)}</div>${chip(t)}</div>
      <div class="z">${esc(trainText(t))}</div>
      <div style="min-width:0">${t.via.length ? `<div class="via">${esc(t.via.join(" – "))}</div>` : ""}<div class="dest">${esc(t.direction || "")}</div>${lauf(note(t), 48)}</div>
      <div class="g${t.platform_changed ? " chg" : ""}"><span>${esc(plat(t))}</span></div></div>`).join("")
    : `<div class="dbb-empty">${state.mine ? "Zurzeit keine Züge Ihrer Strecken" : "Zurzeit keine Abfahrten"}</div>`;
  return `<div class="dbb"><div class="dbb-head"><div><div class="dbb-title">Abfahrt<small lang="en">Departure</small></div><div class="dbb-st">${esc(name)}</div></div><div class="dbb-clock" id="clock">${clock()}</div></div>
    <div class="dbb-cols"><span>Zeit</span><span>Zug</span><span>Über / Ziel</span><span>Gleis</span></div>${body}
    <div class="dbb-foot">${foot()}</div></div>`;
}

function platformView(name) {
  const rows = rowsShown();
  const gls = [...new Set(rows.map(plat).filter(Boolean))].sort((a, b) => a.localeCompare(b, "de", { numeric: true }));
  if (!gls.includes(state.gl)) state.gl = gls[0] || null;
  const list = rows.filter((t) => plat(t) === state.gl), x = list[0], n = list[1];
  const main = x ? `<div class="dbg"><div class="dbg-gl"><span>Gleis</span><b>${esc(state.gl)}</b></div><div style="min-width:0">
        <div class="dbg-tz"><span>${time(x.planned)}</span><span>${esc(trainText(x))}</span>${chip(x)}</div>
        ${x.via.length ? `<div class="dbg-via">über ${esc(x.via.join(" – "))}</div>` : ""}
        <div class="dbg-dest">${esc(x.direction || "")}</div>${lauf(note(x), 60)}</div></div>
      ${n ? `<div class="dbg-next"><span class="k">Folgezug</span><b>${time(n.planned)}</b><span>${esc(trainText(n))}</span><b>${esc(n.direction || "")}</b>${chip(n)}</div>` : ""}`
    : `<div class="dbb-empty">${gls.length ? "" : "Zurzeit keine Abfahrten"}</div>`;
  return `<div class="dbb"><div class="dbb-head"><div><div class="dbb-title">Gleis ${esc(state.gl || "–")}</div><div class="dbb-st">${esc(name)}</div></div><div class="dbb-clock" id="clock">${clock()}</div></div>${main}<div class="dbb-foot">${foot()}</div></div>
    ${gls.length ? `<div class="gl-pick" role="group" aria-label="Gleis wählen"><span class="hint">Gleis</span>${gls.map((g) => `<button type="button" class="chip chip-s" data-gl="${esc(g)}" aria-pressed="${g === state.gl}">${esc(g)}</button>`).join("")}</div>` : ""}`;
}

function sideView() {
  const routes = state.data.routes;
  if (!routes.length) return `<p class="muted small">Von hier beginnt keine Ihrer Strecken.</p>`;
  return routes.map((r) => `<a class="mini" href="/route/${r.id}"><h3>nach ${esc(r.dest_name)}<span>7 Tage: ${r.punctuality_7d === null ? "–" : `${Math.round(r.punctuality_7d)} %`} pünktlich</span></h3>
    ${r.next.length ? r.next.map((t) => `<div class="r"><b>${time(t.planned)}</b><span>${esc(trainText(t))}</span><span>${esc(r.dest_name)}</span>${chip(t)}</div>`).join("") : `<div class="none">Keine Fahrten in den nächsten Stunden</div>`}</a>`).join("");
}

const clock = () => new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin" }).format(new Date());
const foot = () => `<span>Stand ${clock()}</span>${state.data.last_update ? `<span>Daten ${esc(relTime(state.data.last_update))}</span>` : ""}<span>Aktualisiert sich jede Minute</span>`;

function render() {
  const d = state.data, st = d.stations.find((s) => s.eva === d.eva);
  const sel = $("#station");
  sel.innerHTML = d.stations.map((s) => `<option value="${esc(s.eva)}"${s.eva === d.eva ? " selected" : ""}>${esc(s.name)}</option>`).join("");
  const name = st ? st.name : "";
  document.title = `Live ${name} – Pendelmonitor`;
  $("#board").innerHTML = state.view === "board" ? boardView(name) : platformView(name);
  $("#routes").innerHTML = sideView();
}

async function load() {
  try {
    state.data = await api("/api/live", { eva: state.eva });
    if (!state.data.stations.length) { $("#board").innerHTML = `<div class="notice">Noch keine Strecken eingerichtet.</div>`; return; }
    state.eva = state.data.eva;
    setUpdated(state.data.last_update);
    render();
  } catch (e) { errorBox($("#board"), e); }
}

$("#station").addEventListener("change", (e) => {
  state.eva = e.target.value; store.set("live.eva", state.eva); setParams({ eva: state.eva }); state.gl = null; load();
});
$("#view").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]"); if (!b) return;
  $("#view").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  state.view = b.dataset.v; setParams({ view: state.view === "board" ? null : state.view });
  if (state.data) render();
});
$("#view").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === state.view));
$("#mine").checked = state.mine;
$("#mine").addEventListener("change", (e) => { state.mine = e.target.checked; store.set("live.mine", state.mine ? "1" : "0"); if (state.data) render(); });
$("#board").addEventListener("click", (e) => {
  const b = e.target.closest("[data-gl]"); if (!b) return;
  state.gl = b.dataset.gl; render();
});

load();
setInterval(() => { if (document.visibilityState === "visible") load(); }, 60000);
setInterval(() => { const c = document.getElementById("clock"); if (c) c.textContent = clock(); }, 15000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
