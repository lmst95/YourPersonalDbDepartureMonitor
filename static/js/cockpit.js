// Cockpit building blocks: statistics over single trips (quantiles for any selection), the cross chart
// (hour boxplots on top, weekday boxplots on the right, heatmap in between), calendar, distribution,
// sortable table. Everything is computed in the browser from /api/routes/{id}/samples.
import { esc, pct, num, pctColor, PUNCTUAL } from "./ui.js";

export const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
export const WD_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
const FAILED = new Set(["cancelled", "partial"]);

export const f1 = (v) => (v === null || v === undefined || Number.isNaN(v) ? "–" : v.toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 }));
export const f0 = (v) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Math.round(v).toLocaleString("de-DE"));
export const hm = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
export const tint = (p, a) => {
  const c = pctColor(p);
  return c.startsWith("rgb(") ? c.replace("rgb(", "rgba(").replace(")", `,${a})`) : c;
};

// ---------- data
/** API payload -> list of trip objects. `delay` is filled by setMode(). */
export function unpack(d) {
  const days = d.days.map((iso) => {
    const dt = new Date(`${iso}T12:00:00Z`);
    return { iso, wd: (dt.getUTCDay() + 6) % 7, date: dt };
  });
  const trains = d.trains.map(([category, number, line, direction]) => ({ category, number, line, direction: direction || "" }));
  const trips = d.trips.map(([di, min, si, dep, arr, ti, ci]) => ({
    d: di, wd: days[di].wd, date: days[di].date, h: Math.floor(min / 60), min, st: d.statuses[si], dep, arr, tr: ti, cs: ci, delay: dep,
  }));
  return { days, trains, trips, causeSets: d.cause_sets, causeText: d.cause_text };
}
export function setMode(trips, mode) { for (const t of trips) t.delay = mode === "arr" ? t.arr : t.dep; }

