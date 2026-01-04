# DB Live Tracker

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-00a393.svg)](https://fastapi.tiangolo.com)

A real-time Deutsche Bahn train tracking system with interactive map visualization. Track train departures, delays, and reliability statistics across German railway routes.

> **⚠️ API Credentials Required**: This application requires Deutsche Bahn API credentials. You must sign up at [Deutsche Bahn Developer Portal](https://developers.deutschebahn.com/) to obtain your own `DB_CLIENT_ID` and `DB_API_KEY`. This software is provided under the MIT License, but usage of Deutsche Bahn's API is subject to their own terms of service.

## Features

- **Historical Data Polling**: Continuously polls Deutsche Bahn IRIS API for past hour train data (actual delays, not estimates)
- **Background Polling**: Built-in automatic polling within the web server (no separate processes needed!)
- **Interactive Map**: Visualize all tracked routes on an interactive German map using Leaflet.js
- **Statistical Analysis**: Hourly boxplot statistics showing delay patterns and reliability
- **REST API**: Full-featured FastAPI backend for data access
- **Auto-geocoding**: Automatic station coordinate lookup using OpenStreetMap Nominatim

## Architecture

### Components

1. **Polling Service** ([db_live_connections.py](db_live_connections.py))
   - Fetches live train data from Deutsche Bahn IRIS API
   - Stores departure information to SQLite database
   - Supports one-time or continuous polling

2. **FastAPI Backend** ([db_live_api.py](db_live_api.py))
   - REST API for routes, departures, and statistics
   - Auto-geocoding of station coordinates
   - Serves interactive frontend

3. **Interactive Frontend** ([static/](static/))
   - Leaflet.js map showing all routes
   - Hover over routes to see details
   - Click to view hourly delay statistics (boxplots)

### Database Schema

**Routes Table:**
```sql
CREATE TABLE routes (
    id INTEGER PRIMARY KEY,
    origin_name TEXT NOT NULL,
    dest_name TEXT NOT NULL,
    origin_eva TEXT NOT NULL,
    dest_eva TEXT NOT NULL,
    origin_lat REAL,
    origin_lon REAL,
    dest_lat REAL,
    dest_lon REAL
);
```

**Departures Table:**
```sql
CREATE TABLE departures (
    id INTEGER PRIMARY KEY,
    route_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    category TEXT,
    number TEXT,
    planned_dt TIMESTAMP NOT NULL,
    realtime_dt TIMESTAMP,
    delay_min INTEGER,
    planned_platform TEXT,
    realtime_platform TEXT,
    inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Setup

### Prerequisites

- Python >= 3.9
- Deutsche Bahn API credentials ([Get them here](https://developers.deutschebahn.com/))

### Installation

1. **Clone or download this repository**

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure API credentials:**

   Create a `.env` file in the project directory:
   ```env
   DB_CLIENT_ID=your_client_id_here
   DB_API_KEY=your_api_key_here
   ```

## Usage

### Option A: Background Polling (Recommended - Easiest!)

The simplest way to run the system is to use the built-in background polling:

1. **Configure routes in `.env`:**
   ```env
   POLLING_ENABLED=true
   POLLING_INTERVAL=3600
   POLLING_ROUTES=Berlin Hbf->Hamburg Hbf;Hamburg Hbf->Berlin Hbf;München Hbf->Frankfurt Hbf
   ```

2. **Start the server:**
   ```bash
   python db_live_api.py --db ./train_db.db --port 8080
   ```

That's it! The server will automatically poll your routes every hour and serve the web interface.

See [BACKGROUND_POLLING.md](BACKGROUND_POLLING.md) for detailed configuration options.

### Option B: Manual Polling (Advanced)

For more control, use the separate polling service:

#### 1. Collect Data (Polling Service)

**Single poll (console output only):**
```bash
python db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf"
```

**Store to database:**
```bash
python db_live_connections.py \
  --from "Berlin Hbf" \
  --to "Hamburg Hbf" \
  --store \
  --db ./train_db.db
```

**Continuous polling (every 5 minutes):**
```bash
python db_live_connections.py \
  --from "Berlin Hbf" \
  --to "Hamburg Hbf" \
  --store \
  --db ./train_db.db \
  --interval 300
```

**Multiple routes** (recommended: use a shell script):
```bash
#!/bin/bash
# poll_routes.sh

python db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf" --store --interval 300 &
python db_live_connections.py --from "München Hbf" --to "Frankfurt Hbf" --store --interval 300 &
python db_live_connections.py --from "Köln Hbf" --to "Berlin Hbf" --store --interval 300 &

wait
```

#### 2. Start Web Server

**Start FastAPI server:**
```bash
python db_live_api.py --db ./train_db.db --host 0.0.0.0 --port 8080
```

Then open: [http://localhost:8080](http://localhost:8080)

### 3. Production Deployment

**Using systemd (Linux):**

Create `/etc/systemd/system/db-live-poller.service`:
```ini
[Unit]
Description=DB Live Train Polling Service
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/db_api
Environment="DB_CLIENT_ID=your_client_id"
Environment="DB_API_KEY=your_api_key"
ExecStart=/usr/bin/python3 db_live_connections.py --from "Berlin Hbf" --to "Hamburg Hbf" --store --interval 300
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/db-live-api.service`:
```ini
[Unit]
Description=DB Live API Server
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/db_api
ExecStart=/usr/bin/python3 db_live_api.py --db /path/to/train_db.db --host 0.0.0.0 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable db-live-poller db-live-api
sudo systemctl start db-live-poller db-live-api
```

**Using Docker (Recommended):**

The easiest way to deploy is using Docker with the provided configuration files.

**Quick start with Docker Compose:**
```bash
# Build and start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Access at http://localhost:8080
```

**Or with Docker directly:**
```bash
# Build the image
docker build -t db-live-tracker .

# Run with environment file
docker run -d -p 8080:8080 \
  --env-file .env \
  -v $(pwd)/data:/data \
  db-live-tracker
```

The Docker container automatically:
- Loads credentials from `.env` file
- Starts background polling (if enabled)
- Persists data to mounted volume
- Includes health checks and auto-restart

**See [DOCKER.md](DOCKER.md) for complete Docker deployment guide** including:
- Configuration options
- Production deployment
- Monitoring and troubleshooting
- Backup/restore procedures
- Reverse proxy setup

## API Documentation

Once the server is running, visit:
- Interactive API docs: [http://localhost:8080/docs](http://localhost:8080/docs)
- Alternative docs: [http://localhost:8080/redoc](http://localhost:8080/redoc)

### Key Endpoints

**GET /api/routes**
- Returns all routes with coordinates
- Auto-geocodes missing coordinates

**GET /api/routes/{route_id}/stats**
- Returns hourly statistics for a specific route
- Includes delay distributions for boxplot visualization

**GET /api/departures**
- Query parameters:
  - `route_id`: Filter by route
  - `since`: Hours to look back (default: 24)
  - `q`: Search query
  - `limit`: Result limit (default: 1000)
  - `offset`: Pagination offset

## Frontend Usage

1. **Open the map** at [http://localhost:8080](http://localhost:8080)
2. **View routes**: All tracked routes are shown as lines on the map
3. **Hover** over a route to highlight it
4. **Click** on a route to see a popup with basic information
5. **Click "Statistik anzeigen"** to open detailed hourly statistics with boxplots

## Development

### Project Structure

```
db_api/
├── db_live_connections.py  # Polling service
├── db_live_api.py          # FastAPI server with background polling
├── requirements.txt        # Python dependencies
├── .env                    # API credentials (create this)
├── .gitignore             # Git ignore rules
├── static/                # Frontend files
│   ├── index.html         # Main HTML with map interface
│   ├── details.html       # Departure details page
│   ├── app.js             # Map application JavaScript
│   └── details.js         # Details page JavaScript
└── train_db.db            # SQLite database (created automatically)
```

### Adding New Features

**Custom statistics:**
Edit `calculate_hourly_stats()` in [db_live_api.py](db_live_api.py:127)

**Map customization:**
Edit styles in [static/index.html](static/index.html)

**Different visualizations:**
Modify `createBoxplot()` in [static/app.js](static/app.js)

## Troubleshooting

### "Missing environment variables DB_CLIENT_ID / DB_API_KEY"
- Create a `.env` file with your credentials
- Or set environment variables: `export DB_CLIENT_ID=...`

### "No routes visible on map"
- Ensure you've collected data first using the polling service
- Check database: `sqlite3 train_db.db "SELECT * FROM routes;"`
- Coordinates are auto-geocoded on first API request (may take time)

### "Geocoding failed"
- Nominatim has rate limits (1 request/second)
- Server will automatically retry and cache results
- For many routes, consider pre-populating coordinates

### "CORS errors in browser"
- FastAPI has CORS enabled by default for development
- For production, configure CORS middleware properly

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

### Third-Party Services & Data

**Important Disclaimers:**

- **Deutsche Bahn API**: This software requires access to the Deutsche Bahn IRIS API. You must obtain your own API credentials from [Deutsche Bahn Developer Portal](https://developers.deutschebahn.com/) and comply with their terms of service. The data retrieved from the API remains the property of Deutsche Bahn AG.

- **OpenStreetMap Data**: Geocoding services use OpenStreetMap Nominatim, subject to [OSM's usage policy](https://operations.osmfoundation.org/policies/nominatim/).

- **No Warranty**: This software is provided "as is" without warranty of any kind. The authors are not responsible for any issues arising from the use of this software or third-party APIs.

### Open Source Dependencies

This project uses the following open-source libraries:

- **FastAPI** (MIT License) - Modern Python web framework
- **Uvicorn** (BSD-3-Clause License) - ASGI server
- **Requests** (Apache 2.0 License) - HTTP library
- **Leaflet.js** (BSD-2-Clause License) - Interactive maps
- **Plotly.js** (MIT License) - Statistical visualizations

All dependencies maintain licenses compatible with the MIT License.

## Credits

- **Deutsche Bahn**: For providing the IRIS API
- **OpenStreetMap Contributors**: For geocoding data
- **Open Source Community**: For the excellent libraries used in this project

## Support

For issues or questions, please open an issue on the project repository.

**Note**: For API access issues, rate limits, or data-related questions, please contact Deutsche Bahn support directly.
