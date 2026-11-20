// Shared helpers. Every value coming from the API is inserted via esc() or textContent.

export const PUNCTUAL = 6;

export function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export const $ = (sel, root = document) => root.querySelector(sel);

export async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  let data = null;
  try { data = await r.json(); } catch { /* ignore */ }
  if (!r.ok) throw new Error((data && data.error) || `Fehler ${r.status}`);
  return data;
}

export function errorBox(el, err) {
  el.innerHTML = `<div class="notice">Daten konnten nicht geladen werden: ${esc(err.message || err)}</div>`;
}

// ---------- time & numbers
const tf = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin" });
const df = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", timeZone: "Europe/Berlin" });
const dfl = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "Europe/Berlin" });
export const time = (iso) => (iso ? tf.format(new Date(iso)) : "–");
export const day = (iso) => (iso ? df.format(new Date(iso)) : "–");
export const dateLong = (iso) => (iso ? dfl.format(new Date(iso)) : "–");
export const num = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString("de-DE"));
export const pct = (n) => (n === null || n === undefined ? "–" : `${Number(n).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`);
export const mins = (n) => (n === null || n === undefined ? "–" : `${Number(n).toLocaleString("de-DE", { maximumFractionDigits: 1 })} min`);

export function relTime(iso) {
  if (!iso) return "noch keine Daten";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "gerade eben";
  if (s < 3600) return `vor ${Math.round(s / 60)} min`;
  if (s < 86400) return `vor ${Math.round(s / 3600)} h`;
  return dateLong(iso);
}

export function pctClass(p) {
  if (p === null || p === undefined) return "";
  return p >= 90 ? "good" : p >= 75 ? "mid" : "poor";
}

// colour scale for punctuality (also used on the map); red → yellow → green
export function pctColor(p) {
  if (p === null || p === undefined || Number.isNaN(p)) return "#9aa1ad";
  const stops = [[50, [215, 48, 31]], [70, [252, 141, 89]], [82, [254, 224, 139]], [92, [145, 207, 96]], [100, [26, 152, 80]]];
  if (p <= stops[0][0]) return rgb(stops[0][1]);
  for (let i = 1; i < stops.length; i++) {
    if (p <= stops[i][0]) {
      const [a, ca] = stops[i - 1], [b, cb] = stops[i];
      const t = (p - a) / (b - a);
      return rgb(ca.map((v, k) => Math.round(v + (cb[k] - v) * t)));
    }
  }
  return rgb(stops.at(-1)[1]);
}
const rgb = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;

// ---------- trip presentation
export const STATUS = {
  ok: { label: "", cls: "" },
  cancelled: { label: "Fällt aus", cls: "b-fail" },
  partial: { label: "Ziel entfällt", cls: "b-fail" },
  sev: { label: "Ersatzverkehr", cls: "b-sev" },
  replaced: { label: "Ersatzzug", cls: "b-sev" },
  replacement: { label: "Ersatzfahrt", cls: "b-muted" },
  unknown: { label: "Keine Ist-Daten", cls: "b-muted" },
};

export function delayBadge(trip) {
  const st = trip.status;
  if (st && st !== "ok" && st !== "replaced") {
    const s = STATUS[st] || STATUS.unknown;
    return `<span class="badge ${s.cls}">${esc(s.label)}</span>`;
  }
  const d = trip.delay_min;
  if (d === null || d === undefined) return `<span class="badge b-muted">?</span>`;
  const est = trip.time_type && trip.time_type !== "REAL";
  const cls = d < 1 ? "b-ok" : d < PUNCTUAL ? "b-ok" : d <= 15 ? "b-warn" : "b-bad";
  const txt = d <= 0 ? (d < 0 ? `${d}` : "pünktlich") : `+${d}`;
  const title = est ? "Prognose" : "tatsächliche Abfahrt";
  return `<span class="badge ${cls}" title="${title}">${est ? "≈ " : ""}${esc(txt)}</span>`;
}

export function trainLabel(t) {
  const name = t.line && !String(t.line).match(/^\d+$/) && t.category !== "ICE" && t.category !== "IC"
    ? `${t.line}` : `${t.category || ""} ${t.number || ""}`.trim();
  return `<span class="train" title="${esc(`${t.category || ""} ${t.number || ""}`)}">${esc(name)}</span>`;
}

