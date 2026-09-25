"""
Crew-facing routes — login, form submission, and public crew list.
"""
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session
from webapp.database import get_db
from webapp.extensions import IST
from webapp.utils import parse_dt, sign_on_match, archive_to_duty_log
from webapp.crew_logic import _get_crew_list_data

crew_bp = Blueprint('crew', __name__)


@crew_bp.route('/')
def index():
    return render_template('index.html')


@crew_bp.route('/crew_login', methods=['GET', 'POST'])
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
            return redirect(url_for('crew.form', manual='1'))

        conn.close()
        session['crew_id'] = crew_id
        return redirect(url_for('crew.form'))
    return render_template('login.html')


@crew_bp.route('/form')
def form():
    crew_id = session.get('crew_id')
    if not crew_id:
        return redirect(url_for('crew.crew_login'))

    manual = request.args.get('manual', '0') == '1'

    conn = get_db()
    row = conn.execute('SELECT * FROM crew_records WHERE crew_id = ?', (crew_id,)).fetchone()
    crew_data = dict(row) if row else {}
    if manual:
        for k in list(crew_data.keys()):
            if k not in ['crew_id', 'name', 'desig']:
                crew_data[k] = ''
    else:
        # Fetch latest submission to override/populate form fields
        sub = conn.execute('SELECT * FROM crew_submissions WHERE crew_id = ? ORDER BY submitted_at DESC LIMIT 1', (crew_id,)).fetchone()
        sub_dict = {}
        if sub:
            sub_dict = dict(sub)
            if sign_on_match(crew_data.get('sign_on_time'), sub_dict.get('sign_on_time')):
                # Override with manual submission data if present
                for field in ['loco_no', 'train_no', 'bpc_no', 'to_sttn', 'cto_time', 'current_location', 'departure_time', 'relief_datetime']:
                    if sub_dict.get(field):
                        crew_data[field] = sub_dict[field]
            else:
                sub = None
                sub_dict = {}

        # Fetch admin edits to override both CMS records and crew submissions
        admin_edits = conn.execute('SELECT field, value, updated_at FROM admin_edits WHERE crew_id = ?', (crew_id,)).fetchall()

        sub_dt = None
        if sub and sub_dict.get('submitted_at'):
            sub_dt = parse_dt(sub_dict['submitted_at'])

        sign_on_dt = parse_dt(crew_data.get('sign_on_time'))

        for edit in admin_edits:
            if edit['value'] is not None and edit['value'].strip() != '':
                admin_dt = parse_dt(edit['updated_at'])
                # Ignore admin edits that happened before this duty started
                if sign_on_dt and admin_dt and admin_dt < sign_on_dt:
                    continue

                if edit['field'] == 'current_location':
                    # Only override current_location if admin edit is newer than latest submission
                    if not sub_dt or (admin_dt and admin_dt >= sub_dt):
                        crew_data[edit['field']] = edit['value']
                else:
                    crew_data[edit['field']] = edit['value']

    # Format sign_on_time for display in form (readonly field)
    if crew_data.get('sign_on_time') and crew_data['sign_on_time'] != '-':
        dt = parse_dt(crew_data['sign_on_time'])
        if dt:
            crew_data['sign_on_time'] = dt.strftime('%d/%m/%y %H:%M')

    # Parse CTO date and time for HTML inputs
    if crew_data.get('cto_time') and crew_data['cto_time'] != '-':
        dt = parse_dt(crew_data['cto_time'])
        if dt:
            crew_data['cto_date_val'] = dt.strftime('%Y-%m-%d')
            crew_data['cto_time_val'] = dt.strftime('%H:%M')

    if crew_data.get('departure_time') and crew_data['departure_time'] != '-':
        dt = parse_dt(crew_data['departure_time'])
        if dt:
            crew_data['departure_date_val'] = dt.strftime('%Y-%m-%d')
            crew_data['departure_time_val'] = dt.strftime('%H:%M')

    if crew_data.get('relief_datetime') and crew_data['relief_datetime'] != '-':
        dt = parse_dt(crew_data['relief_datetime'])
        if dt:
            crew_data['relief_date_val'] = dt.strftime('%Y-%m-%d')
            crew_data['relief_time_val'] = dt.strftime('%H:%M')

    conn.close()

    return render_template('form.html', crew_id=crew_id, crew=crew_data, manual=manual)


