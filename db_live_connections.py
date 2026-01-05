#!/usr/bin/env python3
"""
DB Timetables (IRIS) based pipeline: list all *direct* connections from A -> B
leaving within the next 60 minutes, including live delay (Verspätung) at Abfahrt.

- Uses the official Deutsche Bahn **Timetables v1** (IRIS) API endpoints:
  * GET /station/{pattern}       -> station lookup (EVA + RIL100/DS100)
  * GET /plan/{eva}/{date}/{hour} -> planned timetable per hour (XML)
  * GET /fchg/{eva}               -> full change feed with live updates (XML)

"Connections" here are interpreted as *direct trains* that depart from the
start station and pass the destination station (appears in the planned route).
If you also need journeys with transfers, use a trip planner (HAFAS) alongside
Timetables or a public wrapper and adapt this script.

Requirements
------------
Python >= 3.9 (uses zoneinfo), requests.

Environment variables (required):
- DB_CLIENT_ID  : your DB API Marketplace application client id
- DB_API_KEY    : your DB API Marketplace API key (client secret)

These can be set via environment variables or placed in a .env file in the same directory.

Usage
-----
$ export DB_CLIENT_ID=... DB_API_KEY=...
$ python db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf"

Or create a .env file:
$ python db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf"

Output: table with all *direct* departures within the next hour from origin to
        destination, showing planned/real time and delay in minutes.
"""

from __future__ import annotations

