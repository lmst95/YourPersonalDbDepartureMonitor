"""SQLite storage (schema 3): compact, clustered tables plus daily aggregates.

Tables
  journeys        journey id (OpenBoards journeyId / 'iris:<service_id>') <-> small integer jid
  stations        every stop place seen (eva, stada, name, coordinates)
  routes          mirror of ROUTES from .env (active = currently configured)
  watched_stations mirror of STATIONS from .env
  stop_events     one row per journey and observed board (departures / arrivals), clustered by
                  (board_eva, kind, planned) – WITHOUT ROWID
  route_trips     result per route and journey incl. status, clustered by (route_id, planned)
  agg_hour / agg_line / agg_cause / agg_done
                  daily aggregates per scope ('r' route, 'd' station departures, 'a' station arrivals)
  sync_state, cause_codes, collector_state, meta

Representation (all lossless):
  * times are Unix seconds; local time is always Europe/Berlin (iso_local() restores the ISO string)
  * path: "8001921,8006643x,8000096" – stop EVA + flags (x cancelled, a additional); the stada comes from
    `stations`, it is only written as ":<stada>" (or ":" for none) where it differs from that table
  * cause codes: "43,31"; stop_eva is NULL when equal to board_eva and '' when none was given
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import TZ
from .openboards import parse_ts, stop_place_name

log = logging.getLogger(__name__)

SCHEMA_VERSION = "3"
PUNCTUAL_LIMIT = 6        # DB definition: less than 6 minutes late
COUNTED = ("ok", "replaced")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS journeys (
    jid INTEGER PRIMARY KEY,
    ext TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS stations (
    eva        TEXT PRIMARY KEY,
    stada      TEXT,
    name       TEXT NOT NULL,
    lat        REAL,
    lon        REAL,
    coords_source TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stations_stada ON stations (stada);

CREATE TABLE IF NOT EXISTS routes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_eva TEXT NOT NULL,
    dest_eva   TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (origin_eva, dest_eva)
);

CREATE TABLE IF NOT EXISTS watched_stations (
    eva        TEXT PRIMARY KEY,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stop_events (
    board_eva       TEXT NOT NULL,     -- queried board (observed station)
    kind            TEXT NOT NULL,     -- dep | arr
    planned         INTEGER NOT NULL,  -- unix seconds
    jid             INTEGER NOT NULL,  -- journeys.jid
    stop_eva        TEXT,              -- actual stop place if different from board_eva (Hbf tief, SEV bus stop)
    category        TEXT,
    number          TEXT,
    line            TEXT,
    transport_type  TEXT,
    actual          INTEGER,           -- best known time
    time_type       TEXT,              -- SCHEDULE | PREVIEW | REAL
    delay_min       INTEGER,
    planned_platform TEXT,
    platform        TEXT,
    platform_history TEXT,
    cancelled       INTEGER NOT NULL DEFAULT 0,
    additional      INTEGER NOT NULL DEFAULT 0,
    journey_cancelled INTEGER NOT NULL DEFAULT 0,
    no_passenger_change INTEGER NOT NULL DEFAULT 0,
    journey_type    TEXT,
    sev_real_type   TEXT,
    replaces_jid    INTEGER,
    replaced_by_jid INTEGER,
    replaced_by_label TEXT,
    coupled_jid     INTEGER,           -- smallest journey of a coupled train (travelsWith)
    dest_eva        TEXT,
    origin_eva      TEXT,
    cause_codes     TEXT,              -- "43,31"
    path            TEXT,              -- compact stop list (departures only)
    final           INTEGER NOT NULL DEFAULT 0,
    iris            INTEGER NOT NULL DEFAULT 0,   -- 1 = migrated legacy (Timetables/IRIS) data
    st_status       TEXT,              -- station view: ok|cancelled|replaced|replacement|sev|unknown|ignore
    st_delay        INTEGER,
    st_real         INTEGER,
    updated_at      INTEGER,
    PRIMARY KEY (board_eva, kind, planned, jid)
) WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_journey ON stop_events (jid, board_eva, kind);

CREATE TABLE IF NOT EXISTS route_trips (
    route_id        INTEGER NOT NULL,
    planned         INTEGER NOT NULL,
    jid             INTEGER NOT NULL,
    category        TEXT,
    number          TEXT,
    line            TEXT,
    status          TEXT NOT NULL,     -- ok|cancelled|partial|sev|replaced|replacement|unknown
    time_type       TEXT,
    dep_actual      INTEGER,
    delay_min       INTEGER,           -- departure delay from the passenger's view
    arr_planned     INTEGER,
    arr_actual      INTEGER,
    arr_delay_min   INTEGER,
    planned_platform TEXT,
    platform        TEXT,
    platform_changed INTEGER NOT NULL DEFAULT 0,
    extra           INTEGER NOT NULL DEFAULT 0,
    coupled_jid     INTEGER,
    replacement_label TEXT,
    cause_codes     TEXT,
    final           INTEGER NOT NULL DEFAULT 0,
    iris            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (route_id, planned, jid)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS agg_hour (
    scope TEXT NOT NULL, key TEXT NOT NULL, day INTEGER NOT NULL, hour INTEGER NOT NULL,
    planned INTEGER NOT NULL DEFAULT 0, measured INTEGER NOT NULL DEFAULT 0, punctual INTEGER NOT NULL DEFAULT 0,
    sum_delay INTEGER NOT NULL DEFAULT 0, max_delay INTEGER,
    d0 INTEGER NOT NULL DEFAULT 0, d1 INTEGER NOT NULL DEFAULT 0, d6 INTEGER NOT NULL DEFAULT 0,
    d16 INTEGER NOT NULL DEFAULT 0, d31 INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0, partial INTEGER NOT NULL DEFAULT 0, replaced INTEGER NOT NULL DEFAULT 0,
    sev INTEGER NOT NULL DEFAULT 0, unknown INTEGER NOT NULL DEFAULT 0, extra INTEGER NOT NULL DEFAULT 0,
    platform_changed INTEGER NOT NULL DEFAULT 0,
    arr_measured INTEGER NOT NULL DEFAULT 0, arr_punctual INTEGER NOT NULL DEFAULT 0, arr_sum_delay INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key, day, hour)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS agg_line (
    scope TEXT NOT NULL, key TEXT NOT NULL, day INTEGER NOT NULL, line TEXT NOT NULL, dir_eva TEXT NOT NULL,
    n INTEGER NOT NULL, m INTEGER NOT NULL, p INTEGER NOT NULL, f INTEGER NOT NULL, sum_delay INTEGER NOT NULL,
    PRIMARY KEY (scope, key, day, line, dir_eva)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS agg_cause (
    scope TEXT NOT NULL, key TEXT NOT NULL, day INTEGER NOT NULL, code TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (scope, key, day, code)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS agg_done (
    scope TEXT NOT NULL, key TEXT NOT NULL, day INTEGER NOT NULL,
    PRIMARY KEY (scope, key, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sync_state (
    board_eva    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    service_date TEXT NOT NULL,
    finalized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    events       INTEGER,
    PRIMARY KEY (board_eva, kind, service_date)
);

CREATE TABLE IF NOT EXISTS cause_codes (code TEXT PRIMARY KEY, text TEXT);

CREATE TABLE IF NOT EXISTS collector_state (
    key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

AGG_HOUR_FIELDS = ["planned", "measured", "punctual", "sum_delay", "max_delay", "d0", "d1", "d6", "d16", "d31",
                   "cancelled", "partial", "replaced", "sev", "unknown", "extra", "platform_changed",
                   "arr_measured", "arr_punctual", "arr_sum_delay"]


# ----------------------------------------------------------------- time helpers
def to_epoch(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    t = parse_ts(value) if isinstance(value, str) else value
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    return int(t.timestamp())


def iso_local(value: Any) -> Optional[str]:
    """Unix seconds / ISO string / datetime -> local ISO string with offset (Europe/Berlin)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        t = dt.datetime.fromtimestamp(int(value), TZ)
    else:
        t = parse_ts(value) if isinstance(value, str) else value
        if t is None:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=TZ)
    return t.astimezone(TZ).isoformat(timespec="seconds")


