#!/usr/bin/env bash
# ============================================================
#  KGP Crew Monitoring System — Linux Production Startup
#  Uses gunicorn. Run after setting env vars or creating .env
# ============================================================

set -e

# Load .env if present
if [ -f ".env" ]; then
  export $(grep -v '^#' .env | xargs)
fi

export FLASK_ENV=${FLASK_ENV:-production}
export PORT=${PORT:-8080}

echo "[CMS] Starting production server on port $PORT ..."
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  webapp.wsgi:app
