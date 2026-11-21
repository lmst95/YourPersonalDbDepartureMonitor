// Cockpit page shared by the route and the station page: summary line, cross chart (hour × weekday) with
// linked selection panel, calendar, sortable timetable, collapsible trip list. The page module passes in how
// to fetch the samples and what to show in its own head.
import { esc, errorBox, getParams, setParams, periodChips, pct, mins, num, $ } from "./ui.js";
import {
  WD, WD_LONG, f1, hm, unpack, setMode, summ, worstCell, byTrain, causes, daily, trainName, lineKey, pill, delta,
  dotClass, runText, chart, cross, hist, causeBars, calendar, dots, DOT_LEGEND, miniBox, sortTable,
} from "./cockpit.js";

const DAY_SETS = { all: [0, 1, 2, 3, 4, 5, 6], wk: [0, 1, 2, 3, 4], sa: [5], su: [6] };

/**
 * cfg.fetch(range, mode)  -> samples payload (range = {period} or {from, to})
 * cfg.onData(d, data)     -> page head, extra sections
 * cfg.modeRefetch         -> true: the mode switch (dep/arr) loads other data (station);
 *                            false: it switches between departure and arrival delay of the same trips (route)
 * cfg.direction           -> show the direction of each train in the timetable
 * cfg.analyse(params)     -> URL of the analysis page for the current selection
 */
