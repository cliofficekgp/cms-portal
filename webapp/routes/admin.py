"""
Admin routes — dashboard, settings, reports, scraper control, and crew list.
"""
import csv
import io
import os
import secrets
import string
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, flash, Response as FlaskResponse)
from werkzeug.security import generate_password_hash
from webapp.database import get_db
from webapp.extensions import IST, BASE_DIR, scraper_state, rtis_state
from webapp.routes.auth import login_required, super_admin_required
from webapp.crew_logic import _get_crew_list_data, _build_duty_sheet_data, _parse_dt_report

admin_bp = Blueprint('admin', __name__)


# ---------------------------------------------------------------------------
# Super Admin
# ---------------------------------------------------------------------------

@admin_bp.route('/super_admin')
@super_admin_required
def super_admin():
    conn = get_db()
    users = conn.execute('SELECT id, username, role FROM users').fetchall()
    passcodes = conn.execute('SELECT passcode, is_used FROM signup_passcodes').fetchall()

    current_month_prefix = datetime.now(IST).strftime('%Y-%m')
    ocr_usage = conn.execute('''
        SELECT account_name, SUM(request_count) as total_requests
        FROM ocr_usage_log
        WHERE usage_date LIKE ?
        GROUP BY account_name
    ''', (f'{current_month_prefix}%',)).fetchall()

    conn.close()
    return render_template('super_admin_dashboard.html', users=users, passcodes=passcodes, ocr_usage=ocr_usage, state=scraper_state)


