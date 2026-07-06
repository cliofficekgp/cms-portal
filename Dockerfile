FROM python:3.11-slim

# Install Chromium + ChromeDriver for the CMS scraper, plus SQLite CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    unzip \
    curl \
    chromium \
    chromium-driver \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Tell selenium/scraper to use the system Chromium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Create a non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Install Python deps first (layer cache optimisation)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source (exclude venv/data via .dockerignore)
COPY . .

# Runtime data directory — mount a named volume here in production
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

# Production environment flag
ENV FLASK_ENV=production
ENV PORT=8080
EXPOSE 8080

# Use gunicorn.conf.py for all settings
CMD ["gunicorn", "--config", "gunicorn.conf.py", "webapp.wsgi:app"]
