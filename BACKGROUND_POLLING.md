# Background Polling Guide

The FastAPI server now includes built-in background polling functionality, eliminating the need to run separate polling processes.

## How It Works

When enabled, the server automatically polls configured routes at regular intervals and stores the data to the database. This runs as a background task within the FastAPI server process.

**Data Collection Strategy:** The system fetches data from the **past hour** rather than the future. This ensures you capture actual delay data instead of estimated delays, providing accurate historical tracking of train performance.

## Configuration

All configuration is done via the `.env` file:

### 1. Enable Polling

```env
POLLING_ENABLED=true
```

Set to `true` to enable, `false` to disable.

### 2. Set Polling Interval

```env
POLLING_INTERVAL=3600
```

Interval in seconds between polling cycles:
- `3600` = 1 hour (recommended)
- `1800` = 30 minutes
- `7200` = 2 hours

**Important:** Each polling cycle fetches data for the **past hour** to capture actual delays (not estimates). The system queries trains that have already departed to get real delay information. If you set the interval to 1 hour, you get complete coverage with no gaps.

### 3. Configure Routes

```env
POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf;München Hbf->Frankfurt Hbf
```

**Route Format:**
- **Bidirectional**: `Origin<->Destination` (automatically polls both directions)
- **Unidirectional**: `Origin->Destination` (polls only this direction)
- **Multiple routes**: Separate with semicolons (`;`)

**Example with multiple routes:**
```env
POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf;München Hbf<->Frankfurt Hbf;Köln Hbf->Berlin Hbf
```

This is much simpler than the old way:
```env
# Old way (still supported):
POLLING_ROUTES=Berlin Hbf->Hamburg Hbf;Hamburg Hbf->Berlin Hbf;München Hbf->Frankfurt Hbf;Frankfurt Hbf->München Hbf
```

## Complete .env Example

```env
# Deutsche Bahn API Credentials
DB_CLIENT_ID=your_client_id_here
DB_API_KEY=your_api_key_here

# Background Polling Configuration
POLLING_ENABLED=true
POLLING_INTERVAL=3600
POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf;München Hbf<->Frankfurt Hbf
```

## Starting the Server with Background Polling

Simply start the server as usual:

```bash
python db_live_api.py --db ./train_db.db --port 8080
```

You'll see output like:

```
Starting background polling:
  - Interval: 3600 seconds (1.0 hours)
  - Routes: 4
    • Berlin Hbf → Hamburg Hbf
    • Hamburg Hbf → Berlin Hbf
    • München Hbf → Frankfurt Hbf
    • Frankfurt Hbf → München Hbf

INFO:     Started server process [12345]
INFO:     Uvicorn running on http://127.0.0.1:8080
```

## Monitoring Polling Status

Visit the polling status endpoint:

```bash
curl http://localhost:8080/api/polling/status
```

Response:
```json
{
  "enabled": true,
  "interval_seconds": 3600,
  "interval_hours": 1.0,
  "routes_count": 4,
  "routes": [
    {"origin": "Berlin Hbf", "destination": "Hamburg Hbf"},
    {"origin": "Hamburg Hbf", "destination": "Berlin Hbf"},
    {"origin": "München Hbf", "destination": "Frankfurt Hbf"},
    {"origin": "Frankfurt Hbf", "destination": "München Hbf"}
  ],
  "running": true
}
```

