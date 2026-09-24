"""
Shared singletons and global state.

This module holds all shared state that needs to be accessible across blueprints,
preventing circular import issues. Import these from any module that needs them.
"""
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

# Timezone
IST = ZoneInfo("Asia/Kolkata")

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'data', 'crew.db'))

# Required secrets — fail fast if not provided
def _require_env(name, default=None):
    """Load a required secret from environment; fail startup if missing (unless default provided)."""
    val = os.environ.get(name)
    if val is not None:
        return val
    if default is not None:
        return default
    raise RuntimeError(f"FATAL: Required environment variable '{name}' is not set. Startup aborted.")

API_SECRET = _require_env('API_SECRET')

# Note: CMS_USERNAME, CMS_PASSWORD, RTIS_USERNAME, RTIS_PASSWORD are NOT environment
# variables. They are stored in the database (cms_settings table) and managed via
# Admin → Settings UI, because Indian Railways forces 6-month credential rotation
# and requires daily OTP — admins must update them without redeployment.

# Serialize all SQLite write transactions across threads.
# SQLite only allows one writer at a time; this lock ensures writers queue
# in-process instead of hitting a 30-second busy_timeout and failing.
DB_WRITE_LOCK = threading.Lock()

# Global state for CMS scraper (accessed by both admin routes and API routes)
scraper_state = {
    'status': 'starting',
    'message': 'Initializing background thread...',
    'image_base64': '',
    'action_required': False,
    'action_type': '',  # 'captcha' or 'otp'
    'submitted_value': None,
    'last_updated': datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST'),
    'last_run': None,  # Timestamp of the last successful CMS sync
    'active_tunnel': 'Unknown',
    'last_ddddocr_failure': 'Never'
}

# Global state for RTIS scraper
rtis_state = {
    'status': 'starting',
    'message': 'Initializing RTIS background thread...',
    'image_base64': '',
    'action_required': False,
    'action_type': '',  # 'captcha' or 'otp'
    'submitted_value': None,
    'last_updated': datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST'),
    'last_run': None,  # Timestamp of the last successful RTIS sync
    'active_tunnel': 'Unknown',
    'last_ddddocr_failure': 'Never'
}

# Global process handles (set by threads.py, accessed by admin routes)
scraper_process = None
rtis_process = None
