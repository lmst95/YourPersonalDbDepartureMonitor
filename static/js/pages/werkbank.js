// Analysis workbench: filters (weekdays, hours, lines) on the left, six views on the right that all use the
// same filtered trips. Works for a route or a station; the state lives in the URL so links can preset it.
// The file is deliberately not called "analyse.js": ad blockers (EasyPrivacy) block scripts with that name.
import { api, esc, errorBox, getParams, setParams, periodChips, pct, mins, num, setUpdated, $ } from "../ui.js";
import {
  WD, WD_LONG, f1, hm, unpack, setMode, summ, byHour, byWd, byTrain, causes, trainName, lineKey, pill, dotClass, runText,
  chart, cross, boxplot, hbox, hist, causeBars, dots, DOT_LEGEND, miniBox, sortTable, niceMax,
} from "../cockpit.js";

const p0 = getParams();
const ALL_WD = [0, 1, 2, 3, 4, 5, 6];
const parseH = (v) => { const m = /^(\d{1,2})-(\d{1,2})$/.exec(v || ""); return m ? [Math.min(23, +m[1]), Math.max(+m[1] + 1, Math.min(24, +m[2]))] : [0, 24]; };
const state = {
  route: p0.route || "", station: p0.station || "",
  period: p0.from ? "custom" : (p0.period || "30d"), from: p0.from || "", to: p0.to || "",
  mode: p0.mode === "arr" || p0.kind === "arr" ? "arr" : "dep",
  wd: new Set(p0.wd ? p0.wd.split(",").map(Number).filter((x) => x >= 0 && x <= 6) : ALL_WD),
  h: parseH(p0.h), lines: p0.lines ? new Set(p0.lines.split("|")) : null,
  view: ["hour", "wd", "heat", "dist", "trains", "trips"].includes(p0.view) ? p0.view : "hour",
  raw: null, data: null, rs: [],
};
const VIEWS = [["hour", "Stunde"], ["wd", "Wochentag"], ["heat", "Heatmap"], ["dist", "Verteilung"], ["trains", "Züge"], ["trips", "Einzelfahrten"]];
const range = () => (state.period === "custom" ? { from: state.from, to: state.to } : { period: state.period });
const isStation = () => !!state.station && !state.route;

function syncUrl() {
  setParams({
    route: state.route || null, station: isStation() ? state.station : null,
    mode: !isStation() && state.mode === "arr" ? "arr" : null, kind: isStation() && state.mode === "arr" ? "arr" : null,
    wd: state.wd.size === 7 ? null : [...state.wd].sort().join(","),
    h: state.h[0] === 0 && state.h[1] === 24 ? null : `${state.h[0]}-${state.h[1]}`,
    lines: state.lines ? [...state.lines].join("|") : null, view: state.view === "hour" ? null : state.view,
  });
}

// ---------- scope picker
async function loadCatalog() {
  const c = await api("/api/catalog");
  if (!state.route && !state.station && c.routes.length) state.route = String(c.routes[0].id);
  $("#scope").innerHTML = `<optgroup label="Strecken">${c.routes.map((r) => `<option value="r:${r.id}">${esc(r.origin_name)} → ${esc(r.dest_name)}</option>`).join("")}</optgroup>
    <optgroup label="Bahnhöfe (alle Züge)">${c.stations.map((s) => `<option value="s:${esc(s.eva)}">${esc(s.name)}</option>`).join("")}</optgroup>`;
  $("#scope").value = state.route ? `r:${state.route}` : `s:${state.station}`;
}

// ---------- filters
function filtered() {
  const d = state.data;
  return d.trips.filter((r) => state.wd.has(r.wd) && r.h >= state.h[0] && r.h < state.h[1]
    && (!state.lines || state.lines.has(lineKey(d.trains[r.tr]))));
}
function renderFacets() {
  $("#f-wd").innerHTML = WD.map((l, i) => `<button type="button" class="btn btn-s${state.wd.has(i) ? " on" : ""}" data-i="${i}" aria-pressed="${state.wd.has(i)}" title="${WD_LONG[i]}">${l}</button>`).join("");
  let o1 = "", o2 = "";
  for (let h = 0; h < 24; h++) o1 += `<option value="${h}"${h === state.h[0] ? " selected" : ""}>${h}:00</option>`;
  for (let h = 1; h <= 24; h++) o2 += `<option value="${h}"${h === state.h[1] ? " selected" : ""}>${h}:00</option>`;
  $("#f-hf").innerHTML = o1; $("#f-ht").innerHTML = o2;
  const counts = new Map();
  for (const r of state.data.trips) { const n = lineKey(state.data.trains[r.tr]); counts.set(n, (counts.get(n) || 0) + 1); }
  const names = [...counts].sort((a, b) => b[1] - a[1]);
  $("#f-lines").innerHTML = names.length > 30 ? `<p class="hint">${names.length} Linien – Filter in der Ansicht „Züge“.</p>`
    : names.map(([n, c]) => `<label class="chk"><input type="checkbox" data-l="${esc(n)}"${!state.lines || state.lines.has(n) ? " checked" : ""}><span>${esc(n)}</span><span class="c">${num(c)}</span></label>`).join("");
}

