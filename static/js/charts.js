// Charts and analysis blocks shared by the route and the station page (same element ids on both pages).
import { esc, pct, mins, num, dateLong, pctColor, chartColors } from "./ui.js";

const WD = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const WD_ORDER = [1, 2, 3, 4, 5, 6, 0];
const WD_LONG = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"];
const $ = (sel) => document.querySelector(sel);
let chart = null;

export function renderHeat(rows) {
  const el = $("#heat"), info = $("#heat-info");
  if (!rows.length) { el.innerHTML = `<div class="empty">Keine Daten im Zeitraum</div>`; info.textContent = ""; return; }
  const hours = rows.map((r) => r.h);
  const hMin = Math.min(...hours), hMax = Math.max(...hours);
  const cell = new Map(rows.map((r) => [`${r.wd}-${r.h}`, r]));
  el.style.gridTemplateColumns = `auto repeat(${hMax - hMin + 1}, max-content)`;
  let html = `<span></span>`;
  for (let h = hMin; h <= hMax; h++) html += `<span class="hl">${h}</span>`;
  for (const wd of WD_ORDER) {
    html += `<span class="dl">${WD[wd]}</span>`;
    for (let h = hMin; h <= hMax; h++) {
      const c = cell.get(`${wd}-${h}`);
      if (!c || !c.n) { html += `<button type="button" class="none" disabled aria-label="${WD_LONG[wd]} ${h} Uhr: keine Fahrten"></button>`; continue; }
      const p = c.m ? (100 * c.p) / c.m : null;
      html += `<button type="button" data-k="${wd}-${h}" style="background:${pctColor(p)}" aria-label="${WD_LONG[wd]} ${h} Uhr: ${p === null ? "keine Messung" : Math.round(p) + " Prozent pünktlich"}">${p === null ? "" : Math.round(p)}</button>`;
    }
  }
  el.innerHTML = html;
  info.textContent = "";
  el.onclick = (e) => {
    const b = e.target.closest("button[data-k]");
    if (!b) return;
    el.querySelectorAll("button[aria-pressed]").forEach((x) => x.removeAttribute("aria-pressed"));
    b.setAttribute("aria-pressed", "true");
    const c = cell.get(b.dataset.k);
    const [wd, h] = b.dataset.k.split("-").map(Number);
    const p = c.m ? (100 * c.p) / c.m : null;
    info.innerHTML = `<strong>${WD_LONG[wd]}, ${h}–${h + 1} Uhr:</strong> ${pct(p === null ? null : Math.round(p * 10) / 10)} pünktlich · Ø ${mins(c.avg)} · ${num(c.n)} Fahrten${c.f ? ` · ${num(c.f)} Ausfälle` : ""}`;
  };
}

export function renderTrend(d) {
  const rows = d.trend;
  const c = chartColors();
  $("#trend-hint").textContent = d.trend_bucket === "week" ? "pro Woche" : "pro Tag";
  const labels = rows.map((r) => d.trend_bucket === "week" ? `KW ${r.b.slice(6)} (${dateLong(r.first_day).slice(0, 5)})` : dateLong(r.b).slice(0, 5));
  const data = {
    labels,
    datasets: [
      { type: "line", label: "Pünktlich (%)", data: rows.map((r) => (r.m ? Math.round((1000 * r.p) / r.m) / 10 : null)), borderColor: c.accent, backgroundColor: c.accent, yAxisID: "y", tension: .25, pointRadius: rows.length > 60 ? 0 : 2.5, spanGaps: true, borderWidth: 2.5 },
      { type: "bar", label: "Ausfälle", data: rows.map((r) => r.f), backgroundColor: c.fail + "99", yAxisID: "y2", borderRadius: 3, maxBarThickness: 18 },
    ],
  };
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { color: c.text, boxWidth: 12 } },
      tooltip: { callbacks: { afterBody: (items) => { const r = rows[items[0].dataIndex]; return `Ø Verspätung: ${mins(r.avg)}\nFahrten: ${num(r.n)}`; } } } },
    scales: {
      x: { ticks: { color: c.text, maxRotation: 0, autoSkipPadding: 12 }, grid: { display: false } },
      y: { min: 0, max: 100, ticks: { color: c.text, callback: (v) => `${v} %` }, grid: { color: c.grid } },
      y2: { position: "right", beginAtZero: true, ticks: { color: c.text, precision: 0 }, grid: { display: false } },
    },
  };
  if (chart) chart.destroy();
  chart = new Chart($("#trend"), { data, options });
}

export function renderDist(d) {
  const x = d.distribution;
  const parts = [
    ["pünktlich (0 min)", x.d0, "var(--ok)"], ["1–5 min", x.d1, "#91cf60"], ["6–15 min", x.d6, "#fdae61"],
    ["16–30 min", x.d16, "#f46d43"], ["> 30 min", x.d31, "#d73027"], ["Ausfall / Ziel entfällt", x.failed, "var(--fail)"],
  ];
  const total = parts.reduce((s, p) => s + (p[1] || 0), 0);
  $("#dist").innerHTML = total ? parts.map(([l, n, col]) => n ? `<span style="width:${(100 * n) / total}%;background:${col}" title="${esc(l)}: ${n}"></span>` : "").join("") : "";
  $("#dist-legend").innerHTML = parts.map(([l, n, col]) => `<div><span class="dot" style="background:${col}"></span><span>${esc(l)}</span><span class="muted tabular" style="margin-left:auto">${total ? Math.round((100 * (n || 0)) / total) : 0} %</span></div>`).join("");
}

export function renderCauses(rows, period) {
  const el = $("#causes");
  if (!rows.length) { el.innerHTML = `<li class="muted small">Keine Gründe gemeldet${period === "all" ? "" : " – für ältere Daten liegen keine Gründe vor"}.</li>`; return; }
  const max = rows[0].n;
  el.innerHTML = rows.map((r) => `<li><div class="top"><span>${esc(r.text)}</span><span class="n">${num(r.n)}×</span></div><div class="bar"><span style="width:${(100 * r.n) / max}%"></span></div></li>`).join("");
}

export function renderTrains(rows) {
  const el = $("#trains");
  if (!rows.length) { el.innerHTML = `<li class="muted small">Zu wenige Fahrten im Zeitraum.</li>`; return; }
  el.innerHTML = rows.map((r) => {
    const bad = r.n ? (100 * (r.m - r.p + r.f)) / r.n : 0;
    const name = `${r.category || ""} ${r.number || ""}`.trim();
    return `<li><div class="top"><span><span class="train">${esc(r.line && r.line !== r.number ? r.line : r.category)}</span> ${esc(name)} <span class="muted small">meist ${esc(r.usual_time)}</span></span>
      <span class="n">${Math.round(bad)} % unpünktlich/aus · Ø ${mins(r.avg)} · ${num(r.n)} Fahrten</span></div>
      <div class="bar"><span style="width:${bad}%;background:${pctColor(100 - bad)}"></span></div></li>`;
  }).join("");
}

