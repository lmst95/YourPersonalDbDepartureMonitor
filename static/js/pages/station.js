// Station page: same cockpit as a route, for all trains at the station (departures or arrivals),
// plus sortable tables per line/direction and for the routes starting or ending here.
import { api, esc, pct, mins, num, setUpdated, dateLong, $ } from "../ui.js";
import { createCockpit } from "../cockpit-page.js";
import { summ, groupBy, trainName, pill, f1, miniBox, sortTable } from "../cockpit.js";

const eva = location.pathname.split("/").pop();

function renderLines(data, state) {
  const arr = state.mode === "arr";
  const rows = [];
  for (const [, rs] of groupBy(data.trips, (r) => { const t = data.trains[r.tr]; return `${trainName(t)}|${t.direction}`; })) {
    const t = data.trains[rs[0].tr];
    rows.push({ line: trainName(t), direction: t.direction, ...summ(rs) });
  }
  sortTable($("#lines"), {
    rows: rows.filter((r) => r.n >= 5), key: "n", dir: "desc", maxH: 420, empty: "Zu wenige Fahrten im Zeitraum.",
    cols: [
      { key: "line", label: "Linie", render: (r) => `<span class="train">${esc(r.line)}</span>` },
      { key: "direction", label: arr ? "Von" : "Nach", render: (r) => esc(r.direction || "–") },
      { key: "n", label: "Fahrten", num: true, render: (r) => num(r.n) },
      { key: "punct", label: "Pünktlich", num: true, asc: true, render: (r) => pill(r.punct) },
      { key: "p50", label: "Median", num: true, render: (r) => f1(r.p50) },
      { key: "p90", label: "90 %", num: true, render: (r) => f1(r.p90) },
      { key: "box", label: "Verteilung", sort: (r) => r.p75, render: (r) => miniBox(r, 90) },
      { key: "failRate", label: "Ausfälle", num: true, render: (r) => pct(r.failRate === null ? null : Math.round(r.failRate * 10) / 10) },
    ],
  });
}

function renderRoutes(d, range) {
  $("#routes-section").classList.toggle("hidden", !d.routes.length);
  if (!d.routes.length) return;
  const qs = new URLSearchParams(range).toString();
  sortTable($("#routes"), {
    rows: d.routes.map((r) => ({ ...r, out: r.origin_eva === eva, other: r.origin_eva === eva ? r.dest_name : r.origin_name })),
    key: "other", dir: "asc",
    cols: [
      { key: "other", label: "Strecke", render: (r) => `<a href="/route/${r.id}?${esc(qs)}">${r.out ? "→" : "←"} ${esc(r.other)}</a>` },
      { key: "p", label: "Pünktlich", num: true, asc: true, sort: (r) => r.kpis.punctuality, render: (r) => pill(r.kpis.punctuality) },
      { key: "avg", label: "Ø", num: true, sort: (r) => r.kpis.avg_delay, render: (r) => mins(r.kpis.avg_delay) },
      { key: "f", label: "Ausfälle", num: true, sort: (r) => r.kpis.cancel_rate, render: (r) => pct(r.kpis.cancel_rate) },
      { key: "n", label: "Fahrten", num: true, sort: (r) => r.kpis.planned, render: (r) => num(r.kpis.planned) },
    ],
  });
}

const cockpit = createCockpit({
  fetch: (range, mode) => api(`/api/stations/${encodeURIComponent(eva)}/samples`, { ...range, kind: mode }),
  modeRefetch: true,
  direction: true,
  analyse: (q) => `/analyse?${new URLSearchParams({ station: eva, ...q })}`,
  onData(d, data, state) {
    document.title = `${d.station.name} – Pendelmonitor`;
    $("#title").innerHTML = `${esc(d.station.name)}${d.watched_only ? ' <span class="flag">nur Bahnhof</span>' : ""}`;
    const other = d.other_kpis;
    $("#mode").title = other?.punctuality !== null && other?.punctuality !== undefined
      ? `${state.mode === "dep" ? "Ankünfte" : "Abfahrten"}: ${pct(other.punctuality)} pünktlich` : "";
    $("#analyse-link").href = `/analyse?${new URLSearchParams({ station: eva, ...cockpit.range(), ...(state.mode === "arr" ? { kind: "arr" } : {}) })}`;
    $("#livechip").href = `/live?eva=${encodeURIComponent(eva)}`;
    $("#livechip").innerHTML = `<i></i>Live-Tafel`;
    $("#livechip").classList.remove("hidden");
    $("#note").textContent = d.since ? `Bahnhofsdaten (alle Züge) liegen seit ${dateLong(d.since)} vor. Pünktlich = weniger als 6 min Verspätung.` : "";
    renderRoutes(d, cockpit.range());
  },
  onRender(d, data, state) { renderLines(data, state); },
});

cockpit.load();
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
