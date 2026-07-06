"""
WSGI entry point for production servers (gunicorn, uWSGI, etc.)

Usage:
    gunicorn --config gunicorn.conf.py webapp.wsgi:app
"""
import os

# Load .env file if it exists (production deployments)
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ.setdefault(key.strip(), val.strip())

from webapp.app import app, init_db

# Initialise the database on first import (safe: uses CREATE TABLE IF NOT EXISTS)
init_db()
