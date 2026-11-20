"""Database migrations.

legacy_to_v2  old Timetables/IRIS database (routes + departures) -> schema 2 (keeps departures_legacy)
v2_to_v3      schema 2 -> schema 3 (integer journey ids, unix times, compact path, clustered tables,
              aggregates). Lossless and self-checking: the new tables are built next to the old ones,
              every row is converted back and compared with the original, and only if all rows match are
              the tables swapped in one transaction. On any difference nothing is changed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

from . import TZ, storage

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    pass


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS stations (
    eva TEXT PRIMARY KEY, stada TEXT, name TEXT NOT NULL, lat REAL, lon REAL, coords_source TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_stations_stada ON stations (stada);
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, origin_eva TEXT NOT NULL, dest_eva TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE (origin_eva, dest_eva));
CREATE TABLE IF NOT EXISTS stop_events (
    journey_id TEXT NOT NULL, board_eva TEXT NOT NULL, kind TEXT NOT NULL, stop_eva TEXT, category TEXT, number TEXT,
    line TEXT, transport_type TEXT, planned TEXT NOT NULL, actual TEXT, time_type TEXT, delay_min INTEGER,
    planned_platform TEXT, platform TEXT, platform_history TEXT, cancelled INTEGER NOT NULL DEFAULT 0,
    additional INTEGER NOT NULL DEFAULT 0, journey_cancelled INTEGER NOT NULL DEFAULT 0,
    no_passenger_change INTEGER NOT NULL DEFAULT 0, journey_type TEXT, sev_real_type TEXT, replaces_journey_id TEXT,
    replaced_by_journey_id TEXT, replaced_by_label TEXT, coupled_group TEXT, dest_eva TEXT, origin_eva TEXT,
    cause_codes TEXT, path TEXT, final INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'openboards',
    st_status TEXT, st_delay INTEGER, st_real INTEGER, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (journey_id, board_eva, kind));
CREATE INDEX IF NOT EXISTS idx_events_board_time ON stop_events (board_eva, kind, planned);
CREATE TABLE IF NOT EXISTS route_trips (
    route_id INTEGER NOT NULL, journey_id TEXT NOT NULL, planned TEXT NOT NULL, category TEXT, number TEXT, line TEXT,
    status TEXT NOT NULL, time_type TEXT, dep_actual TEXT, delay_min INTEGER, arr_planned TEXT, arr_actual TEXT,
    arr_delay_min INTEGER, planned_platform TEXT, platform TEXT, platform_changed INTEGER NOT NULL DEFAULT 0,
    extra INTEGER NOT NULL DEFAULT 0, coupled_group TEXT, replacement_label TEXT, cause_codes TEXT,
    final INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'openboards', PRIMARY KEY (route_id, journey_id));
CREATE INDEX IF NOT EXISTS idx_trips_route_time ON route_trips (route_id, planned);
CREATE TABLE IF NOT EXISTS watched_stations (
    eva TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sync_state (
    board_eva TEXT NOT NULL, kind TEXT NOT NULL, service_date TEXT NOT NULL,
    finalized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, events INTEGER, PRIMARY KEY (board_eva, kind, service_date));
CREATE TABLE IF NOT EXISTS cause_codes (code TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS collector_state (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ----------------------------------------------------------------------------- legacy -> v2
def legacy_to_v2(conn: sqlite3.Connection) -> None:
    log.info("Alte Datenbank (Timetables/IRIS) gefunden – übernehme die Daten")
    with conn:
        conn.execute("ALTER TABLE routes RENAME TO routes_legacy")
        conn.execute("ALTER TABLE departures RENAME TO departures_legacy")
        conn.executescript(SCHEMA_V2)
        conn.execute("""
            INSERT OR IGNORE INTO stations (eva, name, lat, lon, coords_source)
            SELECT origin_eva, origin_name, origin_lat, origin_lon, 'legacy' FROM routes_legacy
            UNION SELECT dest_eva, dest_name, dest_lat, dest_lon, 'legacy' FROM routes_legacy""")
        conn.execute("INSERT INTO routes (id, origin_eva, dest_eva, active) SELECT id, origin_eva, dest_eva, 1 FROM routes_legacy")
        status_expr = "d.status" if "status" in _columns(conn, "departures_legacy") else "NULL"
        conn.execute(f"""
            INSERT OR IGNORE INTO stop_events (journey_id, board_eva, kind, stop_eva, category, number, planned, actual,
                time_type, delay_min, planned_platform, platform, cancelled, final, source)
            SELECT 'iris:' || d.service_id, r.origin_eva, 'dep', r.origin_eva, d.category, d.number, d.planned_dt,
                   d.realtime_dt, CASE WHEN d.realtime_dt IS NULL THEN 'SCHEDULE' ELSE 'REAL' END, d.delay_min,
                   d.planned_platform, d.realtime_platform, CASE WHEN {status_expr} = 'c' THEN 1 ELSE 0 END, 1, 'iris'
            FROM departures_legacy d JOIN routes_legacy r ON r.id = d.route_id""")
        conn.execute(f"""
            INSERT OR IGNORE INTO route_trips (route_id, journey_id, planned, category, number, status, time_type,
                dep_actual, delay_min, planned_platform, platform, platform_changed, final, source)
            SELECT d.route_id, 'iris:' || d.service_id, d.planned_dt, d.category, d.number,
                   CASE WHEN {status_expr} = 'c' THEN 'cancelled' WHEN {status_expr} = 'p' THEN 'partial'
                        WHEN d.realtime_dt IS NULL THEN 'unknown' ELSE 'ok' END,
                   CASE WHEN d.realtime_dt IS NULL THEN 'SCHEDULE' ELSE 'REAL' END,
                   d.realtime_dt, d.delay_min, d.planned_platform, d.realtime_platform,
                   CASE WHEN d.realtime_platform IS NOT NULL AND d.planned_platform IS NOT NULL
                             AND d.realtime_platform <> d.planned_platform THEN 1 ELSE 0 END, 1, 'iris'
            FROM departures_legacy d""")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '2')")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('legacy_migrated_at', ?)",
                     (dt.datetime.now(TZ).isoformat(timespec="seconds"),))
    log.info("Altdaten übernommen (Original bleibt als departures_legacy erhalten)")


# ----------------------------------------------------------------------------- v2 -> v3
def _utc_ts_to_epoch(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    return int(dt.datetime.fromisoformat(v).replace(tzinfo=dt.timezone.utc).timestamp())


def _same_instant(a: Optional[str], epoch: Optional[int]) -> bool:
    if a is None or epoch is None:
        return a is None and epoch is None
    return storage.to_epoch(a) == epoch and storage.iso_local(epoch) is not None


def _json_list(v: Optional[str]) -> List[Any]:
    return json.loads(v) if v else []


V3_NEW = storage.SCHEMA.replace("CREATE TABLE IF NOT EXISTS stop_events (", "CREATE TABLE stop_events_new (") \
    .replace("CREATE TABLE IF NOT EXISTS route_trips (", "CREATE TABLE route_trips_new (") \
    .replace("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_journey ON stop_events (jid, board_eva, kind);", "")


def v2_to_v3(conn: sqlite3.Connection) -> None:
    t0 = time.monotonic()
    log.info("Datenbank-Umbau auf Schema 3 (kompakt, sortiert, Tageszusammenfassungen) – Daten werden geprüft")
    have = _columns(conn, "stop_events")
    for col, typ in (("origin_eva", "TEXT"), ("st_status", "TEXT"), ("st_delay", "INTEGER"), ("st_real", "INTEGER")):
        if col not in have:  # very early v2 builds
            conn.execute(f"ALTER TABLE stop_events ADD COLUMN {col} {typ}")
    conn.commit()
    for t in ("stop_events_new", "route_trips_new", "journeys"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(V3_NEW)  # journeys, agg_*, *_new (sync_state, stations, … exist already)

    stadas = storage.stada_map(conn)
    cache: Dict[str, int] = {}
    try:
        n_ev = _convert_events(conn, stadas, cache)
        n_tr = _convert_trips(conn, cache)
        _check_events(conn, stadas)
        _check_trips(conn)
    except Exception:
        conn.rollback()
        for t in ("stop_events_new", "route_trips_new", "journeys", "agg_hour", "agg_line", "agg_cause", "agg_done"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        log.exception("Umbau abgebrochen – die Datenbank ist unverändert")
        raise
    conn.commit()

    # swap in one transaction
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE stop_events")
        conn.execute("DROP TABLE route_trips")
        conn.execute("ALTER TABLE stop_events_new RENAME TO stop_events")
        conn.execute("ALTER TABLE route_trips_new RENAME TO route_trips")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_journey ON stop_events (jid, board_eva, kind)")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (storage.SCHEMA_VERSION,))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('v3_migrated_at', ?)",
                     (dt.datetime.now(TZ).isoformat(timespec="seconds"),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""

    missing = conn.execute("SELECT DISTINCT board_eva, kind FROM stop_events WHERE iris = 0 AND st_status IS NULL").fetchall()
    for b in missing:
        storage.classify_station(conn, b["board_eva"], b["kind"], 0, 2**31)
    conn.commit()
    log.info("Tageszusammenfassungen werden berechnet …")
    storage.rebuild_aggregates(conn)
    log.info("Speicher wird freigegeben (VACUUM) …")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log.info("Umbau fertig in %.0f s: %d Halte, %d Streckenfahrten – alle Zeilen geprüft, keine Abweichung",
             time.monotonic() - t0, n_ev, n_tr)


def _convert_events(conn: sqlite3.Connection, stadas, cache) -> int:
    cols = storage._EVENT_COLS + ["st_status", "st_delay", "st_real"]
    sql = f"INSERT INTO stop_events_new ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    batch, n = [], 0
    for r in conn.execute("SELECT * FROM stop_events ORDER BY board_eva, kind, planned").fetchall():
        r = dict(r)
        row = {
            "board_eva": r["board_eva"], "kind": r["kind"], "planned": storage.to_epoch(r["planned"]),
            "jid": storage.jid_for(conn, r["journey_id"], cache),
            "stop_eva": storage.encode_stop_eva(r["stop_eva"], r["board_eva"]),
            "category": r["category"], "number": r["number"], "line": r["line"], "transport_type": r["transport_type"],
            "actual": storage.to_epoch(r["actual"]), "time_type": r["time_type"], "delay_min": r["delay_min"],
            "planned_platform": r["planned_platform"], "platform": r["platform"],
            "platform_history": r["platform_history"], "cancelled": r["cancelled"], "additional": r["additional"],
            "journey_cancelled": r["journey_cancelled"], "no_passenger_change": r["no_passenger_change"],
            "journey_type": r["journey_type"], "sev_real_type": r["sev_real_type"],
            "replaces_jid": storage.jid_for(conn, r["replaces_journey_id"], cache),
            "replaced_by_jid": storage.jid_for(conn, r["replaced_by_journey_id"], cache),
            "replaced_by_label": r["replaced_by_label"],
            "coupled_jid": storage.jid_for(conn, r["coupled_group"], cache),
            "dest_eva": r["dest_eva"], "origin_eva": r.get("origin_eva"),
            "cause_codes": storage.encode_causes(_json_list(r["cause_codes"])),
            "path": storage.encode_path(json.loads(r["path"]), stadas) if r["path"] is not None else None,
            "final": r["final"], "iris": 1 if r["source"] == "iris" else 0,
            "updated_at": _utc_ts_to_epoch(r["updated_at"]),
            "st_status": r.get("st_status"), "st_delay": r.get("st_delay"), "st_real": r.get("st_real"),
        }
        batch.append(row)
        if len(batch) >= 5000:
            conn.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    conn.executemany(sql, batch)
    return n + len(batch)


def _convert_trips(conn: sqlite3.Connection, cache) -> int:
    sql = """INSERT INTO route_trips_new (route_id, planned, jid, category, number, line, status, time_type, dep_actual,
             delay_min, arr_planned, arr_actual, arr_delay_min, planned_platform, platform, platform_changed, extra,
             coupled_jid, replacement_label, cause_codes, final, iris)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    batch, n = [], 0
    for r in conn.execute("SELECT * FROM route_trips ORDER BY route_id, planned").fetchall():
        batch.append((
            r["route_id"], storage.to_epoch(r["planned"]), storage.jid_for(conn, r["journey_id"], cache), r["category"],
            r["number"], r["line"], r["status"], r["time_type"], storage.to_epoch(r["dep_actual"]), r["delay_min"],
            storage.to_epoch(r["arr_planned"]), storage.to_epoch(r["arr_actual"]), r["arr_delay_min"],
            r["planned_platform"], r["platform"], r["platform_changed"], r["extra"],
            storage.jid_for(conn, r["coupled_group"], cache), r["replacement_label"],
            storage.encode_causes(_json_list(r["cause_codes"])), r["final"], 1 if r["source"] == "iris" else 0))
        if len(batch) >= 5000:
            conn.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    conn.executemany(sql, batch)
    return n + len(batch)


