FROM python:3.11-slim-bookworm

# CRITICAL FIX: Wipe the default live repo files, then write the July 1st snapshot
RUN rm -rf /etc/apt/sources.list.d/* && \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260701T000000Z/ bookworm main" > /etc/apt/sources.list && \
    echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260701T000000Z/ bookworm-security main" >> /etc/apt/sources.list

# Proceed with your standard installs (it can now ONLY see the July 1st archive)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    chromium \
    chromium-driver \
    sqlite3 \
    gosu \
    tini \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser
ENV HOME=/home/appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && chown -R appuser:appuser /app /home/appuser

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV FLASK_ENV=production
ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -f http://localhost:8080/ || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]