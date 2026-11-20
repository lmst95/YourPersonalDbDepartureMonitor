"""Collector process: the only component with API keys and write access.

* reads .env (reloads when the file changes) and mirrors ROUTES into the routes table
* live tick (LIVE_INTERVAL): departure boards at origins, arrival boards at destinations, around "now"
* finalize: once per day after FINALIZE_AT, and for every missing day (up to BACKFILL_DAYS back),
  the whole day is fetched again and written with final = 1
* geocodes configured stations without coordinates (OpenStreetMap Nominatim, GEOCODING=off disables)

Run:  python -m dbmonitor.collector
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import TZ
from .config import RouteSpec, Settings, StationRef, load_settings
from .openboards import OpenBoardsClient, OpenBoardsError
from .stations import Nominatim
from . import storage

log = logging.getLogger("dbmonitor.collector")

LIVE_BACK = dt.timedelta(minutes=60)
LIVE_AHEAD_DEP = dt.timedelta(minutes=90)
LIVE_AHEAD_ARR = dt.timedelta(minutes=180)
LATE_MARGIN = dt.timedelta(hours=4)     # board windows filter on actual time -> fetch longer
MAX_TRAVEL = dt.timedelta(hours=8)      # arrivals affect departures up to this much earlier


def _ep(t: dt.datetime) -> int:
    return int(t.timestamp())


class Collector:
    def __init__(self, env_path: Optional[Path] = None, db_path: Optional[str] = None):
        self.env_path = env_path
        self.settings: Settings = load_settings(env_path)
        self.db_path = db_path or self.settings.db_path
        self.conn = storage.connect(self.db_path)
        storage.init_db(self.conn)
        self.client = OpenBoardsClient(self.settings.client_id, self.settings.api_key)
        self.nominatim = Nominatim()
        self.next_live = 0.0
        self.stop = False

    # ------------------------------------------------------------ config
    def reload_if_changed(self) -> bool:
        path = self.settings.env_path
        mtime = path.stat().st_mtime if path and path.exists() else 0.0
        if mtime == self.settings.env_mtime:
            return False
        log.info(".env geändert – lade Konfiguration neu")
        self.settings = load_settings(self.env_path)
        self.client = OpenBoardsClient(self.settings.client_id, self.settings.api_key)
        return True

    def resolve(self, ref: StationRef) -> Optional[str]:
        """EVA for a configured station; ensures a stations row with stada and name exists."""
        c = self.conn
        if ref.eva:
            row = c.execute("SELECT * FROM stations WHERE eva = ?", (ref.eva,)).fetchone()
            if row is None or not row["stada"]:
                sp = None
                try:
                    sp = self.client.lookup_stop_place(ref.eva)
                except OpenBoardsError as e:
                    log.warning("EVA %s konnte nicht geprüft werden: %s", ref.eva, e)
                name = (sp or {}).get("name") or (row["name"] if row else None) or ref.label or ref.eva
                storage.upsert_station(c, ref.eva, name, (sp or {}).get("stada"))
                c.commit()
            return ref.eva
        # legacy: name only -> must already be known locally (exact name)
        rows = c.execute("SELECT eva FROM stations WHERE lower(name) = lower(?) ORDER BY stada IS NULL",
                         (ref.name,)).fetchall()
        if len(rows) == 1 or (rows and len({r["eva"] for r in rows}) == 1):
            return rows[0]["eva"]
        log.error("Bahnhof '%s' ist %s. Bitte die EVA-Nummer in ROUTES eintragen (dbmon add / dbmon search).",
                  ref.name, "nicht eindeutig" if rows else "unbekannt")
        return None

    def sync_routes(self) -> List[int]:
        """Mirror ROUTES (and STATIONS) into the database. Returns ids of routes that became active."""
        c = self.conn
        self.sync_stations()
        if not self.settings.routes_configured:
            log.warning("Keine ROUTES in der .env – behalte die Strecken aus der Datenbank. "
                        "Mit 'dbmon import-db' lassen sie sich in die .env übernehmen.")
            return []
        for err in self.settings.route_errors:
            log.error("Konfiguration: %s", err)
        wanted: Set[Tuple[str, str]] = set()
        unresolved = 0
        for spec in self.settings.routes:
            o, d = self.resolve(spec.origin), self.resolve(spec.dest)
            if o and d:
                wanted.add((o, d))
            else:
                unresolved += 1
        if self.settings.routes and not wanted:
            log.error("Keine der konfigurierten Strecken konnte aufgelöst werden – ändere nichts.")
            return []
        activated = []
        existing = {(r["origin_eva"], r["dest_eva"]): r for r in c.execute("SELECT * FROM routes")}
        for key in wanted:
            r = existing.get(key)
            if r is None:
                cur = c.execute("INSERT INTO routes (origin_eva, dest_eva, active) VALUES (?, ?, 1)", key)
                activated.append(cur.lastrowid)
            elif not r["active"]:
                c.execute("UPDATE routes SET active = 1 WHERE id = ?", (r["id"],))
                activated.append(r["id"])
        if unresolved or self.settings.route_errors:
            log.warning("%d Strecke(n) aus der .env nicht auflösbar – bestehende Strecken werden daher nicht "
                        "ausgeblendet. Bitte EVA-Nummern eintragen (dbmon add / dbmon import-db).",
                        unresolved or len(self.settings.route_errors))
        for key, r in existing.items():
            if unresolved or self.settings.route_errors:
                break
            if key not in wanted and r["active"]:
                c.execute("UPDATE routes SET active = 0 WHERE id = ?", (r["id"],))
                log.info("Strecke %s -> %s deaktiviert (Daten bleiben erhalten)", *key)
        c.commit()
        if activated:
            log.info("%d Strecke(n) aktiviert – berechne Zuordnung aus vorhandenen Daten", len(activated))
            storage.reclassify(c, 0, 2**31, route_ids=activated)
            for rid in activated:  # aggregates for the history that just became visible
                for d, final in storage._route_days_final(c, rid, 0, storage.day_bounds(dt.datetime.now(TZ).date())[0]).items():
                    if final:
                        storage.write_aggregates(c, "r", str(rid), d)
            c.commit()
        return activated

    def sync_stations(self) -> None:
        """Mirror STATIONS (stations observed on their own) into watched_stations."""
        c = self.conn
        wanted = {e for e in (self.resolve(ref) for ref in self.settings.stations) if e}
        existing = {r["eva"]: r["active"] for r in c.execute("SELECT eva, active FROM watched_stations")}
        for eva in wanted:
            if eva not in existing:
                c.execute("INSERT INTO watched_stations (eva) VALUES (?)", (eva,))
                log.info("Bahnhof %s wird beobachtet – die letzten Tage werden nachgeholt", eva)
            elif not existing[eva]:
                c.execute("UPDATE watched_stations SET active = 1 WHERE eva = ?", (eva,))
        for eva, active in existing.items():
            if active and eva not in wanted:
                c.execute("UPDATE watched_stations SET active = 0 WHERE eva = ?", (eva,))
                log.info("Bahnhof %s nicht mehr beobachtet (Daten bleiben erhalten)", eva)
        c.commit()

    def observed_stations(self) -> List[str]:
        return [r[0] for r in self.conn.execute("""
            SELECT origin_eva FROM routes WHERE active = 1 UNION SELECT dest_eva FROM routes WHERE active = 1
            UNION SELECT eva FROM watched_stations WHERE active = 1 ORDER BY 1""")]

    def boards(self) -> List[Tuple[str, str]]:
        # every observed station gets departures and arrivals (needed for the station monitor)
        evas = self.observed_stations()
        return [(e, "dep") for e in evas] + [(e, "arr") for e in evas]

    # ------------------------------------------------------------- sync
    def sync_board(self, eva: str, kind: str, q_start: dt.datetime, q_end: dt.datetime,
                   keep_start: dt.datetime, keep_end: dt.datetime, final: bool) -> int:
        entries, ctx = self.client.board_range(kind, eva, q_start, q_end)
        c = self.conn
        storage.upsert_stop_places(c, ctx["stopPlaces"])
        storage.upsert_cause_codes(c, ctx["causeCodes"])
        ks, ke, qe = _ep(keep_start), _ep(keep_end), _ep(q_end)
        events = [storage.event_from_entry(e, kind, eva, ctx["stopPlaces"], final) for e in entries]
        # No lower bound on the planned time: the board filters on the *actual* time, so a train that is
        # many hours late shows up in a later window (e.g. planned 19:00, departs 05:00 next day). Such
        # stragglers are kept – the finalization of the following day thereby also records them.
        # Upper bound: trains planned after the window belong to the next run.
        events = [ev for ev in events if ev["planned"] is not None and ev["planned"] < ke]
        storage.upsert_events(c, events)
        if final:
            ids = [storage.jid_for(c, ev["journey_id"]) for ev in events]
            # live rows that should have been in this board but are not (e.g. withdrawn extra trains).
            # Rows whose (predicted) time lies after the queried window are legitimately missing.
            c.execute(f"""DELETE FROM stop_events WHERE board_eva = ? AND kind = ? AND final = 0
                          AND iris = 0 AND planned >= ? AND planned < ?
                          AND COALESCE(actual, planned) < ?
                          AND jid NOT IN ({','.join('?' * len(ids)) or "-1"})""",
                      (eva, kind, ks, ke, qe, *ids))
            if kind == "dep":
                storage.drop_legacy_for_day(c, eva, ks, ke)
        first = min([ks] + [ev["planned"] for ev in events])
        storage.classify_station(c, eva, kind, first, ke)
        if kind == "dep":
            storage.reclassify(c, first, ke, origin_eva=eva)
        else:
            storage.reclassify(c, first - int(MAX_TRAVEL.total_seconds()), ke, dest_eva=eva)
        c.commit()
        return len(events)

    def live_tick(self) -> None:
        now = dt.datetime.now(TZ).replace(microsecond=0)
        n = 0
        for eva, kind in self.boards():
            ahead = LIVE_AHEAD_DEP if kind == "dep" else LIVE_AHEAD_ARR
            try:
                n += self.sync_board(eva, kind, now - LIVE_BACK, now + ahead,
                                     now - LIVE_BACK - LATE_MARGIN, now + ahead, final=False)
            except Exception as e:  # keep going with the other boards
                log.exception("Live-Abfrage %s %s fehlgeschlagen", kind, eva)
                storage.set_state(self.conn, "last_error", f"{now:%d.%m. %H:%M} live {kind} {eva}: {e}")
        storage.set_state(self.conn, "last_live", now.isoformat(timespec="seconds"))
        storage.set_state(self.conn, "last_data_update", now.isoformat(timespec="seconds"))
        log.info("Live: %d Halte aktualisiert, %d Requests gesamt", n, self.client.request_count)

    def pending_days(self) -> List[Tuple[str, str, dt.date]]:
        """Board-days not finalized yet, newest first. Yesterday only after FINALIZE_AT, so that late
        trains of the evening have run before the day is written as final."""
        now = dt.datetime.now(TZ)
        today = now.date()
        first = 1 if now.time() >= self.settings.finalize_at else 2
        days = [today - dt.timedelta(days=i) for i in range(first, self.settings.backfill_days + 1)]
        done = {(r["board_eva"], r["kind"], r["service_date"]) for r in self.conn.execute(
            "SELECT board_eva, kind, service_date FROM sync_state")}
        out = []
        for day in days:  # newest first
            for eva, kind in self.boards():
                if (eva, kind, day.isoformat()) not in done:
                    out.append((eva, kind, day))
        return out

    def finalize_day(self, eva: str, kind: str, day: dt.date) -> int:
        start = dt.datetime.combine(day, dt.time(0), TZ)
        end = start + dt.timedelta(days=1)
        now = dt.datetime.now(TZ)
        n = self.sync_board(eva, kind, start, min(end + LATE_MARGIN, now), start, end, final=True)
        self.conn.execute("INSERT OR REPLACE INTO sync_state (board_eva, kind, service_date, events) VALUES (?, ?, ?, ?)",
                          (eva, kind, day.isoformat(), n))
        storage.refresh_aggregates_after_finalize(self.conn, eva, kind, day)
        self.conn.commit()
        return n

    def finalize_pending(self, max_items: Optional[int] = None, interleave_live: bool = True) -> int:
        pending = self.pending_days()
        if not pending:
            return 0
        log.info("Abschluss: %d offene Tafel-Tage", len(pending))
        done = 0
        for eva, kind, day in pending[:max_items]:
            if self.stop:
                break
            try:
                n = self.finalize_day(eva, kind, day)
                done += 1
                log.info("Abgeschlossen %s %s %s: %d Halte", day, kind, eva, n)
            except Exception as e:
                log.exception("Abschluss %s %s %s fehlgeschlagen", day, kind, eva)
                storage.set_state(self.conn, "last_error", f"Abschluss {day} {kind} {eva}: {e}")
            if interleave_live:
                if self.reload_if_changed():
                    self.sync_routes()  # new routes become visible right away; their days follow later
                if time.time() >= self.next_live:
                    self.live_tick()
                    self.next_live = time.time() + self.settings.live_interval
                if done % 10 == 1:
                    self.geocode_missing(limit=10)  # map coordinates don't wait for the whole backfill
        storage.set_state(self.conn, "last_finalize", dt.datetime.now(TZ).isoformat(timespec="seconds"))
        return done

    def geocode_missing(self, limit: Optional[int] = None) -> int:
        """Coordinates for observed stations (first) and for the intermediate stops drawn on the map.
        limit: at most this many lookups (Nominatim allows ~1 per second); returns the number done."""
        if not self.settings.geocoding:
            return 0
        c = self.conn
        need: Dict[str, Optional[Tuple[str, str]]] = {e: None for e in self.observed_stations()}
        for r in c.execute("SELECT id, origin_eva, dest_eva FROM routes WHERE active = 1").fetchall():
            for eva in storage.route_geometry(c, r["id"])[1:-1]:
                need.setdefault(eva, (r["origin_eva"], r["dest_eva"]))
        if not need:
            return 0
        ph = ",".join("?" * len(need))
        rows = c.execute(f"""SELECT * FROM stations WHERE eva IN ({ph}) AND lat IS NULL
                             AND COALESCE(coords_source, '') <> 'not-found'""", tuple(need)).fetchall()
        rows = sorted(rows, key=lambda r: need.get(r["eva"]) is not None)[:limit]  # observed stations first
        for r in rows:
            if self.stop:
                break
            coords = self.nominatim.geocode(r["name"])
            ends = need.get(r["eva"])
            if coords and ends and not self._plausible(coords, ends):
                log.warning("Koordinaten für %s verworfen (liegen nicht zwischen den Endpunkten)", r["name"])
                coords = None
            if coords:
                storage.upsert_station(c, r["eva"], r["name"], None, coords[0], coords[1], "nominatim")
                log.info("Koordinaten für %s (%s) via Nominatim", r["name"], r["eva"])
            else:
                c.execute("UPDATE stations SET coords_source = 'not-found' WHERE eva = ?", (r["eva"],))
            c.commit()
        return len(rows)

    def _plausible(self, coords: Tuple[float, float], ends: Tuple[str, str]) -> bool:
        pts = [tuple(x) for x in self.conn.execute(
            "SELECT lat, lon FROM stations WHERE eva IN (?, ?) AND lat IS NOT NULL", ends).fetchall()]
        if len(pts) < 2:
            return True
        lat0, lat1 = sorted(p[0] for p in pts)
        lon0, lon1 = sorted(p[1] for p in pts)
        pad_lat = max(0.15, 0.3 * (lat1 - lat0))
        pad_lon = max(0.2, 0.3 * (lon1 - lon0))
        return lat0 - pad_lat <= coords[0] <= lat1 + pad_lat and lon0 - pad_lon <= coords[1] <= lon1 + pad_lon

    # ------------------------------------------------------------- loop
    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        self.sync_routes()
        storage.set_state(self.conn, "started", dt.datetime.now(TZ).isoformat(timespec="seconds"))
        while not self.stop:
            try:
                if self.reload_if_changed():
                    self.sync_routes()
                if time.time() >= self.next_live:
                    self.next_live = time.time() + self.settings.live_interval
                    self.live_tick()
                self.geocode_missing(limit=30)  # new stations appear on the map right away
                # finalize whatever is open: the daily run after FINALIZE_AT, gaps after downtime,
                # and the backfill of newly added stations – all handled the same way
                self.finalize_pending()
                self.geocode_missing()
            except Exception as e:
                log.exception("Fehler im Collector-Durchlauf")
                storage.set_state(self.conn, "last_error", f"{dt.datetime.now(TZ):%d.%m. %H:%M}: {e}")
            for _ in range(30):
                if self.stop:
                    break
                time.sleep(1)
        log.info("Collector beendet")


def main() -> None:
    ap = argparse.ArgumentParser(description="DB Departure Monitor – Collector")
    ap.add_argument("--env", type=Path, help="Pfad zur .env (Standard: .env im Projekt oder $DBMON_ENV)")
    ap.add_argument("--db", help="SQLite-Datei (Standard: DB_PATH aus .env)")
    ap.add_argument("--once", action="store_true", help="nur eine Live-Abfrage und offene Abschlüsse, dann Ende")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    col = Collector(args.env, args.db)
    if args.once:
        col.sync_routes()
        col.live_tick()
        col.finalize_pending(interleave_live=False)
        col.geocode_missing()
    else:
        col.run_forever()


if __name__ == "__main__":
    main()