def _fail(what: str, old: Any, new: Any) -> None:
    raise MigrationError(f"Abweichung bei {what}: alt={old!r} neu={new!r}")


def _check_events(conn: sqlite3.Connection, stadas) -> None:
    """Every converted row back to the old representation and compared field by field."""
    ext = {r[0]: r[1] for r in conn.execute("SELECT jid, ext FROM journeys")}
    old_n = conn.execute("SELECT COUNT(*) FROM stop_events").fetchone()[0]
    new_n = conn.execute("SELECT COUNT(*) FROM stop_events_new").fetchone()[0]
    if old_n != new_n:
        _fail("Anzahl Halte", old_n, new_n)
    new_rows = {}
    for r in conn.execute("SELECT * FROM stop_events_new"):
        new_rows[(ext[r["jid"]], r["board_eva"], r["kind"])] = r
    same = ["category", "number", "line", "transport_type", "time_type", "delay_min", "planned_platform", "platform",
            "platform_history", "cancelled", "additional", "journey_cancelled", "no_passenger_change", "journey_type",
            "sev_real_type", "replaced_by_label", "dest_eva", "origin_eva", "final", "st_status", "st_delay", "st_real"]
    for o in conn.execute("SELECT * FROM stop_events"):
        key = (o["journey_id"], o["board_eva"], o["kind"])
        n = new_rows.get(key)
        if n is None:
            _fail("Halt fehlt", key, None)
        for c in same:
            if o[c] != n[c]:
                _fail(f"{key} {c}", o[c], n[c])
        if not _same_instant(o["planned"], n["planned"]) or not _same_instant(o["actual"], n["actual"]):
            _fail(f"{key} Zeit", (o["planned"], o["actual"]), (n["planned"], n["actual"]))
        if storage.decode_stop_eva(n["stop_eva"], n["board_eva"]) != o["stop_eva"]:
            _fail(f"{key} stop_eva", o["stop_eva"], n["stop_eva"])
        for oc, nc in (("replaces_journey_id", "replaces_jid"), ("replaced_by_journey_id", "replaced_by_jid"),
                       ("coupled_group", "coupled_jid")):
            if o[oc] != (ext[n[nc]] if n[nc] is not None else None):
                _fail(f"{key} {oc}", o[oc], n[nc])
        if _json_list(o["cause_codes"]) != storage.decode_causes(n["cause_codes"]):
            _fail(f"{key} cause_codes", o["cause_codes"], n["cause_codes"])
        old_path = json.loads(o["path"]) if o["path"] is not None else None
        if old_path != storage.decode_path(n["path"], stadas):
            _fail(f"{key} path", o["path"], n["path"])
        if ("iris" if n["iris"] else "openboards") != o["source"]:
            _fail(f"{key} source", o["source"], n["iris"])
        if _utc_ts_to_epoch(o["updated_at"]) != n["updated_at"]:
            _fail(f"{key} updated_at", o["updated_at"], n["updated_at"])