@admin_bp.route('/super_admin/generate_passcode', methods=['POST'])
@super_admin_required
def generate_passcode():
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    conn = get_db()
    conn.execute('INSERT INTO signup_passcodes (passcode, created_by) VALUES (?, ?)', (code, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('admin.super_admin'))


@admin_bp.route('/super_admin/reset_password/<int:user_id>', methods=['POST'])
@super_admin_required
def reset_user_password(user_id):
    conn = get_db()
    user = conn.execute('SELECT username, role FROM users WHERE id = ?', (user_id,)).fetchone()
    if user and user['role'] != 'super_admin':
        # Generate a secure one-time temporary password
        temp_pw = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        hashed_pw = generate_password_hash(temp_pw)
        conn.execute('UPDATE users SET password_hash = ?, force_password_change = 1 WHERE id = ?', (hashed_pw, user_id))
        conn.commit()
        # Fetch template data *before* closing the connection
        users = conn.execute('SELECT id, username, role FROM users').fetchall()
        passcodes = conn.execute('SELECT passcode, is_used FROM signup_passcodes').fetchall()
        reset_username = user['username']
        conn.close()
        # Show the temporary password once directly to the admin who triggered the reset.
        # It is intentionally not flashed/redirected, to keep it out of session data/logs.
        return render_template('super_admin_dashboard.html',
                               users=users, passcodes=passcodes,
                               ocr_usage=[], state=scraper_state,
                               temp_password=temp_pw,
                               reset_username=reset_username)
    conn.close()
    return redirect(url_for('admin.super_admin'))


# ---------------------------------------------------------------------------
# Admin Dashboard
# ---------------------------------------------------------------------------

@admin_bp.route('/admin')
@login_required
def admin():
    return render_template('admin.html', state=scraper_state, rtis_state=rtis_state)


@admin_bp.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if request.method == 'POST':
        # Checkbox: present = 1, absent = 0
        allow_manual = 1 if request.form.get('allow_manual_entry') == 'on' else 0
        cms_username = request.form.get('cms_username', '').strip()
        cms_password = request.form.get('cms_password', '').strip()
        rtis_username = request.form.get('rtis_username', '').strip()
        rtis_password = request.form.get('rtis_password', '').strip()
        now_str = datetime.now(IST).strftime('%d-%m-%Y %H:%M:%S')

        conn = get_db()
        try:
            conn.execute(
                '''UPDATE cms_settings
                   SET cms_username = ?, cms_password = ?,
                       rtis_username = ?, rtis_password = ?,
                       allow_manual_entry = ?, updated_at = ?
                   WHERE id = 1''',
                (cms_username, cms_password, rtis_username, rtis_password, allow_manual, now_str)
            )
            conn.commit()
            flash('Settings saved successfully.', 'success')
        except Exception as e:
            flash(f'Failed to save settings: {str(e)}', 'error')
        finally:
            conn.close()
        return redirect(url_for('admin.admin_settings'))

    conn = get_db()
    row = conn.execute('SELECT * FROM cms_settings WHERE id = 1').fetchone()
    conn.close()
    return render_template('admin_settings.html', settings=row)


@admin_bp.route('/admin/reports')
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

@admin_bp.route('/admin/report/duty_sheet')
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

@admin_bp.route('/admin/report/non_submitters')
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
    logs = conn.execute('SELECT crew_id, name, desig, from_sttn, sign_on_time, category, has_submission FROM crew_duty_log').fetchall()

    # Active records
    recs = conn.execute('''
        SELECT r.crew_id, r.name, r.desig, r.from_sttn, r.sign_on_time, r.category,
               (SELECT COUNT(id) FROM crew_submissions s WHERE s.crew_id = r.crew_id LIMIT 1) as has_submission
        FROM crew_records r WHERE r.is_active = 1
    ''').fetchall()
    conn.close()

    duty_map = {}  # (crew_id, sign_on_dt_str) -> dict

    def process_row(row):
        sign_on_dt = _parse_dt_report(row['sign_on_time'])
        if sign_on_dt and from_dt <= sign_on_dt <= to_dt:
            key = (row['crew_id'], row['sign_on_time'])
            if key not in duty_map:
                duty_map[key] = {
                    'crew_id': row['crew_id'],
                    'name': row['name'] or '',
                    'desig': row['desig'] or '',
                    'has_submission': bool(row['has_submission'])
                }
            else:
                # If either has submission, it's submitted
                duty_map[key]['has_submission'] = duty_map[key]['has_submission'] or bool(row['has_submission'])

    for row in logs: process_row(row)
    for row in recs: process_row(row)

    # Now aggregate per crew
    crew_stats = {}
    for key, data in duty_map.items():
        cid = data['crew_id']
        if cid not in crew_stats:
            crew_stats[cid] = {
                'crew_id': cid,
                'name': data['name'],
                'desig': data['desig'],
                'total_duties': 0,
                'submitted': 0,
                'not_submitted': 0
            }

        crew_stats[cid]['total_duties'] += 1
        if data['has_submission']:
            crew_stats[cid]['submitted'] += 1
        else:
            crew_stats[cid]['not_submitted'] += 1

    # Filter to only show crew who have at least one not_submitted duty
    non_submitters = [stats for stats in crew_stats.values() if stats['not_submitted'] > 0]
    non_submitters.sort(key=lambda x: x['not_submitted'], reverse=True)

    if do_export:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Crew ID', 'Name', 'Designation', 'Total Duties', 'Submitted', 'Not Submitted'])
        for row in non_submitters:
            writer.writerow([
                row['crew_id'], row['name'], row['desig'],
                row['total_duties'], row['submitted'], row['not_submitted']
            ])
        output.seek(0)
        filename = f"Non_Submitters_{from_date_str}_to_{to_date_str}.csv"
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


# ---------------------------------------------------------------------------
# Crew List (admin)
# ---------------------------------------------------------------------------

@admin_bp.route('/crew_list')
@login_required
def crew_list():
    conn = get_db()
    data, total_signon, total_10_hrs, total_12_hrs, rtis_locations = _get_crew_list_data(conn)
    conn.close()

    return render_template('crew_list.html',
                           crew_list=data,
                           total_signon=total_signon,
                           total_10_hrs=total_10_hrs,
                           total_12_hrs=total_12_hrs,
                           rtis_locations=rtis_locations)


# ---------------------------------------------------------------------------
# Scraper Admin Controls
# ---------------------------------------------------------------------------

@admin_bp.route('/admin/submit_action', methods=['POST'])
@login_required
def admin_submit_action():
    action_value = request.form.get('action_value', '').strip()
    if action_value:
        scraper_state['submitted_value'] = action_value
        scraper_state['action_required'] = False
        scraper_state['message'] = 'Action submitted. Waiting for scraper to process...'
        scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin.admin'))


@admin_bp.route('/admin/scraper/trigger', methods=['POST'])
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
    return redirect(url_for('admin.admin'))


@admin_bp.route('/admin/scraper/cancel', methods=['POST'])
@login_required
def scraper_cancel():
    stop_file = os.path.join(BASE_DIR, 'data', 'stop.txt')
    with open(stop_file, 'w') as f:
        f.write('stop')

    scraper_state['status'] = 'cancelled'
    scraper_state['message'] = 'Stopping scraper gracefully...'
    scraper_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin.admin'))


@admin_bp.route('/admin/scraper/run_now', methods=['POST'])
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
    return redirect(url_for('admin.admin'))


# ---------------------------------------------------------------------------
# RTIS Scraper Admin Controls
# ---------------------------------------------------------------------------

@admin_bp.route('/admin/rtis/submit_action', methods=['POST'])
@login_required
def rtis_submit_action():
    action_value = request.form.get('action_value', '').strip()
    if action_value:
        rtis_state['submitted_value'] = action_value
        rtis_state['action_required'] = False
        rtis_state['message'] = 'Action submitted. Waiting for RTIS scraper to process...'
        rtis_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin.admin'))


@admin_bp.route('/admin/rtis/trigger', methods=['POST'])
@login_required
def rtis_trigger():
    stop_file = os.path.join(BASE_DIR, 'data', 'rtis_stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'rtis_fatal_error.txt')
    if os.path.exists(stop_file): os.remove(stop_file)
    if os.path.exists(fatal_file): os.remove(fatal_file)
    rtis_state['status'] = 'starting'
    rtis_state['message'] = 'Manual RTIS trigger initiated...'
    rtis_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin.admin'))


@admin_bp.route('/admin/rtis/cancel', methods=['POST'])
@login_required
def rtis_cancel():
    stop_file = os.path.join(BASE_DIR, 'data', 'rtis_stop.txt')
    with open(stop_file, 'w') as f: f.write('stop')
    rtis_state['status'] = 'cancelled'
    rtis_state['message'] = 'Stopping RTIS scraper gracefully...'
    rtis_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin.admin'))


@admin_bp.route('/admin/rtis/run_now', methods=['POST'])
@login_required
def rtis_run_now():
    run_now_file = os.path.join(BASE_DIR, 'data', 'rtis_run_now.txt')
    stop_file = os.path.join(BASE_DIR, 'data', 'rtis_stop.txt')
    fatal_file = os.path.join(BASE_DIR, 'data', 'rtis_fatal_error.txt')
    if os.path.exists(stop_file): os.remove(stop_file)
    if os.path.exists(fatal_file): os.remove(fatal_file)
    with open(run_now_file, 'w') as f: f.write('run')
    rtis_state['status'] = 'starting'
    rtis_state['message'] = 'Manual RTIS run triggered — waking scraper...'
    rtis_state['last_updated'] = datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
    return redirect(url_for('admin.admin'))