function quant(sorted, p) {
  if (!sorted.length) return null;
  const i = (sorted.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}
/** Same definitions as the server: punctual = measured and < 6 min late; failed = cancelled or partial. */
export function summ(rs) {
  let failed = 0, ok = 0, sum = 0, late15 = 0;
  const ds = [];
  for (const r of rs) {
    if (FAILED.has(r.st)) failed++;
    if (r.delay === null || r.delay === undefined) continue;
    ds.push(r.delay); sum += r.delay;
    if (r.delay < PUNCTUAL) ok++;
    if (r.delay > 15) late15++;
  }
  ds.sort((a, b) => a - b);
  const m = ds.length, n = rs.length;
  return {
    n, m, failed, ok,
    punct: m ? (100 * ok) / m : null,
    trav: m + failed ? (100 * ok) / (m + failed) : null,
    failRate: n ? (100 * failed) / n : null,
    late15: m ? (100 * late15) / m : null,
    mean: m ? sum / m : null,
    p10: quant(ds, 0.1), p25: quant(ds, 0.25), p50: quant(ds, 0.5), p75: quant(ds, 0.75), p90: quant(ds, 0.9),
    max: m ? ds[m - 1] : null,
  };
}
export function groupBy(rs, fn) {
  const m = new Map();
  for (const r of rs) { const k = fn(r); let a = m.get(k); if (!a) m.set(k, (a = [])); a.push(r); }
  return m;
}
export function hourRange(rs) {
  if (!rs.length) return [5, 24];
  let lo = 23, hi = 0;
  for (const r of rs) { if (r.h < lo) lo = r.h; if (r.h > hi) hi = r.h; }
  return [lo, hi + 1];
}
export function byHour(rs, h0, h1) {
  const g = groupBy(rs, (r) => r.h), out = [];
  for (let h = h0; h < h1; h++) out.push({ key: h, label: String(h), ...summ(g.get(h) || []) });
  return out;
}
export function byWd(rs) {
  const g = groupBy(rs, (r) => r.wd);
  return WD.map((label, i) => ({ key: i, label, ...summ(g.get(i) || []) }));
}
export function worstCell(rs, minMeasured = 8) {
  let w = null;
  for (const [k, v] of groupBy(rs, (r) => r.wd * 100 + r.h)) {
    const s = summ(v);
    if (s.m < minMeasured) continue;
    if (!w || s.punct < w.punct) w = { wd: Math.floor(k / 100), h: k % 100, punct: s.punct };
  }
  return w;
}
/** One row per train (category + number + line) with its usual departure time and all runs. */
export function byTrain(rs, trains) {
  const out = [];
  for (const [ti, runs] of groupBy(rs, (r) => r.tr)) {
    runs.sort((a, b) => a.d - b.d || a.min - b.min);
    const mins = new Map();
    for (const r of runs) mins.set(r.min, (mins.get(r.min) || 0) + 1);
    const usual = [...mins].sort((a, b) => b[1] - a[1] || a[0] - b[0])[0][0];
    const t = trains[ti];
    const last = runs.slice(-14);
    out.push({ ti, ...t, usual, runs, ...summ(runs), bad14: last.filter((r) => FAILED.has(r.st) || r.delay >= PUNCTUAL).length });
  }
  return out;
}
export function causes(rs, data, limit = 6) {
  const c = new Map();
  for (const r of rs) for (const code of data.causeSets[r.cs] || []) c.set(code, (c.get(code) || 0) + 1);
  return [...c].sort((a, b) => b[1] - a[1]).slice(0, limit).map(([code, n]) => ({ text: data.causeText[code] || code, n }));
}
export function daily(trips, nDays) {
  const g = groupBy(trips, (r) => r.d), out = [];
  for (let d = 0; d < nDays; d++) out.push(summ(g.get(d) || []));
  return out;
}

// ---------- labels
export function trainName(t) {
  return t.line && !/^\d+$/.test(t.line) && t.category !== "ICE" && t.category !== "IC" ? t.line : `${t.category || ""} ${t.number || ""}`.trim();
}
/** Line for filters: the line name, or the category when there is none (older data: "S", "RE", …). */
export function lineKey(t) {
  return t.line && !/^\d+$/.test(t.line) ? t.line : t.category || "?";
}
export function pill(p) {
  if (p === null || p === undefined) return `<span class="pill">–</span>`;
  return `<span class="pill ${p >= 90 ? "p-ok" : p >= 75 ? "p-warn" : "p-bad"}">${pct(Math.round(p * 10) / 10)}</span>`;
}
export function delta(a, b, { invert = false, unit = "" } = {}) {
  if (a === null || a === undefined || b === null || b === undefined) return "";
  const d = a - b;
  if (Math.abs(d) < 0.05) return `<i class="dl muted">±0</i>`;
  const good = invert ? d < 0 : d > 0;
  return `<i class="dl ${good ? "up" : "down"}" title="Vergleich mit dem Zeitraum davor">${d > 0 ? "▲" : "▼"} ${f1(Math.abs(d))}${unit}</i>`;
}
export function dotClass(r) {
  if (!r) return "";
  if (FAILED.has(r.st)) return "x";
  if (r.delay === null || r.delay === undefined) return "n";
  return r.delay > 15 ? "b" : r.delay >= PUNCTUAL ? "w" : "o";
}
export function runText(r) {
  if (FAILED.has(r.st)) return r.st === "partial" ? "Ziel entfällt" : "Ausfall";
  if (r.delay === null || r.delay === undefined) return r.st === "sev" ? "Bus" : "?";
  return r.delay > 0 ? `+${r.delay}` : String(r.delay);
}

// ---------- charts (SVG at real pixel width, redrawn on resize)
const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver((es) => {
  for (const e of es) {
    const el = e.target, w = Math.floor(e.contentRect.width);
    if (w > 0 && w !== el._w) { el._w = w; el.innerHTML = el._draw(w); }
  }
}) : null;
export function chart(el, fn) {
  el._draw = fn;
  el._w = el.clientWidth;
  if (ro) ro.observe(el);
  if (el._w > 0) el.innerHTML = fn(el._w);
}
export function niceMax(v) { return [10, 15, 20, 30, 45, 60, 90, 120, 180].find((x) => x >= v) || Math.ceil(v / 60) * 60; }
export function ticks(max) {
  const st = max <= 10 ? 2 : max <= 20 ? 5 : max <= 60 ? 10 : max <= 120 ? 30 : 60;
  const o = [];
  for (let t = 0; t <= max; t += st) o.push(t);
  return o;
}
const tip = (g, head) => `${head} · ${num(g.n)} Fahrten · ${pct(g.punct === null ? null : Math.round(g.punct))} pünktlich · Median ${f1(g.p50)} min · mittlere Hälfte ${f1(g.p25)}–${f1(g.p75)} · 10–90 %: ${f1(g.p10)}–${f1(g.p90)} min`;

/** Cross chart. sel = {wd?, h?}. Clickable parts carry data-wd / data-h. */
export function cross(w, rs, sel) {
  const [H0, H1] = hourRange(rs), nh = H1 - H0, lw = 26, rw = Math.round(Math.max(110, Math.min(220, w * 0.26)));
  const gap = 12, top = 124, ax = 18, rowH = w < 560 ? 20 : 24, gw = w - lw - rw - gap, cw = gw / nh;
  const y0 = top + ax, yb = y0 + rowH * 7, h = yb + 18;
  const hrs = byHour(rs, H0, H1), wds = byWd(rs), g = groupBy(rs, (r) => r.wd * 100 + r.h);
  const yMax = niceMax(Math.max(6, ...hrs.map((x) => x.p90 || 0), ...wds.map((x) => x.p90 || 0)));
  const cl = (v) => Math.max(-1, Math.min(yMax, v)), y = (v) => top - 8 - ((cl(v) + 1) / (yMax + 1)) * (top - 16);
  const rx0 = lw + gw + gap, rx1 = w - 40, xs = (v) => rx0 + ((cl(v) + 1) / (yMax + 1)) * (rx1 - rx0);
  const any = sel.wd !== undefined || sel.h !== undefined;
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="Verspätung nach Stunde und Wochentag">`;
  for (const t of ticks(yMax)) s += `<line x1="${lw}" x2="${lw + gw}" y1="${y(t)}" y2="${y(t)}" class="grid"/><text x="${lw - 5}" y="${y(t) + 3.5}" font-size="9.5" text-anchor="end">${t}</text>`;
  s += `<text x="2" y="${top + 12}" font-size="9.5">min</text><line x1="${lw}" x2="${lw + gw}" y1="${y(PUNCTUAL - 0.5)}" y2="${y(PUNCTUAL - 0.5)}" class="limit"/>`;
  for (const t of ticks(yMax)) s += `<line x1="${xs(t)}" x2="${xs(t)}" y1="${y0}" y2="${yb}" class="grid"/><text x="${xs(t)}" y="${yb + 12}" font-size="9" text-anchor="middle">${t}</text>`;
  s += `<line x1="${xs(PUNCTUAL - 0.5)}" x2="${xs(PUNCTUAL - 0.5)}" y1="${y0}" y2="${yb}" class="limit"/>`;
  // how to read (top right corner)
  const lx = rx0;
  s += `<text x="${lx}" y="14" font-size="10.5" class="strong">So lesen</text>
    <rect x="${lx}" y="24" width="12" height="16" rx="2" fill="${tint(80, 0.35)}" stroke="${pctColor(80)}"/><line x1="${lx}" x2="${lx + 12}" y1="32" y2="32" class="med"/>
    <text x="${lx + 18}" y="30" font-size="9.5">Box: mittlere Hälfte</text><text x="${lx + 18}" y="41" font-size="9.5">Strich: Median</text>
    <line x1="${lx + 6}" x2="${lx + 6}" y1="48" y2="64" class="whisk"/><text x="${lx + 18}" y="59" font-size="9.5">Linie: 10–90 %</text>
    <line x1="${lx}" x2="${lx + 12}" y1="74" y2="74" class="limit"/><text x="${lx + 18}" y="77" font-size="9.5">ab hier verspätet</text>
    <text x="${lx}" y="${top + 12}" font-size="9.5" class="strong">je Wochentag →</text>`;
  hrs.forEach((x, i) => {
    const cx = lw + cw * (i + 0.5), on = sel.h === x.key && sel.wd === undefined;
    s += `<g class="hit" data-h="${x.key}"><rect x="${lw + cw * i}" y="0" width="${cw}" height="${top + ax}" class="${on ? "selbg" : "clear"}"/>`;
    if (x.m && x.p50 !== null) {
      const bw = Math.max(3, Math.min(15, cw * 0.62));
      s += `<title>${esc(tip(x, `${x.key}–${x.key + 1} Uhr`))}</title>
        <line x1="${cx}" x2="${cx}" y1="${y(x.p10)}" y2="${y(x.p90)}" class="whisk"/>
        <rect x="${cx - bw / 2}" y="${y(x.p75)}" width="${bw}" height="${Math.max(1.5, y(x.p25) - y(x.p75))}" rx="2" fill="${tint(x.punct, 0.38)}" stroke="${pctColor(x.punct)}"/>
        <line x1="${cx - bw / 2}" x2="${cx + bw / 2}" y1="${y(x.p50)}" y2="${y(x.p50)}" class="med"/>`;
      if (x.p90 > yMax) s += `<text x="${cx}" y="9" font-size="8.5" text-anchor="middle">↑${Math.round(x.p90)}</text>`;
    }
    s += `<text x="${cx}" y="${top + 12}" font-size="10" text-anchor="middle" class="${sel.h === x.key ? "acc" : "lab"}">${cw < 17 && x.key % 2 ? "" : x.key}</text></g>`;
  });
  WD.forEach((l, wd) => {
    const yy = y0 + wd * rowH, onR = sel.wd === wd && sel.h === undefined;
    s += `<g class="hit" data-wd="${wd}"><rect x="0" y="${yy}" width="${lw - 2}" height="${rowH}" class="clear"/><text x="${lw - 6}" y="${yy + rowH / 2 + 3.5}" font-size="10.5" text-anchor="end" class="${sel.wd === wd ? "acc" : "lab"} strong">${l}</text></g>`;
    for (let i = 0; i < nh; i++) {
      const hh = H0 + i, v = g.get(wd * 100 + hh), st = v ? summ(v) : null, has = st && st.m >= 3;
      const inSel = (sel.wd === undefined || sel.wd === wd) && (sel.h === undefined || sel.h === hh);
      const cell = sel.wd === wd && sel.h === hh;
      s += `<rect class="hit cell${cell ? " on" : ""}" data-wd="${wd}" data-h="${hh}" x="${(lw + cw * i + 1).toFixed(1)}" y="${yy + 1}" width="${Math.max(1, cw - 2).toFixed(1)}" height="${rowH - 2}" rx="2" fill="${has ? tint(st.punct, 0.85) : "var(--surface-2)"}" opacity="${any && !inSel ? 0.35 : 1}"><title>${esc(has ? tip(st, `${WD_LONG[wd]} ${hh}–${hh + 1} Uhr`) : `${WD_LONG[wd]} ${hh} Uhr: ${st ? "zu wenige Messungen" : "keine Fahrten"}`)}</title></rect>`;
    }
    const x = wds[wd];
    if (x.m && x.p50 !== null) {
      const cy = yy + rowH / 2, bh = Math.min(12, rowH - 8);
      s += `<g class="hit" data-wd="${wd}"><rect x="${rx0 - 4}" y="${yy}" width="${w - rx0 + 4}" height="${rowH}" class="${onR ? "selbg" : "clear"}"/><title>${esc(tip(x, WD_LONG[wd]))}</title>
        <line x1="${xs(x.p10)}" x2="${xs(x.p90)}" y1="${cy}" y2="${cy}" class="whisk"/>
        <rect x="${xs(x.p25)}" y="${cy - bh / 2}" width="${Math.max(1.5, xs(x.p75) - xs(x.p25))}" height="${bh}" rx="2" fill="${tint(x.punct, 0.38)}" stroke="${pctColor(x.punct)}"/>
        <line x1="${xs(x.p50)}" x2="${xs(x.p50)}" y1="${cy - bh / 2 - 1}" y2="${cy + bh / 2 + 1}" class="med"/>
        <text x="${w - 2}" y="${cy + 3.5}" font-size="10.5" text-anchor="end" class="strong">${Math.round(x.punct)} %</text></g>`;
    }
  });
  return `${s}</svg>`;
}

/** Vertical boxplots (e.g. per hour); o.range = [from, to) highlighted. */
export function boxplot(w, groups, o = {}) {
  const h = o.h || 170, pl = 28, pr = 6, pt = 8, pb = 20;
  const yMax = o.yMax || niceMax(Math.max(6, ...groups.map((g) => g.p90 || 0)));
  const iw = w - pl - pr, ih = h - pt - pb, y = (v) => pt + ih - ((Math.max(-1, Math.min(yMax, v)) + 1) / (yMax + 1)) * ih;
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="${esc(o.aria || "Verspätung als Boxplot")}">`;
  for (const t of ticks(yMax)) s += `<line x1="${pl}" x2="${w - pr}" y1="${y(t)}" y2="${y(t)}" class="grid"/><text x="${pl - 5}" y="${y(t) + 3.5}" font-size="9.5" text-anchor="end">${t}</text>`;
  s += `<line x1="${pl}" x2="${w - pr}" y1="${y(PUNCTUAL - 0.5)}" y2="${y(PUNCTUAL - 0.5)}" class="limit"/>`;
  const n = groups.length, step = iw / Math.max(1, n), bw = Math.max(3, Math.min(18, step * 0.58));
  groups.forEach((g, i) => {
    const cx = pl + step * (i + 0.5);
    if (o.range && g.key >= o.range[0] && g.key < o.range[1]) s += `<rect x="${cx - step / 2}" y="${pt}" width="${step}" height="${ih}" class="selbg"/>`;
    if (g.m && g.p50 !== null) {
      s += `<g><title>${esc(tip(g, o.title ? o.title(g) : g.label))}</title><line x1="${cx}" x2="${cx}" y1="${y(g.p10)}" y2="${y(g.p90)}" class="whisk"/>
        <rect x="${cx - bw / 2}" y="${y(g.p75)}" width="${bw}" height="${Math.max(1.5, y(g.p25) - y(g.p75))}" rx="2" fill="${tint(g.punct, 0.38)}" stroke="${pctColor(g.punct)}"/>
        <line x1="${cx - bw / 2}" x2="${cx + bw / 2}" y1="${y(g.p50)}" y2="${y(g.p50)}" class="med"/></g>`;
    }
    s += `<text x="${cx}" y="${h - 6}" font-size="10" text-anchor="middle" class="lab">${n > 14 && i % 2 ? "" : esc(g.label)}</text>`;
  });
  return `${s}</svg>`;
}

/** Horizontal boxplots, one row per group, share of punctual trips at the right. */
export function hbox(w, groups, yMax) {
  const rowH = 20, lw = 26, rw = 40, h = groups.length * rowH + 16;
  const x = (v) => lw + ((Math.max(-1, Math.min(yMax, v)) + 1) / (yMax + 1)) * (w - lw - rw);
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="Verspätung je Wochentag">`;
  for (const t of ticks(yMax)) s += `<line x1="${x(t)}" x2="${x(t)}" y1="0" y2="${h - 14}" class="grid"/><text x="${x(t)}" y="${h - 3}" font-size="9" text-anchor="middle">${t}</text>`;
  s += `<line x1="${x(PUNCTUAL - 0.5)}" x2="${x(PUNCTUAL - 0.5)}" y1="0" y2="${h - 14}" class="limit"/>`;
  groups.forEach((g, i) => {
    const cy = i * rowH + rowH / 2;
    s += `<text x="${lw - 6}" y="${cy + 3.5}" font-size="10.5" text-anchor="end" class="lab strong">${esc(g.label)}</text>`;
    if (!g.m || g.p50 === null) return;
    s += `<g><title>${esc(tip(g, g.label))}</title><line x1="${x(g.p10)}" x2="${x(g.p90)}" y1="${cy}" y2="${cy}" class="whisk"/>
      <rect x="${x(g.p25)}" y="${cy - 6}" width="${Math.max(1.5, x(g.p75) - x(g.p25))}" height="12" rx="2" fill="${tint(g.punct, 0.38)}" stroke="${pctColor(g.punct)}"/>
      <line x1="${x(g.p50)}" x2="${x(g.p50)}" y1="${cy - 7}" y2="${cy + 7}" class="med"/>
      <text x="${w - 2}" y="${cy + 3.5}" font-size="10.5" text-anchor="end" class="strong">${Math.round(g.punct)} %</text></g>`;
  });
  return `${s}</svg>`;
}

/** Small boxplot for a table cell, fixed scale 0–30 min so rows are comparable. */
export function miniBox(g, w = 110, max = 30) {
  if (!g.m || g.p50 === null) return "";
  const h = 16, x = (v) => 2 + ((Math.max(-1, Math.min(max, v)) + 1) / (max + 1)) * (w - 4);
  return `<svg width="${w}" height="${h}" aria-hidden="true"><line x1="${x(PUNCTUAL - 0.5)}" x2="${x(PUNCTUAL - 0.5)}" y1="0" y2="${h}" class="limit"/>
    <line x1="${x(g.p10)}" x2="${x(g.p90)}" y1="8" y2="8" class="whisk"/>
    <rect x="${x(g.p25)}" y="3" width="${Math.max(2, x(g.p75) - x(g.p25))}" height="10" rx="2" fill="${tint(g.punct, 0.4)}" stroke="${pctColor(g.punct)}"/>
    <line x1="${x(g.p50)}" x2="${x(g.p50)}" y1="2" y2="14" class="med"/>${g.p90 > max ? `<text x="${w - 1}" y="12" font-size="9" text-anchor="end">›</text>` : ""}</svg>`;
}

/** Distribution like the server's buckets plus failures. */
export function hist(rs) {
  const s = summ(rs), n = s.m + s.failed;
  if (!n) return `<p class="muted small">Keine gemessenen Fahrten.</p>`;
  const c = [0, 0, 0, 0, 0];
  for (const r of rs) {
    const d = r.delay;
    if (d === null || d === undefined) continue;
    c[d < 1 ? 0 : d <= 5 ? 1 : d <= 15 ? 2 : d <= 30 ? 3 : 4]++;
  }
  const rows = [["pünktlich (0 min)", c[0], pctColor(100)], ["1–5 min", c[1], pctColor(92)], ["6–15 min", c[2], pctColor(75)],
    ["16–30 min", c[3], pctColor(62)], ["> 30 min", c[4], pctColor(50)], ["Ausfall / Ziel entfällt", s.failed, "var(--fail)"]];
  const mx = Math.max(...rows.map((r) => r[1])) || 1;
  return `<div class="hb">${rows.map(([l, v, col]) => `<span>${l}</span><i style="width:${((100 * v) / mx).toFixed(1)}%;background:${col}"></i><span class="v">${f1((100 * v) / n)} %</span>`).join("")}</div>`;
}
export function causeBars(list) {
  if (!list.length) return `<p class="muted small">Keine Gründe gemeldet. Für Daten vor der Umstellung auf OpenBoards gibt es keine Gründe.</p>`;
  const mx = list[0].n;
  return `<div class="hb hb-c">${list.map((c) => `<span title="${esc(c.text)}">${esc(c.text)}</span><i style="width:${((100 * c.n) / mx).toFixed(1)}%"></i><span class="v">${num(c.n)}</span>`).join("")}</div>`;
}

/** Calendar: one column per week, one row per weekday. dayStats[i] belongs to days[i]. */
export function calendar(days, dayStats, selD) {
  const maxDays = 371;
  const first = Math.max(0, days.length - maxDays);
  const shown = days.slice(first);
  if (!shown.length) return `<p class="muted small">Keine Tage im Zeitraum.</p>`;
  const off = shown[0].wd;
  const weeks = Math.ceil((shown.length + off) / 7);
  // days can have gaps (no trips) – place by real date distance, not by index
  const t0 = shown[0].date.getTime() - off * 86400000;
  const cells = new Map(shown.map((d, i) => [Math.round((d.date.getTime() - t0) / 86400000), first + i]));
  const nWeeks = Math.max(weeks, Math.ceil((Math.max(...cells.keys()) + 1) / 7));
  const fmt = new Intl.DateTimeFormat("de-DE", { day: "numeric", month: "numeric", timeZone: "UTC" });
  const fmtL = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "UTC" });
  let s = `<div class="cal" style="grid-template-columns:22px repeat(${nWeeks},minmax(${nWeeks > 20 ? 14 : 20}px,1fr))"><span></span>`;
  for (let k = 0; k < nWeeks; k++) s += `<span class="hl">${k % (nWeeks > 20 ? 4 : 1) === 0 ? fmt.format(new Date(t0 + k * 7 * 86400000)) : ""}</span>`;
  for (let wd = 0; wd < 7; wd++) {
    s += `<span class="rl">${WD[wd]}</span>`;
    for (let k = 0; k < nWeeks; k++) {
      const di = cells.get(k * 7 + wd);
      if (di === undefined) { s += `<span></span>`; continue; }
      const st = dayStats[di], d = days[di];
      const label = `${fmtL.format(d.date)}: ${st.m ? `${pct(Math.round(st.punct))} pünktlich, ${num(st.n)} Fahrten` : "keine Messung"}`;
      s += `<button type="button" class="c${selD === di ? " on" : ""}" data-d="${di}" style="background:${st.m ? tint(st.punct, 0.85) : "var(--surface-2)"}" title="${esc(label)}" aria-label="${esc(label)}">${nWeeks > 20 ? "" : d.date.getUTCDate()}</button>`;
    }
  }
  return `${s}</div>${first ? `<p class="muted small">Gezeigt werden die letzten ${maxDays} Tage.</p>` : ""}`;
}

export function dots(runs, n = 14) {
  const a = runs.slice(-n);
  const fmt = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "UTC" });
  let s = `<span class="dots" aria-label="Letzte ${a.length} Fahrten">`;
  for (let i = a.length; i < n; i++) s += `<i></i>`;
  for (const r of a) s += `<i class="${dotClass(r)}" title="${esc(`${fmt.format(r.date)}: ${runText(r)}`)}"></i>`;
  return `${s}</span>`;
}
export const DOT_LEGEND = `<div class="dotleg"><span><i class="o"></i>pünktlich</span><span><i class="w"></i>6–15 min</span><span><i class="b"></i>&gt; 15 min</span><span><i class="x"></i>Ausfall</span><span><i class="n"></i>ohne Messung</span></div>`;

// ---------- sortable table
/** cols: [{key, label, num?, asc?, sort?(row), render(row), tip?}]. Click on a header sorts, again reverses. */
export function sortTable(el, cfg) {
  const st = { key: cfg.key, dir: cfg.dir || "desc", open: null, limit: cfg.limit || Infinity };
  function sorted() {
    const c = cfg.cols.find((x) => x.key === st.key), sv = c.sort || ((r) => r[c.key]), m = st.dir === "asc" ? 1 : -1;
    return [...cfg.rows].sort((a, b) => {
      const x = sv(a), y = sv(b);
      if (x === null || x === undefined) return y === null || y === undefined ? 0 : 1;
      if (y === null || y === undefined) return -1;
      return (typeof x === "string" ? x.localeCompare(y, "de", { numeric: true }) : x - y) * m;
    });
  }
  function draw() {
    const rows = sorted(), shown = rows.slice(0, st.limit);
    let s = `<div class="tw"${cfg.maxH ? ` style="max-height:${cfg.maxH}px"` : ""}><table class="t"><thead><tr>`;
    for (const c of cfg.cols) {
      const as = c.key === st.key ? (st.dir === "asc" ? "ascending" : "descending") : null;
      s += `<th class="${c.num ? "r" : ""}"${as ? ` aria-sort="${as}"` : ""}><button type="button" data-k="${c.key}" title="${esc(c.tip || `Nach ${c.label} sortieren`)}">${esc(c.label)}</button></th>`;
    }
    s += `</tr></thead><tbody>`;
    if (!shown.length) s += `<tr><td colspan="${cfg.cols.length}" class="empty">${esc(cfg.empty || "Keine Einträge.")}</td></tr>`;
    shown.forEach((r, i) => {
      const id = String(cfg.id ? cfg.id(r) : i), open = st.open === id;
      s += `<tr data-id="${esc(id)}" class="${cfg.expand ? "click" : ""}${open ? " open" : ""}"${cfg.expand ? ` tabindex="0" aria-expanded="${open}"` : ""}>${cfg.cols.map((c) => `<td class="${c.num ? "r" : ""}">${c.render ? c.render(r) : esc(r[c.key] ?? "–")}</td>`).join("")}</tr>`;
      if (open && cfg.expand) s += `<tr class="detail"><td colspan="${cfg.cols.length}"><div data-exp></div></td></tr>`;
    });
    s += `</tbody></table></div>`;
    if (rows.length > shown.length) s += `<div class="tmore"><button class="btn" type="button" data-more>${num(rows.length - shown.length)} weitere anzeigen</button></div>`;
    el.innerHTML = s;
    if (st.open !== null && cfg.expand) {
      const box = el.querySelector("[data-exp]"), r = rows.find((x) => String(cfg.id(x)) === st.open);
      if (box && r) cfg.expand(box, r);
    }
  }
  el.onclick = (e) => {
    const b = e.target.closest("th button");
    if (b) {
      const k = b.dataset.k, c = cfg.cols.find((x) => x.key === k);
      if (st.key === k) st.dir = st.dir === "asc" ? "desc" : "asc";
      else { st.key = k; st.dir = c.num ? (c.asc ? "asc" : "desc") : "asc"; }
      draw(); el.querySelector(`th button[data-k="${k}"]`)?.focus();
      return;
    }
    if (e.target.closest("[data-more]")) { st.limit += cfg.limit || 100; draw(); return; }
    const tr = e.target.closest("tr[data-id]");
    if (tr && cfg.expand) { st.open = st.open === tr.dataset.id ? null : tr.dataset.id; draw(); }
  };
  el.onkeydown = (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr && cfg.expand && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); tr.click(); }
  };
  draw();
  return { set(rows) { cfg.rows = rows; st.limit = cfg.limit || Infinity; draw(); } };
}
