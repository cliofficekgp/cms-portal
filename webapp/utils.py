"""
Utility functions used across the application.
"""
import math
import csv
import os
from datetime import datetime
from webapp.extensions import IST, BASE_DIR, DB_WRITE_LOCK


def parse_dt(val):
    """Parse various datetime string formats into timezone-aware datetime."""
    if not val or val == '-':
        return None
    val = val.strip().replace(' IST', '')
    for fmt in [
        '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%y %H:%M',
        '%d-%m-%Y', '%d-%m-%y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
        '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%y %H:%M:%S',
        '%d/%m/%y %H:%M', '%d/%m/%Y', '%d/%m/%y'
    ]:
        try:
            dt = datetime.strptime(val, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def parse_sign_on_dt(val):
    """Parse sign-on time with format priority: CMS portal formats first, then fallbacks."""
    if not val or val == '-':
        return None
    val = val.strip().replace(' IST', '')
    # CMS portal typically uses these formats
    for fmt in ['%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%y %H:%M']:
        try:
            dt = datetime.strptime(val, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            pass
    # Fallback to generic parser
    return parse_dt(val)


def sign_on_match(submission_signon, cms_signon, tolerance_minutes=60):
    """
    Check if a submission's sign-on time matches the current duty's sign-on time.
    Used to prevent stale submissions from a previous duty bleeding into the current one.
    """
    if not submission_signon or not cms_signon:
        return False
    sub_dt = parse_sign_on_dt(submission_signon)
    cms_dt = parse_sign_on_dt(cms_signon)
    if not sub_dt or not cms_dt:
        return False
    delta_minutes = abs((sub_dt - cms_dt).total_seconds() / 60)
    return delta_minutes <= tolerance_minutes


# Load stations on module import
stations_list = []

def load_stations():
    """Load station coordinates from CSV for nearest-station lookup."""
    global stations_list
    stations_path = os.path.join(BASE_DIR, 'data', '01_station_code.csv')
    try:
        with open(stations_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    lat = float(row['lat'])
                    lon = float(row['long'])
                    stations_list.append({
                        'code': row['code'],
                        'lat': lat,
                        'lon': lon
                    })
                except (ValueError, KeyError):
                    pass
    except Exception as e:
        print(f"Failed to load stations: {e}")

load_stations()


def haversine(lat1, lon1, lat2, lon2):
    """Calculate distance between two GPS coordinates in kilometers."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def get_nearest_station(lat, lon):
    """Find the nearest station code to the given GPS coordinates."""
    if not stations_list:
        return ""
    nearest = min(stations_list, key=lambda s: haversine(lat, lon, s['lat'], s['lon']))
    return nearest['code']


def archive_to_duty_log(conn, crew_id, has_submission=None):
    """Archive the current crew_records row for a crew_id into crew_duty_log.

    If has_submission is None, auto-detect by checking crew_submissions table.
    Call this BEFORE overwriting or soft-deleting a crew_records row.
    """
    rec = conn.execute(
        'SELECT crew_id, name, desig, from_sttn, sign_on_time, sign_off_time, duty_hrs, category FROM crew_records WHERE crew_id = ?',
        (crew_id,)
    ).fetchone()
    if not rec:
        return

    if has_submission is None:
        sub = conn.execute('SELECT id FROM crew_submissions WHERE crew_id = ?', (crew_id,)).fetchone()
        has_submission = 1 if sub else 0

    # Avoid duplicate log entries for the exact same duty (same crew_id + sign_on_time)
    sign_on = rec['sign_on_time'] or ''
    if sign_on:
        dup = conn.execute(
            'SELECT id FROM crew_duty_log WHERE crew_id = ? AND sign_on_time = ?',
            (crew_id, sign_on)
        ).fetchone()
        if dup:
            # Update the has_submission flag in case it changed
            if has_submission:
                conn.execute(
                    'UPDATE crew_duty_log SET has_submission = 1 WHERE id = ?',
                    (dup['id'],)
                )
            return

    created_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')
    conn.execute('''
        INSERT INTO crew_duty_log
            (crew_id, name, desig, from_sttn, sign_on_time, sign_off_time, duty_hrs, category, has_submission, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        rec['crew_id'], rec['name'], rec['desig'], rec['from_sttn'],
        rec['sign_on_time'], rec['sign_off_time'], rec['duty_hrs'],
        rec['category'], has_submission, created_at
    ))