import os
import sys
import argparse
import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo
from pathlib import Path
import requests
import xml.etree.ElementTree as ET

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file if it exists
def load_env():
    """Load environment variables from .env file in the same directory."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_env()

BASE = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
TZ = ZoneInfo("Europe/Berlin")
TIMEOUT = 20
DEFAULT_WINDOW_HOURS = 1  # Default time window in hours

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # Exponential backoff base (seconds)
RETRY_BACKOFF_MAX = 60  # Maximum backoff time (seconds)

# ------------------------------ Data models ---------------------------------

@dataclass
class Station:
    name: str
    eva: str  # 7 digits EVA number as string, e.g. "8000105"
    ril100: Optional[str]  # DS100 code, e.g. "BLS"


@dataclass
class Departure:
    service_id: str               # s/@id from XML
    category: Optional[str]       # tl/@c (e.g., ICE, IC, RE, S)
    number: Optional[str]         # tl/@n (e.g., ICE 123 => 123)
    planned_dt: dt.datetime       # departure planned (local)
    realtime_dt: Optional[dt.datetime]  # departure realtime if changed
    planned_platform: Optional[str]
    realtime_platform: Optional[str]
    planned_path_ds100: List[str]  # dp/@ppth split by ';' - contains station NAMES (not DS100 codes)
    status: Optional[str] = None  # dp/@cs: 'c' = cancelled, 'p' = partial, 'a' = additional, None = normal

    @property
    def delay_min(self) -> Optional[int]:
        if self.realtime_dt is None:
            return 0
        return int((self.realtime_dt - self.planned_dt).total_seconds() // 60)

    @property
    def is_cancelled(self) -> bool:
        return self.status == 'c'

    @property
    def is_partial(self) -> bool:
        return self.status == 'p'

    @property
    def is_additional(self) -> bool:
        return self.status == 'a'


# ------------------------------ HTTP helpers --------------------------------

def _headers(accept: str = "application/xml") -> Dict[str, str]:
    cid = os.environ.get("DB_CLIENT_ID")
    api_key = os.environ.get("DB_API_KEY")
    if not cid or not api_key:
        logger.error("Missing environment variables DB_CLIENT_ID / DB_API_KEY")
        logger.error("Please set them in your environment or create a .env file.")
        sys.exit(2)
    return {
        "DB-Client-Id": cid,
        "DB-Api-Key": api_key,
        "Accept": accept,
        "User-Agent": "db-live-pipeline/1.0"
    }


def _retry_request(func, *args, **kwargs):
    """Execute a function with exponential backoff retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except (ValueError, requests.exceptions.JSONDecodeError) as e:
            # JSON parsing errors are not retryable - bad response format
            logger.error(f"Invalid response format (JSON decode error): {e}")
            raise
        except requests.HTTPError as e:
            # Don't retry on 4xx errors (client errors)
            if e.response is not None and 400 <= e.response.status_code < 500:
                logger.error(f"Client error {e.response.status_code}: {e}")
                raise

            # Retry on 5xx errors (server errors)
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Request failed after {MAX_RETRIES} attempts: {e}")
                raise

            backoff = min(RETRY_BACKOFF_BASE ** (attempt + 1), RETRY_BACKOFF_MAX)
            logger.warning(f"Server error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            logger.info(f"Retrying in {backoff} seconds...")
            time.sleep(backoff)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # Network-level errors are retryable
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Request failed after {MAX_RETRIES} attempts: {e}")
                raise

            backoff = min(RETRY_BACKOFF_BASE ** (attempt + 1), RETRY_BACKOFF_MAX)
            logger.warning(f"Network error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            logger.info(f"Retrying in {backoff} seconds...")
            time.sleep(backoff)
        except requests.exceptions.RequestException as e:
            # Other request exceptions
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Request failed after {MAX_RETRIES} attempts: {e}")
                raise

            backoff = min(RETRY_BACKOFF_BASE ** (attempt + 1), RETRY_BACKOFF_MAX)
            logger.warning(f"Request error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            logger.info(f"Retrying in {backoff} seconds...")
            time.sleep(backoff)


def _get_json(url: str) -> dict:
    def _fetch():
        logger.debug(f"Fetching JSON from: {url}")
        r = requests.get(url, headers=_headers("application/json"), timeout=TIMEOUT)
        r.raise_for_status()

        # Check if response has content
        if not r.content:
            logger.error(f"Empty response from API: {url}")
            raise ValueError("API returned empty response")

        try:
            return r.json()
        except ValueError as e:
            logger.error(f"Invalid JSON in response from {url}: {r.text[:200]}")
            raise

    return _retry_request(_fetch)


def _get_xml(url: str) -> ET.Element:
    def _fetch():
        logger.debug(f"Fetching XML from: {url}")
        r = requests.get(url, headers=_headers("application/xml"), timeout=TIMEOUT)
        r.raise_for_status()

        # Check if response has content
        if not r.content:
            logger.error(f"Empty response from API: {url}")
            raise ValueError("API returned empty response")

        try:
            return ET.fromstring(r.content)
        except ET.ParseError as e:
            logger.error(f"Invalid XML in response from {url}: {r.text[:200]}")
            raise

    return _retry_request(_fetch)


# ------------------------------ Station Cache --------------------------------

# Global in-memory cache for station lookups
_station_cache: Dict[str, List[Station]] = {}

def _get_station_cache_db(db_path: str = "./train_db.db"):
    """Get or create station cache table in database."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Create station cache table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS station_cache (
            search_pattern TEXT PRIMARY KEY,
            stations_json TEXT NOT NULL,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def _load_cached_stations(pattern: str, db_path: str = "./train_db.db") -> Optional[List[Station]]:
    """Load stations from cache (memory first, then database)."""
    # Check in-memory cache first
    pattern_lower = pattern.lower().strip()
    if pattern_lower in _station_cache:
        logger.debug(f"✓ Station cache hit (memory): '{pattern}'")
        return _station_cache[pattern_lower]

    # Check database cache
    try:
        import json
        conn = _get_station_cache_db(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT stations_json FROM station_cache WHERE search_pattern = ?",
            (pattern_lower,)
        )
        row = cur.fetchone()
        conn.close()

        if row:
            logger.debug(f"✓ Station cache hit (database): '{pattern}'")
            stations_data = json.loads(row[0])
            stations = [Station(**s) for s in stations_data]
            # Update in-memory cache
            _station_cache[pattern_lower] = stations
            return stations
    except Exception as e:
        logger.warning(f"Error loading from station cache: {e}")

    return None

def _save_to_cache(pattern: str, stations: List[Station], db_path: str = "./train_db.db"):
    """Save stations to cache (memory and database)."""
    pattern_lower = pattern.lower().strip()

    # Save to in-memory cache
    _station_cache[pattern_lower] = stations

    # Save to database cache
    try:
        import json
        from dataclasses import asdict

        conn = _get_station_cache_db(db_path)
        cur = conn.cursor()

        stations_json = json.dumps([asdict(s) for s in stations])
        cur.execute(
            "INSERT OR REPLACE INTO station_cache (search_pattern, stations_json, cached_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (pattern_lower, stations_json)
        )
        conn.commit()
        conn.close()
        logger.debug(f"✓ Cached {len(stations)} stations for pattern: '{pattern}'")
    except Exception as e:
        logger.warning(f"Error saving to station cache: {e}")


# ------------------------------ API wrappers --------------------------------

def search_station(pattern: str, use_cache: bool = True, db_path: str = "./train_db.db") -> List[Station]:
    """Search stations via /station/{pattern}.

    Returns a list of candidates (best-effort parsing of JSON or XML).
    Supports caching to avoid redundant API calls.
    """
    # Try to load from cache first
    if use_cache:
        cached = _load_cached_stations(pattern, db_path)
        if cached is not None:
            return cached

    # Cache miss - fetch from API
    logger.info(f"Fetching station from API: '{pattern}'")

    import urllib.parse as up
    url = f"{BASE}/station/{up.quote(pattern)}"

    # Prefer JSON; fall back to XML if necessary
    try:
        logger.debug(f"Attempting JSON station search for: {pattern}")
        r = requests.get(url, headers=_headers("application/json"), timeout=TIMEOUT)
        r.raise_for_status()

        # Try to parse as JSON
        if r.content:
            try:
                data = r.json()
                # Example shape (observed in practice, may change):
                # [{"name": "Berlin Hbf", "evaNo": "8011160", "ril100": "BLS"}, ...]
                stations = []
                for item in data if isinstance(data, list) else data.get("result", []):
                    name = item.get("name") or item.get("nameLong") or item.get("n")
                    eva = str(item.get("evaNo") or item.get("eva") or item.get("id"))
                    ril = item.get("ril100") or item.get("ds100") or item.get("ril")
                    if name and eva:
                        stations.append(Station(name=name, eva=eva.zfill(7), ril100=ril))
                if stations:
                    logger.debug(f"Found {len(stations)} stations via JSON")
                    # Save to cache before returning
                    if use_cache:
                        _save_to_cache(pattern, stations, db_path)
                    return stations
            except (ValueError, requests.exceptions.JSONDecodeError):
                # Not JSON, fall through to XML
                logger.debug("Response is not JSON, falling back to XML")
                pass
    except requests.HTTPError as e:
        # Fall through to XML parsing on non-200 or unexpected JSON
        if e.response is not None and e.response.status_code == 406:
            logger.debug("HTTP 406 - Not Acceptable, falling back to XML")
            pass
        else:
            # If 4xx/5xx other than Not Acceptable, re-raise
            raise
    except Exception as e:
        # Fall back to XML
        logger.debug(f"JSON attempt failed: {e}, falling back to XML")
        pass

    # Fallback: XML
    logger.debug(f"Using XML station search for: {pattern}")
    root = _get_xml(url)
    # Try to find <station ...> elements
    stations: List[Station] = []
    for st in root.findall(".//station"):
        name = st.get("name") or st.get("nameLong")
        eva = st.get("evaNo") or st.get("eva") or st.get("id")
        ril = st.get("ril100") or st.get("ds100")
        if name and eva:
            stations.append(Station(name=name, eva=str(eva).zfill(7), ril100=ril))
    logger.debug(f"Found {len(stations)} stations via XML")

    # Save to cache before returning
    if use_cache and stations:
        _save_to_cache(pattern, stations, db_path)

    return stations


def fetch_plan_hour(eva: str, when: dt.datetime) -> ET.Element:
    """Fetch planned timetable for a specific hour (local Europe/Berlin).
    Timetables /plan expects date YYMMDD and hour HH.
    """
    local = when.astimezone(TZ)
    date = local.strftime("%y%m%d")
    hour = local.strftime("%H")
    url = f"{BASE}/plan/{eva}/{date}/{hour}"
    return _get_xml(url)


def fetch_full_changes(eva: str) -> ET.Element:
    url = f"{BASE}/fchg/{eva}"
    return _get_xml(url)


# --------------------------- XML parsing helpers -----------------------------

def _parse_time_hhmm_to_dt(base_date: dt.date, hhmm: str) -> dt.datetime:
    return dt.datetime.combine(base_date, dt.time(int(hhmm[:2]), int(hhmm[2:])), tzinfo=TZ)


def _parse_ts_yymmddhhmm_to_dt(val: str) -> dt.datetime:
    # e.g., "2509231310" -> 2025-09-23 13:10 Europe/Berlin
    return dt.datetime(2000 + int(val[:2]), int(val[2:4]), int(val[4:6]), int(val[6:8]), int(val[8:10]), tzinfo=TZ)


def parse_departures_from_plan(plan_root: ET.Element) -> List[Departure]:
    """Extract departures (planned) for one station-hour block.
    Returns a list of Departure objects with planned data; realtime fields empty.
    """
    res: List[Departure] = []
    # The root is <timetable>, children <s>
    for s in plan_root.findall(".//s"):
        dp = s.find("dp")
        if dp is None:
            continue
        tl = s.find("tl")
        sid = s.get("id") or ""

        # Get planned time - handle missing/invalid attributes
        pt_attr = dp.get("pt")
        if not pt_attr or len(pt_attr) < 7:
            continue
        pt = pt_attr[6:]  # planned time, HHMM (skip date prefix YYMMDD)
        if not pt:
            continue

        # Get base date from root timetable
        base_date_attr = plan_root.get("d")  # YYMMDD
        if base_date_attr:
            base_date = dt.date(2000 + int(base_date_attr[:2]), int(base_date_attr[2:4]), int(base_date_attr[4:6]))
        else:
            # Fallback: today in TZ
            base_date = dt.datetime.now(TZ).date()

        planned_dt = _parse_time_hhmm_to_dt(base_date, pt)

        cat = tl.get("c") if tl is not None else None
        num = tl.get("n") if tl is not None else None
        ppth = dp.get("ppth", "")

        # Fixed: Handle empty path, return list not single element
        path_list = [p.strip().upper() for p in ppth.split(";") if p.strip()]

        res.append(Departure(
            service_id=sid,
            category=cat,
            number=num,
            planned_dt=planned_dt,
            realtime_dt=None,
            planned_platform=dp.get("pp"),
            realtime_platform=None,
            planned_path_ds100=path_list,
        ))
    return res


def merge_realtime(deps: List[Departure], fchg_root: ET.Element) -> None:
    """Augment departures with realtime (ct), changed platform (cp), and status (cs) using /fchg feed.
    We match by service_id where possible; if not present, best-effort by category/number+time.
    Status codes: 'c' = cancelled, 'p' = partial cancellation, 'a' = additional train
    """
    # Build index by s/@id
    change_by_id: Dict[str, ET.Element] = {}
    for s in fchg_root.findall(".//s"):
        sid = s.get("id")
        if sid:
            change_by_id[sid] = s

    for d in deps:
        s = change_by_id.get(d.service_id)
        # Fixed: Check if s is None instead of len(s)
        if s is None:
            # fallback: best-effort by type/number and planned time proximity
            # (kept simple to avoid false positives)
            continue
        dp = s.find("dp")
        if dp is None:
            continue
        ct = dp.get("ct")
        if ct:
            d.realtime_dt = _parse_ts_yymmddhhmm_to_dt(ct)
        cp = dp.get("cp")
        if cp:
            d.realtime_platform = cp
        cs = dp.get("cs")
        if cs:
            d.status = cs
            logger.debug(f"Train {d.category} {d.number} status: {cs}")


# ------------------------------- Core logic ---------------------------------

class StationNotFoundError(Exception):
    """Raised when a station cannot be found."""
    pass


def resolve_station_single(pattern: str, use_cache: bool = True, db_path: str = "./train_db.db") -> Station:
    logger.debug(f"Resolving station for pattern: '{pattern}'")
    candidates = search_station(pattern, use_cache=use_cache, db_path=db_path)

    if not candidates:
        logger.error(f"No station found for pattern: '{pattern}'")
        raise StationNotFoundError(f"Keine Station gefunden für Muster: {pattern}")

    # Log all candidates at DEBUG level
    logger.debug(f"Found {len(candidates)} candidate(s):")
    for i, st in enumerate(candidates):
        logger.debug(f"  [{i+1}] Name: '{st.name}' | EVA: {st.eva} | RIL100: {st.ril100}")

    # Prefer exact (case-insensitive) name match, else first candidate
    lowered = pattern.lower().strip()
    for st in candidates:
        if st.name.lower() == lowered:
            logger.info(f"✓ Station resolved: '{st.name}' (EVA: {st.eva})")
            return st

    # Use first candidate
    logger.info(f"✓ Station resolved: '{candidates[0].name}' (EVA: {candidates[0].eva})")
    return candidates[0]


def find_direct_departures_next_hour(origin: Station, dest: Station, now: Optional[dt.datetime] = None, window_hours: float = DEFAULT_WINDOW_HOURS) -> List[Departure]:
    """
    Find direct departures from origin to destination within a time window.

    Args:
        origin: Origin station
        dest: Destination station
        now: Start time (defaults to current time)
        window_hours: Time window in hours (default: 1 hour)
    """
    if now is None:
        now = dt.datetime.now(TZ)
    window_end = now + dt.timedelta(hours=window_hours)

    # Fetch plan for all hours in the window
    roots = []
    current = now
    hours_covered = set()

    while current <= window_end:
        hour_key = (current.date(), current.hour)
        if hour_key not in hours_covered:
            roots.append(fetch_plan_hour(origin.eva, current))
            hours_covered.add(hour_key)
        current += dt.timedelta(hours=1)

    departures: List[Departure] = []
    for r in roots:
        departures.extend(parse_departures_from_plan(r))

    # Only those that depart within [now, now+60min]
    deps_in_window = [d for d in departures if now <= d.planned_dt <= window_end]

    # Keep *direct* trains whose planned path includes the destination
    # Note: The API returns station NAMES in the path, not DS100 codes
    # We need to match against the station name, handling variations like "Hbf"
    dest_name_upper = dest.name.strip().upper()
    # Remove common suffixes for better matching
    dest_name_base = dest_name_upper.replace(" HBF", "").replace(" (", "")

    def matches_destination(planned_path: List[str]) -> bool:
        """Check if destination appears in planned path."""
        for station in planned_path:
            station_upper = station.upper()
            # Try exact match first
            if dest_name_upper in station_upper or station_upper in dest_name_upper:
                return True
            # Try base name match (without "Hbf")
            if dest_name_base in station_upper or station_upper.startswith(dest_name_base):
                return True
        return False

    direct = [d for d in deps_in_window if matches_destination(d.planned_path_ds100)]

    # Merge realtime
    fchg = fetch_full_changes(origin.eva)
    merge_realtime(direct, fchg)

    # Sort by realtime if available, else planned
    direct.sort(key=lambda d: d.realtime_dt or d.planned_dt)
    return direct


def format_row(d: Departure) -> str:
    rt = d.realtime_dt.strftime("%H:%M") if d.realtime_dt else "—"
    delay = d.delay_min if d.delay_min is not None else "—"
    plat = d.realtime_platform or d.planned_platform or ""
    catnum = f"{d.category or ''} {d.number or ''}".strip()
    return f"{d.planned_dt:%H:%M}  {rt:>5}  {delay:>3}  {plat:>3}  {catnum:<6}  id={d.service_id}"


def store_to_database(db_path: str, origin: Station, dest: Station, deps: List[Departure]) -> int:
    """
    Store departures to SQLite database.
    If a departure already exists (same route, service_id, planned_dt), it updates
    the delay information with the latest data.
    Returns total number of departures inserted or updated.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Ensure tables exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_name TEXT NOT NULL,
            dest_name TEXT NOT NULL,
            origin_eva TEXT NOT NULL,
            dest_eva TEXT NOT NULL,
            origin_lat REAL,
            origin_lon REAL,
            dest_lat REAL,
            dest_lon REAL,
            UNIQUE(origin_eva, dest_eva)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS departures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            service_id TEXT NOT NULL,
            category TEXT,
            number TEXT,
            planned_dt TIMESTAMP NOT NULL,
            realtime_dt TIMESTAMP,
            delay_min INTEGER,
            planned_platform TEXT,
            realtime_platform TEXT,
            status TEXT,
            inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (route_id) REFERENCES routes(id),
            UNIQUE(route_id, service_id, planned_dt)
        )
    """)

    # Check if status column exists (for existing databases), add if missing
    cur.execute("PRAGMA table_info(departures)")
    dep_columns = {row[1] for row in cur.fetchall()}
    if 'status' not in dep_columns:
        cur.execute("ALTER TABLE departures ADD COLUMN status TEXT")
        logger.info("Added status column to departures table for cancellation tracking")

    # Get or create route
    cur.execute(
        "SELECT id FROM routes WHERE origin_eva = ? AND dest_eva = ?",
        (origin.eva, dest.eva)
    )
    row = cur.fetchone()

    if row:
        route_id = row[0]
    else:
        cur.execute(
            "INSERT INTO routes (origin_name, dest_name, origin_eva, dest_eva) VALUES (?, ?, ?, ?)",
            (origin.name, dest.name, origin.eva, dest.eva)
        )
        route_id = cur.lastrowid

    # Insert or update departures (replace with latest data)
    inserted = 0
    updated = 0
    for d in deps:
        # Check if entry already exists
        cur.execute(
            """
            SELECT id, delay_min FROM departures
            WHERE route_id = ? AND service_id = ? AND planned_dt = ?
            """,
            (route_id, d.service_id, d.planned_dt.isoformat())
        )
        existing = cur.fetchone()

        if existing:
            # Update existing entry with latest delay information
            cur.execute(
                """
                UPDATE departures
                SET realtime_dt = ?, delay_min = ?, realtime_platform = ?, status = ?
                WHERE id = ?
                """,
                (
                    d.realtime_dt.isoformat() if d.realtime_dt else None,
                    d.delay_min,
                    d.realtime_platform,
                    d.status,
                    existing[0]
                )
            )
            updated += 1
        else:
            # Insert new entry
            cur.execute(
                """
                INSERT INTO departures
                (route_id, service_id, category, number, planned_dt, realtime_dt, delay_min, planned_platform, realtime_platform, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    d.service_id,
                    d.category,
                    d.number,
                    d.planned_dt.isoformat(),
                    d.realtime_dt.isoformat() if d.realtime_dt else None,
                    d.delay_min,
                    d.planned_platform,
                    d.realtime_platform,
                    d.status
                )
            )
            inserted += 1

    conn.commit()
    conn.close()

    if updated > 0:
        logger.debug(f"Updated {updated} existing departure(s) with latest delay information")

    return inserted + updated


def main():
    ap = argparse.ArgumentParser(description="Alle direkten DB-Verbindungen A->B in der nächsten Stunde (mit Live-Verspätung der Abfahrt).")
    ap.add_argument("--from", dest="origin", required=True, help="Start-Haltestelle, z.B. 'Berlin Hbf'")
    ap.add_argument("--to", dest="dest", required=True, help="Ziel-Haltestelle, z.B. 'Hamburg Hbf'")
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_HOURS, metavar="HOURS",
                    help=f"Zeitfenster in Stunden (Standard: {DEFAULT_WINDOW_HOURS})")
    ap.add_argument("--store", action="store_true", help="Speichere Ergebnisse in SQLite-Datenbank")
    ap.add_argument("--db", default="./train_db.db", help="Pfad zur SQLite-Datenbank (nur mit --store)")
    ap.add_argument("--interval", type=int, metavar="SECONDS", help="Wiederhole Abfrage alle N Sekunden (kontinuierlicher Modus)")
    args = ap.parse_args()

    try:
        origin = resolve_station_single(args.origin)
        dest = resolve_station_single(args.dest)
    except StationNotFoundError as e:
        logger.error(str(e))
        logger.error("Please check the station names and try again.")
        sys.exit(1)

    def poll_once():
        """Single polling iteration."""
        logger.info(f"Start: {origin.name} (EVA {origin.eva}, RIL100 {origin.ril100})")
        logger.info(f"Ziel : {dest.name} (EVA {dest.eva}, RIL100 {dest.ril100})")

        # Fixed: Use current time for next hour window, not past hour
        now = dt.datetime.now(TZ)
        window_end = now + dt.timedelta(hours=args.window)
        logger.info(f"Zeitraum: {now:%Y-%m-%d %H:%M} – {window_end:%Y-%m-%d %H:%M} {now.tzname()} ({args.window}h)")

        deps = find_direct_departures_next_hour(origin, dest, now, window_hours=args.window)

        if not deps:
            logger.info(f"Keine direkte Abfahrt innerhalb der nächsten {args.window} Stunde(n) gefunden.")
        else:
            # Header - keep console output for interactive use
            print("Plan  Real   Δmin Pl.  Zug     (Service-ID)")
            print("----- -----  ---- ---  ------  -------------------------")
            for d in deps:
                print(format_row(d))
            logger.info(f"Gefunden: {len(deps)} Abfahrten")

        # Store to database if requested
        if args.store:
            inserted = store_to_database(args.db, origin, dest, deps)
            logger.info(f"✓ {inserted} Abfahrten in Datenbank gespeichert ({args.db})")

        return deps

    # Run once or continuously
    if args.interval:
        logger.info(f"Kontinuierlicher Modus: Wiederhole alle {args.interval} Sekunden (Ctrl+C zum Beenden)")
        logger.info("=" * 70)
        try:
            while True:
                poll_once()
                logger.info(f"Warte {args.interval} Sekunden...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Programm beendet.")
    else:
        poll_once()


if __name__ == "__main__":
    main()
