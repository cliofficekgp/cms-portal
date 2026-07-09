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
from flask import Flask, render_template, request, jsonify, redirect, url_for, session

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

# Global state for scraper
scraper_state = {
    'status': 'starting',
    'message': 'Initializing background thread...',
    'image_base64': '',
    'action_required': False,
    'action_type': '', # 'captcha' or 'otp'
    'submitted_value': None,
    'last_updated': datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
}

# ---------------------------------------------------------------------------
# Background Scraper Thread
# ---------------------------------------------------------------------------

def run_scraper_thread():
    scraper_script = os.path.join(BASE_DIR, 'scraper', 'login.py')
    python_exe = sys.executable
    while True:
        try:
            print("[Thread] Starting scraper subprocess...")
            scraper_state['status'] = 'running'
            scraper_state['message'] = 'Scraper subprocess starting...'
            subprocess.run([python_exe, scraper_script], cwd=os.path.join(BASE_DIR, 'scraper'))
            print("[Thread] Scraper subprocess exited. Restarting in 30 seconds...")
            import time
            time.sleep(30)
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
    conn = sqlite3.connect(DB_PATH)
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
        CREATE TABLE IF NOT EXISTS cms_settings (
            id            INTEGER PRIMARY KEY,
            cms_username  TEXT,
            cms_password  TEXT
        )
    ''')

    row_count = cur.execute('SELECT COUNT(*) FROM cms_settings').fetchone()[0]
    if row_count == 0:
        cur.execute(
            'INSERT INTO cms_settings (id, cms_username, cms_password) VALUES (1, ?, ?)',
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
        conn.close()
        
        session['crew_id'] = crew_id
        if row:
            return redirect(url_for('form'))
        return redirect(url_for('form', manual='1'))
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
            0, '', '', crew_id, departure_time
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
        if cms_user and cms_pass:
            conn.execute('UPDATE cms_settings SET cms_username = ?, cms_password = ? WHERE id = 1', (cms_user, cms_pass))
            conn.commit()
            return redirect(url_for('admin_settings'))
    row = conn.execute('SELECT cms_username, cms_password FROM cms_settings WHERE id = 1').fetchone()
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
        WHERE sign_off_time IS NOT NULL AND sign_off_time != '' AND sign_off_time != '-' AND is_active = 1
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
    conn.close()
    
    data = [dict(row) for row in rows]
    
    now = datetime.now(IST)
    
    def parse_dt(val):
        if not val or val == '-': return None
        val = val.strip()
        for fmt in ['%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
            try:
                dt = datetime.strptime(val, fmt)
                return dt.replace(tzinfo=IST)
            except ValueError:
                pass
        return None

    for row in data:
        # Calculate Duty Hours
        sign_on_dt = None
        if row.get('sign_on_time') and row['sign_on_time'] != '-':
            sign_on_dt = parse_dt(row['sign_on_time'])
            if sign_on_dt:
                delta = now - sign_on_dt
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                row['duty_hrs'] = f"{hours:02d}:{minutes:02d}"

        # Calculate PDD
        row['pdd'] = '-'
        lobbies = {'KGP', 'ADL', 'SRC', 'TPKR', 'NMP', 'BLS', 'GTS'}
        from_sttn = row.get('from_sttn', '') or ''
        from_sttn = from_sttn.upper()
        cto_sttn = row.get('current_location', '') or ''
        cto_sttn = cto_sttn.upper()
        
        if from_sttn in lobbies and from_sttn == cto_sttn and sign_on_dt:
            departure_dt = parse_dt(row.get('departure_time'))
            end_time = departure_dt if departure_dt else now
            pdd_delta = end_time - sign_on_dt
            if pdd_delta.total_seconds() > 0:
                p_hours = int(pdd_delta.total_seconds() // 3600)
                p_minutes = int((pdd_delta.total_seconds() % 3600) // 60)
                row['pdd'] = f"{p_hours:02d}:{p_minutes:02d}"

        # Format dates universally (DD/MM/YY HH:MM)
        for col in ['sign_on_time', 'cto_time', 'relief_datetime', 'departure_time']:
            dt = parse_dt(row.get(col, ''))
            if dt:
                row[col] = dt.strftime('%d/%m/%y %H:%M')
            elif col in row and not row[col]:
                row[col] = '-'

    def cto_sort_key(r):
        dt = parse_dt(r.get('cto_time'))
        return dt if dt else datetime.min
            
    data.sort(key=cto_sort_key, reverse=True)
    
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
            has_manual_sub = False
            if sub:
                has_manual_sub = sign_on_match(existing.get('sign_on_time'), rec.get('sign_on_time'), 60)

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

    payload_ids = set(payload.keys())
    all_db = conn.execute('SELECT crew_id FROM crew_records').fetchall()
    for row in all_db:
        cid = row['crew_id']
        if cid not in payload_ids:
            sub = conn.execute('SELECT id FROM crew_submissions WHERE crew_id = ? ORDER BY submitted_at DESC LIMIT 1', (cid,)).fetchone()
            if sub:
                conn.execute('UPDATE crew_submissions SET ingest_miss_count = ingest_miss_count + 1 WHERE id = ?', (sub['id'],))
            else:
                conn.execute('DELETE FROM crew_records WHERE crew_id = ?', (cid,))
        else:
            # reset miss count if found
            conn.execute('UPDATE crew_submissions SET ingest_miss_count = 0 WHERE crew_id = ?', (cid,))

    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'inserted': inserted, 'updated': updated, 'protected': skipped})

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    # use_reloader=False is critical: prevents double-spawning the scraper/cleanup threads
    app.run(host='0.0.0.0', port=port, debug=app.config['DEBUG'], use_reloader=False)