def _check_trips(conn: sqlite3.Connection) -> None:
    ext = {r[0]: r[1] for r in conn.execute("SELECT jid, ext FROM journeys")}
    old_n = conn.execute("SELECT COUNT(*) FROM route_trips").fetchone()[0]
    new_n = conn.execute("SELECT COUNT(*) FROM route_trips_new").fetchone()[0]
    if old_n != new_n:
        _fail("Anzahl Streckenfahrten", old_n, new_n)
    new_rows = {(r["route_id"], ext[r["jid"]]): r for r in conn.execute("SELECT * FROM route_trips_new")}
    same = ["category", "number", "line", "status", "time_type", "delay_min", "arr_delay_min", "planned_platform",
            "platform", "platform_changed", "extra", "replacement_label", "final"]
    for o in conn.execute("SELECT * FROM route_trips"):
        key = (o["route_id"], o["journey_id"])
        n = new_rows.get(key)
        if n is None:
            _fail("Streckenfahrt fehlt", key, None)
        for c in same:
            if o[c] != n[c]:
                _fail(f"{key} {c}", o[c], n[c])
        for c, nc in (("planned", "planned"), ("dep_actual", "dep_actual"), ("arr_planned", "arr_planned"),
                      ("arr_actual", "arr_actual")):
            if not _same_instant(o[c], n[nc]):
                _fail(f"{key} {c}", o[c], n[nc])
        if o["coupled_group"] != (ext[n["coupled_jid"]] if n["coupled_jid"] is not None else None):
            _fail(f"{key} coupled_group", o["coupled_group"], n["coupled_jid"])
        if _json_list(o["cause_codes"]) != storage.decode_causes(n["cause_codes"]):
            _fail(f"{key} cause_codes", o["cause_codes"], n["cause_codes"])
        if ("iris" if n["iris"] else "openboards") != o["source"]:
            _fail(f"{key} source", o["source"], n["iris"])
