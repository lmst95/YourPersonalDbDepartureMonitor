import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, pctColor, sparkline, setUpdated,
  textMatches, dateLong, $ } from "../ui.js";

const p0 = getParams();
const state = { period: p0.period || "7d", q: p0.q || "", data: null };

function row(s) {
  const d = s.dep, a = s.arr;
  return `<li><a href="/station/${encodeURIComponent(s.eva)}?period=${encodeURIComponent(state.period)}">
    <span class="name">${esc(s.name)}${s.routes ? "" : ' <span class="flag">nur Bahnhof</span>'}</span>
    <span class="badge" style="background:${pctColor(d.punctuality)};color:#111;justify-self:end" title="Pünktlich ab">${pct(d.punctuality)}</span>
    <span class="vals">
      <span><span class="lbl">Ankunft </span><b>${pct(a.punctuality)}</b></span>
      <span><span class="lbl">Ø </span><b>${mins(d.avg_delay)}</b></span>
      <span><span class="lbl">Ausfälle </span><b>${pct(d.cancel_rate)}</b></span>
      <span><span class="lbl">Züge/Tag </span><b>${num(s.per_day)}</b></span>
    </span>
    <span class="spark-cell" aria-hidden="true">${sparkline(s.spark || [], { w: 110, h: 28 })}</span>
  </a></li>`;
}

function render() {
  const all = state.data.stations;
  const rows = all.filter((s) => textMatches(s.name, state.q));
  $("#count").textContent = state.q ? `${rows.length} von ${all.length} Bahnhöfen` : "";
  $("#jump").innerHTML = `<option value="">Direkt zu Bahnhof …</option>` + all
    .map((s) => `<option value="${esc(s.eva)}">${esc(s.name)} (${pct(s.dep.punctuality)})</option>`).join("");
  const head = `<li class="head" aria-hidden="true"><span>Bahnhof</span><span>Pünktlich ab</span><span>Pünktlich an</span><span>Ø Verspätung</span><span>Ausfälle</span><span>Züge/Tag</span><span>30 Tage</span></li>`;
  $("#list").innerHTML = !all.length
    ? `<li class="empty">Noch keine Bahnhöfe beobachtet.</li>`
    : rows.length ? head + rows.map(row).join("") : `<li class="empty">Kein Bahnhof passt zu „${esc(state.q)}“.</li>`;
}

async function load() {
  try {
    state.data = await api("/api/stations", { period: state.period });
    $("#period-label").textContent = `${state.data.label} · alle Züge an den beobachteten Bahnhöfen`;
    $("#note").textContent = state.data.since
      ? `Bahnhofsdaten (alle Züge) liegen seit ${dateLong(state.data.since)} vor. Ältere Daten enthalten nur die Züge der Strecken und fließen hier nicht ein. Pünktlich = weniger als 6 min Verspätung.`
      : "";
    render();
  } catch (e) { errorBox($("#list"), e); }
}

periodChips($("#periods"), state.period, (p) => { state.period = p; setParams({ period: p === "7d" ? null : p }); load(); });
const qInput = $("#q");
qInput.value = state.q;
let t;
qInput.addEventListener("input", () => {
  clearTimeout(t);
  t = setTimeout(() => { state.q = qInput.value.trim(); setParams({ q: state.q }); if (state.data) render(); }, 150);
});
$("#jump").addEventListener("change", (e) => {
  if (e.target.value) location.href = `/station/${encodeURIComponent(e.target.value)}?period=${encodeURIComponent(state.period)}`;
});
load();
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
