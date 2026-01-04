FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY db_live_api.py .
COPY db_live_connections.py .
COPY static/ ./static/

# Create data directory for database
RUN mkdir -p /data

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8080/api/routes', timeout=5)"

# Run the application
CMD ["python", "db_live_api.py", "--db", "/data/train_db.db", "--host", "0.0.0.0", "--port", "8080"]
