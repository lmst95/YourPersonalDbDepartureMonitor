import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, sparkline, setUpdated,
  textMatches, dateLong, $ } from "../ui.js";
import { pill, sortTable } from "../cockpit.js";

const p0 = getParams();
const state = { period: p0.period || "7d", q: p0.q || "", data: null };

function render() {
  const all = state.data.stations;
  const rows = all.filter((s) => textMatches(s.name, state.q));
  $("#count").textContent = state.q ? `${rows.length} von ${all.length} Bahnhöfen` : "";
  $("#jump").innerHTML = `<option value="">Direkt zu Bahnhof …</option>` + all
    .map((s) => `<option value="${esc(s.eva)}">${esc(s.name)} (${pct(s.dep.punctuality)})</option>`).join("");
  if (!all.length) { $("#list").innerHTML = `<p class="empty">Noch keine Bahnhöfe beobachtet.</p>`; return; }
  sortTable($("#list"), {
    rows, key: "p", dir: "asc", empty: `Kein Bahnhof passt zu „${state.q}“.`,
    cols: [
      { key: "name", label: "Bahnhof", render: (s) => `<a class="rname" href="/station/${encodeURIComponent(s.eva)}?period=${encodeURIComponent(state.period)}">${esc(s.name)}</a>${s.routes ? "" : ' <span class="flag">nur Bahnhof</span>'}` },
      { key: "p", label: "Pünktlich ab", num: true, asc: true, sort: (s) => s.dep.punctuality, render: (s) => pill(s.dep.punctuality) },
      { key: "pa", label: "Pünktlich an", num: true, asc: true, sort: (s) => s.arr.punctuality, render: (s) => pct(s.arr.punctuality) },
      { key: "avg", label: "Ø", num: true, sort: (s) => s.dep.avg_delay, render: (s) => mins(s.dep.avg_delay) },
      { key: "f", label: "Ausfälle", num: true, sort: (s) => s.dep.cancel_rate, render: (s) => pct(s.dep.cancel_rate) },
      { key: "pd", label: "Züge/Tag", num: true, sort: (s) => s.per_day, render: (s) => num(s.per_day) },
      { key: "routes", label: "Strecken", num: true, render: (s) => num(s.routes) },
      { key: "trend", label: "30 Tage", sort: (s) => (s.spark || []).filter((x) => x.p !== null).at(-1)?.p, render: (s) => sparkline(s.spark || [], { w: 100, h: 24 }) },
    ],
  });
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
