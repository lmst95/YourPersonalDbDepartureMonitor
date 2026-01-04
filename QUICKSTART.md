# Quick Start Guide

Get the DB Live Tracker running in 5 minutes!

## Prerequisites

1. **Python 3.9+** installed
2. **Deutsche Bahn API credentials** ([Sign up here](https://developers.deutschebahn.com/))

## Step-by-Step Setup

### 1. Install Dependencies

```bash
# Create virtual environment (optional but recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install packages
pip install -r requirements.txt
```

### 2. Configure API Credentials

Create a `.env` file in the project directory:

```bash
cat > .env << EOF
DB_CLIENT_ID=your_client_id_here
DB_API_KEY=your_api_key_here
EOF
```

Replace `your_client_id_here` and `your_api_key_here` with your actual credentials.

### 3. Collect Some Data

Run the polling service to collect initial data:

```bash
# Single poll to test
python db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf"

# Store to database
python db_live_connections.py \
  --from "Berlin Hbf" \
  --to "Hamburg Hbf" \
  --store \
  --db ./train_db.db

# Use a longer time window (e.g., 3 hours)
python db_live_connections.py \
  --from "Berlin Hbf" \
  --to "Hamburg Hbf" \
  --window 3 \
  --store
```

You should see output like:
```
Start: Berlin Hbf (EVA 8011160, RIL100 BL)
Ziel : Hamburg Hbf (EVA 8002549, RIL100 AH)
...
✓ 5 Abfahrten in Datenbank gespeichert (./train_db.db)
```

### 4. Start the Web Server

```bash
python db_live_api.py --db ./train_db.db --host 127.0.0.1 --port 8080
```

You should see:
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8080
```

### 5. Open in Browser

Visit [http://localhost:8080](http://localhost:8080)

You should see:
- An interactive map of Germany
- Route(s) drawn as lines between stations
- Click on a route to view statistics

## Continuous Data Collection

### Option 1: Background Polling (Easiest!)

Enable automatic polling within the web server by editing `.env`:

```env
POLLING_ENABLED=true
POLLING_INTERVAL=3600
POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf;München Hbf->Frankfurt Hbf
```

**Route Format:**
- `Station A<->Station B` = Bidirectional (both directions automatically)
- `Station A->Station B` = Unidirectional (only this direction)

Then just start the server - polling runs automatically!

```bash
python db_live_api.py --db ./train_db.db --port 8080
```

The server will poll your configured routes every hour (3600 seconds), fetching the past hour's data to capture actual delays instead of estimates.

See [BACKGROUND_POLLING.md](BACKGROUND_POLLING.md) for more details.

### Option 2: Separate Polling Process

For more control, use the `--interval` flag:

```bash
# Poll every 5 minutes (300 seconds)
python db_live_connections.py \
  --from "Berlin Hbf" \
  --to "Hamburg Hbf" \
  --store \
  --db ./train_db.db \
  --interval 300
```

## Troubleshooting

### "Missing environment variables DB_CLIENT_ID / DB_API_KEY"

Make sure:
1. You created the `.env` file in the project directory
2. The file contains valid credentials
3. The file is in the same directory as the scripts

### "No routes visible on map"

You need to collect data first:
1. Run the polling service with `--store` flag
2. Check database: `sqlite3 train_db.db "SELECT * FROM routes;"`
3. Refresh the browser page

### Server won't start

Check if port 8080 is already in use:
```bash
# Try a different port
python db_live_api.py --port 8081
```

### Import errors

Make sure all dependencies are installed:
```bash
pip install -r requirements.txt
```

## Next Steps

1. **Add more routes**: Edit the `POLLING_ROUTES` in your `.env` file
2. **Explore the API**: Visit [http://localhost:8080/docs](http://localhost:8080/docs)
3. **Customize the map**: Edit `static/index.html` and `static/app.js`
4. **Docker deployment**: See [DOCKER.md](DOCKER.md) for containerized deployment
5. **Schedule polling**: Set up as a systemd service (see README.md)

## Getting Help

- Check the full [README.md](README.md) for detailed documentation
- View API docs at `/docs` endpoint
- Inspect database: `sqlite3 train_db.db`

Enjoy tracking trains!
