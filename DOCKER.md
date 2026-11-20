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

## Upgrading from the single-container version (≤ 2.x)

1. `git pull`, then `docker compose up -d --build`. The old container `db-live-tracker` is replaced; the
   volume `db-data` (and with it `train_db.db`) is kept.
2. On the first start the collector migrates the database (old table kept as `departures_legacy`).
3. `docker compose exec collector dbmon import-db` writes the existing routes into `ROUTES` in `.env`.
   Remove the old `POLLING_*` / `DELAYED_POLLING_*` variables – they are no longer used.
4. The collector then re-fetches the last 29 days from OpenBoards (≈ 30 minutes for 20 routes) and
   replaces the old IRIS data for that period.

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