function renderSummary() {
  const s = summ(state.rs), name = state.raw.route ? `${state.raw.route.origin_name} → ${state.raw.route.dest_name}` : state.raw.station.name;
  const tags = [state.raw.label, state.wd.size === 7 ? "alle Tage" : [...state.wd].sort().map((i) => WD[i]).join(", "),
    `${state.h[0]}–${state.h[1]} Uhr`, state.mode === "arr" ? "Ankunft" : "Abfahrt"];
  if (state.lines) tags.push([...state.lines].join(", ") || "keine Linie");
  $("#filterline").innerHTML = `<b>${esc(name)}</b>${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}`;
  const r1 = (v) => (v === null ? null : Math.round(v * 10) / 10);
  $("#kpis").innerHTML = [
    ["Fahrten", num(s.n)], ["Pünktlich", pct(r1(s.punct))], ["für Reisende", pct(r1(s.trav))], ["Ø", mins(r1(s.mean))],
    ["Median", `${f1(s.p50)} min`], ["Mittlere Hälfte", `${f1(s.p25)}–${f1(s.p75)}`], ["9 von 10 unter", `${f1(s.p90)} min`],
    ["Höchste", s.max === null ? "–" : `${s.max} min`], ["Ausfälle", pct(r1(s.failRate))],
  ].map(([l, v]) => `<div><dt>${l}</dt><dd>${v}</dd></div>`).join("");
  $("#kpis").title = "Pünktlich = gemessene Fahrten unter 6 min. Für Reisende = Ausfälle zählen als unpünktlich.";
}

