# Docker deployment

## Services

| Service | Gets | Does |
|---|---|---|
| `collector` | `.env` (mounted to `/config/.env`), database volume | fetches OpenBoards data, migrates/updates the database, runs `dbmon` |
| `web` | database volume only – **no `.env`, no keys** | serves the public read-only UI on port 8080 |

Both containers run as an unprivileged user with a read-only root filesystem and no capabilities.

## Start

```bash
cp .env.example .env              # enter DB_CLIENT_ID / DB_API_KEY (the file must exist before "up")
docker compose up -d --build
docker compose exec collector dbmon add      # add routes (name search or EVA numbers)
docker compose exec collector dbmon status
docker compose logs -f collector
```

Open http://localhost:8080.

`dbmon` writes the mounted `.env` (inside the container as uid 1000). If the host file is owned by another
user, make it writable for uid 1000 or edit `ROUTES` on the host; the collector reloads the file
automatically either way.

## Upgrading an existing installation (≤ 2.x, single container `db-live-tracker`)

The database is migrated automatically on the first start of the new collector (old IRIS data → schema 3).
The migration checks every row and changes nothing if anything differs; the original legacy table is kept
as `departures_legacy`. Still, take a backup first.

1. **Subscribe OpenBoards.** In the DB API Marketplace, subscribe the existing app to *OpenBoards* (plan Free).
   The credentials in `.env` stay the same.
2. **Stop the old container and back up the database:**
   ```bash
   docker compose stop
   docker cp db-live-tracker:/data/train_db.db ./train_db.backup.db
   ```
3. **Update the code** (stay in the same directory – the volume name depends on it):
   ```bash
   git pull
   ```
4. **Prepare `.env`:**
   - Comment out `POLLING_ROUTES` (and remove `POLLING_ENABLED`, `POLLING_INTERVAL`, `DELAYED_POLLING_*`).
     The routes are taken from the database and written to `ROUTES` in step 6.
   - The collector writes `.env` as uid 1000 (`dbmon add`, `import-db`). On Linux, if your user has another uid:
     `sudo chown 1000 .env` (keep the mode at 600), or edit `ROUTES` on the host instead.
5. **Start the new stack** (removes the old single container, keeps the volume):
   ```bash
   docker compose up -d --build --remove-orphans
   docker compose logs -f collector   # wait for "Umbau fertig … keine Abweichung"
   ```
6. **Take the routes over into `.env`:**
   ```bash
   docker compose exec collector dbmon import-db
   docker compose exec collector dbmon status
   ```
7. The collector now fetches the last 29 days from OpenBoards (≈ 30 min for 20 routes, ≈ 2 h with the
   main-line preset) and replaces the old IRIS data of that period. The web UI is usable right away.

**Rollback:** `docker compose down`, check out the previous version (`git checkout <old commit>`), copy
`train_db.backup.db` back into the volume as `train_db.db`, start the old version.

### Without Docker

```bash
git pull && pip install -r requirements.txt
cp train_db.db train_db.backup.db
python3 -m dbmonitor.collector          # migrates on first start, then keeps running
./dbmon import-db                       # in a second shell, once the migration is done
python3 -m dbmonitor.web --port 8080    # e.g. as systemd services, see README
```

## Behind a reverse proxy (recommended for public access)

Bind the web service to localhost (`"127.0.0.1:8080:8080"` in `docker-compose.yml`) and terminate TLS in
the proxy. Example for Caddy:

```
monitor.example.org {
    reverse_proxy 127.0.0.1:8080
    # optional: limit request rate per client with the caddy-ratelimit plugin
}
```

The app sends its own security headers (CSP, `X-Frame-Options`, `nosniff`, …), answers GET only and has no
API documentation endpoints.

## Backups

The database lives in the volume `db-data`. A consistent copy while running:

```bash
docker compose exec collector python -c "import sqlite3; s=sqlite3.connect('/data/train_db.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
docker compose cp collector:/data/backup.db ./backup.db
```