export function platform(t) {
  if (!t.platform && !t.planned_platform) return "";
  const changed = t.platform_changed && t.platform !== t.planned_platform;
  return `<span class="plat ${changed ? "changed" : ""}">Gl. ${esc(t.platform || t.planned_platform)}${changed ? ` <span class="muted">(statt ${esc(t.planned_platform)})</span>` : ""}</span>`;
}

export function tripRow(t, { past = false, showDate = false } = {}) {
  const shifted = t.dep_actual && t.delay_min && t.status !== "cancelled" && t.status !== "partial";
  const flags = [];
  if (t.status === "replaced" && t.replacement_label) flags.push(`<span class="flag">Ersatz: ${esc(t.replacement_label)}</span>`);
  if (t.status === "cancelled" && t.replacement_label) flags.push(`<span class="flag">Ersatz: ${esc(t.replacement_label)}</span>`);
  if (t.extra) flags.push(`<span class="flag">Zusatzfahrt</span>`);
  const ad = t.arr_delay_min;
  const arr = t.arr_planned ? `Ankunft ${time(t.arr_planned)}${ad !== null && ad !== undefined ? ` (${ad > 0 ? `+${ad}` : ad < 0 ? ad : "±0"})` : ""}` : "";
  return `<li class="row ${past ? "past" : ""}">
    <div class="t">${shifted ? `<s>${time(t.planned)}</s><span class="rt">${time(t.dep_actual)}</span>` : time(t.planned)}</div>
    <div class="main">${trainLabel(t)} ${flags.join(" ")}</div>
    <div class="right">${delayBadge(t)}</div>
    <div class="meta">${showDate ? `<span>${esc(day(t.planned))}</span>` : ""}${platform(t)}${arr ? `<span>${esc(arr)}</span>` : ""}</div>
  </li>`;
}

// ---------- small inline sparkline (no library)
export function sparkline(points, { w = 120, h = 34 } = {}) {
  const vals = points.map((p) => p.p).filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return `<svg class="spark" width="${w}" height="${h}" aria-hidden="true"></svg>`;
  const min = Math.min(60, ...vals), max = 100;
  const step = w / (points.length - 1);
  let d = "", started = false;
  points.forEach((p, i) => {
    if (p.p === null || p.p === undefined) { started = false; return; }
    const x = i * step, y = h - 3 - ((p.p - min) / (max - min)) * (h - 6);
    d += `${started ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    started = true;
  });
  const last = vals.at(-1);
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Pünktlichkeit der letzten 30 Tage">
    <path d="${d}" fill="none" stroke="${pctColor(last)}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

// ---------- URL state
export function getParams() { return Object.fromEntries(new URL(location.href).searchParams); }
export function setParams(obj) {
  const url = new URL(location.href);
  for (const [k, v] of Object.entries(obj)) { if (v === null || v === undefined || v === "") url.searchParams.delete(k); else url.searchParams.set(k, v); }
  history.replaceState(null, "", url);
}

export const PERIOD_CHOICES = [["1d", "24 h"], ["7d", "7 Tage"], ["30d", "30 Tage"], ["90d", "90 Tage"], ["365d", "12 Monate"], ["all", "Alles"]];

export function periodChips(el, current, onChange, { custom = false } = {}) {
  el.innerHTML = PERIOD_CHOICES.map(([v, l]) => `<button type="button" class="chip" data-v="${v}" aria-pressed="${v === current}">${l}</button>`).join("")
    + (custom ? `<button type="button" class="chip" data-v="custom" aria-pressed="${current === "custom"}">Zeitraum…</button>` : "");
  el.onclick = (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    onChange(b.dataset.v);
  };
}

export function setUpdated(iso) {
  const el = document.getElementById("updated");
  if (el) el.textContent = iso ? `Daten ${relTime(iso)}` : "";
}

export function chartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  return { text: v("--text-2"), grid: v("--border"), accent: v("--accent"), bad: v("--bad"), ok: v("--ok"), warn: v("--warn"), fail: v("--fail") };
}

// ---------- text search (case/umlaut insensitive, all words must match)
export const normText = (s) => (s || "").toLocaleLowerCase("de").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
export function textMatches(haystack, query) {
  const words = normText(query).split(/\s+/).filter(Boolean);
  const hay = normText(haystack);
  return words.every((w) => hay.includes(w));
}
