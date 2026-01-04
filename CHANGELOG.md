# Changelog

All notable changes to the DB Live Tracker project.

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
