"""
Flask application factory for the CMS crew management system.

Creates and configures the Flask app, registers blueprints, and starts background threads.
"""
import os
import sys
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from flask import Flask
from werkzeug.security import generate_password_hash

# Fix for known thread-safety bug with strptime lazily loading in Python
import _strptime
datetime.strptime("2020", "%Y")

# Guard: ensure background threads are started exactly once, even if create_app()
# is called multiple times (e.g. wsgi.py import + explicit call, gunicorn on_starting).
_threads_started = False

# Load .env file early (before extensions import requires env vars)
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _, _val = _line.partition('=')
                os.environ.setdefault(_key.strip(), _val.strip())

from webapp.extensions import IST, BASE_DIR, DB_PATH, DB_WRITE_LOCK
from webapp.database import get_db


def recover_db_if_malformed(db_path):
    """Attempt to recover a corrupted SQLite database via iterdump."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        res = conn.execute("PRAGMA integrity_check;").fetchone()
        conn.close()
        if res and res[0].lower() == 'ok':
            return
    except Exception:
        pass

    print("[DB] WARNING: Database is malformed! Attempting automated recovery via iterdump...")
    import tempfile, shutil, time
    try:
        conn = sqlite3.connect(db_path)
        dump_sql = ""
        for line in conn.iterdump():
            dump_sql += line + "\n"
        conn.close()

        backup_path = db_path + f".corrupted.{int(time.time())}"
        shutil.move(db_path, backup_path)

        conn2 = sqlite3.connect(db_path)
        conn2.executescript(dump_sql)
        conn2.close()
        print(f"[DB] Recovery successful. Corrupted file backed up to {backup_path}")
    except Exception as e:
        print(f"[DB] Automated recovery failed: {e}")


def _recover_wal_if_needed():
    """On startup, checkpoint and clean up any leftover WAL files to prevent disk I/O errors."""
    wal_path = DB_PATH + '-wal'
    shm_path = DB_PATH + '-shm'
    if not os.path.exists(wal_path):
        return
    print("[DB] WAL file detected on startup — running checkpoint to recover...")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.close()
        print("[DB] WAL checkpoint completed successfully.")
    except Exception as e:
        print(f"[DB] WAL checkpoint failed, attempting to remove WAL files: {e}")
        try:
            os.remove(wal_path)
        except: pass
        try:
            os.remove(shm_path)
        except: pass


def init_db():
    """Initialize database with production schema and migrations."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    recover_db_if_malformed(DB_PATH)
    _recover_wal_if_needed()
    conn = get_db()
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS crew_records (
            crew_id       TEXT PRIMARY KEY,
            name          TEXT,
            desig         TEXT,
            from_sttn     TEXT,
            sign_on_time  TEXT,
            to_sttn       TEXT,
            sign_off_time TEXT,
            duty_hrs      TEXT,
            route         TEXT,
            loco_no       TEXT,
            train_no      TEXT,
            category      TEXT,
            manually_edited INTEGER DEFAULT 0,
            synced_at     TEXT,
            found_in_ns   TEXT DEFAULT 'no'
        )
    ''')

    # Migration for found_in_ns and is_active if they don't exist
    try:
        cur.execute('ALTER TABLE crew_records ADD COLUMN found_in_ns TEXT DEFAULT "no"')
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute('ALTER TABLE crew_records ADD COLUMN is_active INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS crew_submissions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            crew_id          TEXT,
            name             TEXT,
            desig            TEXT,
            sign_on_time     TEXT,
            from_sttn        TEXT,
            to_sttn          TEXT,
            loco_no          TEXT,
            train_no         TEXT,
            bpc_no           TEXT,
            current_location TEXT,
            cto_time         TEXT,
            duty_hrs         TEXT,
            sign_off_time    TEXT,
            submitted_at     TEXT,
            is_relief        INTEGER DEFAULT 0,
            relief_station   TEXT,
            relief_datetime  TEXT,
            handover_crew_id TEXT
        )
    ''')

    # Migrations for new columns (safe: each ALTER is caught if column already exists)
    for col_def in [
        'is_relief INTEGER DEFAULT 0',
        'relief_station TEXT',
        'relief_datetime TEXT',
        'handover_crew_id TEXT',
        'ingest_miss_count INTEGER DEFAULT 0',
        'departure_time TEXT',
        'is_active INTEGER DEFAULT 1'
    ]:
        try:
            cur.execute(f'ALTER TABLE crew_submissions ADD COLUMN {col_def}')
        except sqlite3.OperationalError:
            pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS booking_ta_crew (
            crew_id       TEXT PRIMARY KEY,
            ordering_time TEXT,
            mobile_number TEXT,
            fetched_at    TEXT
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS admin_edits (
            crew_id     TEXT NOT NULL,
            field       TEXT NOT NULL,
            value       TEXT,
            updated_at  TEXT,
            PRIMARY KEY (crew_id, field)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS cms_settings (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            cms_username        TEXT,
            cms_password        TEXT,
            rtis_username       TEXT,
            rtis_password       TEXT,
            allow_manual_entry  INTEGER DEFAULT 0,
            updated_at          TEXT
        )
    ''')

    # Migration: add allow_manual_entry column to existing DBs
    try:
        cur.execute('ALTER TABLE cms_settings ADD COLUMN allow_manual_entry INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass

    # Migration: add rtis columns to existing DBs
    try:
        cur.execute("ALTER TABLE cms_settings ADD COLUMN rtis_username TEXT DEFAULT ''")
        cur.execute("ALTER TABLE cms_settings ADD COLUMN rtis_password TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # Migration: add updated_at column to existing DBs
    try:
        cur.execute("ALTER TABLE cms_settings ADD COLUMN updated_at TEXT")
    except sqlite3.OperationalError:
        pass

    row_count = cur.execute('SELECT COUNT(*) FROM cms_settings').fetchone()[0]
    if row_count == 0:
        cur.execute(
            'INSERT INTO cms_settings (id, allow_manual_entry) VALUES (1, 0)'
        )
    else:
        # For existing deployments, keep existing credentials (they're stored in DB for admin updates)
        # No-op - credentials remain as-is in the database
        pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT,
            secret_question TEXT,
            secret_answer_hash TEXT,
            force_password_change INTEGER DEFAULT 0
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS signup_passcodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            passcode TEXT UNIQUE,
            created_by INTEGER,
            is_used INTEGER DEFAULT 0
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS loco_locations (
            loco_no         TEXT PRIMARY KEY,
            crew_id         TEXT,
            latitude        REAL,
            longitude       REAL,
            location_name   TEXT,
            prev_latitude   REAL,
            prev_longitude  REAL,
            same_as_previous INTEGER DEFAULT 1,
            updated_at      TEXT,
            created_at      TEXT
        )
    ''')

    # crew_duty_log: append-only archive of every duty cycle (one row per duty per crew)
    # Kept for 30 days; enables monthly non-submitter reports even after active records are recycled
    cur.execute('''
        CREATE TABLE IF NOT EXISTS crew_duty_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            crew_id         TEXT NOT NULL,
            name            TEXT,
            desig           TEXT,
            from_sttn       TEXT,
            sign_on_time    TEXT,
            sign_off_time   TEXT,
            duty_hrs        TEXT,
            category        TEXT,
            has_submission  INTEGER DEFAULT 0,
            created_at      TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_duty_log_crew_id ON crew_duty_log (crew_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_duty_log_sign_on ON crew_duty_log (sign_on_time)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_submissions_crew_id ON crew_submissions (crew_id)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS ocr_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT,
            usage_date TEXT,
            request_count INTEGER DEFAULT 0,
            UNIQUE(account_name, usage_date)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS cli_movements (
            cli_id TEXT PRIMARY KEY,
            name TEXT,
            hq_code TEXT,
            departure_entry_datetime TEXT,
            departure_datetime TEXT,
            from_station TEXT,
            via TEXT,
            to_station TEXT,
            arrival_entry_datetime TEXT,
            arrival_datetime TEXT,
            train_no TEXT,
            loco_no TEXT,
            authority TEXT,
            duty_type TEXT,
            service_type TEXT,
            purpose TEXT,
            entry_mode TEXT,
            remarks TEXT,
            fetched_at TEXT,
            from_lat REAL,
            from_lon REAL,
            to_lat REAL,
            to_lon REAL
        )
    ''')

    # Bootstrap default super_admin if no users exist (secure first-run only)
    user_count = cur.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if user_count == 0:
        bootstrap_user = os.environ.get('BOOTSTRAP_ADMIN_USERNAME')
        bootstrap_pass = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD')
        if not bootstrap_user or not bootstrap_pass:
            raise RuntimeError(
                "No administrator exists and first-run bootstrap credentials are missing. "
                "Set BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD environment variables."
            )
        if len(bootstrap_pass) < 6:
            raise RuntimeError("Bootstrap admin password must be at least 6 characters.")
        bootstrap_hash = generate_password_hash(bootstrap_pass)
        cur.execute('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                    (bootstrap_user, bootstrap_hash, 'super_admin'))

    conn.commit()
    conn.close()


def run_cleanup_thread():
    """Hourly cleanup thread to hard-delete data older than 30 days."""
    # Delay startup so scrapers can complete their first sync cycle without lock contention
    time.sleep(180)

    BATCH_SIZE = 500
    while True:
        conn = None
        try:
            print("[Thread] Running cleanup job...")
            conn = get_db()
            cutoff = datetime.now(IST) - timedelta(days=30)

            # --- Read phase (no lock needed) ---
            # Find stale crew_submissions
            subs = conn.execute('SELECT id, sign_on_time FROM crew_submissions').fetchall()
            stale_sub_ids = []
            for row in subs:
                try:
                    dt = datetime.strptime(row['sign_on_time'], '%d-%m-%Y %H:%M')
                    if dt < cutoff:
                        stale_sub_ids.append(row['id'])
                except: pass

            # Find stale inactive crew_records
            recs = conn.execute('SELECT crew_id, sign_on_time, is_active FROM crew_records').fetchall()
            stale_rec_ids = []
            for row in recs:
                if row['is_active'] == 1:
                    continue
                try:
                    dt = datetime.strptime(row['sign_on_time'], '%d-%m-%Y %H:%M')
                    if dt < cutoff:
                        stale_rec_ids.append(row['crew_id'])
                except: pass

            # Find stale duty log entries
            logs = conn.execute('SELECT id, sign_on_time FROM crew_duty_log').fetchall()
            stale_log_ids = []
            for row in logs:
                try:
                    dt = datetime.strptime(row['sign_on_time'], '%d-%m-%Y %H:%M')
                    if dt < cutoff:
                        stale_log_ids.append(row['id'])
                except: pass

            # --- Write phase (lock per batch, released between batches) ---
            total_batches = max(len(stale_sub_ids), len(stale_rec_ids), len(stale_log_ids), 1)
            for i in range(0, total_batches, BATCH_SIZE):
                with DB_WRITE_LOCK:
                    sub_batch = stale_sub_ids[i:i+BATCH_SIZE]
                    if sub_batch:
                        placeholders = ','.join('?' * len(sub_batch))
                        conn.execute(f'DELETE FROM crew_submissions WHERE id IN ({placeholders})', sub_batch)

                    rec_batch = stale_rec_ids[i:i+BATCH_SIZE]
                    if rec_batch:
                        placeholders = ','.join('?' * len(rec_batch))
                        conn.execute(f'DELETE FROM crew_records WHERE crew_id IN ({placeholders})', rec_batch)

                    log_batch = stale_log_ids[i:i+BATCH_SIZE]
                    if log_batch:
                        placeholders = ','.join('?' * len(log_batch))
                        conn.execute(f'DELETE FROM crew_duty_log WHERE id IN ({placeholders})', log_batch)

                    conn.commit()
                # Lock released between batches — other threads can write
                time.sleep(0.1)

            if stale_sub_ids or stale_rec_ids or stale_log_ids:
                print(f"[Thread] Cleanup done: {len(stale_sub_ids)} subs, {len(stale_rec_ids)} records, {len(stale_log_ids)} logs deleted.")
            else:
                print("[Thread] Cleanup: nothing to delete.")

        except Exception as e:
            if isinstance(e, sqlite3.OperationalError):
                err_msg = str(e).lower()
                if "disk i/o error" in err_msg or "database is locked" in err_msg or "readonly" in err_msg:
                    print(f"CRITICAL: DB I/O Error detected in cleanup thread ({e}). Force restarting container...", flush=True)
                    os._exit(1)
            print(f"[Thread] Cleanup thread exception: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except: pass
        time.sleep(3600)


def create_app():
    """Flask application factory."""
    app = Flask(__name__)

    # Configuration (reads from environment; safe defaults for local dev only)
    _is_prod = os.environ.get('FLASK_ENV', 'development').lower() == 'production'
    app.secret_key = os.environ.get('SECRET_KEY')
    if not app.secret_key:
        raise RuntimeError("FATAL: SECRET_KEY environment variable is not set. Startup aborted.")
    app.config.update(
        DEBUG=not _is_prod,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=_is_prod,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    # Initialize database
    init_db()

    # Register blueprints
    from webapp.routes.auth import auth_bp
    from webapp.routes.crew import crew_bp
    from webapp.routes.admin import admin_bp
    from webapp.routes.api_internal import api_internal_bp
    from webapp.routes.api_public import api_public_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(crew_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_internal_bp)
    app.register_blueprint(api_public_bp)

    # Global error handler for SQLite disk I/O errors
    @app.errorhandler(sqlite3.OperationalError)
    def handle_db_error(e):
        err_msg = str(e).lower()
        if "disk i/o error" in err_msg or "readonly" in err_msg:
            print(f"CRITICAL: SQLite I/O Error detected. Force restarting container...", flush=True)
            os._exit(1)
        # Return generic error message to client, log full error internally
        print(f"Database error: {e}", flush=True)
        return "Internal server error", 500

    # Start background threads exactly once (guard against duplicate create_app calls
    # from wsgi.py import + explicit call, or gunicorn on_starting hook).
    # Also skip in Werkzeug reloader child process.
    global _threads_started
    if not _threads_started and not os.environ.get("WERKZEUG_RUN_MAIN"):
        _threads_started = True
        from webapp.threads import run_scraper_thread, run_rtis_thread

        scraper_thread = threading.Thread(target=run_scraper_thread, daemon=True)
        scraper_thread.start()

        rtis_thread = threading.Thread(target=run_rtis_thread, daemon=True)
        rtis_thread.start()

        cleanup_thread = threading.Thread(target=run_cleanup_thread, daemon=True)
        cleanup_thread.start()

    return app


# For direct execution (dev server)
app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
