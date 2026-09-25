"""
Internal API routes — authenticated by X-API-Secret header.
Called by scraper subprocesses for sync, state updates, and RTIS loco tracking.
"""
from datetime import datetime
import hmac
from flask import Blueprint, request, jsonify
from webapp.database import get_db
from webapp.extensions import IST, API_SECRET, DB_WRITE_LOCK, scraper_state, rtis_state
from webapp.utils import parse_dt, archive_to_duty_log

api_internal_bp = Blueprint('api_internal', __name__)

def require_api_secret():
    """Helper to check API secret using constant-time comparison."""
    auth = request.headers.get('X-API-Secret', '')
    if not auth or not hmac.compare_digest(auth, API_SECRET):
        return False
    return True

# ---------------------------------------------------------------------------
# CMS Scraper API Endpoints
# ---------------------------------------------------------------------------

@api_internal_bp.route('/api/scraper/state', methods=['POST'])
def update_scraper_state():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    scraper_state['status'] = payload.get('status', scraper_state['status'])
    scraper_state['message'] = payload.get('message', scraper_state['message'])
    if 'action_required' in payload:
        scraper_state['action_required'] = payload['action_required']
        scraper_state['action_type'] = payload.get('action_type', '')
        scraper_state['image_base64'] = payload.get('image_base64', '')
        scraper_state['phone_number'] = payload.get('phone_number')
        scraper_state['submitted_value'] = None # reset

    if 'active_tunnel' in payload:
        scraper_state['active_tunnel'] = payload['active_tunnel']

    if 'last_ddddocr_failure' in payload:
        scraper_state['last_ddddocr_failure'] = payload['last_ddddocr_failure']

    scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return jsonify({'success': True})


@api_internal_bp.route('/api/scraper/action', methods=['GET'])
def get_scraper_action():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    return jsonify({
        'submitted_value': scraper_state.get('submitted_value')
    })


# ---------------------------------------------------------------------------
# RTIS Scraper API Endpoints
# ---------------------------------------------------------------------------

@api_internal_bp.route('/api/rtis/state', methods=['POST'])
def update_rtis_state():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    rtis_state['status'] = payload.get('status', rtis_state['status'])
    rtis_state['message'] = payload.get('message', rtis_state['message'])
    if 'action_required' in payload:
        rtis_state['action_required'] = payload['action_required']
        rtis_state['action_type'] = payload.get('action_type', '')
        rtis_state['image_base64'] = payload.get('image_base64', '')
        rtis_state['phone_number'] = payload.get('phone_number')
        rtis_state['submitted_value'] = None # reset
    if 'active_tunnel' in payload:
        rtis_state['active_tunnel'] = payload['active_tunnel']
    if 'last_run' in payload:
        rtis_state['last_run'] = payload['last_run']

    rtis_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return jsonify({'success': True})


@api_internal_bp.route('/api/rtis/action', methods=['GET'])
def get_rtis_action():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'submitted_value': rtis_state.get('submitted_value')})


@api_internal_bp.route('/api/rtis/active_locos', methods=['GET'])
def get_active_locos():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    conn = get_db()
    locos = set()
    mapping = {}
    locos_to_release = []

    try:
        # Query active crews with their loco from submissions/admin (ignore CMS loco) and relief/handover info.
        query = '''
        SELECT
            r.crew_id,
            COALESCE(a_loco.value, s.loco_no) AS loco_no,
            COALESCE(a_relief.value, s.is_relief, 0) AS is_relief,
            s.handover_crew_id,
            s.relief_datetime
        FROM crew_records r
        LEFT JOIN (
            SELECT crew_id, loco_no, is_relief, handover_crew_id, relief_datetime FROM crew_submissions
            WHERE id IN (SELECT MAX(id) FROM crew_submissions GROUP BY crew_id)
        ) s ON r.crew_id = s.crew_id
        LEFT JOIN (
            SELECT crew_id, value FROM admin_edits WHERE field = 'loco_no'
        ) a_loco ON r.crew_id = a_loco.crew_id
        LEFT JOIN (
            SELECT crew_id, value FROM admin_edits WHERE field = 'is_relief'
        ) a_relief ON r.crew_id = a_relief.crew_id
        WHERE r.is_active = 1
          AND COALESCE(a_loco.value, s.loco_no) IS NOT NULL
          AND COALESCE(a_loco.value, s.loco_no) != ''
          AND COALESCE(a_loco.value, s.loco_no) != '-'
        '''
        rows = conn.execute(query).fetchall()

        for r in rows:
            is_rel = int(r['is_relief'] or 0)
            loco_str = r['loco_no']
            if not loco_str or loco_str in ('', '-'):
                continue
            for l in loco_str.split(','):
                l = l.strip()
                if not l:
                    continue
                if is_rel == 1:
                    # Crew has been fully relieved: if a handover crew exists, track loco for them
                    handover = (r['handover_crew_id'] or '').strip().upper()
                    if handover:
                        mapping[l] = handover
                        locos.add(l)
                    # else: no handover crew — loco will be in locos_to_release
                else:
                    # Active crew; if relief handover is registered, transfer tracking to handover crew
                    handover = (r['handover_crew_id'] or '').strip().upper()
                    if handover and r['relief_datetime']:
                        mapping[l] = handover
                    else:
                        mapping[l] = r['crew_id']
                    locos.add(l)

        # Find loco_locations rows whose loco is no longer in the active set
        existing_loco_rows = conn.execute('SELECT loco_no FROM loco_locations').fetchall()
        locos_to_release = [row['loco_no'] for row in existing_loco_rows if row['loco_no'] not in locos]

    except Exception as e:
        print(f"Error in active_locos: {e}")

    conn.close()
    return jsonify({'locos': list(locos), 'mapping': mapping, 'locos_to_release': locos_to_release})


