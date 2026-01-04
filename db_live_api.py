#!/usr/bin/env python3
"""
FastAPI-based web server for DB Live train tracking system.
Provides REST API and serves interactive map-based frontend.

Features:
- REST API for departures, routes, and statistics
- Interactive map visualization with Leaflet.js
- Boxplot statistics for route reliability
- Auto-geocoding of station coordinates

Requirements:
- Python >= 3.9
- pip install fastapi uvicorn sqlite3 requests

Start:
$ python db_live_api.py --db ./train_db.db --host 0.0.0.0 --port 8080
# then browse: http://localhost:8080
"""

from __future__ import annotations

import argparse
import sqlite3
import datetime as dt
import logging
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import time
import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
def load_env():
    """Load environment variables from .env file."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        logger.info(f"Loading environment from: {env_path.absolute()}")
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    else:
        logger.warning(f".env file not found at {env_path.absolute()}")

load_env()

TZ = ZoneInfo("Europe/Berlin")

# ------------------------------ Database Utils ------------------------------

class Database:
    """Database connection manager with schema initialization."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = self._connect()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        """Ensure database schema exists and is up to date."""
        # Create routes table if it doesn't exist
        self.conn.execute("""
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

        # Create departures table if it doesn't exist
        self.conn.execute("""
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
                inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (route_id) REFERENCES routes(id),
                UNIQUE(route_id, service_id, planned_dt)
            )
        """)

        # Check if coordinate columns exist in existing tables, add if missing
        cursor = self.conn.execute("PRAGMA table_info(routes)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'origin_lat' not in columns:
            self.conn.execute("ALTER TABLE routes ADD COLUMN origin_lat REAL")
        if 'origin_lon' not in columns:
            self.conn.execute("ALTER TABLE routes ADD COLUMN origin_lon REAL")
        if 'dest_lat' not in columns:
            self.conn.execute("ALTER TABLE routes ADD COLUMN dest_lat REAL")
        if 'dest_lon' not in columns:
            self.conn.execute("ALTER TABLE routes ADD COLUMN dest_lon REAL")

        # Check if status column exists in departures table, add if missing
        cursor = self.conn.execute("PRAGMA table_info(departures)")
        dep_columns = {row[1] for row in cursor.fetchall()}

        if 'status' not in dep_columns:
            self.conn.execute("ALTER TABLE departures ADD COLUMN status TEXT")
            logger.info("Added status column to departures table for cancellation tracking")

        self.conn.commit()

    def query_one(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def query_all(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def execute(self, sql: str, params: Tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


# ------------------------------ Geocoding -----------------------------------

class Geocoder:
    """Geocode station names to coordinates using OpenStreetMap Nominatim."""

    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    USER_AGENT = "DB-Live-Tracker/1.0"
    RATE_LIMIT = 1.0  # seconds between requests (Nominatim requires max 1 req/sec)

    def __init__(self):
        self._last_request = 0.0

    def _rate_limit(self):
        """Ensure we don't exceed Nominatim rate limits."""
        elapsed = time.time() - self._last_request
        if elapsed < self.RATE_LIMIT:
            time.sleep(self.RATE_LIMIT - elapsed)
        self._last_request = time.time()

    def geocode_station(self, station_name: str, country: str = "Germany") -> Optional[Tuple[float, float]]:
        """
        Geocode a station name to (latitude, longitude).
        Returns None if geocoding fails.
        """
        self._rate_limit()

        # Try with "Bahnhof" suffix for better results
        queries = [
            f"{station_name}, {country}",
            f"{station_name} Bahnhof, {country}",
            f"Bahnhof {station_name}, {country}"
        ]

        for query in queries:
            try:
                response = requests.get(
                    self.NOMINATIM_URL,
                    params={
                        "q": query,
                        "format": "json",
                        "limit": 1,
                        "countrycodes": "de"
                    },
                    headers={"User-Agent": self.USER_AGENT},
                    timeout=10
                )
                response.raise_for_status()
                results = response.json()

                if results and len(results) > 0:
                    lat = float(results[0]["lat"])
                    lon = float(results[0]["lon"])
                    return (lat, lon)
            except Exception as e:
                logger.warning(f"Geocoding failed for '{query}': {e}")
                continue

        return None


# ------------------------------ Statistics ----------------------------------

def calculate_hourly_stats(db: Database, route_id: int) -> List[Dict[str, Any]]:
    """
    Calculate hourly statistics (for boxplots) for a given route.
    Returns list of dicts with hour and delay statistics.
    """
    rows = db.query_all(
        """
        SELECT
            CAST(strftime('%H', planned_dt) AS INTEGER) as hour,
            delay_min
        FROM departures
        WHERE route_id = ? AND delay_min IS NOT NULL
        ORDER BY hour
        """,
        (route_id,)
    )

    # Group by hour
    hourly_data: Dict[int, List[int]] = {}
    for row in rows:
        hour = row["hour"]
        delay = row["delay_min"]
        if hour not in hourly_data:
            hourly_data[hour] = []
        hourly_data[hour].append(delay)

    # Calculate statistics for each hour
    stats = []
    for hour in range(24):
        if hour in hourly_data and len(hourly_data[hour]) > 0:
            delays = sorted(hourly_data[hour])
            n = len(delays)
            stats.append({
                "hour": hour,
                "delays": delays,  # All delay values for boxplot
                "count": n,
                "min": min(delays),
                "max": max(delays),
                "median": delays[n // 2],
                "mean": sum(delays) / n
            })
        else:
            stats.append({
                "hour": hour,
                "delays": [],
                "count": 0,
                "min": None,
                "max": None,
                "median": None,
                "mean": None
            })

    return stats


# ------------------------------ Background Polling --------------------------

class BackgroundPoller:
    """Background task for polling train data."""

    def __init__(self, db: Database):
        self.db = db
        self.task: Optional[asyncio.Task] = None
        self.enabled = os.getenv("POLLING_ENABLED", "false").lower() == "true"
        self.interval = int(os.getenv("POLLING_INTERVAL", "3600"))
        self.routes = self._parse_routes(os.getenv("POLLING_ROUTES", ""))

    def _parse_routes(self, routes_str: str) -> List[Tuple[str, str]]:
        """Parse routes from environment variable."""
        if not routes_str:
            return []

        routes = []
        for route in routes_str.split(";"):
            route = route.strip()
            if "->" in route:
                origin, dest = route.split("->", 1)
                routes.append((origin.strip(), dest.strip()))
        return routes

    async def start(self):
        """Start the background polling task."""
        logger.info("Background Polling Configuration:")
        logger.info(f"  POLLING_ENABLED env var: {os.getenv('POLLING_ENABLED', 'not set')}")
        logger.info(f"  Enabled (parsed): {self.enabled}")
        logger.info(f"  Routes configured: {len(self.routes)}")

        if not self.enabled:
            logger.warning("Background polling is disabled")
            logger.info("  To enable: Set POLLING_ENABLED=true in .env file")
            return

        if not self.routes:
            logger.warning("No routes configured for polling")
            logger.info("  To configure: Set POLLING_ROUTES in .env file")
            return

        logger.info("Starting background polling:")
        logger.info(f"  - Interval: {self.interval} seconds ({self.interval/3600:.1f} hours)")
        logger.info(f"  - Routes: {len(self.routes)}")
        for origin, dest in self.routes:
            logger.info(f"    • {origin} → {dest}")

        self.task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        """Stop the background polling task."""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            logger.info("Background polling stopped")

    async def _poll_loop(self):
        """Main polling loop."""
        while True:
            try:
                await self._poll_all_routes()
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    async def _poll_all_routes(self):
        """Poll all configured routes."""
        logger.info(f"[{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S}] Starting polling cycle...")

        # Import polling functions from db_live_connections
        import sys
        sys.path.insert(0, str(Path(__file__).parent))

        try:
            from db_live_connections import (
                resolve_station_single,
                find_direct_departures_next_hour,
                store_to_database,
                StationNotFoundError
            )
        except ImportError as e:
            logger.error(f"Error importing polling functions: {e}")
            return

        total_inserted = 0
        for origin_name, dest_name in self.routes:
            try:
                # Resolve stations
                logger.debug(f"  → Resolving stations: {origin_name} → {dest_name}")
                origin = await asyncio.to_thread(resolve_station_single, origin_name)
                dest = await asyncio.to_thread(resolve_station_single, dest_name)
                logger.debug(f"     Origin: {origin.name} (EVA: {origin.eva}, RIL100: {origin.ril100})")
                logger.debug(f"     Dest: {dest.name} (EVA: {dest.eva}, RIL100: {dest.ril100})")

                # Find departures for the PAST hour to get actual delays (not estimates)
                now = dt.datetime.now(TZ)
                past_hour = now - dt.timedelta(hours=1)
                logger.debug(f"     Fetching departures from {past_hour:%H:%M} to {now:%H:%M} (past hour)")
                deps = await asyncio.to_thread(
                    find_direct_departures_next_hour,
                    origin,
                    dest,
                    past_hour,  # Start from 1 hour ago
                    1.0  # 1 hour window
                )
                logger.debug(f"     Found {len(deps)} departures")

                # Store to database
                if deps:
                    inserted = store_to_database(self.db.db_path, origin, dest, deps)
                    total_inserted += inserted
                    logger.info(f"  ✓ {origin_name} → {dest_name}: {inserted} departures stored")
                else:
                    logger.info(f"  - {origin_name} → {dest_name}: No direct departures in time window")

            except StationNotFoundError as e:
                logger.error(f"  ✗ {origin_name} → {dest_name}: Station not found - {e}")
                logger.warning(f"     Skipping this route. Please check station names in .env file")
            except Exception as e:
                import traceback
                logger.error(f"  ✗ {origin_name} → {dest_name}: Error - {e}")
                logger.debug(f"     Traceback: {traceback.format_exc()}")

        logger.info(f"Polling cycle complete. Total: {total_inserted} departures stored")


# Global poller instance
poller: Optional[BackgroundPoller] = None


# ------------------------------ FastAPI App ---------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    # Startup
    global poller
    if db:
        poller = BackgroundPoller(db)
        await poller.start()

    yield

    # Shutdown
    if poller:
        await poller.stop()


app = FastAPI(
    title="DB Live Tracker API",
    description="Real-time train tracking with interactive map visualization",
    version="2.0.0",
    lifespan=lifespan
)

# Add CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global database instance (set during startup)
db: Optional[Database] = None
geocoder = Geocoder()


# Mount static files at module level (before routes)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main map interface."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    else:
        # Return a placeholder if static files don't exist yet
        return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <head><title>DB Live Tracker</title></head>
            <body>
                <h1>DB Live Tracker</h1>
                <p>Frontend is being set up. Please check back soon.</p>
                <p>API Documentation: <a href="/docs">/docs</a></p>
            </body>
            </html>
        """)


@app.get("/details.html", response_class=HTMLResponse)
async def details():
    """Serve the details page."""
    html_path = Path(__file__).parent / "static" / "details.html"
    if html_path.exists():
        return FileResponse(html_path)
    else:
        return HTMLResponse("<h1>Details page not found</h1>", status_code=404)


@app.get("/api/routes")
async def get_routes():
    """Get all routes with coordinates for map visualization."""
    rows = db.query_all("""
        SELECT
            id,
            origin_name,
            dest_name,
            origin_eva,
            dest_eva,
            origin_lat,
            origin_lon,
            dest_lat,
            dest_lon
        FROM routes
        ORDER BY origin_name, dest_name
    """)

    routes = []
    for row in rows:
        route = dict(row)

        # Auto-geocode if coordinates are missing
        if route["origin_lat"] is None or route["origin_lon"] is None:
            coords = geocoder.geocode_station(route["origin_name"])
            if coords:
                route["origin_lat"], route["origin_lon"] = coords
                db.execute(
                    "UPDATE routes SET origin_lat = ?, origin_lon = ? WHERE id = ?",
                    (coords[0], coords[1], route["id"])
                )
                db.commit()

        if route["dest_lat"] is None or route["dest_lon"] is None:
            coords = geocoder.geocode_station(route["dest_name"])
            if coords:
                route["dest_lat"], route["dest_lon"] = coords
                db.execute(
                    "UPDATE routes SET dest_lat = ?, dest_lon = ? WHERE id = ?",
                    (coords[0], coords[1], route["id"])
                )
                db.commit()

        routes.append(route)

    return {"routes": routes}


@app.get("/api/routes/{route_id}/stats")
async def get_route_stats(route_id: int):
    """Get hourly statistics for a specific route (for boxplot visualization)."""
    # Check if route exists
    route = db.query_one("SELECT * FROM routes WHERE id = ?", (route_id,))
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    stats = calculate_hourly_stats(db, route_id)

    return {
        "route_id": route_id,
        "origin_name": route["origin_name"],
        "dest_name": route["dest_name"],
        "hourly_stats": stats
    }


@app.get("/api/polling/status")
async def get_polling_status():
    """Get background polling status."""
    if not poller:
        return {
            "enabled": False,
            "message": "Polling not initialized"
        }

    return {
        "enabled": poller.enabled,
        "interval_seconds": poller.interval,
        "interval_hours": poller.interval / 3600,
        "routes_count": len(poller.routes),
        "routes": [{"origin": o, "destination": d} for o, d in poller.routes],
        "running": poller.task is not None and not poller.task.done() if poller.task else False
    }


@app.get("/api/departures")
async def get_departures(
    route_id: Optional[int] = Query(None, description="Filter by route ID"),
    since: Optional[int] = Query(None, description="Hours to look back", ge=1, le=8760),
    all_time: bool = Query(False, description="Get all data regardless of time"),
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    q: Optional[str] = Query(None, description="Search query"),
    limit: int = Query(1000, description="Result limit", ge=1, le=5000),
    offset: int = Query(0, description="Pagination offset", ge=0)
):
    """Get departure data with filters."""
    where = []
    params: List[Any] = []

    # Time filtering
    if all_time:
        # No time filter - get all data
        pass
    elif date_from or date_to:
        # Date range mode
        if date_from and date_to:
            where.append("DATE(planned_dt) BETWEEN ? AND ?")
            params.extend([date_from, date_to])
        elif date_from:
            where.append("DATE(planned_dt) >= ?")
            params.append(date_from)
        elif date_to:
            where.append("DATE(planned_dt) <= ?")
            params.append(date_to)
    else:
        # Relative time mode (default)
        hours = since if since is not None else 24
        t_to = dt.datetime.now(TZ)
        t_from = t_to - dt.timedelta(hours=hours)
        where.append("datetime(planned_dt) BETWEEN ? AND ?")
        params.extend([t_from.isoformat(), t_to.isoformat()])

    if route_id is not None:
        where.append("route_id = ?")
        params.append(route_id)

    if q:
        where.append(
            "(IFNULL(category,'') || ' ' || IFNULL(number,'') LIKE ? "
            "OR IFNULL(service_id,'') LIKE ? "
            "OR IFNULL(planned_platform,'') LIKE ? "
            "OR IFNULL(realtime_platform,'') LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    where_sql = " AND ".join(where) if where else "1=1"

    # Get total count for pagination
    count_row = db.query_one(
        f"""
        SELECT COUNT(*) as total
        FROM departures d
        JOIN routes r ON r.id = d.route_id
        WHERE {where_sql}
        """,
        tuple(params)
    )
    total = count_row['total'] if count_row else 0

    rows = db.query_all(
        f"""
        SELECT d.*, r.origin_name, r.dest_name
        FROM departures d
        JOIN routes r ON r.id = d.route_id
        WHERE {where_sql}
        ORDER BY datetime(COALESCE(realtime_dt, planned_dt)) DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [limit, offset])
    )

    data = [dict(r) for r in rows]

    return {
        "meta": {
            "since_hours": since,
            "limit": limit,
            "offset": offset,
            "count": len(data),
            "total": total,
            "now": dt.datetime.now(TZ).isoformat()
        },
        "departures": data
    }


# ------------------------------ Startup -------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="FastAPI server for DB Live tracking")
    ap.add_argument("--db", default="./train_db.db", help="Path to SQLite database")
    ap.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    ap.add_argument("--port", default=8080, type=int, help="Port to bind to")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Initialize database
    db = Database(args.db)

    # Run with uvicorn
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
