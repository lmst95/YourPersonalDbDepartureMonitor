# Changelog

All notable changes to the DB Live Tracker / Pendelmonitor project.

## [Unreleased]

### Changed
- Route page redesigned as a dense cockpit: one summary line (share punctual, "every n-th train late", worst hour, most reliable train, median, 90 % value, comparison with the previous period) instead of six tiles
- Cross chart: hour boxplots on top, weekday boxplots on the right, heatmap in between with shared axes; clicking an hour, weekday or cell fills a selection panel (quantiles, distribution, reasons) and filters the timetable
- Timetable: every regular train with share punctual, median, 90 % value, mini boxplot, cancellations and its last 14 runs; sortable by every column, filters for days, time of day and line, click shows each single run
- Calendar of daily punctuality (click selects the day); the full trip list moved into a collapsible section
- Live data moved out of the analysis pages: new page `/live` in the look of station displays (blue, white lines, "ca. +5" chips, running text, "über …" line) with departure board, platform display and the next trains of the routes starting there; route and station pages only link to it

- Station page uses the same cockpit (departures or arrivals, timetable with direction) plus sortable tables per line/direction and for the routes starting or ending there
- Start page: switch between map (default) and the route table at every screen width, so the map is not pushed below the table
- Start page: routes as a sortable table (punctuality, median, 90 % value, mini boxplot, cancellations, 30-day sparkline, worst hour, next trains), map below
- Comparison page: matrix route × departure hour (punctuality, median or 90 % value; click an hour to sort by it), daily profile per route on the same scale, sortable KPI table; stations as sortable table
- Stations list as sortable table
- Large answers (`/samples`, `/api/compare/hourly`) are cached as encoded JSON in a size-limited LRU (32 MB) instead of Python objects; the web container stayed close to its 256 MB limit before
- Performance on small servers: statistics are cached 5 min and served stale while they are recomputed in the background; concurrent requests compute a value only once; large answers are gzipped once when cached; one read-only, memory-mapped SQLite connection per worker thread instead of one per request; at most 8 requests are computed at once (`WEB_THREADS`); map geometry loads the station table once instead of once per route and is cached 6 h; local time is converted once per hour instead of per trip; gzip level 6 instead of 9; statistics may be cached by the browser for 60 s (live data never)
- The workbench script is called `werkbank.js`: ad blockers (EasyPrivacy) block scripts named `analyse.js`
- Own JS/CSS is revalidated on every load (`Cache-Control: no-cache` + ETag), so an update shows immediately

- `LIVE_INTERVAL` may be as low as 60 s: runs in between only fetch a small window around now (about one request per board), the full window every 10 min; the collector wakes up in time for the next run

### Added
- Analysis workbench `/analyse` for a route or station: filters for weekdays, hours and lines (kept in the URL), views per hour, weekday, heatmap, distribution, train and single trip; "In der Analyse öffnen" in the cockpit carries the selection over
- `GET /api/stations/{eva}/samples`, `GET /api/compare/hourly`, `GET /api/catalog`; `/api/overview` now includes quantiles and the worst hour per route
- `GET /api/routes/{id}/samples`: all trips of a period in compact form (counting rules as in the aggregates) for quantiles of any selection
- `GET /api/live`: board with next stops and route flag plus the next trains per route, in one request
- Gzip compression for API responses

## [3.0.0] - 2026-11-20

Switch from the Timetables/IRIS API (being phased out by DB) to **OpenBoards**, new data model and new UI.

### Added
- OpenBoards client with automatic window splitting, retries and rate limiting
- Separate **collector** process: live queries every 10 min, daily finalization with real times, automatic backfill of gaps (up to 29 days)
- Edge cases: cancellations, cancelled destination stops (partial), replacement trains (delay measured against the original schedule), rail replacement buses (SEV), relief/extra trains, cancelled additional stops, coupled trains, missing real times, platform changes, delay reasons (RIS cause codes)
- Very late trains (up to ~24 h) are kept and finalized with the following day's board
- Storage schema 3: integer journey ids, Unix times, compact stop lists, tables clustered by station/route and time, daily aggregates; lossless self-checking migration (−61 % size on real data, identical KPIs)
- Station monitor: stations page, station detail (all trains, per line and direction, live departures/arrivals), comparison tab for stations, `STATIONS` + `dbmon add-station`
- Map follows the typical stop sequence of each route (intermediate stops geocoded with a plausibility check)
- Open days (new stations, downtime) are finalized right away instead of waiting for the next `FINALIZE_AT`
- `.env.example`: optional preset of 17 main-line connections between major hubs
- Overview: route search (also filters/zooms the map), jump-to-route dropdown, map style toggle (Standard / Grau / Dunkel)
- Arrival punctuality at the destination
- Station groups via `stada` (e.g. Stuttgart Hbf and Stuttgart Hbf (tief))
- CLI `dbmon` to manage `ROUTES` in `.env` on the server (local station search from collected data, EVA numbers verified via OpenBoards)
- New responsive UI (overview with map, route page with live view, heatmap, trend, distribution, reasons, problem trains, trip list; station page; comparison page), dark mode, self-hosted Leaflet and Chart.js
- Unit tests for classification, migration and configuration