def local_dt(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(epoch), TZ)


def day_key(epoch: int) -> int:
    d = local_dt(epoch)
    return d.year * 10000 + d.month * 100 + d.day


def key_to_date(key: int) -> dt.date:
    return dt.date(key // 10000, key // 100 % 100, key % 100)


def day_bounds(day: dt.date) -> Tuple[int, int]:
    start = dt.datetime.combine(day, dt.time(0), TZ)
    return int(start.timestamp()), int((start + dt.timedelta(days=1)).timestamp())


def minutes_between(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None or b is None:
        return None
    return int((b - a) // 60)


# ----------------------------------------------------------------- compact encodings
def encode_path(path: Optional[List[Dict[str, Any]]], station_stada: Dict[str, Optional[str]]) -> Optional[str]:
    if path is None:
        return None
    parts = []
    for s in path:
        tok = s.get("e") or ""
        if s.get("x"):
            tok += "x"
        if s.get("a"):
            tok += "a"
        stada = s.get("s")
        if stada != station_stada.get(s.get("e")):
            tok += ":" + (stada or "")
        parts.append(tok)
    return ",".join(parts)


def decode_path(text: Optional[str], station_stada: Dict[str, Optional[str]]) -> Optional[List[Dict[str, Any]]]:
    if text is None:
        return None
    if text == "":
        return []
    out = []
    for tok in text.split(","):
        stada_override = None
        has_override = ":" in tok
        if has_override:
            tok, stada_override = tok.split(":", 1)
        flags = ""
        while tok and tok[-1] in "xa":
            flags = tok[-1] + flags
            tok = tok[:-1]
        eva = tok or None
        s: Dict[str, Any] = {"e": eva, "s": (stada_override or None) if has_override else station_stada.get(eva)}
        if "x" in flags:
            s["x"] = 1
        if "a" in flags:
            s["a"] = 1
        out.append(s)
    return out


def encode_stop_eva(stop_eva: Optional[str], board_eva: str) -> Optional[str]:
    """NULL = same as the board, '' = no stop place given, otherwise the stop place."""
    if stop_eva == board_eva:
        return None
    return "" if stop_eva is None else stop_eva


def decode_stop_eva(value: Optional[str], board_eva: str) -> Optional[str]:
    if value is None:
        return board_eva
    return value or None


def encode_causes(codes: Optional[Sequence[str]]) -> Optional[str]:
    return ",".join(codes) if codes else None


def decode_causes(text: Optional[str]) -> List[str]:
    return text.split(",") if text else []


# ----------------------------------------------------------------- connection / schema
def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if path == ":memory:":
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    p = Path(path).resolve()
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(str(p), timeout=60)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA journal_size_limit = 67108864")   # truncate the WAL file to <= 64 MB after checkpoints
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA cache_size = -32000")   # 32 MB page cache
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db(conn: sqlite3.Connection) -> None:
    """Create schema 3, migrating older databases in place (legacy IRIS -> 2 -> 3)."""
    tables = _tables(conn)
    if "departures" in tables and "service_id" in _columns(conn, "departures"):
        from . import migrate
        migrate.legacy_to_v2(conn)
        tables = _tables(conn)
    if "stop_events" in tables and "journey_id" in _columns(conn, "stop_events"):
        from . import migrate
        migrate.v2_to_v3(conn)
        return
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.commit()


# ----------------------------------------------------------------- journeys
def jid_for(conn: sqlite3.Connection, ext: Optional[str], cache: Optional[Dict[str, int]] = None) -> Optional[int]:
    if not ext:
        return None
    if cache is not None and ext in cache:
        return cache[ext]
    row = conn.execute("SELECT jid FROM journeys WHERE ext = ?", (ext,)).fetchone()
    jid = row[0] if row else conn.execute("INSERT INTO journeys (ext) VALUES (?)", (ext,)).lastrowid
    if cache is not None:
        cache[ext] = jid
    return jid


# ----------------------------------------------------------------- state
def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO collector_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                 (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value))
    conn.commit()


def get_state(conn: sqlite3.Connection) -> Dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM collector_state")}


# -------------------------------------------------------------- stations
def upsert_stop_places(conn: sqlite3.Connection, stop_places: Dict[str, Dict[str, Any]]) -> None:
    rows = [(eva, sp.get("stada"), stop_place_name(sp)) for eva, sp in stop_places.items()]
    conn.executemany("""
        INSERT INTO stations (eva, stada, name) VALUES (?, ?, ?)
        ON CONFLICT(eva) DO UPDATE SET
            stada = COALESCE(excluded.stada, stations.stada),
            name = CASE WHEN stations.coords_source = 'legacy' AND stations.stada IS NULL
                        THEN excluded.name ELSE COALESCE(stations.name, excluded.name) END,
            updated_at = CURRENT_TIMESTAMP
    """, rows)


def upsert_station(conn: sqlite3.Connection, eva: str, name: str, stada: Optional[str] = None,
                   lat: Optional[float] = None, lon: Optional[float] = None, source: Optional[str] = None) -> None:
    conn.execute("""
        INSERT INTO stations (eva, stada, name, lat, lon, coords_source) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(eva) DO UPDATE SET
            stada = COALESCE(excluded.stada, stations.stada),
            name = COALESCE(excluded.name, stations.name),
            lat = COALESCE(excluded.lat, stations.lat),
            lon = COALESCE(excluded.lon, stations.lon),
            coords_source = COALESCE(excluded.coords_source, stations.coords_source),
            updated_at = CURRENT_TIMESTAMP
    """, (eva, stada, name, lat, lon, source if lat is not None else None))


def stada_map(conn: sqlite3.Connection) -> Dict[str, Optional[str]]:
    return {r[0]: r[1] for r in conn.execute("SELECT eva, stada FROM stations")}


def station_group(conn: sqlite3.Connection, eva: str) -> Tuple[Optional[str], Set[str]]:
    """(stada, all evas of the same station) – Hbf and Hbf (tief) share a stada."""
    row = conn.execute("SELECT stada FROM stations WHERE eva = ?", (eva,)).fetchone()
    stada = row["stada"] if row else None
    if not stada:
        return None, {eva}
    evas = {r["eva"] for r in conn.execute("SELECT eva FROM stations WHERE stada = ?", (stada,))}
    return stada, evas | {eva}


# ----------------------------------------------------------- stop events
def event_from_entry(e: Dict[str, Any], kind: str, board_eva: str, stop_places: Dict[str, Dict[str, Any]],
                     final: bool) -> Dict[str, Any]:
    """OpenBoards entry -> event dict (external ids; converted to jids by upsert_events)."""
    tr = e.get("transport") or {}
    ji = e.get("journeyInfo") or {}
    planned, actual = to_epoch(e.get("timeSchedule")), to_epoch(e.get("time"))
    ids = [e.get("journeyId")] + [t.get("journeyID") for t in e.get("travelsWith") or [] if t.get("journeyID")]
    repl_by = (e.get("replacedBy") or [{}])[0]
    repl_for = (e.get("replacementFor") or [{}])[0]
    hist = [p.get("name") if isinstance(p, dict) else str(p) for p in e.get("platformHistory") or []]

    path = None
    if kind == "dep":
        def stop(eva: Optional[str], cancelled: Any, additional: Any = False) -> Dict[str, Any]:
            s = {"e": eva, "s": (stop_places.get(eva) or {}).get("stada")}
            if cancelled:
                s["x"] = 1
            if additional:
                s["a"] = 1
            return s
        path = [stop(v.get("evaNumber"), v.get("cancelled"), v.get("additional")) for v in tr.get("via") or []]
        dest = ji.get("destination") or {}
        if dest.get("evaNumber") and (not path or path[-1]["e"] != dest["evaNumber"]):
            path.append(stop(dest.get("evaNumber"), dest.get("cancelled")))

    return {
        "journey_id": e.get("journeyId"),
        "board_eva": board_eva,
        "kind": kind,
        "stop_eva": e.get("evaNumber"),
        "category": tr.get("category"),
        "number": str(tr.get("journeyNumber")) if tr.get("journeyNumber") is not None else None,
        "line": (tr.get("line") or {}).get("name"),
        "transport_type": tr.get("type"),
        "planned": planned,
        "actual": actual,
        "time_type": e.get("timeType"),
        "delay_min": minutes_between(planned, actual),
        "planned_platform": (e.get("platformSchedule") or {}).get("name"),
        "platform": (e.get("platform") or {}).get("name"),
        "platform_history": json.dumps(hist) if hist else None,
        "cancelled": int(bool(e.get("cancelled"))),
        "additional": int(bool(e.get("additional"))),
        "journey_cancelled": int(bool(ji.get("journeyCancelled"))),
        "no_passenger_change": int(bool(e.get("noPassengerChange"))),
        "journey_type": ji.get("type"),
        "sev_real_type": (tr.get("replacementTransport") or {}).get("realType"),
        "replaces_journey_id": repl_for.get("journeyID"),
        "replaced_by_journey_id": repl_by.get("journeyID"),
        "replaced_by_label": " ".join(str(x) for x in (repl_by.get("category"), repl_by.get("journeyNumber")) if x) or None,
        "coupled_group": min(i for i in ids if i) if len(ids) > 1 else None,
        "dest_eva": (ji.get("destination") or {}).get("evaNumber"),
        "origin_eva": (ji.get("origin") or {}).get("evaNumber"),
        "cause_codes": list(e.get("_causeCodes") or []),
        "path": path,
        "final": int(final),
    }


_EVENT_COLS = [
    "board_eva", "kind", "planned", "jid", "stop_eva", "category", "number", "line", "transport_type", "actual",
    "time_type", "delay_min", "planned_platform", "platform", "platform_history", "cancelled", "additional",
    "journey_cancelled", "no_passenger_change", "journey_type", "sev_real_type", "replaces_jid", "replaced_by_jid",
    "replaced_by_label", "coupled_jid", "dest_eva", "origin_eva", "cause_codes", "path", "final", "iris",
    "updated_at",
]


def event_row(conn: sqlite3.Connection, ev: Dict[str, Any], stadas: Dict[str, Optional[str]],
              cache: Optional[Dict[str, int]] = None, iris: int = 0) -> Dict[str, Any]:
    """Event dict (external ids, path list) -> stop_events row (jids, compact encodings)."""
    row = {c: ev.get(c) for c in _EVENT_COLS}
    row["jid"] = jid_for(conn, ev["journey_id"], cache)
    row["replaces_jid"] = jid_for(conn, ev.get("replaces_journey_id"), cache)
    row["replaced_by_jid"] = jid_for(conn, ev.get("replaced_by_journey_id"), cache)
    row["coupled_jid"] = jid_for(conn, ev.get("coupled_group"), cache)
    row["stop_eva"] = encode_stop_eva(ev.get("stop_eva"), ev["board_eva"])
    row["cause_codes"] = encode_causes(ev.get("cause_codes"))
    row["path"] = encode_path(ev.get("path"), stadas)
    row["iris"] = iris
    row["updated_at"] = int(dt.datetime.now(TZ).timestamp())
    return row


def upsert_events(conn: sqlite3.Connection, events: Iterable[Dict[str, Any]]) -> int:
    """Insert/update events. Rows already finalized are only overwritten by final data."""
    events = [ev for ev in events if ev.get("journey_id") and ev.get("planned") is not None]
    if not events:
        return 0
    stadas = stada_map(conn)
    cache: Dict[str, int] = {}
    cols = ", ".join(_EVENT_COLS)
    ph = ", ".join(f":{c}" for c in _EVENT_COLS)
    upd = ", ".join(f"{c} = excluded.{c}" for c in _EVENT_COLS if c not in ("jid", "board_eva", "kind"))
    sql = f"""
        INSERT INTO stop_events ({cols}) VALUES ({ph})
        ON CONFLICT(jid, board_eva, kind) DO UPDATE SET {upd}
        WHERE stop_events.final = 0 OR excluded.final = 1
    """
    for ev in events:
        conn.execute(sql, event_row(conn, ev, stadas, cache))
    return len(events)


def upsert_cause_codes(conn: sqlite3.Connection, codes: Dict[str, str]) -> None:
    conn.executemany("INSERT OR REPLACE INTO cause_codes (code, text) VALUES (?, ?)",
                     [(c, t) for c, t in codes.items() if t])


# ------------------------------------------------------ route classification
def _find_dest(path: List[Dict[str, Any]], dest_stada: Optional[str], dest_evas: Set[str]) -> Optional[Dict[str, Any]]:
    for s in path:
        if (dest_stada and s.get("s") == dest_stada) or s.get("e") in dest_evas:
            return s
    return None


def classify_route(conn: sqlite3.Connection, route: sqlite3.Row, start: int, end: int,
                   stadas: Optional[Dict[str, Optional[str]]] = None) -> int:
    """(Re)build route_trips for one route and departures planned in [start, end).

    Status rules (docs/DESIGN_OPENBOARDS.md, "Sonderfälle"):
      replacement  this journey replaces another journey of the route -> shown, not counted
      cancelled    departure cancelled / whole journey cancelled / no passenger change at origin
      replaced     cancelled, a replacement train serves the route (delay = replacement actual - original plan)
      sev          rail replacement bus (itself, or the cancelled train was replaced by one)
      partial      departs, but the destination stop is cancelled (via, journey end or arrival board)
      unknown      finalized without real time although not cancelled
      ok           otherwise
    Cancelled additional stops ("zurückgenommener Zusatzhalt") are ignored entirely.
    """
    stadas = stadas if stadas is not None else stada_map(conn)
    dest_stada, dest_evas = station_group(conn, route["dest_eva"])
    origin = route["origin_eva"]
    events = conn.execute("""SELECT * FROM stop_events WHERE board_eva = ? AND kind = 'dep' AND iris = 0
                             AND planned >= ? AND planned < ?""", (origin, start, end)).fetchall()
    by_id = {e["jid"]: e for e in events}

    def dep_event(jid: Optional[int]) -> Optional[sqlite3.Row]:
        if jid is None:
            return None
        if jid in by_id:
            return by_id[jid]
        return conn.execute("SELECT * FROM stop_events WHERE jid = ? AND board_eva = ? AND kind = 'dep'",
                            (jid, origin)).fetchone()

    arr_rows: Dict[int, sqlite3.Row] = {}
    if events:
        wanted = {e["jid"] for e in events} | {e["replaced_by_jid"] for e in events if e["replaced_by_jid"]}
        q = ",".join("?" * len(dest_evas))
        for a in conn.execute(f"""SELECT * FROM stop_events WHERE kind = 'arr' AND board_eva IN ({q})
                                  AND jid IN ({','.join('?' * len(wanted))})""", (*dest_evas, *wanted)):
            arr_rows[a["jid"]] = a

    path_cache: Dict[int, Optional[Dict[str, Any]]] = {}

    def serves(ev: sqlite3.Row) -> Optional[Dict[str, Any]]:
        if ev["jid"] not in path_cache:
            path = decode_path(ev["path"], stadas) or []
            path_cache[ev["jid"]] = _find_dest(path, dest_stada, dest_evas)
        return path_cache[ev["jid"]]

    rows = []
    for ev in events:
        if ev["cancelled"] and ev["additional"]:
            continue
        dest_stop = serves(ev)
        if dest_stop is None:
            continue
        status, effective, replacement_label = "ok", ev, None
        replaces = dep_event(ev["replaces_jid"])
        if replaces is not None and serves(replaces) is not None:
            status = "replacement"
        elif ev["cancelled"] or ev["journey_cancelled"] or ev["no_passenger_change"]:
            status = "cancelled"
            repl = dep_event(ev["replaced_by_jid"])
            if repl is not None and not repl["cancelled"] and serves(repl) is not None:
                status = "sev" if repl["sev_real_type"] else "replaced"
                effective = repl
                replacement_label = " ".join(x for x in (repl["category"], repl["number"]) if x)
            elif ev["replaced_by_label"]:
                replacement_label = ev["replaced_by_label"]
        elif dest_stop.get("x"):
            status = "partial"
        elif ev["sev_real_type"]:
            status = "sev"

        arr = arr_rows.get(effective["jid"])
        orig_arr = arr_rows.get(ev["jid"])
        if status in ("ok", "sev") and arr is not None and (arr["cancelled"] or arr["no_passenger_change"]):
            status = "partial"
        if status == "ok" and ev["final"] and ev["time_type"] != "REAL":
            status = "unknown"

        delay = minutes_between(ev["planned"], effective["actual"]) if status in COUNTED else None
        base_arr = orig_arr if orig_arr is not None else arr
        arr_planned = base_arr["planned"] if base_arr is not None else None
        arr_actual = arr["actual"] if arr is not None and not arr["cancelled"] else None
        arr_delay = minutes_between(arr_planned, arr_actual) if status in COUNTED and arr is not None \
            and arr["time_type"] == "REAL" else None
        rows.append((
            route["id"], ev["planned"], ev["jid"], ev["category"], ev["number"], ev["line"], status,
            effective["time_type"], effective["actual"], delay, arr_planned, arr_actual, arr_delay,
            ev["planned_platform"], effective["platform"],
            int(bool(ev["platform"] and ev["planned_platform"] and ev["platform"] != ev["planned_platform"])),
            int(ev["journey_type"] in ("RELIEF", "EXTRA") or bool(ev["additional"])),
            ev["coupled_jid"], replacement_label, ev["cause_codes"], int(ev["final"]),
        ))

    conn.execute("DELETE FROM route_trips WHERE route_id = ? AND iris = 0 AND planned >= ? AND planned < ?",
                 (route["id"], start, end))
    conn.executemany("""
        INSERT OR REPLACE INTO route_trips (
            route_id, planned, jid, category, number, line, status, time_type, dep_actual, delay_min,
            arr_planned, arr_actual, arr_delay_min, planned_platform, platform, platform_changed, extra,
            coupled_jid, replacement_label, cause_codes, final, iris)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, rows)
    return len(rows)


def routes_for(conn: sqlite3.Connection, origin_eva: Optional[str] = None, dest_eva: Optional[str] = None,
               route_ids: Optional[List[int]] = None) -> List[sqlite3.Row]:
    q, params = "SELECT * FROM routes WHERE 1=1", []
    if origin_eva:
        q += " AND origin_eva = ?"
        params.append(origin_eva)
    if dest_eva:
        _, evas = station_group(conn, dest_eva)
        q += f" AND dest_eva IN ({','.join('?' * len(evas))})"
        params += list(evas)
    if route_ids:
        q += f" AND id IN ({','.join('?' * len(route_ids))})"
        params += route_ids
    return conn.execute(q, params).fetchall()


def reclassify(conn: sqlite3.Connection, start: int, end: int, origin_eva: Optional[str] = None,
               dest_eva: Optional[str] = None, route_ids: Optional[List[int]] = None) -> int:
    stadas = stada_map(conn)
    return sum(classify_route(conn, r, start, end, stadas) for r in routes_for(conn, origin_eva, dest_eva, route_ids))


def drop_legacy_for_day(conn: sqlite3.Connection, board_eva: str, start: int, end: int) -> None:
    """OpenBoards data for this origin/day is final -> legacy IRIS rows of the same period are superseded."""
    conn.execute("DELETE FROM stop_events WHERE board_eva = ? AND kind = 'dep' AND planned >= ? AND planned < ? "
                 "AND iris = 1", (board_eva, start, end))
    conn.execute("""DELETE FROM route_trips WHERE iris = 1 AND planned >= ? AND planned < ?
                    AND route_id IN (SELECT id FROM routes WHERE origin_eva = ?)""", (start, end, board_eva))


# ------------------------------------------------------ station view
def classify_station(conn: sqlite3.Connection, board_eva: str, kind: str, start: int, end: int) -> int:
    """Status of every train at one observed station (all trains, not only those of a route).

    ignore       cancelled additional stop (was never part of the timetable)
    replacement  replaces another train at this station -> shown, not counted twice
    replaced     cancelled, a replacement train runs (delay = replacement actual - original plan)
    sev          rail replacement bus, or cancelled train replaced by a bus
    cancelled    stop cancelled or no passenger change
    unknown      finalized without real time
    ok           otherwise
    """
    rows = conn.execute("""SELECT * FROM stop_events WHERE board_eva = ? AND kind = ? AND iris = 0
                           AND planned >= ? AND planned < ?""", (board_eva, kind, start, end)).fetchall()
    by_id = {r["jid"]: r for r in rows}

    def other(jid: Optional[int]) -> Optional[sqlite3.Row]:
        if jid is None:
            return None
        return by_id.get(jid) or conn.execute(
            "SELECT * FROM stop_events WHERE jid = ? AND board_eva = ? AND kind = ?", (jid, board_eva, kind)).fetchone()

    out = []
    for ev in rows:
        status, delay, real = "ok", ev["delay_min"], ev["time_type"] == "REAL"
        if ev["cancelled"] and ev["additional"]:
            status, delay, real = "ignore", None, False
        elif other(ev["replaces_jid"]) is not None:
            status, delay, real = "replacement", None, False
        elif ev["cancelled"] or ev["no_passenger_change"]:
            status, delay, real = "cancelled", None, False
            repl = other(ev["replaced_by_jid"])
            if repl is not None and not repl["cancelled"]:
                if repl["sev_real_type"]:
                    status = "sev"
                else:
                    status = "replaced"
                    real = repl["time_type"] == "REAL"
                    delay = minutes_between(ev["planned"], repl["actual"])
        elif ev["sev_real_type"]:
            status, delay, real = "sev", None, False
        elif ev["final"] and not real:
            status, delay = "unknown", None
        out.append((status, delay, int(bool(real)), ev["jid"], board_eva, kind))
    conn.executemany("UPDATE stop_events SET st_status = ?, st_delay = ?, st_real = ? "
                     "WHERE jid = ? AND board_eva = ? AND kind = ?", out)
    return len(out)


# ------------------------------------------------------ aggregation (shared by collector and web)
def trip_rows(conn: sqlite3.Connection, scope: str, key: str, start: int, end: int) -> List[Dict[str, Any]]:
    """Raw rows of a scope in a time range, shaped uniformly for aggregate()."""
    if scope == "r":
        rows = conn.execute("""
            SELECT planned, jid, coupled_jid, status, time_type, delay_min, arr_delay_min, extra, platform_changed,
                   cause_codes, line, category, NULL AS dir_eva
            FROM route_trips WHERE route_id = ? AND planned >= ? AND planned < ?""", (int(key), start, end))
    else:
        kind = "dep" if scope == "d" else "arr"
        rows = conn.execute("""
            SELECT planned, jid, coupled_jid, st_status AS status,
                   CASE WHEN st_real = 1 THEN 'REAL' ELSE time_type END AS time_type,
                   st_delay AS delay_min, NULL AS arr_delay_min,
                   CASE WHEN journey_type IN ('RELIEF','EXTRA') OR additional = 1 THEN 1 ELSE 0 END AS extra,
                   CASE WHEN platform IS NOT NULL AND planned_platform IS NOT NULL AND platform <> planned_platform
                        THEN 1 ELSE 0 END AS platform_changed,
                   cause_codes, line, category, CASE WHEN kind = 'dep' THEN dest_eva ELSE origin_eva END AS dir_eva
            FROM stop_events WHERE board_eva = ? AND kind = ? AND iris = 0 AND planned >= ? AND planned < ?
              AND st_status IS NOT NULL AND st_status <> 'ignore'""", (key, kind, start, end))
    return [dict(r) for r in rows]


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rows -> {'hour': {(day, hour): {...}}, 'line': {(day, line, dir): {...}}, 'cause': {(day, code): n}}.
    Coupled trains (one physical train, several journeys) count once."""
    jids = {r["jid"] for r in rows}
    hours: Dict[Tuple[int, int], Dict[str, int]] = {}
    lines: Dict[Tuple[int, str, str], Dict[str, int]] = {}
    causes: Counter = Counter()
    for r in rows:
        if r["coupled_jid"] is not None and r["coupled_jid"] != r["jid"] and r["coupled_jid"] in jids:
            continue
        t = local_dt(r["planned"])
        day = t.year * 10000 + t.month * 100 + t.day
        h = hours.setdefault((day, t.hour), {f: 0 for f in AGG_HOUR_FIELDS})
        st = r["status"]
        counted = st in COUNTED and r["time_type"] == "REAL" and r["delay_min"] is not None
        if st != "replacement":
            h["planned"] += 1
        if counted:
            d = r["delay_min"]
            h["measured"] += 1
            h["sum_delay"] += d
            h["max_delay"] = d if h["measured"] == 1 else max(h["max_delay"], d)
            if d < PUNCTUAL_LIMIT:
                h["punctual"] += 1
            h["d0" if d < 1 else "d1" if d <= 5 else "d6" if d <= 15 else "d16" if d <= 30 else "d31"] += 1
        if st in ("cancelled", "partial", "replaced", "sev", "unknown"):
            h[st] += 1
        if r["extra"] and st != "replacement":
            h["extra"] += 1
        if r["platform_changed"]:
            h["platform_changed"] += 1
        if r["arr_delay_min"] is not None:
            h["arr_measured"] += 1
            h["arr_sum_delay"] += r["arr_delay_min"]
            if r["arr_delay_min"] < PUNCTUAL_LIMIT:
                h["arr_punctual"] += 1
        if st != "replacement":
            lk = (day, r["line"] or r["category"] or "", r.get("dir_eva") or "")
            ln = lines.setdefault(lk, {"n": 0, "m": 0, "p": 0, "f": 0, "sum_delay": 0})
            ln["n"] += 1
            if counted:
                ln["m"] += 1
                ln["sum_delay"] += r["delay_min"]
                if r["delay_min"] < PUNCTUAL_LIMIT:
                    ln["p"] += 1
            if st == "cancelled":
                ln["f"] += 1
        if r["cause_codes"] and (st in ("cancelled", "partial") or (r["delay_min"] or 0) >= PUNCTUAL_LIMIT):
            for code in decode_causes(r["cause_codes"]):
                if code != "00":
                    causes[(day, code)] += 1
    for h in hours.values():
        if h["measured"] == 0:
            h["max_delay"] = None
    return {"hour": hours, "line": lines, "cause": dict(causes)}


def write_aggregates(conn: sqlite3.Connection, scope: str, key: str, day: dt.date, done: bool = True) -> None:
    """(Re)compute the aggregates of one scope/key/day from the raw rows."""
    start, end = day_bounds(day)
    dk = day.year * 10000 + day.month * 100 + day.day
    agg = aggregate(trip_rows(conn, scope, key, start, end))
    for table in ("agg_hour", "agg_line", "agg_cause", "agg_done"):
        conn.execute(f"DELETE FROM {table} WHERE scope = ? AND key = ? AND day = ?", (scope, key, dk))
    conn.executemany(
        f"INSERT INTO agg_hour (scope, key, day, hour, {', '.join(AGG_HOUR_FIELDS)}) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' * len(AGG_HOUR_FIELDS))})",
        [(scope, key, d, h, *[v[f] for f in AGG_HOUR_FIELDS]) for (d, h), v in agg["hour"].items()])
    if scope != "r":
        conn.executemany("INSERT INTO agg_line VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [(scope, key, d, ln, de, v["n"], v["m"], v["p"], v["f"], v["sum_delay"])
                          for (d, ln, de), v in agg["line"].items()])
    conn.executemany("INSERT INTO agg_cause VALUES (?, ?, ?, ?, ?)",
                     [(scope, key, d, code, n) for (d, code), n in agg["cause"].items()])
    if done:
        conn.execute("INSERT INTO agg_done VALUES (?, ?, ?)", (scope, key, dk))


def _route_days_final(conn: sqlite3.Connection, route_id: int, start: int, end: int) -> Dict[dt.date, bool]:
    """day -> True if every trip of that day is final (finalized OpenBoards data or legacy data)."""
    out: Dict[dt.date, bool] = {}
    for planned, final in conn.execute("SELECT planned, final FROM route_trips WHERE route_id = ? "
                                       "AND planned >= ? AND planned < ?", (route_id, start, end)):
        d = local_dt(planned).date()
        out[d] = out.get(d, True) and bool(final)
    return out


def refresh_aggregates_after_finalize(conn: sqlite3.Connection, board_eva: str, kind: str, day: dt.date) -> None:
    """After a final board-day: station aggregates and those of the affected routes.
    The day before is refreshed too (stragglers / arrivals of trains that departed the evening before)."""
    done_days = {r[0] for r in conn.execute(
        "SELECT service_date FROM sync_state WHERE board_eva = ? AND kind = ?", (board_eva, kind))}
    for d in (day - dt.timedelta(days=1), day):
        if d == day or d.isoformat() in done_days:
            write_aggregates(conn, "d" if kind == "dep" else "a", board_eva, d)
    routes = routes_for(conn, origin_eva=board_eva) if kind == "dep" else routes_for(conn, dest_eva=board_eva)
    start, _ = day_bounds(day - dt.timedelta(days=1))
    _, end = day_bounds(day)
    for r in routes:
        for d, final in _route_days_final(conn, r["id"], start, end).items():
            if final:
                write_aggregates(conn, "r", str(r["id"]), d)


def rebuild_aggregates(conn: sqlite3.Connection) -> None:
    """All aggregates from scratch: route days whose trips are all final, station days that are finalized."""
    for table in ("agg_hour", "agg_line", "agg_cause", "agg_done"):
        conn.execute(f"DELETE FROM {table}")
    for board, kind, d in conn.execute("SELECT board_eva, kind, service_date FROM sync_state").fetchall():
        write_aggregates(conn, "d" if kind == "dep" else "a", board, dt.date.fromisoformat(d))
    today = dt.datetime.now(TZ).date()
    for r in conn.execute("SELECT id FROM routes").fetchall():
        for d, final in _route_days_final(conn, r["id"], 0, day_bounds(today)[0]).items():
            if final:
                write_aggregates(conn, "r", str(r["id"]), d)
    conn.commit()


# ------------------------------------------------------ map geometry
def route_geometry(conn: sqlite3.Connection, route_id: int, sample: int = 150) -> List[str]:
    """Typical stop sequence (EVA numbers) of a route: origin, intermediate stops, destination."""
    r = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if r is None:
        return []
    stadas = stada_map(conn)
    dest_stada, dest_evas = station_group(conn, r["dest_eva"])
    counts: Dict[Tuple[str, ...], int] = {}
    for row in conn.execute("""
            SELECT e.path FROM route_trips t
            JOIN stop_events e ON e.jid = t.jid AND e.board_eva = ? AND e.kind = 'dep'
            WHERE t.route_id = ? AND t.iris = 0 AND e.path IS NOT NULL
            ORDER BY t.planned DESC LIMIT ?""", (r["origin_eva"], route_id, sample)):
        seq = []
        for stop in decode_path(row["path"], stadas) or []:
            if (dest_stada and stop.get("s") == dest_stada) or stop.get("e") in dest_evas:
                break
            if stop.get("e"):
                seq.append(stop["e"])
        else:
            continue
        counts[tuple(seq)] = counts.get(tuple(seq), 0) + 1
    # among the common stop patterns (>= 30 % of the most common one) take the one with the most stops
    middle: List[str] = []
    if counts:
        top = max(counts.values())
        common = [seq for seq, n in counts.items() if n >= max(2, 0.3 * top)] or [max(counts, key=counts.get)]
        middle = list(max(common, key=lambda seq: (len(seq), counts[seq])))
    return [r["origin_eva"], *middle, r["dest_eva"]]
