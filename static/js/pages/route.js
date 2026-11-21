// Route page: cockpit (see cockpit-page.js) + reverse direction link, live chip and the full trip list.
import { api, esc, errorBox, pct, num, tripRow, setUpdated, time, $ } from "../ui.js";
import { createCockpit } from "../cockpit-page.js";

const routeId = Number(location.pathname.split("/").pop());
const trips = { status: "", q: "", offset: 0, loaded: false };

const cockpit = createCockpit({
  fetch: (range) => api(`/api/routes/${routeId}/samples`, range),
  analyse: (q) => `/analyse?${new URLSearchParams({ route: String(routeId), ...q })}`,
  onData(d) {
    const r = d.route;
    document.title = `${r.origin_name} → ${r.dest_name} – Pendelmonitor`;
    $("#title").innerHTML = `<a href="/station/${encodeURIComponent(r.origin_eva)}">${esc(r.origin_name)}</a><span class="arrow">→</span><a href="/station/${encodeURIComponent(r.dest_eva)}">${esc(r.dest_name)}</a>`;
    const rev = $("#reverse");
    if (r.reverse_id) {
      rev.classList.remove("hidden");
      rev.href = `/route/${r.reverse_id}${location.search}`;
      const rp = d.reverse_kpis?.punctuality;
      rev.textContent = `⇄ Gegenrichtung${rp !== null && rp !== undefined ? ` · ${pct(rp)}` : ""}`;
    }
    $("#analyse-link").href = `/analyse?${new URLSearchParams({ route: String(routeId), ...cockpit.range() })}`;
    loadLiveChip();
  },
  afterLoad() { if (trips.loaded || $("#trips-box").open) loadTrips(); },
});

// ---------- all single trips (loaded when opened)
const FILTERS = [["", "Alle"], ["late", "Verspätet"], ["failed", "Ausfälle"], ["replaced", "Ersatzzug"], ["sev", "Bus-Ersatz"], ["unknown", "Ohne Ist-Daten"]];
async function loadTrips(append = false) {
  if (!append) trips.offset = 0;
  const list = $("#trips");
  try {
    const d = await api(`/api/routes/${routeId}/trips`, { ...cockpit.range(), status: trips.status, q: trips.q, limit: 50, offset: trips.offset });
    const html = d.trips.map((t) => tripRow(t, { showDate: true })).join("");
    if (append) list.insertAdjacentHTML("beforeend", html);
    else list.innerHTML = html || `<li class="empty">Keine Fahrten gefunden.</li>`;
    trips.offset += d.trips.length;
    $("#trips-total").textContent = `${num(d.total)} Fahrten`;
    $("#more").classList.toggle("hidden", trips.offset >= d.total);
    trips.loaded = true;
  } catch (e) { errorBox(list, e); }
}

// ---------- live chip (the board itself lives on /live)
async function loadLiveChip() {
  const raw = cockpit.state.raw;
  if (!raw) return;
  try {
    const d = await api(`/api/routes/${routeId}/live`);
    const t = d.next[0], el = $("#livechip");
    el.href = `/live?eva=${encodeURIComponent(raw.route.origin_eva)}`;
    if (!t) el.innerHTML = `<i></i>Live-Tafel`;
    else {
      const late = t.status === "cancelled" || t.status === "partial" ? `<em>fällt aus</em>` : t.delay_min > 0 ? `<em>+${t.delay_min}</em>` : "";
      el.innerHTML = `<i></i>Live: nächster Zug <b>${time(t.planned)}</b> ${late}`;
    }
    el.classList.remove("hidden");
  } catch { /* chip is optional */ }
}

$("#trip-filter").innerHTML = FILTERS.map(([v, l]) => `<button type="button" class="chip" data-v="${v}" aria-pressed="${v === trips.status}">${l}</button>`).join("");
$("#trip-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $("#trip-filter").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  trips.status = b.dataset.v; loadTrips();
});
let qTimer;
$("#trip-q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { trips.q = e.target.value.trim(); loadTrips(); }, 300); });
$("#more").addEventListener("click", () => loadTrips(true));
$("#trips-box").addEventListener("toggle", () => { if ($("#trips-box").open && !trips.loaded) loadTrips(); });

cockpit.load();
api("/api/health").then((h) => setUpdated(h.last_update)).catch(() => {});
setInterval(() => { if (document.visibilityState === "visible") loadLiveChip(); }, 60000);
