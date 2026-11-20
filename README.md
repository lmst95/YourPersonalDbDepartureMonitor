# Pendelmonitor – Your Personal DB Departure Monitor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Logs the **real** delays of the Deutsche Bahn connections you care about and shows punctuality,
cancellations and patterns on a fast, mobile-friendly web page.

* Data source: DB InfraGO **OpenBoards** API (successor of the Timetables/IRIS API, which DB is phasing out)
* Live view every 10 minutes **plus** a daily finalization with the actual (`REAL`) times
* Gaps (server down, new route) are filled automatically up to 29 days back
* Handles cancellations, cancelled destination stops, replacement trains, rail replacement buses,
  relief/extra trains, coupled trains, platform changes and delay reasons
* Departure **and** arrival punctuality per route
* **Station monitor**: every train at the observed stations (all lines and directions), not only your routes
* Map draws routes along their real intermediate stops
* Public read-only web UI, configuration only on the server (`.env` + CLI `dbmon`)

> **API credentials required.** Create an app in the [DB API Marketplace](https://developers.deutschebahn.com/)
> and subscribe it to **OpenBoards** (plan *Free*). No other subscription is needed.

## How it works

```
             .env (keys, ROUTES)          dbmon (CLI, via SSH)
                    │                            │ edits ROUTES
                    ▼                            ▼
 OpenBoards ──► collector ──writes──► SQLite ◄──reads (read-only)── web ──► browser
                live every 10 min                                     no keys, GET only
                finalize daily 03:00
```

* **collector** – the only component with API keys and write access. Reloads `.env` automatically when it changes.
* **web** – opens the database read-only, needs no keys, sends no requests to external services.
* **dbmon** – adds/removes routes in `.env`, shows status, triggers a backfill.

Design details (data model, edge cases, security): [docs/DESIGN_OPENBOARDS.md](docs/DESIGN_OPENBOARDS.md).

## Quick start (without Docker)

```bash
pip install -r requirements.txt
cp .env.example .env          # enter DB_CLIENT_ID / DB_API_KEY
./dbmon add 8001920 8000096   # EVA numbers of start and destination (see "Finding EVA numbers")
python3 -m dbmonitor.collector            # keeps running: live + daily finalization
python3 -m dbmonitor.web --port 8080      # in a second terminal → http://127.0.0.1:8080
```

An existing database from the old version (`train_db.db`) is migrated automatically on the first collector
start; the old table is kept as `departures_legacy`. Take the old routes over into `.env` with
`./dbmon import-db`.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DB_CLIENT_ID`, `DB_API_KEY` | – | DB API Marketplace credentials (collector and `dbmon` only) |
| `ROUTES` | – | Routes as EVA numbers: `8001920(Esslingen)<->8000096(Stuttgart Hbf);8001920->8000302`. `<->` = both directions. Names in `()` are labels only. The legacy `POLLING_ROUTES` with station names is still read. |
| `DB_PATH` | `./train_db.db` | SQLite file (relative to the `.env`) |
| `LIVE_INTERVAL` | `600` | Seconds between live queries (min. 120) |
| `FINALIZE_AT` | `03:00` | Daily finalization of the previous days |
| `BACKFILL_DAYS` | `29` | How many past days are filled in automatically (OpenBoards keeps ~30 days) |
| `STATIONS` | – | Extra stations observed on their own (e.g. an intermediate stop): `8006642(Esslingen-Zell);8005769`. Start and destination stations of `ROUTES` are always observed. |
| `GEOCODING` | `nominatim` | Map coordinates of configured stations via OpenStreetMap Nominatim; `off` disables it |

## `dbmon` CLI

```
dbmon routes                  configured routes with data status
dbmon add                     interactive: name (searched locally) or EVA number for start and destination
dbmon add 8001920 8000096     directly with EVA numbers (--one-way for one direction)
dbmon remove                  remove routes (stored data is kept, the route is hidden in the UI)
dbmon stations               stations observed on their own (STATIONS)
dbmon add-station [EVA]       observe a station without a route, e.g. an intermediate stop
dbmon remove-station          stop observing such a station (data is kept)
dbmon search "Esslingen"      find EVA numbers in the stations already seen by the collector
dbmon status                  last live query, finalized days, gaps, last error
dbmon backfill --days 7       finalize missing days right now
dbmon import-db               write the routes stored in the database into ROUTES
```

`dbmon` only changes the `ROUTES=` line, keeps everything else and writes a `.env.bak` backup.
The collector picks up the change within 30 s.

### Finding EVA numbers

The EVA number (also IBNR) is the 7-digit stop number used by DB, e.g. `8000096` = Stuttgart Hbf.

* `dbmon search NAME` searches every stop the collector has already seen. Each board response contains all
  stops of the trains at your stations, so after the first run the surrounding network is known.
* On bahn.de, search a connection and copy/share its link: the station appears as `@L=8000096@`.
* `dbmon add` verifies a number with one OpenBoards request and fills in the name automatically.

## How the real delay is captured

OpenBoards filters its time window by the **best known time** (forecast or real), not by the schedule.
A train that is 2 h late therefore stays in the "now" window until it has actually left.

| Step | When | Window | Result |
|---|---|---|---|
| Live | every `LIVE_INTERVAL` (10 min) | departures: now −60 min … +90 min, arrivals: −60 … +180 min (by actual time) | forecast while waiting (`PREVIEW`), real time (`REAL`) as soon as the train has left/arrived |
| Finalize | continuously for every day not finalized yet; yesterday only after `FINALIZE_AT` (03:00) | the whole day 00:00–24:00 **plus 4 h** (late trains after midnight) | final `REAL` times and cancellations, `final = 1`; live values can never overwrite them |
| Backfill | on start, on a new route, after downtime | same as finalize, up to `BACKFILL_DAYS` back | gaps are closed automatically |

Stored is everything the board returns with a planned time before the end of the window – including trains
planned much earlier that only now appear because they are very late.

Example – planned 19:00, departs 10 h late at 05:00 the next day:

1. Live: as soon as its forecast moves into the window (≈ 03:30), it is shown and stored; at 05:00 it gets its
   real time.
2. The finalization of the planned day (03:00) cannot contain it yet (it has not left) – and does not delete it.
3. The finalization of the **next** day (03:00 one day later) queries all actual times of that day, finds the
   straggler and stores it as final. This costs no extra requests.

Delays of up to ~24 h are covered this way. Trains that never get a real time are stored as *ohne Ist-Daten*
and are not counted as on time.

## Docker

```bash
docker compose up -d --build
docker compose exec collector dbmon add
docker compose logs -f collector
```

Two services from one image: `collector` (gets the `.env`) and `web` (no `.env`, read-only container).
See [DOCKER.md](DOCKER.md) for reverse proxy, backups and upgrading from the old single container.

## Web UI

| Page | Content |
|---|---|
| **Strecken** `/` | route cards with punctuality, trend and next departures; search, jump-to dropdown; map along the real stops with style toggle |
| Route `/route/{id}` | KPIs (departure + arrival), live view, heatmap, trend, distribution, reasons, problem trains, trip list |
| **Bahnhöfe** `/stations` | all observed stations: departure/arrival punctuality, cancellations, trains per day |
| Station `/station/{eva}` | all trains at the station: KPIs, live board (departures/arrivals), punctuality per line and direction, heatmap, trend, reasons, problem trains, routes from/to here |
| **Vergleich** `/stats` | routes or stations side by side, sortable, with search |

Each observed station is queried for departures and arrivals. Station statistics use OpenBoards data only:
the legacy data contains just the trains of the configured routes.

## Metrics

* **Punctual** = less than 6 minutes late (DB definition), based on trips with real measured time.
* **Cancellation rate** = cancelled trips + trips whose destination stop was cancelled, divided by planned trips.
* **Traveller view** = punctuality that also counts cancellations as not on time.
* Trips without a real time (API never delivered one) are shown as *ohne Ist-Daten* and not counted.
* A cancelled train replaced by a replacement train counts once, with the delay of the replacement train
  relative to the original schedule.

## Storage

SQLite (one file, WAL mode: the collector writes while the web UI reads). Schema 3:

* `stop_events` – every train at an observed station, clustered by station, kind and time (`WITHOUT ROWID`).
  Journey ids are small integers (`journeys` maps them to the full OpenBoards id), times are Unix seconds,
  the stop sequence is a compact list (`8001921,8006643x,8000096`).
* `route_trips` – classified trips per route, clustered by route and time.
* `agg_hour`, `agg_line`, `agg_cause` – daily aggregates written when a day is finalized. Long periods are
  answered from these; today and days not finalized yet are computed from the raw rows with the same function.

Older databases are migrated automatically on collector start. The migration builds the new tables next to the
old ones, converts every row back and compares it with the original, and swaps the tables only if nothing
differs. Expect roughly 5–6 GB per year with ~15 major hubs.

## Development

```bash
python3 -m unittest discover -s tests -v       # classification, migration and config tests
python3 experiments/openboards/openboards_client.py probe --origin 8000105 --dest 8000244
```

```
dbmonitor/
  openboards.py   OpenBoards client (window splitting, retries, rate limit)
  stations.py     local station search, Nominatim coordinates (stations and intermediate stops)
  config.py       .env parsing/writing (ROUTES)
  storage.py      schema 3, compact encodings, upserts, route + station classification, aggregates, route geometry
  migrate.py      legacy -> schema 2 -> schema 3 (lossless, self-checking)
  collector.py    live tick, daily finalization, backfill, geocoding
  cli.py          dbmon
  web.py          read-only FastAPI app + aggregated JSON API
static/           pages (index, route, stations, station, stats, privacy), css, js, vendor (Leaflet, Chart.js)
```

## License

MIT – see [LICENSE](LICENSE).

* **Deutsche Bahn data**: requires your own API credentials; usage is subject to the terms of the
  DB API Marketplace. The data remains the property of Deutsche Bahn / DB InfraGO.
* **OpenStreetMap**: map tiles and Nominatim geocoding are subject to the OSMF usage policies.
* Bundled libraries: Leaflet (BSD-2-Clause), Chart.js (MIT). Python: FastAPI (MIT), Uvicorn (BSD-3-Clause), Requests (Apache 2.0).
* No warranty. For API access questions contact the DB API Marketplace.
