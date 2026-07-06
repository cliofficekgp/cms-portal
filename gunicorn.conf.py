# gunicorn.conf.py — Production Gunicorn configuration
# Ref: https://docs.gunicorn.org/en/stable/configure.html

import os

# Load .env file if present (for bare-metal deployments without a process manager)
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

bind = f"0.0.0.0:{os.environ.get('PORT', 8080)}"

# IMPORTANT: keep workers=1 so the background scraper thread stays a singleton.
# Scale via threads, not workers.
workers = 1
threads = 4
worker_class = "gthread"

timeout = 120
keepalive = 5

# Logging
accesslog = "-"   # stdout
errorlog  = "-"   # stderr
loglevel  = os.environ.get("LOG_LEVEL", "info")

# Initialise DB before serving first request
def on_starting(server):
    from webapp.app import init_db
    init_db()
