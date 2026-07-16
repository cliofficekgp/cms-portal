import os
import sys
import json
import sqlite3
import threading
import subprocess
import csv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
import secrets
import string
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration (reads from environment; safe defaults for local dev only)
# ---------------------------------------------------------------------------
_is_prod = os.environ.get('FLASK_ENV', 'development').lower() == 'production'
app.secret_key = os.environ.get('SECRET_KEY', 'kgp-crew-secret-2026-CHANGE-IN-PROD')
app.config.update(
    DEBUG=not _is_prod,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=_is_prod,  # True only when served over HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
API_SECRET = os.environ.get('API_SECRET', 'cms-sync-secret-key-2026')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'data', 'crew.db'))

IST = ZoneInfo("Asia/Kolkata")

def parse_dt(val):
    if not val or val == '-': return None
    val = val.strip()
    for fmt in ['%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%y %H:%M', '%d-%m-%Y', '%d-%m-%y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
        try:
            dt = datetime.strptime(val, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            pass
    return None

# Global state for scraper
scraper_state = {
    'status': 'starting',
    'message': 'Initializing background thread...',
    'image_base64': '',
    'action_required': False,
    'action_type': '', # 'captcha' or 'otp'
    'submitted_value': None,
    'last_updated': datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST'),
    'last_run': None  # Timestamp of the last successful CMS sync
}

# ---------------------------------------------------------------------------
# Background Scraper Thread
# ---------------------------------------------------------------------------

def run_scraper_thread():
    scraper_script = os.path.join(BASE_DIR, 'scraper', 'login.py')
    python_exe = sys.executable
    stop_file = os.path.join(BASE_DIR, 'data', 'stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'fatal_error.txt')
    
    global scraper_process
    scraper_process = None

    while True:
        try:
            if os.path.exists(stop_file) or os.path.exists(fatal_file):
                import time
                time.sleep(2)
                continue

            print("[Thread] Starting scraper subprocess...")
            scraper_state['status'] = 'running'
            scraper_state['message'] = 'Scraper subprocess starting...'
            scraper_process = subprocess.Popen([python_exe, scraper_script], cwd=os.path.join(BASE_DIR, 'scraper'))
            
            scraper_process.wait()
            scraper_process = None

            # Only sleep 30s and restart if it wasn't intentionally stopped
            if not os.path.exists(stop_file) and not os.path.exists(fatal_file):
                print("[Thread] Scraper subprocess exited. Restarting in 30 seconds...")
                import time
                for _ in range(15): # Check for stop condition during the 30s sleep
                    if os.path.exists(stop_file) or os.path.exists(fatal_file):
                        break
                    time.sleep(2)
        except Exception as e:
            print(f"[Thread] Scraper thread exception: {e}")
            import time
            time.sleep(30)

# (threads are started after get_db/init_db are defined below)

# ---------------------------------------------------------------------------
# Cleanup Thread (Hard Delete > 1 Month)
# ---------------------------------------------------------------------------

def run_cleanup_thread():
    while True:
        try:
            print("[Thread] Running cleanup job...")
            conn = get_db()
            cutoff = datetime.now(IST) - timedelta(days=30)
            
            # Clean crew_submissions
            subs = conn.execute('SELECT id, sign_on_time FROM crew_submissions').fetchall()
            for row in subs:
                try:
                    dt = datetime.strptime(row['sign_on_time'], '%d-%m-%Y %H:%M')
                    if dt < cutoff:
                        conn.execute('DELETE FROM crew_submissions WHERE id = ?', (row['id'],))
                except: pass
            
            # Clean crew_records
            recs = conn.execute('SELECT crew_id, sign_on_time FROM crew_records').fetchall()
            for row in recs:
                try:
                    dt = datetime.strptime(row['sign_on_time'], '%d-%m-%Y %H:%M')
                    if dt < cutoff:
                        conn.execute('DELETE FROM crew_records WHERE crew_id = ?', (row['crew_id'],))
                except: pass
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Thread] Cleanup thread exception: {e}")
        import time
        time.sleep(3600) # Run every hour

# (cleanup thread started after get_db is defined below)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        pass # Column likely already exists
        
    try:
        cur.execute('ALTER TABLE crew_records ADD COLUMN is_active INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass # Column likely already exists

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
            pass # Column already exists
            
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
            id                  INTEGER PRIMARY KEY,
            cms_username        TEXT,
            cms_password        TEXT,
            allow_manual_entry  INTEGER DEFAULT 1
        )
    ''')

    # Migration: add allow_manual_entry column to existing DBs
    try:
        cur.execute('ALTER TABLE cms_settings ADD COLUMN allow_manual_entry INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass  # Column already exists

    row_count = cur.execute('SELECT COUNT(*) FROM cms_settings').fetchone()[0]
    if row_count == 0:
        cur.execute(
            'INSERT INTO cms_settings (id, cms_username, cms_password, allow_manual_entry) VALUES (1, ?, ?, 1)',
            ('KGPCLIHQ', 'Cms@852')
        )

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
    
    # Bootstrap default super_admin if no users exist
    user_count = cur.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if user_count == 0:
        default_hash = generate_password_hash('Cms@123')
        cur.execute('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)', 
                    ('admin', default_hash, 'super_admin'))

    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Start background threads (here so get_db/init_db are already defined)
# ---------------------------------------------------------------------------
scraper_thread = threading.Thread(target=run_scraper_thread, daemon=True)
scraper_thread.start()

cleanup_thread = threading.Thread(target=run_cleanup_thread, daemon=True)
cleanup_thread.start()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('admin_login'))
        if session.get('force_password_change'):
            return redirect(url_for('admin_change_password'))
        return f(*args, **kwargs)
    return decorated_function

def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('admin_login'))
        if session.get('role') != 'super_admin':
            return "Unauthorized. Super Admin access required.", 403
        if session.get('force_password_change'):
            return redirect(url_for('admin_change_password'))
        return f(*args, **kwargs)
    return decorated_function

def parse_sign_on_dt(s):
    if not s or s == '-': return None
    for fmt in ('%d-%m-%Y %H:%M', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None

def sign_on_match(existing_dt_str, incoming_dt_str, tolerance_minutes=60):
    existing = parse_sign_on_dt(existing_dt_str)
    incoming = parse_sign_on_dt(incoming_dt_str)
    if existing is None or incoming is None: return False
    return abs((existing - incoming).total_seconds()) <= tolerance_minutes * 60

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/crew_login', methods=['GET', 'POST'])
def crew_login():
    if request.method == 'POST':
        crew_id = request.form.get('crew_id', '').strip().upper()
        if not crew_id:
            return render_template('login.html', error='Please enter a Crew ID.')

        conn = get_db()
        row = conn.execute('SELECT * FROM crew_records WHERE crew_id = ?', (crew_id,)).fetchone()

        # If crew is not in CMS records, check if admin allows manual entry
        if not row:
            settings = conn.execute(
                'SELECT allow_manual_entry FROM cms_settings WHERE id = 1'
            ).fetchone()
            conn.close()
            allow_manual = settings['allow_manual_entry'] if settings else 1
            if not allow_manual:
                return render_template(
                    'login.html',
                    error=(
                        'Your Crew ID is not registered in the system. '
                        'Manual entry is currently disabled. '
                        'Please contact the admin.'
                    )
                )
            # Manual entry allowed by admin — proceed to blank form
            session['crew_id'] = crew_id
            return redirect(url_for('form', manual='1'))

        conn.close()
        session['crew_id'] = crew_id
        return redirect(url_for('form'))
    return render_template('login.html')

@app.route('/form')
def form():
    crew_id = session.get('crew_id')
    if not crew_id:
        return redirect(url_for('crew_login'))
        
    manual = request.args.get('manual', '0') == '1'

    conn = get_db()
    row = conn.execute('SELECT * FROM crew_records WHERE crew_id = ?', (crew_id,)).fetchone()
    crew_data = dict(row) if row else {}
    
    # Fetch latest submission to override/populate form fields
    sub = conn.execute('SELECT * FROM crew_submissions WHERE crew_id = ? ORDER BY submitted_at DESC LIMIT 1', (crew_id,)).fetchone()
    if sub:
        sub_dict = dict(sub)
        # Override with manual submission data if present
        for field in ['loco_no', 'train_no', 'bpc_no', 'to_sttn', 'cto_time', 'current_location', 'departure_time', 'relief_datetime']:
            if sub_dict.get(field):
                crew_data[field] = sub_dict[field]
                
    # Format sign_on_time for display in form (readonly field)
    if crew_data.get('sign_on_time') and crew_data['sign_on_time'] != '-':
        try:
            dt = datetime.strptime(crew_data['sign_on_time'].strip(), '%d-%m-%Y %H:%M')
            crew_data['sign_on_time'] = dt.strftime('%d/%m/%y %H:%M')
        except:
            pass

    # Parse CTO date and time for HTML inputs
    if crew_data.get('cto_time') and crew_data['cto_time'] != '-':
        try:
            # CTO time is usually stored as 'DD-MM-YYYY HH:MM'
            dt = datetime.strptime(crew_data['cto_time'], '%d-%m-%Y %H:%M')
            crew_data['cto_date_val'] = dt.strftime('%Y-%m-%d')
            crew_data['cto_time_val'] = dt.strftime('%H:%M')
        except:
            pass

    if crew_data.get('departure_time') and crew_data['departure_time'] != '-':
        try:
            dt = datetime.strptime(crew_data['departure_time'], '%d-%m-%Y %H:%M')
            crew_data['departure_date_val'] = dt.strftime('%Y-%m-%d')
            crew_data['departure_time_val'] = dt.strftime('%H:%M')
        except:
            pass
            
    if crew_data.get('relief_datetime') and crew_data['relief_datetime'] != '-':
        try:
            dt = datetime.strptime(crew_data['relief_datetime'], '%d-%m-%Y %H:%M')
            crew_data['relief_date_val'] = dt.strftime('%Y-%m-%d')
            crew_data['relief_time_val'] = dt.strftime('%H:%M')
        except:
            pass

    conn.close()

    return render_template('form.html', crew_id=crew_id, crew=crew_data, manual=manual)

@app.route('/submit', methods=['POST'])
def submit():
    data = request.form

    crew_id       = data.get('crew_id', '').strip().upper()
    name          = data.get('name', '').strip()
    desig         = data.get('desig', '').strip()
    sign_on_time  = data.get('sign_on_time', '').strip()
    from_sttn     = data.get('from_sttn', '').strip().upper()
    to_sttn       = data.get('to_sttn', '').strip().upper()
    loco_no       = data.get('loco_no', '').strip()
    train_no      = data.get('train_no', '').strip()
    bpc_no        = data.get('bpc_no', '').strip()
    current_loc   = data.get('current_location', '').strip().upper()
    cto_date      = data.get('cto_date', '').strip()
    cto_time_val  = data.get('cto_time', '').strip()
    is_relief     = 1 if data.get('is_relief') == '1' else 0
    relief_stn    = data.get('relief_station', '').strip().upper()
    relief_dt     = data.get('relief_datetime', '').strip()
    handover_id   = data.get('handover_crew_id', '').strip().upper()

    departure_date= data.get('departure_date', '').strip()
    departure_time_val = data.get('departure_time', '').strip()

    if cto_date and cto_time_val:
        try:
            parsed_date = datetime.strptime(cto_date, '%Y-%m-%d').strftime('%d-%m-%Y')
            cto_time = f"{parsed_date} {cto_time_val}"
        except:
            cto_time = f"{cto_date} {cto_time_val}"
    else:
        cto_time = ''
        
    if departure_date and departure_time_val:
        try:
            parsed_d_date = datetime.strptime(departure_date, '%Y-%m-%d').strftime('%d-%m-%Y')
            departure_time = f"{parsed_d_date} {departure_time_val}"
        except:
            departure_time = f"{departure_date} {departure_time_val}"
    else:
        departure_time = ''

    submitted_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    conn = get_db()
    conn.execute('''
        INSERT INTO crew_submissions
            (crew_id, name, desig, sign_on_time, from_sttn, to_sttn,
             loco_no, train_no, bpc_no, current_location, cto_time,
             duty_hrs, sign_off_time, submitted_at,
             is_relief, relief_station, relief_datetime, handover_crew_id, departure_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        crew_id, name, desig, sign_on_time, from_sttn, to_sttn,
        loco_no, train_no, bpc_no, current_loc, cto_time,
        '', '-', submitted_at,
        is_relief, relief_stn, relief_dt, handover_id, departure_time
    ))

    existing = conn.execute('SELECT crew_id FROM crew_records WHERE crew_id = ?', (crew_id,)).fetchone()
    if not existing:
        conn.execute('''
            INSERT OR IGNORE INTO crew_records
                (crew_id, name, desig, from_sttn, sign_on_time, to_sttn,
                 sign_off_time, duty_hrs, route, loco_no, train_no,
                 category, manually_edited, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            crew_id, name, desig, from_sttn, sign_on_time, to_sttn,
            '-', '', '', loco_no, train_no,
            'MANUAL', 1, submitted_at
        ))

    # --- Handover Auto-Save for relief crew B ---
    if is_relief and handover_id:
        # Inherit loco, train, bpc from current crew's latest submission
        # from_sttn is NOT changed (kept from crew B's own CMS record)
        existing_b = conn.execute(
            'SELECT * FROM crew_records WHERE crew_id = ?', (handover_id,)
        ).fetchone()
        # Check if crew B already has a submission for the same duty
        existing_b_sub = conn.execute(
            'SELECT id FROM crew_submissions WHERE crew_id = ? ORDER BY submitted_at DESC LIMIT 1',
            (handover_id,)
        ).fetchone()

        handover_from = dict(existing_b)['from_sttn'] if existing_b else ''
        handover_sign_on = dict(existing_b)['sign_on_time'] if existing_b else ''
        handover_name = dict(existing_b)['name'] if existing_b else ''
        handover_desig = dict(existing_b)['desig'] if existing_b else ''

        conn.execute('''
            INSERT INTO crew_submissions
                (crew_id, name, desig, sign_on_time, from_sttn, to_sttn,
                 loco_no, train_no, bpc_no, current_location, cto_time,
                 duty_hrs, sign_off_time, submitted_at,
                 is_relief, relief_station, relief_datetime, handover_crew_id, departure_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            handover_id, handover_name, handover_desig,
            handover_sign_on,
            handover_from,  # from_sttn NOT changed
            to_sttn,
            loco_no, train_no, bpc_no,
            relief_stn,   # incoming crew starts at relief station
            relief_dt,    # their CTO time = relief time
            '', '-', submitted_at,
            0, '', '', crew_id, ''
        ))

        # Ensure crew B exists in crew_records
        if not existing_b:
            conn.execute('''
                INSERT OR IGNORE INTO crew_records
                    (crew_id, name, desig, from_sttn, sign_on_time, to_sttn,
                     sign_off_time, duty_hrs, route, loco_no, train_no,
                     category, manually_edited, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                handover_id, '', '', '', '', to_sttn,
                '-', '', '', loco_no, train_no,
                'MANUAL', 1, submitted_at
            ))

    conn.commit()
    conn.close()

    return render_template('success.html', crew_id=crew_id)


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['force_password_change'] = user['force_password_change']
            
            if user['force_password_change']:
                return redirect(url_for('admin_change_password'))
            
            if user['role'] == 'super_admin':
                return redirect(url_for('super_admin'))
            return redirect(url_for('admin'))
        
        return render_template('admin_login.html', error='Invalid username or password')
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/admin/change_password', methods=['GET', 'POST'])
@login_required
def admin_change_password():
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        if len(new_password) < 6:
            return render_template('admin_change_password.html', error='Password must be at least 6 characters')
            
        hashed = generate_password_hash(new_password)
        conn = get_db()
        conn.execute('UPDATE users SET password_hash = ?, force_password_change = 0 WHERE id = ?', 
                     (hashed, session['user_id']))
        conn.commit()
        conn.close()
        
        session['force_password_change'] = 0
        if session.get('role') == 'super_admin':
            return redirect(url_for('super_admin'))
        return redirect(url_for('admin'))
        
    return render_template('admin_change_password.html')

@app.route('/admin/forgot_password', methods=['GET', 'POST'])
def admin_forgot_password():
    if request.method == 'POST':
        step = request.form.get('step')
        username = request.form.get('username')
        
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        
        if not user:
            conn.close()
            return render_template('admin_forgot.html', error='User not found')
            
        if step == '1':
            conn.close()
            return render_template('admin_forgot.html', step='2', username=username, question=user['secret_question'])
            
        if step == '2':
            answer = request.form.get('answer', '').strip().lower()
            if check_password_hash(user['secret_answer_hash'], answer):
                conn.close()
                return render_template('admin_forgot.html', step='3', username=username)
            conn.close()
            return render_template('admin_forgot.html', step='2', username=username, question=user['secret_question'], error='Incorrect answer')
            
        if step == '3':
            new_password = request.form.get('new_password')
            hashed = generate_password_hash(new_password)
            conn.execute('UPDATE users SET password_hash = ?, force_password_change = 0 WHERE id = ?', (hashed, user['id']))
            conn.commit()
            conn.close()
            return redirect(url_for('admin_login'))
            
    return render_template('admin_forgot.html', step='1')

@app.route('/admin/signup', methods=['GET', 'POST'])
def admin_signup():
    if request.method == 'POST':
        passcode = request.form.get('passcode')
        username = request.form.get('username')
        password = request.form.get('password')
        question = request.form.get('secret_question')
        answer = request.form.get('secret_answer', '').strip().lower()
        
        conn = get_db()
        pc_row = conn.execute('SELECT * FROM signup_passcodes WHERE passcode = ? AND is_used = 0', (passcode,)).fetchone()
        if not pc_row:
            conn.close()
            return render_template('admin_signup.html', error='Invalid or used passcode.')
            
        if conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
            conn.close()
            return render_template('admin_signup.html', error='Username already exists.')
            
        hashed_pw = generate_password_hash(password)
        hashed_ans = generate_password_hash(answer)
        
        conn.execute('INSERT INTO users (username, password_hash, role, secret_question, secret_answer_hash) VALUES (?, ?, ?, ?, ?)',
                     (username, hashed_pw, 'admin', question, hashed_ans))
        conn.execute('UPDATE signup_passcodes SET is_used = 1 WHERE id = ?', (pc_row['id'],))
        conn.commit()
        conn.close()
        
        return redirect(url_for('admin_login'))
    return render_template('admin_signup.html')

# ---------------------------------------------------------------------------
# Super Admin
# ---------------------------------------------------------------------------

@app.route('/super_admin')
@super_admin_required
def super_admin():
    conn = get_db()
    users = conn.execute('SELECT id, username, role FROM users').fetchall()
    passcodes = conn.execute('SELECT passcode, is_used FROM signup_passcodes').fetchall()
    conn.close()
    return render_template('super_admin_dashboard.html', users=users, passcodes=passcodes)

@app.route('/super_admin/generate_passcode', methods=['POST'])
@super_admin_required
def generate_passcode():
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    conn = get_db()
    conn.execute('INSERT INTO signup_passcodes (passcode, created_by) VALUES (?, ?)', (code, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('super_admin'))

@app.route('/super_admin/reset_password/<int:user_id>', methods=['POST'])
@super_admin_required
def reset_user_password(user_id):
    conn = get_db()
    user = conn.execute('SELECT role FROM users WHERE id = ?', (user_id,)).fetchone()
    if user and user['role'] != 'super_admin':
        default_pw = generate_password_hash('Default@123')
        conn.execute('UPDATE users SET password_hash = ?, force_password_change = 1 WHERE id = ?', (default_pw, user_id))
        conn.commit()
    conn.close()
    return redirect(url_for('super_admin'))

# ---------------------------------------------------------------------------
# Admin & Scraper API
# ---------------------------------------------------------------------------

@app.route('/admin')
@login_required
def admin():
    return render_template('admin.html', state=scraper_state)

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    conn = get_db()
    if request.method == 'POST':
        cms_user = request.form.get('cms_username', '').strip()
        cms_pass = request.form.get('cms_password', '').strip()
        # Checkbox: present = 1, absent = 0
        allow_manual = 1 if request.form.get('allow_manual_entry') == 'on' else 0
        if cms_user and cms_pass:
            try:
                conn.execute(
                    'UPDATE cms_settings SET cms_username = ?, cms_password = ?, allow_manual_entry = ? WHERE id = 1',
                    (cms_user, cms_pass, allow_manual)
                )
                conn.commit()
                flash('Settings saved successfully.', 'success')
            except Exception as e:
                flash(f'Failed to save settings: {str(e)}', 'error')
            return redirect(url_for('admin_settings'))
        else:
            flash('Both username and password are required.', 'error')
    row = conn.execute('SELECT cms_username, cms_password, allow_manual_entry FROM cms_settings WHERE id = 1').fetchone()
    conn.close()
    return render_template('admin_settings.html', settings=row)

@app.route('/admin/reports')
@login_required
def admin_reports():
    conn = get_db()
    cutoff = datetime.now(IST) - timedelta(days=30)
    
    # We will fetch all submissions and filter manually due to date format DD-MM-YYYY
    subs = conn.execute('SELECT crew_id, name, desig, bpc_no, submitted_at FROM crew_submissions').fetchall()
    conn.close()
    
    stats = {}
    for row in subs:
        try:
            dt = datetime.strptime(row['submitted_at'], '%d-%m-%Y %H:%M:%S')
            if dt >= cutoff:
                cid = row['crew_id']
                if cid not in stats:
                    stats[cid] = {'crew_id': cid, 'name': row['name'], 'desig': row['desig'], 'total': 0, 'with_bpc': 0}
                
                stats[cid]['total'] += 1
                bpc = row['bpc_no']
                if bpc and bpc.strip() and bpc.strip() != '-':
                    stats[cid]['with_bpc'] += 1
        except:
            pass
            
    # List of crew members who haven't entered BPC at all
    zero_bpc = [v for v in stats.values() if v['with_bpc'] == 0]
    
    # Count for each ID
    all_bpc_stats = list(stats.values())
    
    return render_template('admin_reports.html', zero_bpc=zero_bpc, all_bpc_stats=all_bpc_stats)


# ---------------------------------------------------------------------------
# Report: Duty Sheet Export
# ---------------------------------------------------------------------------

def _parse_dt_report(val):
    """Parse date string (DD-MM-YYYY HH:MM variants) into IST-aware datetime."""
    if not val or val == '-':
        return None
    val = val.strip()
    for fmt in ['%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%y %H:%M',
                '%d-%m-%Y', '%d-%m-%y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def _build_duty_sheet_data(from_dt, to_dt):
    """
    Merge crew_records + crew_submissions + admin_edits (same priority as crew_list)
    and filter rows whose sign_on_time falls within [from_dt, to_dt].
    Returns a list of dicts ready for rendering / CSV export.
    """
    conn = get_db()
    records  = conn.execute('SELECT * FROM crew_records').fetchall()
    subs_raw = conn.execute('''
        SELECT * FROM crew_submissions
        WHERE id IN (SELECT MAX(id) FROM crew_submissions GROUP BY crew_id)
    ''').fetchall()
    ta_raw   = conn.execute('SELECT * FROM booking_ta_crew').fetchall()
    ae_raw   = conn.execute('SELECT crew_id, field, value FROM admin_edits').fetchall()
    conn.close()

    subs_map = {r['crew_id']: dict(r) for r in subs_raw}
    ta_map   = {r['crew_id']: dict(r) for r in ta_raw}

    admin_edits_map = {}
    for ae in ae_raw:
        admin_edits_map.setdefault(ae['crew_id'], {})[ae['field']] = ae['value']

    def merge_row(r_dict, s_dict, ae, ta):
        return {
            'crew_id':          r_dict.get('crew_id') or s_dict.get('crew_id', ''),
            'name':             s_dict.get('name') or r_dict.get('name', ''),
            'desig':            s_dict.get('desig') or r_dict.get('desig', ''),
            'sign_on_time':     r_dict.get('sign_on_time') or s_dict.get('sign_on_time', ''),
            'from_sttn':        r_dict.get('from_sttn') or s_dict.get('from_sttn', ''),
            'to_sttn':          s_dict.get('to_sttn') or r_dict.get('to_sttn', ''),
            'loco_no':          ae.get('loco_no') or s_dict.get('loco_no') or r_dict.get('loco_no', ''),
            'train_no':         ae.get('train_no') or s_dict.get('train_no') or r_dict.get('train_no', ''),
            'bpc_no':           ae.get('bpc_no') or s_dict.get('bpc_no', ''),
            'current_location': ae.get('current_location') or s_dict.get('current_location', ''),
            'cto_time':         ae.get('cto_time') or s_dict.get('cto_time', ''),
            'sign_off_time':    r_dict.get('sign_off_time', ''),
            'is_relief':        int(ae.get('is_relief') or s_dict.get('is_relief') or 0),
            'relief_station':   ae.get('relief_station') or s_dict.get('relief_station', ''),
            'relief_datetime':  ae.get('relief_datetime') or s_dict.get('relief_datetime', ''),
            'handover_crew_id': s_dict.get('handover_crew_id', ''),
            'submitted_at':     s_dict.get('submitted_at', ''),
            'departure_time':   s_dict.get('departure_time', ''),
            'mobile_number':    ta.get('mobile_number', ''),
        }

    processed = {}
    for r in records:
        r = dict(r)
        cid = r['crew_id']
        processed[cid] = merge_row(r, subs_map.get(cid, {}),
                                   admin_edits_map.get(cid, {}),
                                   ta_map.get(cid, {}))

    # Submission-only rows (crew not in CMS records)
    for cid, s in subs_map.items():
        if cid not in processed:
            processed[cid] = merge_row({}, s,
                                       admin_edits_map.get(cid, {}),
                                       ta_map.get(cid, {}))

    now = datetime.now(IST)
    data = []
    for row in processed.values():
        sign_on_dt = _parse_dt_report(row.get('sign_on_time'))
        if not sign_on_dt or not (from_dt <= sign_on_dt <= to_dt):
            continue

        # Compute live duty hours
        delta = now - sign_on_dt
        total_min = int(delta.total_seconds() // 60)
        row['duty_hrs'] = f"{total_min // 60:02d}:{total_min % 60:02d}"

        # Format date strings for display
        for col in ['sign_on_time', 'cto_time', 'relief_datetime', 'departure_time']:
            dt = _parse_dt_report(row.get(col, ''))
            row[col] = dt.strftime('%d/%m/%y %H:%M') if dt else (row.get(col) or '-')

        row['_sign_on_dt'] = sign_on_dt
        data.append(row)

    data.sort(key=lambda r: r['_sign_on_dt'], reverse=True)
    for row in data:
        row.pop('_sign_on_dt', None)

    return data


@app.route('/admin/report/duty_sheet')
@login_required
def report_duty_sheet():
    yesterday = (datetime.now(IST) - timedelta(days=1)).date()
    from_date_str = request.args.get('from_date', yesterday.strftime('%Y-%m-%d'))
    to_date_str   = request.args.get('to_date',   yesterday.strftime('%Y-%m-%d'))
    do_export     = request.args.get('export', '0') == '1'

    try:
        from_dt = datetime.strptime(from_date_str, '%Y-%m-%d').replace(
            hour=0, minute=0, second=0, tzinfo=IST)
        to_dt   = datetime.strptime(to_date_str,   '%Y-%m-%d').replace(
            hour=23, minute=59, second=59, tzinfo=IST)
    except ValueError:
        from_dt = (datetime.now(IST) - timedelta(days=1)).replace(hour=0,  minute=0,  second=0)
        to_dt   = from_dt.replace(hour=23, minute=59, second=59)
        from_date_str = from_dt.strftime('%Y-%m-%d')
        to_date_str   = to_dt.strftime('%Y-%m-%d')

    data = _build_duty_sheet_data(from_dt, to_dt)

    if do_export:
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'Crew ID', 'Name', 'Designation', 'Sign-On Time', 'From Station',
            'To Station', 'Loco No', 'Train No', 'BPC No', 'Current Location',
            'CTO Time', 'Duty Hrs', 'Relief?', 'Relief Station',
            'Relief Date-Time', 'Handover Crew ID', 'Submitted At', 'Mobile No'
        ])
        for row in data:
            writer.writerow([
                row.get('crew_id', ''),        row.get('name', ''),
                row.get('desig', ''),           row.get('sign_on_time', ''),
                row.get('from_sttn', ''),       row.get('to_sttn', ''),
                row.get('loco_no', ''),         row.get('train_no', ''),
                row.get('bpc_no', ''),          row.get('current_location', ''),
                row.get('cto_time', ''),        row.get('duty_hrs', ''),
                'Yes' if row.get('is_relief') else 'No',
                row.get('relief_station', ''), row.get('relief_datetime', ''),
                row.get('handover_crew_id', ''), row.get('submitted_at', ''),
                row.get('mobile_number', ''),
            ])
        output.seek(0)
        filename = f"Duty_Sheet_{from_date_str}_to_{to_date_str}.csv"
        from flask import Response as FlaskResponse
        return FlaskResponse(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    return render_template('report_duty_sheet.html',
                           data=data,
                           from_date=from_date_str,
                           to_date=to_date_str,
                           count=len(data))


# ---------------------------------------------------------------------------
# Report: Non-Submitting Crew
# ---------------------------------------------------------------------------

@app.route('/admin/report/non_submitters')
@login_required
def report_non_submitters():
    yesterday = (datetime.now(IST) - timedelta(days=1)).date()
    from_date_str = request.args.get('from_date', yesterday.strftime('%Y-%m-%d'))
    to_date_str   = request.args.get('to_date',   yesterday.strftime('%Y-%m-%d'))
    do_export     = request.args.get('export', '0') == '1'

    try:
        from_dt = datetime.strptime(from_date_str, '%Y-%m-%d').replace(
            hour=0, minute=0, second=0, tzinfo=IST)
        to_dt   = datetime.strptime(to_date_str,   '%Y-%m-%d').replace(
            hour=23, minute=59, second=59, tzinfo=IST)
    except ValueError:
        from_dt = (datetime.now(IST) - timedelta(days=1)).replace(hour=0,  minute=0,  second=0)
        to_dt   = from_dt.replace(hour=23, minute=59, second=59)
        from_date_str = from_dt.strftime('%Y-%m-%d')
        to_date_str   = to_dt.strftime('%Y-%m-%d')

    conn = get_db()
    records  = conn.execute(
        'SELECT crew_id, name, desig, from_sttn, sign_on_time, category FROM crew_records'
    ).fetchall()
    subs_all = conn.execute('SELECT crew_id, submitted_at FROM crew_submissions').fetchall()
    conn.close()

    # Crew IDs that have at least one submission within the date range
    submitted_ids = set()
    for row in subs_all:
        dt = _parse_dt_report(row['submitted_at'])
        if dt and from_dt <= dt <= to_dt:
            submitted_ids.add(row['crew_id'])

    # Crew that signed on in range but never submitted
    non_submitters = []
    for row in records:
        sign_on_dt = _parse_dt_report(row['sign_on_time'])
        if sign_on_dt and from_dt <= sign_on_dt <= to_dt:
            if row['crew_id'] not in submitted_ids:
                non_submitters.append({
                    'crew_id':      row['crew_id'],
                    'name':         row['name'] or '',
                    'desig':        row['desig'] or '',
                    'from_sttn':    row['from_sttn'] or '',
                    'sign_on_time': sign_on_dt.strftime('%d/%m/%y %H:%M'),
                    'category':     row['category'] or '',
                })

    non_submitters.sort(key=lambda r: r['crew_id'])

    if do_export:
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Crew ID', 'Name', 'Designation', 'From Station', 'Sign-On Time', 'Category'])
        for row in non_submitters:
            writer.writerow([
                row['crew_id'], row['name'], row['desig'],
                row['from_sttn'], row['sign_on_time'], row['category']
            ])
        output.seek(0)
        filename = f"Non_Submitters_{from_date_str}_to_{to_date_str}.csv"
        from flask import Response as FlaskResponse
        return FlaskResponse(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    return render_template('report_non_submitters.html',
                           data=non_submitters,
                           from_date=from_date_str,
                           to_date=to_date_str,
                           count=len(non_submitters))


@app.route('/admin/submit_action', methods=['POST'])
@login_required
def admin_submit_action():
    action_value = request.form.get('action_value', '').strip()
    if action_value:
        scraper_state['submitted_value'] = action_value
        scraper_state['action_required'] = False
        scraper_state['message'] = 'Action submitted. Waiting for scraper to process...'
        scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin'))

@app.route('/admin/scraper/trigger', methods=['POST'])
@login_required
def scraper_trigger():
    stop_file = os.path.join(BASE_DIR, 'data', 'stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'fatal_error.txt')
    if os.path.exists(stop_file):
        os.remove(stop_file)
    if os.path.exists(fatal_file):
        os.remove(fatal_file)
        
    scraper_state['status'] = 'starting'
    scraper_state['message'] = 'Manual trigger initiated...'
    scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin'))

@app.route('/admin/scraper/cancel', methods=['POST'])
@login_required
def scraper_cancel():
    stop_file = os.path.join(BASE_DIR, 'data', 'stop.txt')
    with open(stop_file, 'w') as f:
        f.write('stop')
        
    scraper_state['status'] = 'cancelled'
    scraper_state['message'] = 'Stopping scraper gracefully...'
    scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin'))

@app.route('/admin/scraper/run_now', methods=['POST'])
@login_required
def scraper_run_now():
    """Wake up a sleeping scraper immediately to start a new sync cycle."""
    run_now_file = os.path.join(BASE_DIR, 'data', 'run_now.txt')
    stop_file = os.path.join(BASE_DIR, 'data', 'stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'fatal_error.txt')

    # If scraper was hard-stopped, clear that first so the thread can restart it
    if os.path.exists(stop_file):
        os.remove(stop_file)
    if os.path.exists(fatal_file):
        os.remove(fatal_file)

    # Write the run-now signal; the scraper's interruptible_sleep will detect it
    with open(run_now_file, 'w') as f:
        f.write('run')

    scraper_state['status'] = 'starting'
    scraper_state['message'] = 'Manual run triggered — waking scraper...'
    scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin'))

@app.route('/api/scraper/state', methods=['POST'])
def update_scraper_state():
    auth = request.headers.get('X-API-Secret', '')
    if auth != API_SECRET: return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    scraper_state['status'] = payload.get('status', scraper_state['status'])
    scraper_state['message'] = payload.get('message', scraper_state['message'])
    if 'action_required' in payload:
        scraper_state['action_required'] = payload['action_required']
        scraper_state['action_type'] = payload.get('action_type', '')
        scraper_state['image_base64'] = payload.get('image_base64', '')
        scraper_state['submitted_value'] = None # reset
    
    scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return jsonify({'success': True})

@app.route('/api/scraper/action', methods=['GET'])
def get_scraper_action():
    auth = request.headers.get('X-API-Secret', '')
    if auth != API_SECRET: return jsonify({'error': 'Unauthorized'}), 401
    
    return jsonify({
        'submitted_value': scraper_state.get('submitted_value')
    })

@app.route('/crew_list')
@login_required
def crew_list():
    conn = get_db()
    # Full join of records and submissions
    
    # Mark signed off crews as inactive
    conn.execute('''
        UPDATE crew_records 
        SET is_active = 0 
        WHERE sign_off_time IS NOT NULL AND sign_off_time != '' AND sign_off_time != '-' AND is_active = 1
    ''')
    conn.execute('''
        UPDATE crew_submissions 
        SET is_active = 0 
        WHERE is_active = 1 
        AND crew_id IN (
            SELECT crew_id FROM crew_records 
            WHERE sign_off_time IS NOT NULL AND sign_off_time != '' AND sign_off_time != '-'
        )
    ''')
    conn.commit()

    query = '''
    SELECT 
        COALESCE(r.crew_id, s.crew_id) AS crew_id,
        COALESCE(s.name, r.name) AS name,
        COALESCE(s.desig, r.desig) AS desig,
        COALESCE(r.sign_on_time, s.sign_on_time) AS sign_on_time,
        COALESCE(r.from_sttn, s.from_sttn) AS from_sttn,
        COALESCE(s.to_sttn, r.to_sttn) AS to_sttn,
        COALESCE(s.loco_no, r.loco_no) AS loco_no,
        COALESCE(s.train_no, r.train_no) AS train_no,
        s.loco_no AS s_loco_no,
        s.train_no AS s_train_no,
        s.bpc_no,
        s.current_location,
        s.cto_time,
        r.duty_hrs,
        r.sign_off_time,
        r.found_in_ns,
        r.synced_at,
        s.submitted_at,
        COALESCE(s.is_relief, 0) AS is_relief,
        s.relief_station,
        s.relief_datetime,
        s.handover_crew_id,
        COALESCE(s.ingest_miss_count, 0) AS ingest_miss_count,
        s.departure_time,
        ta.ordering_time,
        ta.mobile_number
    FROM crew_records r
    LEFT JOIN (
        SELECT * FROM crew_submissions 
        WHERE id IN (
            SELECT MAX(id) FROM crew_submissions GROUP BY crew_id
        )
    ) s ON r.crew_id = s.crew_id
    LEFT JOIN booking_ta_crew ta ON ta.crew_id = COALESCE(r.crew_id, s.crew_id)
    WHERE COALESCE(r.is_active, 1) = 1 AND COALESCE(s.is_active, 1) = 1
    
    UNION
    
    SELECT 
        COALESCE(r.crew_id, s.crew_id) AS crew_id,
        COALESCE(s.name, r.name) AS name,
        COALESCE(s.desig, r.desig) AS desig,
        COALESCE(r.sign_on_time, s.sign_on_time) AS sign_on_time,
        COALESCE(r.from_sttn, s.from_sttn) AS from_sttn,
        COALESCE(s.to_sttn, r.to_sttn) AS to_sttn,
        COALESCE(s.loco_no, r.loco_no) AS loco_no,
        COALESCE(s.train_no, r.train_no) AS train_no,
        s.loco_no AS s_loco_no,
        s.train_no AS s_train_no,
        s.bpc_no,
        s.current_location,
        s.cto_time,
        r.duty_hrs,
        r.sign_off_time,
        r.found_in_ns,
        r.synced_at,
        s.submitted_at,
        COALESCE(s.is_relief, 0) AS is_relief,
        s.relief_station,
        s.relief_datetime,
        s.handover_crew_id,
        COALESCE(s.ingest_miss_count, 0) AS ingest_miss_count,
        s.departure_time,
        ta.ordering_time,
        ta.mobile_number
    FROM (
        SELECT * FROM crew_submissions 
        WHERE id IN (
            SELECT MAX(id) FROM crew_submissions GROUP BY crew_id
        )
    ) s
    LEFT JOIN crew_records r ON r.crew_id = s.crew_id
    LEFT JOIN booking_ta_crew ta ON ta.crew_id = COALESCE(r.crew_id, s.crew_id)
    WHERE COALESCE(r.is_active, 1) = 1 AND COALESCE(s.is_active, 1) = 1
    '''
    
    rows = conn.execute(query).fetchall()

    # Load all admin_edits for efficient lookup
    admin_edit_rows = conn.execute('SELECT crew_id, field, value, updated_at FROM admin_edits').fetchall()
    admin_edits_map = {}
    admin_edits_time_map = {}
    for ae in admin_edit_rows:
        admin_edits_map.setdefault(ae['crew_id'], {})[ae['field']] = ae['value']
        admin_edits_time_map.setdefault(ae['crew_id'], {})[ae['field']] = ae['updated_at']

    conn.close()
    
    data = [dict(row) for row in rows]
    
    now = datetime.now(IST)

    # Fields tracked for source attribution
    EDITABLE_FIELDS = ['loco_no', 'train_no', 'bpc_no', 'current_location', 'cto_time',
                       'is_relief', 'relief_station', 'relief_datetime']
    


    for row in data:
        crew_id = row.get('crew_id', '')
        crew_admin_edits = admin_edits_map.get(crew_id, {})

        sign_on_dt = None
        if row.get('sign_on_time') and row['sign_on_time'] not in ['-', '–']:
            sign_on_dt = parse_dt(row['sign_on_time'])

        # --- Retroactive Stale Data Cleanup ---
        if sign_on_dt:
            # 1. Clean stale admin edits
            stale_fields = []
            for field, val in list(crew_admin_edits.items()):
                ae_time = admin_edits_time_map.get(crew_id, {}).get(field)
                if ae_time:
                    ae_dt = parse_dt(ae_time)
                    if ae_dt and ae_dt < sign_on_dt:
                        stale_fields.append(field)
            
            if stale_fields:
                try:
                    cleanup_conn = get_db()
                    for field in stale_fields:
                        cleanup_conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field = ?", (crew_id, field))
                        del crew_admin_edits[field]
                    cleanup_conn.commit()
                    cleanup_conn.close()
                except Exception as e:
                    print(f"Cleanup stale admin edit error: {e}")

            # 2. Clean stale crew submissions
            sub_time = row.get('submitted_at')
            if sub_time:
                sub_dt = parse_dt(sub_time)
                if sub_dt and sub_dt < sign_on_dt:
                    try:
                        cleanup_conn = get_db()
                        cleanup_conn.execute("DELETE FROM crew_submissions WHERE crew_id = ?", (crew_id,))
                        cleanup_conn.commit()
                        cleanup_conn.close()
                    except Exception as e:
                        print(f"Cleanup stale submission error: {e}")
                    # Wipe submission data from current row so UI immediately ignores it
                    for sf in ['s_loco_no', 's_train_no', 'bpc_no', 'cto_time', 'is_relief', 'relief_station', 'relief_datetime']:
                        if sf in row:
                            row[sf] = None
                    row['submitted_at'] = None

        # --- Determine data source for each editable field ---
        # Source priority: admin > crew submission > cms record
        # We re-fetch from the raw query: loco_no, train_no come from COALESCE(s.loco_no, r.loco_no)
        # We need to detect which source actually filled the value
        #
        # Strategy: track source per field
        src = {}

        for field in ['loco_no', 'train_no']:
            # s_loco_no / s_train_no are the raw submission values (NULL if no submission)
            sub_field = f's_{field}'
            if field in crew_admin_edits and crew_admin_edits[field] is not None:
                # Admin override takes highest priority
                src[field] = 'admin'
                row[field] = crew_admin_edits[field]
            elif row.get(sub_field):
                # Submission had a value for this field — COALESCE picked it (or it's the only source)
                src[field] = 'sub'
            elif row.get(field):
                # Value exists but only from CMS crew_records (submission was NULL for this field)
                src[field] = 'cms'
            else:
                src[field] = 'none'

        for field in ['bpc_no', 'cto_time']:
            if field in crew_admin_edits and crew_admin_edits[field] is not None:
                src[field] = 'admin'
                row[field] = crew_admin_edits[field]
            elif row.get(field):
                src[field] = 'sub'
            else:
                src[field] = 'none'

        row['_loc_update_time'] = ''
        loc_time_dt = None
        if 'current_location' in crew_admin_edits and crew_admin_edits['current_location'] is not None:
            src['current_location'] = 'admin'
            row['current_location'] = crew_admin_edits['current_location']
            loc_time = admin_edits_time_map.get(crew_id, {}).get('current_location', '')
            if loc_time:
                dt = parse_dt(loc_time)
                if dt: 
                    row['_loc_update_time'] = dt.strftime('%d/%m/%y %H:%M')
                    loc_time_dt = dt
        elif row.get('current_location'):
            # Location exists from crew_submission (latest submission wins via MAX(id) in query)
            src['current_location'] = 'sub'
            loc_time = row.get('submitted_at', '')
            if loc_time:
                dt = parse_dt(loc_time)
                if dt: 
                    row['_loc_update_time'] = dt.strftime('%d/%m/%y %H:%M')
                    loc_time_dt = dt
        else:
            # No location from admin edit or crew submission — leave blank
            # Do NOT fall back to from_sttn: that is CMS sign-on station, not current location.
            # PDD must not be calculated using an assumed location.
            row['current_location'] = ''
            src['current_location'] = 'none'

        # Auto-remove location if departure time is after the location update time
        if loc_time_dt and row.get('departure_time') and row.get('departure_time') not in ['-', '–']:
            dep_dt = parse_dt(row['departure_time'])
            if dep_dt and dep_dt > loc_time_dt:
                try:
                    cleanup_conn = get_db()
                    cleanup_conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field = 'current_location'", (crew_id,))
                    cleanup_conn.execute("UPDATE crew_submissions SET current_location = NULL WHERE crew_id = ?", (crew_id,))
                    cleanup_conn.commit()
                    cleanup_conn.close()
                except Exception as e:
                    print(f"Cleanup location error: {e}")
                
                row['current_location'] = ''
                src['current_location'] = 'none'
                row['_loc_update_time'] = ''

        for field in ['relief_station', 'relief_datetime']:
            if field in crew_admin_edits and crew_admin_edits[field] is not None:
                src[field] = 'admin'
                row[field] = crew_admin_edits[field]
            elif row.get(field):
                src[field] = 'sub'
            else:
                src[field] = 'none'

        # Relief flag admin override
        if 'is_relief' in crew_admin_edits:
            try:
                row['is_relief'] = int(crew_admin_edits['is_relief'])
                src['is_relief'] = 'admin'
            except:
                src['is_relief'] = 'sub'
        else:
            src['is_relief'] = 'sub' if row.get('is_relief') else 'none'

        row['_src'] = src

        # Calculate Duty Hours (raw minutes stored for gradient sorting)
        if sign_on_dt:
            delta = now - sign_on_dt
            total_minutes = int(delta.total_seconds() // 60)
            hours = total_minutes // 60
            minutes = total_minutes % 60
            row['duty_hrs'] = f"{hours:02d}:{minutes:02d}"
            row['_duty_minutes'] = total_minutes  # for gradient
        else:
            row['_duty_minutes'] = 0

        # Auto-remove stale relief data if relief_datetime is before current sign_on_time
        if sign_on_dt and row.get('relief_datetime') and row['relief_datetime'] not in ['-', '–']:
            relief_dt = parse_dt(row['relief_datetime'])
            if relief_dt and relief_dt < sign_on_dt:
                try:
                    cleanup_conn = get_db()
                    cleanup_conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field IN ('is_relief', 'relief_station', 'relief_datetime')", (crew_id,))
                    cleanup_conn.execute("UPDATE crew_submissions SET is_relief = 0, relief_station = NULL, relief_datetime = NULL WHERE crew_id = ?", (crew_id,))
                    cleanup_conn.commit()
                    cleanup_conn.close()
                except Exception as e:
                    print(f"Cleanup relief error: {e}")
                
                # Clear from current row so UI reflects immediately
                row['is_relief'] = 0
                row['relief_station'] = ''
                row['relief_datetime'] = ''
                src['is_relief'] = 'none'
                src['relief_station'] = 'none'
                src['relief_datetime'] = 'none'

        # Calculate PDD
        row['pdd'] = '-'
        lobbies = {'KGP', 'ADL', 'SRC', 'TPKR', 'NMP', 'BLS', 'GTS'}
        from_sttn = (row.get('from_sttn', '') or '').upper().strip()
        cto_sttn = (row.get('current_location', '') or '').upper().strip()
        
        # Normalize NPTY to NMP for comparison
        from_sttn_norm = 'NMP' if from_sttn == 'NPTY' else from_sttn
        cto_sttn_norm = 'NMP' if cto_sttn == 'NPTY' else cto_sttn

        is_pdd_sttn = False
        if sign_on_dt:
            # Check special case: sign on in {NMP, KGP} and CTO in {NMP, KGP} (using normalized stations)
            if from_sttn_norm in {'NMP', 'KGP'} and cto_sttn_norm in {'NMP', 'KGP'}:
                is_pdd_sttn = True
            # Otherwise, check if sign on station is in standard lobbies and equals the CTO station
            elif from_sttn_norm in lobbies and from_sttn_norm == cto_sttn_norm:
                is_pdd_sttn = True

        if is_pdd_sttn:
            departure_dt = parse_dt(row.get('departure_time'))
            end_time = departure_dt if departure_dt else now
            pdd_delta = end_time - sign_on_dt
            if pdd_delta.total_seconds() > 0:
                p_hours = int(pdd_delta.total_seconds() // 3600)
                p_minutes = int((pdd_delta.total_seconds() % 3600) // 60)
                row['pdd'] = f"{p_hours:02d}:{p_minutes:02d}"

        # Capture raw datetimes for sorting BEFORE display formatting overwrites the strings
        row['_sign_on_dt'] = sign_on_dt if sign_on_dt else datetime.min.replace(tzinfo=IST)
        row['_cto_dt'] = parse_dt(row.get('cto_time')) or datetime.min.replace(tzinfo=IST)

        # Store ISO sign_on for JS live duty calc
        row['_sign_on_iso'] = sign_on_dt.strftime('%Y-%m-%dT%H:%M:%S') if sign_on_dt else ''

        # Filter ordering time: must be within 2 hours of sign on time
        if sign_on_dt and row.get('ordering_time') and row['ordering_time'] != '-':
            ord_dt = parse_dt(row['ordering_time'])
            if ord_dt:
                if abs((sign_on_dt - ord_dt).total_seconds()) > 7200:
                    row['ordering_time'] = '-'
                else:
                    row['ordering_time'] = ord_dt.strftime('%H:%M')
            else:
                row['ordering_time'] = '-'

        # Format dates universally (DD/MM/YY HH:MM) — happens AFTER capturing sort keys above
        for col in ['sign_on_time', 'relief_datetime', 'departure_time']:
            dt = parse_dt(row.get(col, ''))
            if dt:
                row[col] = dt.strftime('%d/%m/%y %H:%M')
            elif col in row and not row[col]:
                row[col] = '-'

        # Format cto_time as time only
        dt_cto = parse_dt(row.get('cto_time', ''))
        if dt_cto:
            row['cto_time'] = dt_cto.strftime('%H:%M')
        elif 'cto_time' in row and not row['cto_time']:
            row['cto_time'] = '-'

    def list_sort_key(r):
        return (r['_sign_on_dt'], r['_cto_dt'])

    data.sort(key=list_sort_key, reverse=True)

    # Clean up the internal sort-only keys so they don't leak into the template unnecessarily
    for row in data:
        row.pop('_sign_on_dt', None)
        row.pop('_cto_dt', None)

    return render_template('crew_list.html', crew_list=data)

@app.route('/api/save_ns_status', methods=['POST'])
@login_required
def save_ns_status():
    payload = request.get_json()
    if not payload:
        return jsonify({'status': 'error', 'message': 'No data'})
        
    conn = get_db()
    for crew_id, status in payload.items():
        conn.execute('UPDATE crew_records SET found_in_ns = ? WHERE crew_id = ?', (status, crew_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/admin_edit', methods=['POST'])
@login_required
def api_admin_edit():
    """Save an admin override for a specific field of a crew record."""
    payload = request.get_json()
    if not payload:
        return jsonify({'status': 'error', 'message': 'No data'}), 400

    crew_id = payload.get('crew_id', '').strip().upper()
    field   = payload.get('field', '').strip()
    value   = payload.get('value', '').strip()

    ALLOWED_FIELDS = {'loco_no', 'train_no', 'bpc_no', 'current_location',
                      'cto_time', 'is_relief', 'relief_station', 'relief_datetime'}
    if not crew_id or field not in ALLOWED_FIELDS:
        return jsonify({'status': 'error', 'message': 'Invalid field or crew_id'}), 400

    conn = get_db()

    if field == 'cto_time' and value and value not in ['-', '–']:
        # We need to construct the full datetime based on sign_on_time
        r = conn.execute('''
            SELECT COALESCE(s.sign_on_time, r.sign_on_time) as sign_on_time 
            FROM crew_records r 
            LEFT JOIN crew_submissions s ON r.crew_id = s.crew_id 
            WHERE r.crew_id = ?
            ORDER BY s.id DESC LIMIT 1
        ''', (crew_id,)).fetchone()
        
        if r and r['sign_on_time']:
            sign_on_dt = parse_dt(r['sign_on_time'])
            if sign_on_dt:
                try:
                    h, m = map(int, value.split(':'))
                    new_dt = sign_on_dt.replace(hour=h, minute=m, second=0)
                    if new_dt < sign_on_dt:
                        new_dt += timedelta(days=1)
                    value = new_dt.strftime('%d-%m-%Y %H:%M:%S')
                except ValueError:
                    pass

    updated_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')
    conn.execute('''
        INSERT INTO admin_edits (crew_id, field, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(crew_id, field) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    ''', (crew_id, field, value, updated_at))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/add_phone', methods=['POST'])
@login_required
def api_add_phone():
    """Append a phone number (comma-separated) to a crew's mobile_number in booking_ta_crew."""
    payload = request.get_json()
    if not payload:
        return jsonify({'status': 'error', 'message': 'No data'}), 400

    crew_id = payload.get('crew_id', '').strip().upper()
    phone   = payload.get('phone', '').strip()

    if not crew_id or not phone:
        return jsonify({'status': 'error', 'message': 'crew_id and phone required'}), 400

    conn = get_db()
    existing = conn.execute('SELECT mobile_number FROM booking_ta_crew WHERE crew_id = ?', (crew_id,)).fetchone()
    fetched_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    if existing:
        current = existing['mobile_number'] or ''
        numbers = [n.strip() for n in current.split(',') if n.strip()]
        if phone not in numbers:
            numbers.append(phone)
        new_value = ', '.join(numbers)
        conn.execute('UPDATE booking_ta_crew SET mobile_number = ?, fetched_at = ? WHERE crew_id = ?',
                     (new_value, fetched_at, crew_id))
    else:
        conn.execute('INSERT INTO booking_ta_crew (crew_id, mobile_number, fetched_at) VALUES (?, ?, ?)',
                     (crew_id, phone, fetched_at))

    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/crew_lookup/<crew_id>', methods=['GET'])
def api_crew_lookup(crew_id):
    crew_id = crew_id.strip().upper()
    conn = get_db()
    # Find latest submission for this crew
    sub = conn.execute('''
        SELECT loco_no, train_no, bpc_no 
        FROM crew_submissions 
        WHERE crew_id = ? 
        ORDER BY submitted_at DESC LIMIT 1
    ''', (crew_id,)).fetchone()
    
    if not sub:
        # Fallback to crew_records if no submission exists
        rec = conn.execute('''
            SELECT loco_no, train_no, '' as bpc_no 
            FROM crew_records 
            WHERE crew_id = ?
        ''', (crew_id,)).fetchone()
        sub = rec

    conn.close()

    if sub:
        return jsonify({
            'status': 'ok',
            'loco_no': sub['loco_no'],
            'train_no': sub['train_no'],
            'bpc_no': sub['bpc_no']
        })
    else:
        return jsonify({'status': 'not_found'})

@app.route('/api/stations', methods=['GET'])
def api_stations():
    stations = []
    csv_path = os.path.join(BASE_DIR, 'data', '01_station_code.csv')
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    lat = float(row.get('lat', 0))
                    lon = float(row.get('long', 0))
                    if lat and lon:
                        stations.append({
                            'code': row.get('code', row.get('ode', '')),
                            'name': row.get('name', ''),
                            'lat': lat,
                            'lon': lon
                        })
                except ValueError:
                    continue
    except Exception as e:
        print("Error reading stations:", e)
    return jsonify(stations)

@app.route('/api/sync_ta', methods=['POST'])
def api_sync_ta():
    auth = request.headers.get('X-API-Secret', '')
    if auth != API_SECRET: return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, list): return jsonify({'error': 'Invalid payload'}), 400

    conn = get_db()
    fetched_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    for rec in payload:
        crew_id = rec.get('crew_id')
        if not crew_id: continue
        ordering_time = rec.get('ordering_time', '')
        mobile_number = rec.get('mobile_number', '')
        
        conn.execute('''
            INSERT INTO booking_ta_crew (crew_id, ordering_time, mobile_number, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(crew_id) DO UPDATE SET
                ordering_time=excluded.ordering_time,
                mobile_number=excluded.mobile_number,
                fetched_at=excluded.fetched_at
        ''', (crew_id, ordering_time, mobile_number, fetched_at))
        
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'count': len(payload)})

@app.route('/api/sync', methods=['POST'])
def api_sync():
    auth = request.headers.get('X-API-Secret', '')
    if auth != API_SECRET: return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, dict): return jsonify({'error': 'Invalid payload'}), 400

    conn = get_db()
    inserted = updated = skipped = 0
    synced_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    for crew_id, rec in payload.items():
        existing = conn.execute('SELECT * FROM crew_records WHERE crew_id = ?', (crew_id,)).fetchone()
        if existing:
            existing = dict(existing)
            sub = conn.execute('SELECT id FROM crew_submissions WHERE crew_id = ?', (crew_id,)).fetchone()
            
            old_dt = parse_dt(existing.get('sign_on_time'))
            new_dt = parse_dt(rec.get('sign_on_time'))
            is_new_shift = False
            if old_dt and new_dt:
                if abs((old_dt - new_dt).total_seconds()) > 3600:
                    is_new_shift = True
            elif str(existing.get('sign_on_time') or '').strip() != str(rec.get('sign_on_time') or '').strip():
                is_new_shift = True

            if is_new_shift:
                conn.execute('DELETE FROM admin_edits WHERE crew_id = ?', (crew_id,))
                conn.execute('DELETE FROM crew_submissions WHERE crew_id = ?', (crew_id,))
                has_manual_sub = False
            else:
                has_manual_sub = bool(sub)

            if has_manual_sub:
                conn.execute('''
                    UPDATE crew_records SET duty_hrs = ?, sign_off_time = ?, synced_at = ?
                    WHERE crew_id = ?
                ''', (rec.get('duty_hrs', ''), rec.get('sign_off_time', '-'), synced_at, crew_id))
                skipped += 1
            else:
                conn.execute('''
                    UPDATE crew_records
                    SET name=?, desig=?, from_sttn=?, sign_on_time=?,
                        to_sttn=?, sign_off_time=?, duty_hrs=?,
                        route=?, loco_no=?, train_no=?, category=?,
                        manually_edited=0, synced_at=?
                    WHERE crew_id = ?
                ''', (
                    rec.get('name',''), rec.get('desig',''), rec.get('from_sttn',''), rec.get('sign_on_time',''),
                    rec.get('to_sttn',''), rec.get('sign_off_time','-'), rec.get('duty_hrs',''), rec.get('route',''),
                    rec.get('loco_no',''), rec.get('train_no',''), rec.get('category',''), synced_at, crew_id
                ))
                updated += 1
        else:
            # Brand new crew detected. Wipe any ancient stale data just in case.
            conn.execute('DELETE FROM admin_edits WHERE crew_id = ?', (crew_id,))
            conn.execute('DELETE FROM crew_submissions WHERE crew_id = ?', (crew_id,))
            
            conn.execute('''
                INSERT INTO crew_records
                    (crew_id, name, desig, from_sttn, sign_on_time, to_sttn,
                     sign_off_time, duty_hrs, route, loco_no, train_no,
                     category, manually_edited, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                crew_id, rec.get('name',''), rec.get('desig',''), rec.get('from_sttn',''), rec.get('sign_on_time',''),
                rec.get('to_sttn',''), rec.get('sign_off_time','-'), rec.get('duty_hrs',''), rec.get('route',''),
                rec.get('loco_no',''), rec.get('train_no',''), rec.get('category',''), 0, synced_at
            ))
            inserted += 1

    MISS_THRESHOLD = 2  # missed 2 consecutive syncs (~1hr, given 30-min scraper interval) = treat as signed off

    payload_ids = set(payload.keys())
    all_db = conn.execute('SELECT crew_id FROM crew_records').fetchall()
    for row in all_db:
        cid = row['crew_id']
        if cid not in payload_ids:
            sub = conn.execute(
                'SELECT id, ingest_miss_count FROM crew_submissions WHERE crew_id = ? ORDER BY submitted_at DESC LIMIT 1',
                (cid,)
            ).fetchone()
            if sub:
                new_miss_count = (sub['ingest_miss_count'] or 0) + 1
                conn.execute('UPDATE crew_submissions SET ingest_miss_count = ? WHERE id = ?', (new_miss_count, sub['id']))
                if new_miss_count >= MISS_THRESHOLD:
                    conn.execute('''
                        UPDATE crew_records
                        SET sign_off_time = ?, synced_at = ?
                        WHERE crew_id = ? AND (sign_off_time IS NULL OR sign_off_time = '' OR sign_off_time = '-')
                    ''', (synced_at, synced_at, cid))
            else:
                conn.execute('DELETE FROM crew_records WHERE crew_id = ?', (cid,))
        else:
            conn.execute('UPDATE crew_submissions SET ingest_miss_count = 0 WHERE crew_id = ?', (cid,))
    
    conn.commit()
    conn.close()
    # Stamp last successful CMS sync time (shown on admin dashboard)
    scraper_state['last_run'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return jsonify({'status': 'ok', 'inserted': inserted, 'updated': updated, 'protected': skipped})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    # use_reloader=False is critical: prevents double-spawning the scraper/cleanup threads
    app.run(host='0.0.0.0', port=port, debug=app.config['DEBUG'], use_reloader=False)
