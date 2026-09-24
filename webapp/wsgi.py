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

# Import the module-level app instance created by webapp.app's create_app().
# Do NOT call create_app() again — the module-level call already initialises
# the database and starts background threads.  A second call would be harmless
# for init_db (idempotent) but the _threads_started guard in app.py now
# prevents duplicate thread spawns regardless.
from webapp.app import app  # noqa: F401
