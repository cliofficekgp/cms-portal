"""
Core business logic for merging and calculating crew data metrics.
Separated from routes to allow reuse and easier testing.
"""
from datetime import datetime
from webapp.extensions import IST, DB_WRITE_LOCK
from webapp.utils import parse_dt, get_nearest_station
from webapp.database import get_db

def _parse_dt_report(val):
    """Parse date string (DD-MM-YYYY HH:MM variants) into IST-aware datetime."""
    if not val or val == '-':
        return None
    val = val.strip()
    for fmt in [
        '%d-%m-%Y %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%y %H:%M',
        '%d-%m-%Y', '%d-%m-%y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'
    ]:
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None

def _get_crew_list_data(conn):
    """
    Retrieve, merge and calculate crew list data enforcing the priority order:
    admin_edits > crew_submissions > crew_records
    """
    # 1. Pre-process: Mark signed off crews as inactive (WAL handles concurrent reads/writes without locking here)
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

    # 2. Main query combining current CMS (crew_records) and submission data (crew_submissions + booking_ta_crew)
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

    # NOTE: conn stays open — we will batch all cleanup writes through it at the end
    data = [dict(row) for row in rows]

    now = datetime.now(IST)

    # Deferred cleanup lists — collected during the loop, executed in one batch at the end
    _del_admin_edits    = []   # (crew_id, field)
    _del_submissions    = []   # crew_id
    _del_loc_admin      = []   # crew_id
    _null_loc_sub       = []   # crew_id
    _freeze_pdd         = []   # (crew_id, pdd_val)
    _del_relief_admin   = []   # crew_id
    _null_relief_sub    = []   # crew_id
    _release_locos      = []   # loco_no — to delete from loco_locations

    # --- Fetch RTIS Loco Locations ---
    try:
        loco_locs = conn.execute("SELECT loco_no, crew_id, latitude, longitude, location_name, updated_at FROM loco_locations").fetchall()
        rtis_locations = {}       # keyed by loco_no
        rtis_loc_by_crew = {}     # keyed by crew_id — allows matching even when CMS shows a different loco
        for l in loco_locs:
            loc_entry = {
                'lat': l['latitude'],
                'lon': l['longitude'],
                'loc': l['location_name'],
                'time': l['updated_at']
            }
            rtis_locations[l['loco_no']] = loc_entry
            if l['crew_id']:
                rtis_loc_by_crew[l['crew_id']] = loc_entry
    except Exception as e:
        print(f"Error fetching loco locations: {e}")
        rtis_locations = {}
        rtis_loc_by_crew = {}

    for row in data:
        crew_id = row.get('crew_id', '')
        crew_admin_edits = admin_edits_map.get(crew_id, {})

        sign_on_dt = None
        if row.get('sign_on_time') and row['sign_on_time'] not in ['-', '–']:
            sign_on_dt = parse_dt(row['sign_on_time'])

        # --- Retroactive Stale Data Cleanup (deferred) ---
        if sign_on_dt:
            # 1. Queue stale admin edits for deletion
            for field in list(crew_admin_edits.keys()):
                ae_time = admin_edits_time_map.get(crew_id, {}).get(field)
                if ae_time:
                    ae_dt = parse_dt(ae_time)
                    if ae_dt and ae_dt < sign_on_dt:
                        _del_admin_edits.append((crew_id, field))
                        del crew_admin_edits[field]

            # 2. Queue stale crew submissions for deletion
            sub_time = row.get('submitted_at')
            if sub_time:
                sub_dt = parse_dt(sub_time)
                if sub_dt and sub_dt < sign_on_dt:
                    _del_submissions.append(crew_id)
                    # Wipe submission data from current row immediately
                    for sf in ['s_loco_no', 's_train_no', 'bpc_no', 'cto_time', 'is_relief', 'relief_station', 'relief_datetime']:
                        if sf in row:
                            row[sf] = None
                    row['submitted_at'] = None

        # --- Determine data source for each editable field ---
        # Source priority: admin > crew submission > cms record
        src = {}

        for field in ['loco_no', 'train_no']:
            sub_field = f's_{field}'
            if field in crew_admin_edits and crew_admin_edits[field] is not None:
                src[field] = 'admin'
                row[field] = crew_admin_edits[field]
            elif row.get(sub_field):
                src[field] = 'sub'
            elif row.get(field):
                src[field] = 'cms'
            else:
                src[field] = 'none'

        row['_src'] = src

        for field in ['bpc_no', 'cto_time', 'departure_time']:
            if field in crew_admin_edits and crew_admin_edits[field] is not None:
                src[field] = 'admin'
                row[field] = crew_admin_edits[field]
            elif row.get(field):
                src[field] = 'sub'
            else:
                src[field] = 'none'

        row['_loc_update_time'] = ''
        loc_time_dt = None

        admin_loc = crew_admin_edits.get('current_location')
        has_admin_loc = 'current_location' in crew_admin_edits
        admin_loc_time_str = admin_edits_time_map.get(crew_id, {}).get('current_location', '')
        admin_loc_dt = parse_dt(admin_loc_time_str) if admin_loc_time_str else None

        sub_loc = row.get('current_location')
        has_sub_loc = sub_loc is not None
        sub_loc_time_str = row.get('submitted_at', '')
        sub_loc_dt = parse_dt(sub_loc_time_str) if sub_loc_time_str else None

        if has_admin_loc and has_sub_loc and admin_loc_dt and sub_loc_dt:
            if admin_loc_dt >= sub_loc_dt:
                src['current_location'] = 'admin'
                row['current_location'] = admin_loc
                loc_time_dt = admin_loc_dt
            else:
                src['current_location'] = 'sub'
                row['current_location'] = sub_loc
                loc_time_dt = sub_loc_dt
        elif has_admin_loc:
            src['current_location'] = 'admin'
            row['current_location'] = admin_loc
            loc_time_dt = admin_loc_dt
        elif has_sub_loc:
            src['current_location'] = 'sub'
            row['current_location'] = sub_loc
            loc_time_dt = sub_loc_dt
        else:
            row['current_location'] = ''
            src['current_location'] = 'none'
            loc_time_dt = None

        if loc_time_dt:
            row['_loc_time_dt'] = loc_time_dt
            row['_loc_update_time'] = loc_time_dt.strftime('%H:%M')
        else:
            row['_loc_time_dt'] = None

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

        # --- Release loco tracking for relieved crews (deferred) ---
        if row.get('is_relief') == 1:
            loco_str_for_release = row.get('loco_no', '')
            if loco_str_for_release and loco_str_for_release not in ('', '-'):
                for _l in loco_str_for_release.split(','):
                    _l = _l.strip()
                    if _l and _l in rtis_locations:
                        ll_crew = conn.execute('SELECT crew_id FROM loco_locations WHERE loco_no=?', (_l,)).fetchone()
                        if ll_crew and ll_crew['crew_id'] == crew_id:
                            _release_locos.append(_l)

        # --- Dynamically override current_location with latest RTIS location ---
        run_rtis_check = True
        # Skip RTIS check entirely if crew has marked relief
        if row.get('is_relief') == 1:
            run_rtis_check = False

        # Skip RTIS check if loco_no came strictly from CMS
        if row.get('_src', {}).get('loco_no') == 'cms':
            run_rtis_check = False

        if run_rtis_check:
            loco_no_str = row.get('loco_no')
            if not loco_no_str or loco_no_str in ('', '-'):
                run_rtis_check = False

        if run_rtis_check:
            loco_no_str = row.get('loco_no')
            latest_time = None
            latest_loc = None

            def _parse_rtis_time(loc_data):
                time_str = (loc_data.get('time') or '').replace(' IST', '').strip()
                for fmt in ('%d/%m/%y %H:%M:%S', '%d/%m/%y %H:%M'):
                    try:
                        return datetime.strptime(time_str, fmt).replace(tzinfo=IST)
                    except ValueError:
                        pass
                return None

            if loco_no_str:
                locos_rtis = [l.strip() for l in loco_no_str.split(',') if l.strip()]
                for loco in locos_rtis:
                    if loco in rtis_locations:
                        loc_data = rtis_locations[loco]
                        dt = _parse_rtis_time(loc_data)
                        if dt and (latest_time is None or dt > latest_time):
                            latest_time = dt
                            latest_loc = loc_data

            # Secondary: match by crew_id in loco_locations
            if crew_id in rtis_loc_by_crew:
                loc_data = rtis_loc_by_crew[crew_id]
                dt = _parse_rtis_time(loc_data)
                if dt and (latest_time is None or dt > latest_time):
                    latest_time = dt
                    latest_loc = loc_data

            if latest_loc:
                loc_str = latest_loc.get('loc')
                if not loc_str or loc_str.strip().lower() in ('', 'unknown'):
                    loc_str = ''

                # Fallback to nearest station
                if not loc_str and latest_loc.get('lat') and latest_loc.get('lon'):
                    try:
                        loc_str = get_nearest_station(float(latest_loc['lat']), float(latest_loc['lon']))
                    except (ValueError, TypeError):
                        pass

                display_loc = loc_str if loc_str else ''
                row['_latest_rtis_loco'] = {
                    'loc': display_loc,
                    'time': latest_loc['time'],
                    'lat': latest_loc.get('lat', ''),
                    'lon': latest_loc.get('lon', '')
                }

                if loc_str:
                    manual_time = row.get('_loc_time_dt')
                    if not manual_time or (latest_time and latest_time >= manual_time):
                        row['current_location'] = loc_str
                        row['is_rtis_location'] = True
                        row['_loc_update_time'] = latest_time.strftime('%H:%M') if latest_time else latest_loc['time']

        # Read 'is_rtis_departure' from admin_edits if present
        if crew_admin_edits.get('is_rtis_departure') == '1':
            row['is_rtis_departure'] = True

        # Calculate PDD dynamically based on current state
        lobbies = {'KGP', 'ADL', 'SRC', 'TPKR', 'NMP', 'BLS', 'GTS'}
        from_sttn = (row.get('from_sttn', '') or '').upper().strip()
        cto_sttn = (row.get('current_location', '') or '').upper().strip()

        nmp_cluster = {'NMP', 'NPTY', 'NKKH', 'NMMS', 'NMPY', 'NPDY', 'NPRY'}
        from_sttn_norm = 'NMP' if from_sttn in nmp_cluster else from_sttn
        cto_sttn_norm = 'NMP' if cto_sttn in nmp_cluster else cto_sttn

        is_pdd_sttn = False
        if sign_on_dt:
            # If they have departed from any tracked station, they have a PDD.
            if parse_dt(row.get('departure_time')) and (from_sttn_norm in lobbies or from_sttn in {'BHC', 'TATA'}):
                is_pdd_sttn = True
            # Otherwise, if they haven't departed yet, check if they are physically at the lobby
            elif from_sttn_norm in {'NMP', 'KGP'} and cto_sttn_norm in {'NMP', 'KGP'}:
                is_pdd_sttn = True
            elif (from_sttn_norm in lobbies or from_sttn in {'BHC', 'TATA'}) and from_sttn_norm == cto_sttn_norm:
                is_pdd_sttn = True

        if is_pdd_sttn:
            departure_dt = parse_dt(row.get('departure_time'))
            end_time = departure_dt if departure_dt else now
            pdd_delta = end_time - sign_on_dt
            if pdd_delta.total_seconds() > 0:
                p_hours = int(pdd_delta.total_seconds() // 3600)
                p_minutes = int((pdd_delta.total_seconds() % 3600) // 60)
                row['pdd'] = f"{p_hours:02d}:{p_minutes:02d}"
        else:
            row['pdd'] = '-'

        # Auto-remove location based on departure time (deferred)
        if row.get('departure_time') and row.get('departure_time') not in ['-', '–']:
            dep_dt = parse_dt(row['departure_time'])
            if dep_dt:
                if not loc_time_dt or dep_dt > loc_time_dt:
                    _del_loc_admin.append(crew_id)
                    _null_loc_sub.append(crew_id)
                    row['current_location'] = ''
                    src['current_location'] = 'none'
                    row['_loc_update_time'] = dep_dt.strftime('%H:%M')
                elif dep_dt <= loc_time_dt:
                    lobbies = {'KGP', 'ADL', 'SRC', 'TPKR', 'NMP', 'BLS', 'GTS'}
                    from_sttn = (row.get('from_sttn', '') or '').upper().strip()
                    curr_loc = (row.get('current_location', '') or '').upper().strip()
                    from_sttn_norm = 'NMP' if from_sttn == 'NPTY' else from_sttn
                    curr_loc_norm = 'NMP' if curr_loc == 'NPTY' else curr_loc

                    is_sign_on = False
                    if from_sttn_norm in {'NMP', 'KGP'} and curr_loc_norm in {'NMP', 'KGP'}:
                        is_sign_on = True
                    elif from_sttn_norm in lobbies and from_sttn_norm == curr_loc_norm:
                        is_sign_on = True

                    if is_sign_on:
                        _del_loc_admin.append(crew_id)
                        _null_loc_sub.append(crew_id)
                        row['current_location'] = ''
                        src['current_location'] = 'none'
                        row['_loc_update_time'] = dep_dt.strftime('%H:%M')

        for field in ['relief_station', 'relief_datetime']:
            if field in crew_admin_edits and crew_admin_edits[field] is not None:
                src[field] = 'admin'
                row[field] = crew_admin_edits[field]
            elif row.get(field):
                src[field] = 'sub'
            else:
                src[field] = 'none'


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

        # Auto-remove stale relief data if relief_datetime is before current sign_on_time (deferred)
        if sign_on_dt and row.get('relief_datetime') and row['relief_datetime'] not in ['-', '–']:
            relief_dt = parse_dt(row['relief_datetime'])
            if relief_dt and relief_dt < sign_on_dt:
                _del_relief_admin.append(crew_id)
                _null_relief_sub.append(crew_id)
                row['is_relief'] = 0
                row['relief_station'] = ''
                row['relief_datetime'] = ''
                src['is_relief'] = 'none'
                src['relief_station'] = 'none'
                src['relief_datetime'] = 'none'

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

        # Format dates universally (DD/MM/YY HH:MM)
        for col in ['sign_on_time']:
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

        # Format departure_time as time only
        dt_dep = parse_dt(row.get('departure_time', ''))
        if dt_dep:
            row['departure_time'] = dt_dep.strftime('%H:%M')
        elif 'departure_time' in row and not row['departure_time']:
            row['departure_time'] = '-'

        # Format relief_datetime as time only
        dt_relief = parse_dt(row.get('relief_datetime', ''))
        if dt_relief:
            row['relief_datetime'] = dt_relief.strftime('%H:%M')
        elif 'relief_datetime' in row and not row['relief_datetime']:
            row['relief_datetime'] = '-'

    # --- Execute all deferred DB cleanup in one batch using the single conn ---
    try:
        with DB_WRITE_LOCK:
            for cid, field in _del_admin_edits:
                conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field = ?", (cid, field))
            for cid in _del_submissions:
                conn.execute("DELETE FROM crew_submissions WHERE crew_id = ?", (cid,))
            for cid in _del_loc_admin:
                conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field = 'current_location'", (cid,))
            for cid in _null_loc_sub:
                conn.execute("UPDATE crew_submissions SET current_location = NULL WHERE crew_id = ?", (cid,))
            for cid in _del_relief_admin:
                conn.execute("DELETE FROM admin_edits WHERE crew_id = ? AND field IN ('is_relief', 'relief_station', 'relief_datetime')", (cid,))
            for cid in _null_relief_sub:
                conn.execute("UPDATE crew_submissions SET is_relief = 0, relief_station = NULL, relief_datetime = NULL WHERE crew_id = ?", (cid,))
            for loco_no in set(_release_locos):
                conn.execute("DELETE FROM loco_locations WHERE loco_no = ?", (loco_no,))
                print(f"[RTIS] Auto-released loco {loco_no} (crew fully relieved).")
            conn.commit()
    except Exception as e:
        print(f"Deferred cleanup error: {e}")

    def list_sort_key(r):
        return (r.get('_duty_minutes', 0), r.get('_cto_dt', datetime.min.replace(tzinfo=IST)))

    data.sort(key=list_sort_key, reverse=True)

    total_signon = len(data)
    total_10_hrs = sum(1 for r in data if r.get('_duty_minutes', 0) > 600)
    total_12_hrs = sum(1 for r in data if r.get('_duty_minutes', 0) > 720)

    # Clean up the internal sort-only keys so they don't leak into the template
    for row in data:
        row.pop('_sign_on_dt', None)
        row.pop('_cto_dt', None)

    return data, total_signon, total_10_hrs, total_12_hrs, rtis_locations


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