// ---------- views
const groupCols = (first) => [first,
  { key: "n", label: "Fahrten", num: true, render: (g) => num(g.n) },
  { key: "punct", label: "Pünktlich", num: true, asc: true, render: (g) => pill(g.punct) },
  { key: "trav", label: "für Reisende", num: true, asc: true, render: (g) => pct(g.trav === null ? null : Math.round(g.trav * 10) / 10) },
  { key: "mean", label: "Ø", num: true, render: (g) => f1(g.mean) },
  { key: "p25", label: "25 %", num: true, render: (g) => f1(g.p25) },
  { key: "p50", label: "Median", num: true, render: (g) => f1(g.p50) },
  { key: "p75", label: "75 %", num: true, render: (g) => f1(g.p75) },
  { key: "p90", label: "90 %", num: true, render: (g) => f1(g.p90) },
  { key: "max", label: "Max", num: true, render: (g) => (g.max ?? "–") },
  { key: "failRate", label: "Ausfälle", num: true, render: (g) => pct(g.failRate === null ? null : Math.round(g.failRate * 10) / 10) },
];
function renderView() {
  const v = $("#view"), rs = state.rs, d = state.data;
  $("#views").querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", b.dataset.v === state.view));
  if (!rs.length) { v.innerHTML = `<p class="muted">Keine Fahrten für diese Filter. Wochentage oder Uhrzeit erweitern.</p>`; return; }
  if (state.view === "hour") {
    const hrs = byHour(rs, state.h[0], state.h[1]);
    v.innerHTML = `<div class="chart" id="v-chart"></div><p class="hint">Box = mittlere Hälfte, Strich = Median, Linie = 10–90 %, rot gestrichelt = ab hier verspätet.</p><div id="v-table"></div>`;
    chart($("#v-chart"), (w) => boxplot(w, hrs, { h: 240, title: (g) => `${g.key}–${g.key + 1} Uhr` }));
    sortTable($("#v-table"), { rows: hrs.filter((g) => g.n), key: "key", dir: "asc", maxH: 420, cols: groupCols({ key: "key", label: "Stunde", num: true, asc: true, render: (g) => `${g.key}:00` }) });
  } else if (state.view === "wd") {
    const wds = byWd(rs);
    const yMax = niceMax(Math.max(6, ...wds.map((g) => g.p90 || 0)));
    v.innerHTML = `<div class="chart" id="v-chart"></div><div id="v-table"></div>`;
    chart($("#v-chart"), (w) => hbox(w, wds, yMax));
    sortTable($("#v-table"), { rows: wds.filter((g) => g.n), key: "key", dir: "asc", cols: groupCols({ key: "key", label: "Tag", num: true, asc: true, render: (g) => WD_LONG[g.key] }) });
  } else if (state.view === "heat") {
    v.innerHTML = `<div class="chart" id="v-chart"></div><div class="legend"><span>Felder: Anteil pünktlich</span><span>schlecht</span><span class="scale"></span><span>gut</span><span>· Feld anklicken zeigt die Einzelfahrten dieses Tages und dieser Stunde</span></div>`;
    chart($("#v-chart"), (w) => cross(w, rs, {}));
    $("#v-chart").onclick = (e) => {
      const t = e.target.closest(".hit"); if (!t) return;
      if (t.dataset.wd !== undefined) state.wd = new Set([Number(t.dataset.wd)]);
      if (t.dataset.h !== undefined) { const h = Number(t.dataset.h); state.h = [h, h + 1]; }
      state.view = "trips"; update();
    };
  } else if (state.view === "dist") {
    v.innerHTML = `<div class="exp"><div><h3 class="lbl">Verspätung</h3>${hist(rs)}</div><div><h3 class="lbl">Gründe bei Verspätung und Ausfall</h3>${causeBars(causes(rs, d, 10))}</div></div>`;
  } else if (state.view === "trains") {
    const rows = byTrain(rs, d.trains);
    v.innerHTML = `<div id="v-table"></div>${DOT_LEGEND}`;
    const cols = [
      { key: "usual", label: "Uhrzeit", num: true, asc: true, render: (r) => `<b class="tabular">${hm(r.usual)}</b>` },
      { key: "name", label: "Zug", sort: (r) => trainName(r), render: (r) => `<span class="train">${esc(trainName(r))}</span> <span class="muted small">${esc(`${r.category} ${r.number}`.trim())}</span>` },
    ];
    if (isStation()) cols.push({ key: "direction", label: state.mode === "arr" ? "Von" : "Nach", render: (r) => esc(r.direction || "–") });
    cols.push(
      { key: "n", label: "Fahrten", num: true, render: (r) => num(r.n) },
      { key: "punct", label: "Pünktlich", num: true, asc: true, render: (r) => pill(r.punct) },
      { key: "p50", label: "Median", num: true, render: (r) => f1(r.p50) },
      { key: "p90", label: "90 %", num: true, render: (r) => f1(r.p90) },
      { key: "max", label: "Max", num: true, render: (r) => (r.max ?? "–") },
      { key: "box", label: "Verteilung 0–30 min", sort: (r) => r.p75, render: (r) => miniBox(r) },
      { key: "failRate", label: "Ausfälle", num: true, render: (r) => pct(r.failRate === null ? null : Math.round(r.failRate * 10) / 10) },
      { key: "bad14", label: "Letzte 14 Fahrten", render: (r) => dots(r.runs) },
    );
    sortTable($("#v-table"), { rows, key: "punct", dir: "asc", maxH: 600, limit: 200, cols, id: (r) => r.ti,
      expand: (box, r) => { box.innerHTML = `<div class="runs">${r.runs.slice(-40).map((x) => `<span class="run ${dotClass(x)}">${esc(new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "UTC" }).format(x.date))} ${hm(x.min)} <b>${esc(runText(x))}</b></span>`).join("")}</div>`; } });
  } else {
    const fmt = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit", timeZone: "UTC" });
    const rows = rs.map((r) => ({ ...r, t: d.trains[r.tr], ts: r.date.getTime() / 60000 + r.min, cause: (d.causeSets[r.cs] || []).map((c) => d.causeText[c] || c).join(", ") }));
    v.innerHTML = `<div id="v-table"></div>`;
    const cols = [
      { key: "ts", label: "Datum", num: true, render: (r) => esc(fmt.format(r.date)) },
      { key: "min", label: "Plan", num: true, asc: true, render: (r) => `<b class="tabular">${hm(r.min)}</b>` },
      { key: "train", label: "Zug", sort: (r) => trainName(r.t), render: (r) => `<span class="train">${esc(trainName(r.t))}</span> <span class="muted small">${esc(`${r.t.category} ${r.t.number}`.trim())}</span>` },
    ];
    if (isStation()) cols.push({ key: "dir", label: state.mode === "arr" ? "Von" : "Nach", sort: (r) => r.t.direction, render: (r) => esc(r.t.direction || "–") });
    cols.push(
      { key: "delay", label: "Verspätung", num: true, sort: (r) => (r.st === "cancelled" || r.st === "partial" ? 9999 : r.delay), render: (r) => `<span class="run ${dotClass(r)}">${esc(runText(r))}</span>` },
      { key: "cause", label: "Grund", render: (r) => `<span class="muted">${esc(r.cause)}</span>` },
    );
    sortTable($("#v-table"), { rows, key: "ts", dir: "desc", maxH: 640, limit: 100, cols });
  }
}