Or visit in your browser: [http://localhost:8080/api/polling/status](http://localhost:8080/api/polling/status)

## Polling Cycle Logs

Each polling cycle logs its progress:

```
[2026-01-03 14:00:00] Starting polling cycle...
  ✓ Berlin Hbf → Hamburg Hbf: 5 departures stored
  ✓ Hamburg Hbf → Berlin Hbf: 6 departures stored
  ✓ München Hbf → Frankfurt Hbf: 4 departures stored
  ✓ Frankfurt Hbf → München Hbf: 3 departures stored
Polling cycle complete. Total: 18 departures stored
```

## Advantages Over Separate Polling Processes

1. **Simpler deployment**: One process instead of multiple
2. **Easier monitoring**: Check status via API endpoint
3. **Resource efficient**: Shares the same Python process
4. **Automatic startup**: Polling starts with the server
5. **Graceful shutdown**: Stops cleanly when server stops

## Comparison: Background vs. Separate Polling

### Background Polling (Recommended)
```bash
# Configure in .env
POLLING_ENABLED=true
POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf;München Hbf<->Frankfurt Hbf

# Start server (polling runs automatically)
python db_live_api.py --db ./train_db.db
```

**Pros:**
- Single process
- Automatic startup/shutdown
- Easy status monitoring
- Simpler deployment

**Cons:**
- All routes poll on same schedule
- Requires server restart to change routes

### Separate Polling Processes
```bash
# Start multiple polling processes
python db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf" --store --interval 3600 &
python db_live_connections.py --from "Hamburg Hbf" --to "Berlin Hbf" --store --interval 3600 &
# ... etc
```

**Pros:**
- Independent schedules per route
- Can restart individual pollers
- More granular control

**Cons:**
- Multiple processes to manage
- More complex deployment
- Higher resource usage

## Production Deployment

### Systemd Service

Create `/etc/systemd/system/db-live-tracker.service`:

```ini
[Unit]
Description=DB Live Tracker with Background Polling
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/db_api
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python db_live_api.py --db /path/to/train_db.db --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable db-live-tracker
sudo systemctl start db-live-tracker
sudo systemctl status db-live-tracker
```

View logs:
```bash
sudo journalctl -u db-live-tracker -f
```

### Docker

Update your `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Environment variables will be set via docker run or docker-compose
ENV POLLING_ENABLED=true
ENV POLLING_INTERVAL=3600

EXPOSE 8080

CMD ["python", "db_live_api.py", "--db", "/data/train_db.db", "--host", "0.0.0.0", "--port", "8080"]
```

Run with environment file:
```bash
docker run -d -p 8080:8080 \
  --env-file .env \
  -v $(pwd)/data:/data \
  db-live-tracker
```

Or use Docker Compose (`docker-compose.yml`):

```yaml
version: '3.8'

services:
  db-live-tracker:
    build: .
    ports:
      - "8080:8080"
    env_file:
      - .env
    volumes:
      - ./data:/data
    restart: unless-stopped
```

Start:
```bash
docker-compose up -d
```

## Troubleshooting

### Polling not starting

**Check `.env` file:**
```bash
cat .env | grep POLLING
```

Ensure:
- `POLLING_ENABLED=true` (not `false`)
- `POLLING_ROUTES` contains valid route definitions
- Routes use `->` (unidirectional) or `<->` (bidirectional) separator

### Import errors

If you see "Error importing polling functions", ensure:
- `db_live_connections.py` is in the same directory
- All dependencies are installed: `pip install -r requirements.txt`

### API errors

If polling fails with API errors:
- Check DB_CLIENT_ID and DB_API_KEY are valid
- Verify you haven't exceeded API rate limits
- Check station names are correct (exact match required)

### Database locked errors

If you see "database is locked":
- SQLite has limitations with concurrent writes
- Reduce polling frequency
- Or use PostgreSQL for production (requires code changes)

## Changing Configuration

To change polling configuration:

1. Edit `.env` file
2. Restart the server
3. Check status endpoint to verify new config

```bash
# Edit .env
nano .env

# Restart server (if using systemd)
sudo systemctl restart db-live-tracker

# Check new configuration
curl http://localhost:8080/api/polling/status
```

## Disabling Background Polling

Set in `.env`:
```env
POLLING_ENABLED=false
```

Or remove the line entirely (defaults to `false`).

## Best Practices

1. **Start with 1-hour intervals** for complete coverage
2. **Use bidirectional notation** (`<->`) for routes where you want both directions - it's simpler and less error-prone
3. **Monitor logs** during first few cycles to ensure it's working
4. **Use systemd** or Docker for automatic restarts
5. **Check API limits** - don't poll too frequently or with too many routes

Enjoy simplified polling with your DB Live Tracker!