@api_internal_bp.route('/api/rtis/sync_loco', methods=['POST'])
def sync_loco_location():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    loco_no = payload.get('loco_no')
    crew_id = payload.get('crew_id')
    lat = payload.get('latitude')
    lon = payload.get('longitude')
    loc_name = payload.get('location_name')
    moved = payload.get('moved_gt_100m', False)

    if not loco_no:
        return jsonify({'error': 'loco_no required'}), 400

    conn = get_db()
    now_str = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')

    try:
        # --- Read phase (no lock needed) ---
        existing = conn.execute('SELECT crew_id, latitude, longitude, location_name, updated_at FROM loco_locations WHERE loco_no = ?', (loco_no,)).fetchone()

        # Pre-check departure_time so we don't hold the lock during reads
        should_set_departure = False
        if moved and crew_id:
            existing_dep = conn.execute(
                "SELECT departure_time FROM crew_submissions WHERE crew_id = ? AND is_active = 1",
                (crew_id,)
            ).fetchone()
            admin_dep = conn.execute(
                "SELECT value FROM admin_edits WHERE crew_id = ? AND field = 'departure_time'",
                (crew_id,)
            ).fetchone()

            has_existing = (existing_dep and existing_dep['departure_time'] and
                            existing_dep['departure_time'] not in ('', '-', '–'))
            has_admin = (admin_dep and admin_dep['value'] and
                         admin_dep['value'] not in ('', '-', '–'))

            if not has_existing and not has_admin:
                should_set_departure = True
            else:
                print(f"[RTIS] Skipped departure_time for {crew_id} — already set by {'admin' if has_admin else 'crew'}")

        # --- Write phase (lock only for writes) ---
        with DB_WRITE_LOCK:
            if existing:
                crew_changed = (existing['crew_id'] != crew_id)
                lat_lon_changed = (round(existing['latitude'] or 0, 5) != round(lat or 0, 5) or
                                   round(existing['longitude'] or 0, 5) != round(lon or 0, 5))
                name_changed = (existing['location_name'] != loc_name)
                loc_changed = moved or lat_lon_changed or name_changed or crew_changed
                new_updated_at = now_str if loc_changed else existing['updated_at']

                conn.execute('''
                    UPDATE loco_locations
                    SET crew_id = ?, prev_latitude = ?, prev_longitude = ?,
                        latitude = ?, longitude = ?, location_name = ?,
                        same_as_previous = ?, updated_at = ?
                    WHERE loco_no = ?
                ''', (crew_id, existing['latitude'], existing['longitude'], lat, lon, loc_name,
                      0 if loc_changed else 1, new_updated_at, loco_no))

                if loc_changed:
                    print(f"[RTIS] Loco {loco_no} ({crew_id}): {existing['location_name']} -> {loc_name} "
                          f"| moved={moved} lat_lon_changed={lat_lon_changed} "
                          f"| updated_at={new_updated_at}")
            else:
                conn.execute('''
                    INSERT INTO loco_locations (loco_no, crew_id, latitude, longitude, location_name, prev_latitude, prev_longitude, same_as_previous, updated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)
                ''', (loco_no, crew_id, lat, lon, loc_name, now_str, now_str))
                print(f"[RTIS] Loco {loco_no} ({crew_id}): NEW entry at {loc_name} | {now_str}")

            if should_set_departure:
                try:
                    conn.execute("UPDATE crew_submissions SET departure_time = ? WHERE crew_id = ? AND is_active = 1",
                                 (now_str, crew_id))
                    print(f"[RTIS] Set departure_time for {crew_id} (no prior entry)")
                except Exception as e:
                    print(f"Error updating departure_time: {e}")

            conn.commit()
    except Exception as e:
        print(f"[RTIS] sync_loco ERROR for {loco_no}: {e}")
        conn.close()
        return jsonify({'error': 'Internal server error'}), 500

    conn.close()
    return jsonify({'success': True})


