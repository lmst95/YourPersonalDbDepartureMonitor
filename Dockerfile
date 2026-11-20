FROM python:3.12-slim

ENV TZ=Europe/Berlin PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/data/train_db.db DBMON_ENV=/config/.env

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /config && chown app:app /data /config

COPY dbmonitor/ ./dbmonitor/
COPY static/ ./static/
COPY dbmon ./dbmon
RUN ln -s /app/dbmon /usr/local/bin/dbmon

USER app
EXPOSE 8080

# Default: read-only web UI. The collector runs as a second service (see docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "dbmonitor.web", "--db", "/data/train_db.db", "--host", "0.0.0.0", "--port", "8080"]