### Changed
- Every train is stored once per observed station; routes are derived from the stored journey path (no duplicates across routes)
- Web UI is strictly read-only: read-only DB connection, no API keys, GET only, no API docs, no CORS, CSP and security headers, no external calls triggered by requests
- Configuration only via `.env` (`ROUTES` with EVA numbers); legacy `POLLING_ROUTES` still accepted
- Docker: two services (`collector`, `web`), unprivileged, read-only containers

### Removed
- `db_live_api.py`, `db_live_connections.py`, Plotly, the old pages, `POLLING_ENABLED`/`POLLING_INTERVAL`/`DELAYED_POLLING_*`
- Automatic migration keeps the old data (`departures_legacy`); trips without real time are no longer counted as on time

## [2.0.0] - 2026-01-03

### Added
- **Docker Support**: Complete Docker deployment with Dockerfile, docker-compose.yml, and .dockerignore
- **Docker Documentation**: Comprehensive DOCKER.md guide for containerized deployment
- **Retry Logic with Exponential Backoff**: API requests now automatically retry on network errors
  - Up to 3 retry attempts
  - Exponential backoff: 2s, 4s, 8s (max 60s)
  - Retries on network errors and 5xx server errors
  - No retry on 4xx client errors
- **Structured Logging**: Replaced all print statements with proper logging
  - Timestamped log messages
  - Log levels: INFO, WARNING, ERROR, DEBUG
  - Better error tracking and debugging
- **Historical Data Collection**: Background polling now fetches past hour data (actual delays) instead of future estimates
- **Background Polling**: Integrated automatic polling within the FastAPI server
- **Interactive Map Interface**: Leaflet.js-based visualization of routes on German map
- **FastAPI Backend**: Modern async API replacing legacy Flask server
- **Boxplot Statistics**: Hourly delay distribution charts for route reliability analysis
- **Auto-Geocoding**: Automatic station coordinate lookup using OpenStreetMap Nominatim

### Changed
- **Logging System**: All output now uses Python's logging module instead of print()
  - db_live_connections.py: Configured logging with timestamps
  - db_live_api.py: Structured logging for all API and polling operations
  - Debug-level logs for detailed station/departure information
  - Info-level logs for polling cycle status
  - Warning/Error logs for issues and failures
- **API Request Handling**:
  - Network requests include automatic retry with exponential backoff
  - Improved error messages and logging
  - Better handling of connection errors and timeouts
- **Polling Time Window**: Changed from future (estimated delays) to past (actual delays)
  - Provides accurate historical performance data
  - Better reliability tracking
- **Documentation**: Updated all docs to reflect new Docker deployment and logging features

### Fixed
- Database schema initialization errors on first run
- Route destination matching issues with station names vs DS100 codes
- Environment variable loading and configuration

### Docker Features
- Health checks for container monitoring
- Volume mounting for data persistence
- Environment file support for easy configuration
- Auto-restart on failure
- Optimized image size with multi-stage builds

### Migration from v1.x
1. Update `.env` file with new polling configuration
2. Install new dependencies: `pip install -r requirements.txt`
3. Optional: Use Docker for simplified deployment
4. Review logging output (no more print statements)

### Configuration
New environment variables:
```env
POLLING_ENABLED=true          # Enable background polling
POLLING_INTERVAL=3600         # Polling interval in seconds
POLLING_ROUTES=...            # Routes to poll (semicolon-separated)
```

### Technical Details
- Python 3.9+ required
- Retry logic: 3 attempts with 2^n backoff (max 60s)
- Logging format: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`
- Docker base image: python:3.11-slim
- Port: 8080 (configurable)

## [1.0.0] - Initial Release
- Basic Flask web server
- Manual polling service
- Table-based UI
- SQLite database storage