@api_internal_bp.route('/api/rtis/release_loco', methods=['POST'])
def release_loco():
    """Dissociate a loco from its tracked crew by deleting its loco_locations row.
    Called by the RTIS scraper when a loco is no longer associated with any active crew."""
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    loco_no = payload.get('loco_no')
    if not loco_no:
        return jsonify({'error': 'loco_no required'}), 400

    conn = get_db()
    with DB_WRITE_LOCK:
        conn.execute('DELETE FROM loco_locations WHERE loco_no = ?', (loco_no,))
        conn.commit()
    conn.close()
    print(f"[RTIS] Released loco {loco_no} from tracking (crew signed off or relieved).")
    return jsonify({'success': True, 'loco_no': loco_no})


# ---------------------------------------------------------------------------
# OCR Usage Tracking
# ---------------------------------------------------------------------------

@api_internal_bp.route('/api/ocr_usage', methods=['POST'])
def api_ocr_usage():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    account_name = payload.get('account_name')
    if not account_name:
        return jsonify({'error': 'Missing account_name'}), 400

    usage_date = datetime.now(IST).strftime('%Y-%m-%d')
    conn = get_db()

    with DB_WRITE_LOCK:
        conn.execute('''
            INSERT INTO ocr_usage_log (account_name, usage_date, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(account_name, usage_date) DO UPDATE SET
                request_count = request_count + 1
        ''', (account_name, usage_date))
        conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


# ---------------------------------------------------------------------------
# TA Booking Sync
# ---------------------------------------------------------------------------

@api_internal_bp.route('/api/sync_ta', methods=['POST'])
def api_sync_ta():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, list): return jsonify({'error': 'Invalid payload'}), 400

    conn = get_db()
    fetched_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    for i, rec in enumerate(payload):
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

        if i > 0 and i % 50 == 0:
            conn.commit()

    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'count': len(payload)})


@api_internal_bp.route('/api/sync_cli_movements', methods=['POST'])
def api_sync_cli_movements():
    """Sync CLI movement data from CMS scraper."""
    if not require_api_secret():
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, list):
        return jsonify({'error': 'Invalid payload'}), 400

    conn = get_db()
    fetched_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    for i, rec in enumerate(payload):
        cli_id = rec.get('cli_id')
        if not cli_id:
            continue

        name = rec.get('name', '')
        hq_code = rec.get('hq_code', '')
        departure_entry_datetime = rec.get('departure_entry_datetime', '')
        departure_datetime = rec.get('departure_datetime', '')
        from_station = rec.get('from_station', '')
        via = rec.get('via', '')
        to_station = rec.get('to_station', '')
        arrival_entry_datetime = rec.get('arrival_entry_datetime', '')
        arrival_datetime = rec.get('arrival_datetime', '')
        train_no = rec.get('train_no', '')
        loco_no = rec.get('loco_no', '')
        authority = rec.get('authority', '')
        duty_type = rec.get('duty_type', '')
        service_type = rec.get('service_type', '')
        purpose = rec.get('purpose', '')
        entry_mode = rec.get('entry_mode', '')
        remarks = rec.get('remarks', '')
        from_lat = rec.get('from_lat', 0.0)
        from_lon = rec.get('from_lon', 0.0)
        to_lat = rec.get('to_lat', 0.0)
        to_lon = rec.get('to_lon', 0.0)

        conn.execute('''
            INSERT INTO cli_movements (
                cli_id, name, hq_code, departure_entry_datetime, departure_datetime,
                from_station, via, to_station, arrival_entry_datetime, arrival_datetime,
                train_no, loco_no, authority, duty_type, service_type, purpose,
                entry_mode, remarks, fetched_at, from_lat, from_lon, to_lat, to_lon
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cli_id) DO UPDATE SET
                name=excluded.name,
                hq_code=excluded.hq_code,
                departure_entry_datetime=excluded.departure_entry_datetime,
                departure_datetime=excluded.departure_datetime,
                from_station=excluded.from_station,
                via=excluded.via,
                to_station=excluded.to_station,
                arrival_entry_datetime=excluded.arrival_entry_datetime,
                arrival_datetime=excluded.arrival_datetime,
                train_no=excluded.train_no,
                loco_no=excluded.loco_no,
                authority=excluded.authority,
                duty_type=excluded.duty_type,
                service_type=excluded.service_type,
                purpose=excluded.purpose,
                entry_mode=excluded.entry_mode,
                remarks=excluded.remarks,
                fetched_at=excluded.fetched_at,
                from_lat=excluded.from_lat,
                from_lon=excluded.from_lon,
                to_lat=excluded.to_lat,
                to_lon=excluded.to_lon
        ''', (cli_id, name, hq_code, departure_entry_datetime, departure_datetime,
              from_station, via, to_station, arrival_entry_datetime, arrival_datetime,
              train_no, loco_no, authority, duty_type, service_type, purpose,
              entry_mode, remarks, fetched_at, from_lat, from_lon, to_lat, to_lon))

        if i > 0 and i % 50 == 0:
            conn.commit()

    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'count': len(payload)})


