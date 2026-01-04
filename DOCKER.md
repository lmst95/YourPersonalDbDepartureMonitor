# Docker Deployment Guide

Run the DB Live Tracker in a Docker container with automatic background polling.

## Quick Start

### 1. Prerequisites

- Docker installed ([Get Docker](https://docs.docker.com/get-docker/))
- Docker Compose installed (included with Docker Desktop)
- Valid Deutsche Bahn API credentials in `.env` file

### 2. Build and Run

**Using Docker Compose (Recommended):**

```bash
# Build and start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down
```

**Using Docker directly:**

```bash
# Build the image
docker build -t db-live-tracker .

# Run the container
docker run -d \
  --name db-live-tracker \
  -p 8080:8080 \
  --env-file .env \
  -v $(pwd)/data:/data \
  db-live-tracker

# View logs
docker logs -f db-live-tracker

# Stop the container
docker stop db-live-tracker
docker rm db-live-tracker
```

### 3. Access the Application

Open your browser to: [http://localhost:8080](http://localhost:8080)

## Configuration

The container reads configuration from your `.env` file:

```env
# API Credentials (Required)
DB_CLIENT_ID=your_client_id_here
DB_API_KEY=your_api_key_here

# Background Polling (Optional)
POLLING_ENABLED=true
POLLING_INTERVAL=3600
POLLING_ROUTES=Stuttgart Hbf->Göppingen;Hamburg Hbf->Berlin Hbf
```

## Data Persistence

The database is stored in a Docker volume to persist data across container restarts.

**Docker Compose:** Data is stored in `./data/` directory on your host machine.

**Docker direct:** Use the `-v` flag to mount a volume:
```bash
docker run -v $(pwd)/data:/data ...
```

## Health Check

The container includes a health check that verifies the API is responding:

```bash
# Check container health
docker ps

# Manual health check
docker exec db-live-tracker python -c "import requests; print(requests.get('http://localhost:8080/api/routes').status_code)"
```

## Monitoring

### View Logs

```bash
# Docker Compose
docker-compose logs -f

# Docker
docker logs -f db-live-tracker
```

### Check Polling Status

```bash
# Via API
curl http://localhost:8080/api/polling/status

# Or open in browser
open http://localhost:8080/api/polling/status
```

### Container Stats

```bash
docker stats db-live-tracker
```

## Updating

### Update Configuration

1. Edit `.env` file
2. Restart container:
   ```bash
   docker-compose restart
   # or
   docker restart db-live-tracker
   ```

### Update Code

1. Pull latest changes
2. Rebuild and restart:
   ```bash
   docker-compose down
   docker-compose build
   docker-compose up -d
   ```

## Troubleshooting

### Container won't start

Check logs for errors:
```bash
docker-compose logs
```

Common issues:
- Missing `.env` file
- Invalid API credentials
- Port 8080 already in use (change in `docker-compose.yml`)

### No data appearing

1. Check polling is enabled:
   ```bash
   curl http://localhost:8080/api/polling/status
   ```

2. Verify `.env` has correct settings:
   ```bash
   docker-compose exec db-live-tracker env | grep POLLING
   ```

3. Check database exists:
   ```bash
   docker-compose exec db-live-tracker ls -lh /data/
   ```

### Health check failing

```bash
# Check if server is responding
docker-compose exec db-live-tracker curl http://localhost:8080/api/routes

# Check Python/dependencies
docker-compose exec db-live-tracker python --version
docker-compose exec db-live-tracker pip list
```

## Production Deployment

### Environment Variables

For production, use environment variables instead of `.env` file:

```bash
docker run -d \
  --name db-live-tracker \
  -p 8080:8080 \
  -e DB_CLIENT_ID=your_id \
  -e DB_API_KEY=your_key \
  -e POLLING_ENABLED=true \
  -e POLLING_INTERVAL=3600 \
  -e POLLING_ROUTES="Berlin Hbf->Hamburg Hbf;Hamburg Hbf->Berlin Hbf" \
  -v /opt/db-live-tracker/data:/data \
  db-live-tracker
```

### Reverse Proxy (nginx)

```nginx
server {
    listen 80;
    server_name trains.example.com;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Docker Swarm / Kubernetes

The application is stateless (database is external) and can be scaled horizontally if using a shared database volume.

**Note:** SQLite doesn't handle concurrent writes well. For high-traffic deployments, consider:
- Using a single replica
- Or migrating to PostgreSQL/MySQL

## Resource Limits

Add resource limits in `docker-compose.yml`:

```yaml
services:
  db-live-tracker:
    # ... other config ...
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
        reservations:
          cpus: '0.25'
          memory: 256M
```

## Backup

### Backup Database

```bash
# Docker Compose
docker-compose exec db-live-tracker cp /data/train_db.db /data/train_db.backup.db
docker cp db-live-tracker:/data/train_db.backup.db ./backups/

# Or directly from host (if using volume mount)
cp ./data/train_db.db ./backups/train_db-$(date +%Y%m%d).db
```

### Restore Database

```bash
# Copy backup to container
docker cp ./backups/train_db.backup.db db-live-tracker:/data/train_db.db

# Restart to reload
docker-compose restart
```

## Cleanup

```bash
# Stop and remove container
docker-compose down

# Remove container and volumes
docker-compose down -v

# Remove image
docker rmi db-live-tracker

# Clean up Docker system
docker system prune -a
```

## Advanced: Multi-Stage Build (Smaller Image)

For production, you can optimize the image size:

```dockerfile
FROM python:3.11-slim as builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .

ENV PATH=/root/.local/bin:$PATH

EXPOSE 8080
CMD ["python", "db_live_api.py", "--db", "/data/train_db.db", "--host", "0.0.0.0", "--port", "8080"]
```

## Support

For issues with Docker deployment, check:
- Docker logs: `docker-compose logs`
- Application logs inside container
- API documentation: `http://localhost:8080/docs`

Enjoy containerized train tracking!
