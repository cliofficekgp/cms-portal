"""
Public JSON APIs — authenticated by user session login (except for lookup/active/stations).
Used by client-side JavaScript on the frontend.
"""
import os
import csv
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from webapp.database import get_db
from webapp.extensions import IST, BASE_DIR
from webapp.utils import parse_dt
from webapp.routes.auth import login_required

api_public_bp = Blueprint('api_public', __name__)


@api_public_bp.route('/api/save_ns_status', methods=['POST'])
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


@api_public_bp.route('/api/admin_edit', methods=['POST'])
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
                      'cto_time', 'departure_time', 'is_relief', 'relief_station', 'relief_datetime'}
    if not crew_id or field not in ALLOWED_FIELDS:
        return jsonify({'status': 'error', 'message': 'Invalid field or crew_id'}), 400

    if field == 'loco_no':
        import re
        value = re.sub(r'[^0-9,]', '', value)

    conn = get_db()

    # If location is explicitly being removed, just let it be. PDD will dynamically track via departure_time.
    if field == 'current_location':
        if value != '':
            conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field = 'frozen_pdd'", (crew_id,))

    if field == 'departure_time':
        conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field = 'is_rtis_departure'", (crew_id,))

    if field in ('cto_time', 'departure_time', 'relief_datetime') and value and value not in ['-', '–']:
        # Construct full datetime from sign_on_time + submitted time
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

    # Auto-clear location and freeze PDD if departure_time is entered and is >= last loc update
    if field == 'departure_time' and value and value not in ['-', '–']:
        loc_row = conn.execute('''
            SELECT COALESCE(s.current_location, '') as sub_loc,
                   s.submitted_at as sub_time,
                   a.value as admin_loc,
                   a.updated_at as admin_loc_time
            FROM crew_records r
            LEFT JOIN crew_submissions s ON r.crew_id = s.crew_id
            LEFT JOIN admin_edits a ON r.crew_id = a.crew_id AND a.field = 'current_location'
            WHERE r.crew_id = ?
            ORDER BY s.id DESC LIMIT 1
        ''', (crew_id,)).fetchone()

        if loc_row:
            admin_loc_time_str = loc_row['admin_loc_time']
            admin_loc_dt = parse_dt(admin_loc_time_str) if admin_loc_time_str else None
            sub_loc_time_str = loc_row['sub_time']
            sub_loc_dt = parse_dt(sub_loc_time_str) if sub_loc_time_str else None

            active_loc = ''
            loc_time_dt = None
            if admin_loc_dt and sub_loc_dt:
                if admin_loc_dt >= sub_loc_dt:
                    active_loc = loc_row['admin_loc']
                    loc_time_dt = admin_loc_dt
                else:
                    active_loc = loc_row['sub_loc']
                    loc_time_dt = sub_loc_dt
            elif admin_loc_dt:
                active_loc = loc_row['admin_loc']
                loc_time_dt = admin_loc_dt
            elif sub_loc_dt:
                active_loc = loc_row['sub_loc']
                loc_time_dt = sub_loc_dt

            new_dep_dt = parse_dt(value)
            if active_loc and new_dep_dt and loc_time_dt and new_dep_dt >= loc_time_dt:
                # Clear the location
                conn.execute('''
                    INSERT INTO admin_edits (crew_id, field, value, updated_at)
                    VALUES (?, 'current_location', '', ?)
                    ON CONFLICT(crew_id, field) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                ''', (crew_id, updated_at))

    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@api_public_bp.route('/api/add_phone', methods=['POST'])
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


@api_public_bp.route('/api/crew_lookup/<crew_id>', methods=['GET'])
def api_crew_lookup(crew_id):
    crew_id = crew_id.strip().upper()
    conn = get_db()

    # 1. Find latest submission for this crew
    sub = conn.execute('''
        SELECT loco_no, train_no, bpc_no
        FROM crew_submissions
        WHERE crew_id = ?
        ORDER BY submitted_at DESC LIMIT 1
    ''', (crew_id,)).fetchone()

    if sub:
        result = dict(sub)
    else:
        # Fallback to crew_records if no submission exists
        rec = conn.execute('''
            SELECT loco_no, train_no, '' as bpc_no
            FROM crew_records
            WHERE crew_id = ?
        ''', (crew_id,)).fetchone()
        result = dict(rec) if rec else {'loco_no': '', 'train_no': '', 'bpc_no': ''}

    # 2. Apply any admin overrides
    admin_edits = conn.execute('''
        SELECT field, value
        FROM admin_edits
        WHERE crew_id = ? AND field IN ('loco_no', 'train_no', 'bpc_no')
    ''', (crew_id,)).fetchall()

    for edit in admin_edits:
        if edit['value'] is not None and edit['value'].strip() != '':
            result[edit['field']] = edit['value']

    conn.close()

    if any(result.values()):
        return jsonify({
            'status': 'ok',
            'loco_no': result.get('loco_no', ''),
            'train_no': result.get('train_no', ''),
            'bpc_no': result.get('bpc_no', '')
        })
    else:
        return jsonify({'status': 'not_found'})


@api_public_bp.route('/api/active_crews', methods=['GET'])
def api_active_crews():
    try:
        conn = get_db()
        rows = conn.execute("SELECT crew_id, name, desig FROM crew_records WHERE is_active = 1").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print(f"Error fetching active crews: {e}")
        return jsonify([])


@api_public_bp.route('/api/stations', methods=['GET'])
def api_stations():
    stations = []
    csv_path = os.path.join(BASE_DIR, 'webapp', '01_station_code.csv')
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