function update() {
  state.rs = filtered();
  syncUrl(); renderFacets(); renderSummary(); renderView();
}

async function load() {
  const v = $("#view");
  try {
    const kind = state.mode === "arr" ? "arr" : "dep";
    const d = isStation()
      ? await api(`/api/stations/${encodeURIComponent(state.station)}/samples`, { ...range(), kind })
      : await api(`/api/routes/${encodeURIComponent(state.route)}/samples`, range());
    state.raw = d; state.data = unpack(d);
    const hasArr = isStation() || state.data.trips.some((t) => t.arr !== null && t.arr !== undefined);
    const b = $('#mode [data-v="arr"]');
    b.disabled = !hasArr; b.title = hasArr ? "" : "Für diesen Zeitraum gibt es noch keine Ankunftsdaten";
    if (!hasArr && state.mode === "arr") { state.mode = "dep"; $("#mode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === "dep")); }
    setMode(state.data.trips, isStation() ? "dep" : state.mode);
    const cockpit = isStation() ? `/station/${encodeURIComponent(state.station)}` : `/route/${encodeURIComponent(state.route)}`;
    const qs = new URLSearchParams(range()).toString();
    $("#cockpit-link").href = `${cockpit}?${qs}`;
    $("#back").href = `${cockpit}?${qs}`;
    $("#back").textContent = `← ${d.route ? `${d.route.origin_name} → ${d.route.dest_name}` : d.station.name}`;
    document.title = `Analyse ${d.route ? `${d.route.origin_name} → ${d.route.dest_name}` : d.station.name} – Pendelmonitor`;
    if (state.lines) {
      const names = new Set(state.data.trains.map(lineKey));
      state.lines = new Set([...state.lines].filter((n) => names.has(n)));
      if (!state.lines.size) state.lines = null;
    }
    update();
  } catch (e) { errorBox(v, e); }
}

// ---------- controls
$("#views").innerHTML = VIEWS.map(([k, l]) => `<button type="button" role="tab" data-v="${k}" aria-selected="${k === state.view}">${l}</button>`).join("");
$("#views").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state.view = b.dataset.v; syncUrl(); renderView(); });
$("#scope").addEventListener("change", (e) => {
  const [k, id] = e.target.value.split(":");
  state.route = k === "r" ? id : ""; state.station = k === "s" ? id : ""; state.lines = null;
  load();
});
$("#mode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === state.mode));
$("#mode").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]"); if (!b || b.disabled) return;
  $("#mode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  state.mode = b.dataset.v;
  if (isStation()) load(); else { setMode(state.data.trips, state.mode); update(); }
});
$("#f-wd").addEventListener("click", (e) => {
  const b = e.target.closest("[data-i]"); if (!b) return;
  const i = Number(b.dataset.i);
  if (state.wd.has(i) && state.wd.size > 1) state.wd.delete(i); else state.wd.add(i);
  update();
});
document.querySelectorAll("[data-p]").forEach((b) => b.addEventListener("click", () => {
  state.wd = new Set(b.dataset.p === "work" ? [0, 1, 2, 3, 4] : b.dataset.p === "we" ? [5, 6] : ALL_WD); update();
}));
document.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => { state.h = parseH(b.dataset.t); update(); }));
$("#f-hf").addEventListener("change", (e) => { const v = Number(e.target.value); state.h = [v, Math.max(v + 1, state.h[1])]; update(); });
$("#f-ht").addEventListener("change", (e) => { const v = Number(e.target.value); state.h = [Math.min(state.h[0], v - 1), v]; update(); });
$("#f-lines").addEventListener("change", () => {
  const boxes = [...$("#f-lines").querySelectorAll("[data-l]")];
  const on = boxes.filter((x) => x.checked).map((x) => x.dataset.l);
  state.lines = on.length === boxes.length ? null : new Set(on);
  update();
});
$("#f-reset").addEventListener("click", () => { state.wd = new Set(ALL_WD); state.h = [0, 24]; state.lines = null; update(); });

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

loadCatalog().then(load).catch((e) => errorBox($("#view"), e));
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
