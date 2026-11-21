import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, pctColor, pctClass, setUpdated, dateLong, $ } from "../ui.js";
import { renderHeat, renderTrend, renderDist, renderCauses, renderTrains } from "../charts.js";

const eva = location.pathname.split("/").pop();
const p0 = getParams();
const state = {
  period: p0.from ? "custom" : (p0.period || "30d"), from: p0.from || "", to: p0.to || "",
  kind: p0.kind === "arr" ? "arr" : "dep",
};

const rangeParams = () => (state.period === "custom" ? { from: state.from, to: state.to } : { period: state.period });

function kpiTile(label, value, sub, cls = "") {
  return `<div class="card kpi ${cls}"><div class="label">${esc(label)}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function renderKpis(d) {
  const k = state.kind === "dep" ? d.dep : d.arr;
  const word = state.kind === "dep" ? "Abfahrten" : "Ankünfte";
  $("#kpis").innerHTML = [
    kpiTile("Pünktlich ab", pct(d.dep.punctuality), `${num(d.dep.punctual)} von ${num(d.dep.measured)} Abfahrten`, pctClass(d.dep.punctuality)),
    kpiTile("Pünktlich an", pct(d.arr.punctuality), `${num(d.arr.punctual)} von ${num(d.arr.measured)} Ankünften`, pctClass(d.arr.punctuality)),
    kpiTile("Ø Verspätung", mins(k.avg_delay), `${word} · max. ${mins(k.max_delay)}`),
    kpiTile("Ausfälle", pct(k.cancel_rate), `${num(k.cancelled)} ${word}`, k.cancel_rate > 5 ? "poor" : k.cancel_rate > 2 ? "mid" : ""),
    kpiTile("Für Reisende", pct(k.traveller_punctuality), "Ausfälle zählen als unpünktlich", pctClass(k.traveller_punctuality)),
    kpiTile(word, num(k.planned), "im Zeitraum"),
  ].join("");
  const notes = [];
  if (k.replaced) notes.push(`<span><b>${num(k.replaced)}</b> durch Ersatzzug ersetzt</span>`);
  if (k.sev) notes.push(`<span><b>${num(k.sev)}</b> Ersatzverkehr (Bus)</span>`);
  if (k.extra) notes.push(`<span><b>${num(k.extra)}</b> Zusatzfahrten</span>`);
  if (k.platform_changed) notes.push(`<span><b>${num(k.platform_changed)}</b> Gleiswechsel</span>`);
  if (k.unknown) notes.push(`<span><b>${num(k.unknown)}</b> ohne Ist-Daten (nicht gewertet)</span>`);
  $("#notes").innerHTML = notes.join("");
}

function renderLines(rows) {
  const el = $("#lines");
  $("#lines-hint").textContent = state.kind === "dep"
    ? "Pünktlichkeit der Abfahrten je Linie und Ziel (mind. 5 Fahrten)."
    : "Pünktlichkeit der Ankünfte je Linie und Herkunft (mind. 5 Fahrten).";
  if (!rows.length) { el.innerHTML = `<li class="muted small">Zu wenige Fahrten im Zeitraum.</li>`; return; }
  el.innerHTML = rows.map((r) => {
    const p = r.m ? (100 * r.p) / r.m : null;
    const dir = r.direction ? `${state.kind === "dep" ? "→" : "←"} ${r.direction}` : "";
    return `<li><div class="top"><span><span class="train">${esc(r.line)}</span> ${esc(dir)}</span>
      <span class="n">${pct(p === null ? null : Math.round(p * 10) / 10)} · Ø ${mins(r.avg)} · ${num(r.n)} Fahrten${r.f ? ` · ${num(r.f)} Ausfälle` : ""}</span></div>
      <div class="bar"><span style="width:${p ?? 0}%;background:${pctColor(p)}"></span></div></li>`;
  }).join("");
}

function renderRoutes(routes) {
  $("#routes-section").classList.toggle("hidden", !routes.length);
  $("#routes").innerHTML = routes.map((r) => {
    const out = r.origin_eva === eva, k = r.kpis;
    return `<li><a href="/route/${r.id}?period=${encodeURIComponent(state.period === "custom" ? "30d" : state.period)}">
      <span class="name">${out ? "→" : "←"} ${esc(out ? r.dest_name : r.origin_name)}</span>
      <span class="badge" style="background:${pctColor(k.punctuality)};color:#111">${pct(k.punctuality)}</span>
      <span class="vals"><span><span class="lbl">Ø </span><b>${mins(k.avg_delay)}</b></span><span><span class="lbl">Ausfälle </span><b>${pct(k.cancel_rate)}</b></span><span><span class="lbl">Fahrten </span><b>${num(k.planned)}</b></span></span>
    </a></li>`;
  }).join("");
}

async function load() {
  try {
    const d = await api(`/api/stations/${encodeURIComponent(eva)}`, { ...rangeParams(), kind: state.kind });
    document.title = `${d.station.name} – Pendelmonitor`;
    $("#title").innerHTML = `${esc(d.station.name)}${d.watched_only ? ' <span class="flag">nur Bahnhof</span>' : ""}`;
    $("#period-label").textContent = `${d.label} · alle Züge an diesem Bahnhof`;
    renderKpis(d); renderLines(d.lines); renderHeat(d.heatmap); renderTrend(d); renderDist(d);
    renderCauses(d.causes, state.period); renderTrains(d.trains); renderRoutes(d.routes);
  } catch (e) { errorBox($("#kpis"), e); }
}

// ---------- controls
document.querySelectorAll("#kind button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.kind === state.kind));
$("#kind").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b || b.dataset.kind === state.kind) return;
  state.kind = b.dataset.kind;
  document.querySelectorAll("#kind button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  setParams({ kind: state.kind === "dep" ? null : state.kind });
  load();
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

$("#livechip").href = `/live?eva=${encodeURIComponent(eva)}`;
load();
api("/api/stations", { period: "7d" }).then((s) => {
  if (s.since) $("#note").textContent = `Bahnhofsdaten (alle Züge) liegen seit ${dateLong(s.since)} vor. Pünktlich = weniger als 6 min Verspätung.`;
}).catch(() => {});
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