export function createCockpit(cfg) {
  const p0 = getParams();
  const state = {
    period: p0.from ? "custom" : (p0.period || "30d"), from: p0.from || "", to: p0.to || "",
    mode: p0.mode === "arr" || p0.kind === "arr" ? "arr" : "dep", sel: {}, days: "all", time: "all", lines: null,
    raw: null, data: null,
  };
  const modeParam = cfg.modeRefetch ? "kind" : "mode";
  const range = () => (state.period === "custom" ? { from: state.from, to: state.to } : { period: state.period });
  const hasSel = () => Object.keys(state.sel).length > 0;
  const applySel = (rs, sel) => rs.filter((r) => (sel.wd === undefined || r.wd === sel.wd) && (sel.h === undefined || r.h === sel.h) && (sel.d === undefined || r.d === sel.d));
  const arr = () => state.mode === "arr";
  const verb = () => (arr() ? "kommt" : "fährt");

  function selLabel(sel) {
    const p = [];
    if (sel.d !== undefined) p.push(new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "2-digit", month: "2-digit", timeZone: "UTC" }).format(state.data.days[sel.d].date));
    if (sel.wd !== undefined) p.push(`${WD_LONG[sel.wd]}s`);
    if (sel.h !== undefined) p.push(`${sel.h}–${sel.h + 1} Uhr`);
    return p.join(", ");
  }
  function analyseHref(sel) {
    if (!cfg.analyse) return "";
    const q = { ...range() };
    if (sel.d !== undefined) { const iso = state.data.days[sel.d].iso; q.from = iso; q.to = iso; delete q.period; }
    if (sel.wd !== undefined) q.wd = String(sel.wd);
    if (sel.h !== undefined) q.h = `${sel.h}-${sel.h + 1}`;
    if (arr()) q[modeParam] = "arr";
    return cfg.analyse(q);
  }

  // ---------- summary line
  function renderAnswer() {
    const d = state.raw, rs = state.data.trips, s = summ(rs);
    const prev = d.prev_kpis;
    const useArrKpis = !cfg.modeRefetch && arr();
    if (!s.n) { $("#answer").innerHTML = `<p class="ans-txt">Keine Fahrten im Zeitraum (${esc(d.label)}).</p>`; $("#notes").innerHTML = ""; return; }
    const w = worstCell(rs);
    const trains = byTrain(rs, state.data.trains), maxM = Math.max(0, ...trains.map((t) => t.m));
    // "most reliable train" only among regular trains: at least half as many runs as the most frequent one
    const best = trains.filter((t) => t.m >= Math.max(6, maxM / 2)).sort((a, b) => b.punct - a.punct || b.m - a.m)[0];
    const one = s.trav !== null && s.trav < 99 ? Math.max(2, Math.round(100 / (100 - s.trav))) : null;
    const r1 = (v) => (v === null ? null : Math.round(v * 10) / 10);
    const kp = [
      ["Median", `${f1(s.p50)} min`, ""],
      ["9 von 10 unter", `${f1(s.p90)} min`, ""],
      ["Ø Verspätung", mins(r1(s.mean)), delta(s.mean, prev ? (useArrKpis ? prev.arr_avg_delay : prev.avg_delay) : null, { invert: true })],
      ["Ausfälle", pct(r1(s.failRate)), delta(s.failRate, prev?.cancel_rate, { invert: true })],
      ["über 15 min", pct(r1(s.late15)), ""],
      ["Fahrten", num(s.n), ""],
    ];
    const col = s.punct === null ? "" : s.punct >= 90 ? "good" : s.punct >= 75 ? "mid" : "poor";
    const where = arr() ? "an" : "ab";
    $("#answer").innerHTML = `
      <div class="ans-big ${col}"><b>${s.punct === null ? "–" : Math.round(s.punct)}<small> %</small></b><span>pünktlich ${where} ${delta(s.punct, prev ? (useArrKpis ? prev.arr_punctuality : prev.punctuality) : null)}</span></div>
      <p class="ans-txt">${one ? `<b>Jeder ${one}. Zug ${verb()} mehr als 5 min zu spät ${where} oder fällt aus.</b> ` : "<b>Fast alle Züge pünktlich.</b> "}${w ? `Am schlechtesten: <b>${WD[w.wd]} ${w.h}–${w.h + 1} Uhr</b> (${Math.round(w.punct)} %). ` : ""}${best ? `Am verlässlichsten: <b>${esc(trainName(best))} um ${hm(best.usual)}</b>${cfg.direction && best.direction ? ` ${arr() ? "aus" : "nach"} ${esc(best.direction)}` : ""} (${Math.round(best.punct)} %).` : ""}
        <span class="muted">${esc(d.label)}${prev ? " · Pfeile: Vergleich mit dem Zeitraum davor" : ""}.</span></p>
      <dl class="ans-k">${kp.map(([l, v, dl]) => `<div><dt>${l}</dt><dd>${v}${dl}</dd></div>`).join("")}</dl>`;
    const k = d.kpis, notes = [];
    if (k.partial) notes.push(`<span><b>${num(k.partial)}</b> davon Ziel entfallen</span>`);
    if (k.replaced) notes.push(`<span><b>${num(k.replaced)}</b> durch Ersatzzug ersetzt</span>`);
    if (k.sev) notes.push(`<span><b>${num(k.sev)}</b> Ersatzverkehr (Bus)</span>`);
    if (k.extra) notes.push(`<span><b>${num(k.extra)}</b> Zusatzfahrten</span>`);
    if (k.platform_changed) notes.push(`<span><b>${num(k.platform_changed)}</b> Gleiswechsel</span>`);
    if (k.unknown) notes.push(`<span><b>${num(k.unknown)}</b> ohne Ist-Daten (nicht gewertet)</span>`);
    $("#notes").innerHTML = notes.join("");
  }

  // ---------- cross chart, selection, calendar
  function renderSelection() {
    const all = state.data.trips, sub = applySel(all, state.sel), s = summ(sub), b = summ(all), on = hasSel();
    const r1 = (v) => (v === null ? null : Math.round(v * 10) / 10);
    const href = analyseHref(state.sel);
    $("#sel").innerHTML = `<div class="ph"><h2>Auswahl</h2><span class="sp"></span>${on ? `<button class="btn btn-s" type="button" data-clear>Aufheben</button>` : `<span class="hint">noch nichts gewählt</span>`}</div>
      <div class="selt">${on ? esc(selLabel(state.sel)) : "Alle Fahrten im Zeitraum"}</div>
      ${s.n ? `<div class="selk">
        <div class="wide"><span>Pünktlich</span><b>${s.punct === null ? "–" : pct(r1(s.punct))}</b>${on ? `<span>${delta(s.punct, b.punct)} ggü. allen</span>` : ""}</div>
        <div><span>Median</span><b>${f1(s.p50)} min</b></div><div><span>Mittlere Hälfte</span><b>${f1(s.p25)}–${f1(s.p75)}</b></div><div><span>9 von 10 unter</span><b>${f1(s.p90)} min</b></div>
        <div><span>Ausfälle</span><b>${pct(r1(s.failRate))}</b></div><div><span>Höchste</span><b>${s.max === null ? "–" : `${s.max} min`}</b></div><div><span>Fahrten</span><b>${num(s.n)}</b></div></div>
      <h3 class="lbl">Verteilung</h3>${hist(sub)}
      <h3 class="lbl">Gründe bei Verspätung und Ausfall</h3>${causeBars(causes(sub, state.data, 5))}` : `<p class="muted">Keine Fahrten in dieser Auswahl.</p>`}
      ${href ? `<a class="more-link" href="${esc(href)}">In der Analyse öffnen →</a>` : ""}`;
  }
  const renderCross = () => chart($("#cross"), (w) => cross(w, state.data.trips, state.sel.d === undefined ? state.sel : {}));
  const renderCal = () => { $("#cal").innerHTML = calendar(state.data.days, daily(state.data.trips, state.data.days.length), state.sel.d); };
  function pick(sel) {
    state.sel = JSON.stringify(sel) === JSON.stringify(state.sel) ? {} : sel;
    renderCross(); renderSelection(); renderCal(); renderTrains();
  }

  // ---------- timetable
  function renderLineChips() {
    const names = [...new Set(state.data.trains.map(lineKey))].sort((a, b) => a.localeCompare(b, "de", { numeric: true }));
    const el = $("#f-lines");
    state.lines = null;
    el.innerHTML = names.length < 2 || names.length > 12 ? ""
      : names.map((n) => `<button type="button" class="chip chip-s" data-l="${esc(n)}" aria-pressed="true">${esc(n)}</button>`).join("");
  }
  function renderTrains() {
    const d = state.data;
    const [a, b] = state.time === "all" ? [0, 24] : state.time.split("-").map(Number);
    const days = DAY_SETS[state.days];
    const rs = applySel(d.trips, state.sel).filter((r) => days.includes(r.wd) && r.h >= a && r.h < b && (!state.lines || state.lines.has(lineKey(d.trains[r.tr]))));
    const minRuns = hasSel() ? 1 : 3;
    const rows = byTrain(rs, d.trains).filter((t) => t.n >= minRuns);
    const now = new Date(), nowMin = now.getHours() * 60 + now.getMinutes();
    const next = rows.filter((r) => r.usual >= nowMin).sort((x, y) => x.usual - y.usual)[0];
    $("#trains-n").textContent = `${num(rows.length)} Züge${minRuns > 1 ? ` mit mind. ${minRuns} Fahrten` : ""}`;
    const fs = $("#f-sel");
    fs.classList.toggle("hidden", !hasSel());
    fs.textContent = hasSel() ? `${selLabel(state.sel)} ✕` : "";
    const cols = [
      { key: "usual", label: arr() ? "Ankunft" : "Abfahrt", num: true, asc: true, tip: "Übliche Uhrzeit", render: (r) => `<b class="tabular">${hm(r.usual)}</b>${r === next ? `<span class="nextb">nächster</span>` : ""}` },
      { key: "name", label: "Zug", sort: (r) => trainName(r), render: (r) => { const full = `${r.category} ${r.number}`.trim(); return `<span class="train">${esc(trainName(r))}</span>${full !== trainName(r) ? ` <span class="muted small">${esc(full)}</span>` : ""}`; } },
    ];
    if (cfg.direction) cols.push({ key: "direction", label: arr() ? "Von" : "Nach", render: (r) => esc(r.direction || "–") });
    cols.push(
      { key: "n", label: "Fahrten", num: true, render: (r) => num(r.n) },
      { key: "punct", label: "Pünktlich", num: true, asc: true, render: (r) => pill(r.punct) },
      { key: "p50", label: "Median", num: true, render: (r) => f1(r.p50) },
      { key: "p90", label: "90 %", num: true, tip: "9 von 10 Fahrten sind höchstens so spät (min)", render: (r) => `<span class="${r.p90 > 15 ? "t-bad" : r.p90 >= 6 ? "t-warn" : ""}">${f1(r.p90)}</span>` },
      { key: "box", label: "Verteilung 0–30 min", sort: (r) => r.p75, render: (r) => miniBox(r) },
      { key: "failRate", label: "Ausfälle", num: true, render: (r) => (r.failed ? `<span class="t-fail">${pct(Math.round(r.failRate * 10) / 10)}</span>` : `<span class="muted">0</span>`) },
      { key: "bad14", label: "Letzte 14 Fahrten", tip: "Sortiert nach Zahl der verspäteten oder ausgefallenen Fahrten", render: (r) => dots(r.runs) },
    );
    sortTable($("#trains"), {
      rows, key: "usual", dir: "asc", maxH: 560, limit: 150, id: (r) => r.ti, empty: "Keine Züge für diese Filter.", cols,
      expand: (box, r) => {
        const last = r.runs.slice(-28);
        const fmt = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "UTC" });
        box.innerHTML = `<div class="exp"><div><h3 class="lbl">Letzte ${last.length} Fahrten</h3><div class="runs">${last.map((x) => `<span class="run ${dotClass(x)}">${esc(fmt.format(x.date))}${x.min !== r.usual ? ` ${hm(x.min)}` : ""} <b>${esc(runText(x))}</b></span>`).join("")}</div></div>
          <div><h3 class="lbl">Verteilung dieses Zugs</h3>${hist(r.runs)}</div></div>`;
      },
    });
    $("#dotleg").innerHTML = rows.length ? DOT_LEGEND : "";
  }

  // ---------- load + controls
  function renderAll() {
    setMode(state.data.trips, cfg.modeRefetch ? "dep" : state.mode);
    renderAnswer(); renderCross(); renderSelection(); renderCal(); renderTrains();
    if (cfg.onRender) cfg.onRender(state.raw, state.data, state);
  }
  async function load() {
    try {
      const d = await cfg.fetch(range(), state.mode);
      state.raw = d; state.data = unpack(d); state.sel = {};
      if (!cfg.modeRefetch) {
        const hasArr = state.data.trips.some((t) => t.arr !== null && t.arr !== undefined);
        const b = $('#mode [data-v="arr"]');
        b.disabled = !hasArr;
        b.title = hasArr ? "Verspätung bei Ankunft am Ziel" : "Für diesen Zeitraum gibt es noch keine Ankunftsdaten";
        if (!hasArr && state.mode === "arr") setModeTo("dep", false);
      }
      if (cfg.onData) cfg.onData(d, state.data, state);
      renderLineChips(); renderAll();
    } catch (e) { errorBox($("#answer"), e); }
    if (cfg.afterLoad) cfg.afterLoad(state);
  }
  function setModeTo(m, render = true) {
    state.mode = m;
    $("#mode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === m));
    setParams({ [modeParam]: m === "dep" ? null : m });
    if (!render) return;
    if (cfg.modeRefetch) load();
    else if (state.data) renderAll();
  }
  function segment(el, fn) {
    el.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (!b || b.disabled) return;
      el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
      fn(b.dataset.v);
    });
  }

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
  segment($("#mode"), (v) => setModeTo(v));
  $("#mode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.v === state.mode));
  segment($("#f-days"), (v) => { state.days = v; renderTrains(); });
  segment($("#f-time"), (v) => { state.time = v; renderTrains(); });
  $("#f-lines").addEventListener("click", (e) => {
    const b = e.target.closest("[data-l]");
    if (!b) return;
    const all = [...$("#f-lines").querySelectorAll("[data-l]")].map((x) => x.dataset.l);
    const cur = state.lines || new Set(all);
    if (cur.has(b.dataset.l) && cur.size > 1) cur.delete(b.dataset.l); else cur.add(b.dataset.l);
    state.lines = cur.size === all.length ? null : cur;
    $("#f-lines").querySelectorAll("[data-l]").forEach((x) => x.setAttribute("aria-pressed", !state.lines || state.lines.has(x.dataset.l)));
    renderTrains();
  });
  $("#f-sel").addEventListener("click", () => pick({}));
  $("#cross").addEventListener("click", (e) => {
    const t = e.target.closest(".hit");
    if (!t) return;
    const s = {};
    if (t.dataset.wd !== undefined) s.wd = Number(t.dataset.wd);
    if (t.dataset.h !== undefined) s.h = Number(t.dataset.h);
    pick(s);
  });
  $("#cal").addEventListener("click", (e) => { const b = e.target.closest("[data-d]"); if (b) pick({ d: Number(b.dataset.d) }); });
  $("#sel").addEventListener("click", (e) => { if (e.target.closest("[data-clear]")) pick({}); });

  return { load, state, range };
}

