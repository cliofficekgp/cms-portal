"""
Background daemon threads for running scrapers and cleanup tasks.
"""
import os
import sys
import time
import subprocess
from datetime import datetime, timedelta
from webapp.extensions import BASE_DIR, scraper_state, rtis_state, DB_WRITE_LOCK
from webapp.database import get_db

# Try to use standard threads, fall back to whatever werkzeug provides
import threading

def run_scraper_thread():
    """Supervisor for the CMS login.py scraper process."""
    scraper_script = os.path.join(BASE_DIR, 'scraper', 'login.py')
    python_exe = sys.executable
    stop_file = os.path.join(BASE_DIR, 'data', 'stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'fatal_error.txt')

    # Needs to be able to set this in the extensions runtime
    import webapp.extensions as ext

    while True:
        try:
            if os.path.exists(stop_file):
                if scraper_state['status'] not in ['cancelled', 'error']:
                    scraper_state['status'] = 'cancelled'
                    scraper_state['message'] = 'Scraper is stopped.'
                time.sleep(2)
                continue

            if os.path.exists(fatal_file):
                if scraper_state['status'] not in ['cancelled', 'error']:
                    scraper_state['status'] = 'error'
                    with open(fatal_file, 'r') as f:
                        scraper_state['message'] = f"Fatal Error: {f.read().strip()}. Please fix and trigger manually."
                time.sleep(2)
                continue

            print("[Thread] Starting scraper subprocess...")
            scraper_state['status'] = 'running'
            scraper_state['message'] = 'Scraper subprocess starting...'
            ext.scraper_process = subprocess.Popen([python_exe, scraper_script], cwd=os.path.join(BASE_DIR, 'scraper'))

            ext.scraper_process.wait()
            ext.scraper_process = None

            # Only sleep 30s and restart if it wasn't intentionally stopped
            if not os.path.exists(stop_file) and not os.path.exists(fatal_file):
                print("[Thread] Scraper subprocess exited. Restarting in 30 seconds...")
                for _ in range(15): # Check for stop condition during the 30s sleep
                    if os.path.exists(stop_file) or os.path.exists(fatal_file):
                        break
                    time.sleep(2)
        except Exception as e:
            print(f"[Thread] Scraper thread exception: {e}")
            time.sleep(30)


def run_rtis_thread():
    """Supervisor for the RTIS rtis_scraper.py process."""
    rtis_script = os.path.join(BASE_DIR, 'scraper', 'rtis_scraper.py')
    python_exe = sys.executable
    stop_file = os.path.join(BASE_DIR, 'data', 'rtis_stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'rtis_fatal_error.txt')

    # Needs to be able to set this in the extensions runtime
    import webapp.extensions as ext

    while True:
        try:
            if os.path.exists(stop_file):
                if rtis_state['status'] not in ['cancelled', 'error']:
                    rtis_state['status'] = 'cancelled'
                    rtis_state['message'] = 'RTIS Scraper is stopped.'
                time.sleep(2)
                continue

            if os.path.exists(fatal_file):
                if rtis_state['status'] not in ['cancelled', 'error']:
                    rtis_state['status'] = 'error'
                    with open(fatal_file, 'r') as f:
                        rtis_state['message'] = f"Fatal Error: {f.read().strip()}. Please fix and trigger manually."
                time.sleep(2)
                continue

            print("[Thread] Starting RTIS scraper subprocess...")
            rtis_state['status'] = 'running'
            rtis_state['message'] = 'RTIS Scraper subprocess starting...'
            ext.rtis_process = subprocess.Popen([python_exe, rtis_script], cwd=os.path.join(BASE_DIR, 'scraper'))

            ext.rtis_process.wait()
            ext.rtis_process = None

            # Only sleep 30s and restart if it wasn't intentionally stopped
            if not os.path.exists(stop_file) and not os.path.exists(fatal_file):
                print("[Thread] RTIS Scraper subprocess exited. Restarting in 30 seconds...")
                for _ in range(15): # Check for stop condition during the 30s sleep
                    if os.path.exists(stop_file) or os.path.exists(fatal_file):
                        break
                    time.sleep(2)
        except Exception as e:
            print(f"[Thread] RTIS Scraper thread exception: {e}")
            time.sleep(30)


def run_cleanup_thread():
    """
    Hourly cleanup thread to remove 30-day old data and deleted crew records
    in batches to minimize database locking.

    NOTE: This function is NOT currently started by app.py — app.py has its own
    run_cleanup_thread. This is kept as a reference but should not be imported
    as a replacement without updating the caller.
    """
    from webapp.extensions import IST
    # Sleep initially so scrapers can complete their first sync cycle
    time.sleep(180)
    BATCH_SIZE = 500
    while True:
        db = None
        try:
            print("[Cleanup] Running hourly cleanup...")
            cutoff_date = (datetime.now(IST) - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

            db = get_db()

            # --- Read phase (no lock) ---
            sub_rows = db.execute("""
                SELECT id FROM crew_submissions WHERE submitted_at < ?
            """, (cutoff_date,)).fetchall()
            stale_sub_ids = [r['id'] for r in sub_rows]

            log_rows = db.execute("""
                SELECT id FROM crew_duty_log WHERE created_at < ?
            """, (cutoff_date,)).fetchall()
            stale_log_ids = [r['id'] for r in log_rows]

            rec_rows = db.execute("""
                SELECT crew_id FROM crew_records
                WHERE is_active = 0 AND (synced_at < ? OR synced_at IS NULL)
            """, (cutoff_date,)).fetchall()
            stale_rec_ids = [r['crew_id'] for r in rec_rows]

            # --- Write phase (lock per batch, never sleep while holding lock) ---
            for i in range(0, max(len(stale_sub_ids), len(stale_log_ids), len(stale_rec_ids), 1), BATCH_SIZE):
                with DB_WRITE_LOCK:
                    sub_batch = stale_sub_ids[i:i+BATCH_SIZE]
                    if sub_batch:
                        placeholders = ','.join('?' * len(sub_batch))
                        db.execute(f'DELETE FROM crew_submissions WHERE id IN ({placeholders})', sub_batch)

                    log_batch = stale_log_ids[i:i+BATCH_SIZE]
                    if log_batch:
                        placeholders = ','.join('?' * len(log_batch))
                        db.execute(f'DELETE FROM crew_duty_log WHERE id IN ({placeholders})', log_batch)

                    rec_batch = stale_rec_ids[i:i+BATCH_SIZE]
                    if rec_batch:
                        placeholders = ','.join('?' * len(rec_batch))
                        db.execute(f'DELETE FROM crew_records WHERE crew_id IN ({placeholders})', rec_batch)

                    db.commit()
                # Lock released between batches
                time.sleep(0.1)

            print("[Cleanup] Hourly cleanup complete.")
        except Exception as e:
            print(f"[Cleanup] Error in cleanup thread: {e}")
        finally:
            if db:
                try:
                    db.close()
                except:
                    pass

        # Sleep for an hour
        time.sleep(3600)