@crew_bp.route('/submit', methods=['POST'])
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

    # Mark crew_duty_log entry as has_submission for both this crew and the handover crew
    archive_to_duty_log(conn, crew_id, has_submission=1)
    if is_relief and handover_id:
        archive_to_duty_log(conn, handover_id, has_submission=1)

    conn.commit()
    conn.close()

    return render_template('success.html', crew_id=crew_id)


@crew_bp.route('/live')
def public_crew_list():
    """Public read-only crew list — no login required, phone numbers excluded."""
    conn = get_db()
    data, total_signon, total_10_hrs, total_12_hrs, rtis_locations = _get_crew_list_data(conn)
    conn.close()

    return render_template('public_crew_list.html',
                           crew_list=data,
                           total_signon=total_signon,
                           total_10_hrs=total_10_hrs,
                           total_12_hrs=total_12_hrs,
                           rtis_locations=rtis_locations)


@crew_bp.route('/cli')
def public_cli_dashboard():
    """Public read-only CLI Movement Dashboard — no login required."""
    from datetime import datetime
    from webapp.extensions import IST

    conn = get_db()
    rows = conn.execute('SELECT * FROM cli_movements').fetchall()
    conn.close()

    now = datetime.now(IST)
    cli_data = []
    total_day = 0
    total_night = 0
    last_updated = None

    for row in rows:
        # Parse datetimes
        departure_dt = None
        arrival_dt = None
        try:
            if row['departure_datetime'] and row['departure_datetime'] != '-':
                departure_dt = datetime.strptime(row['departure_datetime'], '%d-%m-%Y %H:%M')
                departure_dt = departure_dt.replace(tzinfo=IST)
        except: pass

        try:
            if row['arrival_datetime'] and row['arrival_datetime'] != '-':
                arrival_dt = datetime.strptime(row['arrival_datetime'], '%d-%m-%Y %H:%M')
                arrival_dt = arrival_dt.replace(tzinfo=IST)
        except: pass

        # Duty Status: ON DUTY if to_station contains "(NA)", else AVAILABLE
        to_station = row['to_station'] or ''
        duty_status = 'ON DUTY' if '(NA)' in to_station else 'AVAILABLE'

        # AT field
        from_station = row['from_station'] or ''
        if duty_status == 'ON DUTY':
            at_field = f"{from_station}--{to_station}"
        else:
            # AVAILABLE: show the current station (the one marked completed)
            at_field = to_station if to_station else from_station

        # REST HRS / ON DUTY HRS
        rest_hrs = ''
        on_duty_hrs = ''
        if duty_status == 'AVAILABLE' and arrival_dt:
            delta = now - arrival_dt
            hours = delta.total_seconds() / 3600
            rest_hrs = f"{hours:.1f}"
        elif duty_status == 'ON DUTY' and departure_dt:
            delta = now - departure_dt
            hours = delta.total_seconds() / 3600
            on_duty_hrs = f"{hours:.1f}"

        # DAY/NIGHT classification
        day_night = 'DAY'
        if departure_dt and (22 <= departure_dt.hour or departure_dt.hour < 4):
            day_night = 'NIGHT'
        elif arrival_dt and (22 <= arrival_dt.hour or arrival_dt.hour < 4):
            day_night = 'NIGHT'

        if day_night == 'DAY':
            total_day += 1
        else:
            total_night += 1

        cli_data.append({
            'cli_id': row['cli_id'],
            'name': row['name'],
            'hq_code': row['hq_code'],
            'duty_status': duty_status,
            'at': at_field,
            'rest_hrs': rest_hrs,
            'on_duty_hrs': on_duty_hrs,
            'day_night': day_night,
            'from_station': from_station,
            'to_station': to_station,
            'train_no': row['train_no'],
            'loco_no': row['loco_no'],
            'authority': row['authority'] or '',
            'duty_type': row['duty_type'] or '',
            'purpose': row['purpose'] or '',
            'remarks': row['remarks'] or ''
        })

        # Track last updated
        if row['fetched_at']:
            if not last_updated or row['fetched_at'] > last_updated:
                last_updated = row['fetched_at']

    # Group by HQ
    hq_groups = {}
    for cli in cli_data:
        hq = cli['hq_code']
        if hq not in hq_groups:
            hq_groups[hq] = []
        hq_groups[hq].append(cli)

    return render_template('cli_dashboard.html',
                           cli_data=cli_data,
                           hq_groups=hq_groups,
                           total_day=total_day,
                           total_night=total_night,
                           last_updated=last_updated or 'Never')