# ---------------------------------------------------------------------------
# Full CMS Sync with Shift Detection
# ---------------------------------------------------------------------------

@api_internal_bp.route('/api/sync', methods=['POST'])
def api_sync():
    if not require_api_secret(): return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, dict): return jsonify({'error': 'Invalid payload'}), 400

    conn = get_db()
    inserted = updated = skipped = 0
    synced_at = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

    for i, (crew_id, rec) in enumerate(payload.items()):
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
                archive_to_duty_log(conn, crew_id)
                conn.execute('DELETE FROM admin_edits WHERE crew_id = ?', (crew_id,))
                conn.execute('DELETE FROM crew_submissions WHERE crew_id = ?', (crew_id,))
                # Reset found_in_ns so old duty's NS check doesn't carry over to new duty
                conn.execute("UPDATE crew_records SET found_in_ns = 'no' WHERE crew_id = ?", (crew_id,))
                has_manual_sub = False
            else:
                has_manual_sub = bool(sub)

            if has_manual_sub:
                conn.execute('''
                    UPDATE crew_records SET duty_hrs = ?, sign_off_time = ?, synced_at = ?, is_active = 1
                    WHERE crew_id = ?
                ''', (rec.get('duty_hrs', ''), rec.get('sign_off_time', '-'), synced_at, crew_id))
                skipped += 1
            else:
                conn.execute('''
                    UPDATE crew_records
                    SET name=?, desig=?, from_sttn=?, sign_on_time=?,
                        to_sttn=?, sign_off_time=?, duty_hrs=?,
                        route=?, loco_no=?, train_no=?, category=?,
                        manually_edited=0, synced_at=?, is_active=1
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

        if i > 0 and i % 25 == 0:
            conn.commit()

    MISS_THRESHOLD = 2  # missed 2 consecutive syncs (~1hr, given 30-min scraper interval) = treat as signed off

    payload_ids = set(payload.keys())
    all_db = conn.execute('SELECT crew_id FROM crew_records').fetchall()
    for i, row in enumerate(all_db):
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
                        SET sign_off_time = ?, synced_at = ?, is_active = 0
                        WHERE crew_id = ? AND (sign_off_time IS NULL OR sign_off_time = '' OR sign_off_time = '-')
                    ''', (synced_at, synced_at, cid))
                    archive_to_duty_log(conn, cid, has_submission=1)
            else:
                archive_to_duty_log(conn, cid, has_submission=0)
                conn.execute('UPDATE crew_records SET is_active = 0 WHERE crew_id = ?', (cid,))
        else:
            conn.execute('UPDATE crew_submissions SET ingest_miss_count = 0 WHERE crew_id = ?', (cid,))

        if i > 0 and i % 25 == 0:
            conn.commit()

    conn.commit()
    conn.close()
    # Stamp last successful CMS sync time (shown on admin dashboard)
    scraper_state['last_run'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return jsonify({'status': 'ok', 'inserted': inserted, 'updated': updated, 'protected': skipped})
