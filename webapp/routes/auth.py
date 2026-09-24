"""
Authentication routes — login, logout, signup, password management.
"""
import secrets
import string
from functools import wraps
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from webapp.database import get_db
from webapp.extensions import IST

auth_bp = Blueprint('auth', __name__)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.admin_login'))
        if session.get('force_password_change') and request.endpoint != 'auth.admin_change_password':
            return redirect(url_for('auth.admin_change_password'))
        return f(*args, **kwargs)
    return decorated_function


def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.admin_login'))
        if session.get('role') != 'super_admin':
            return "Unauthorized. Super Admin access required.", 403
        if session.get('force_password_change') and request.endpoint != 'auth.admin_change_password':
            return redirect(url_for('auth.admin_change_password'))
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route('/admin/login', methods=['GET', 'POST'])
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
                return redirect(url_for('auth.admin_change_password'))

            if user['role'] == 'super_admin':
                return redirect(url_for('admin.super_admin'))
            return redirect(url_for('admin.admin'))

        return render_template('admin_login.html', error='Invalid username or password')
    return render_template('admin_login.html')


@auth_bp.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('auth.admin_login'))


@auth_bp.route('/admin/change_password', methods=['GET', 'POST'])
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
            return redirect(url_for('admin.super_admin'))
        return redirect(url_for('admin.admin'))

    return render_template('admin_change_password.html')


@auth_bp.route('/admin/forgot_password', methods=['GET', 'POST'])
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
            new_password = request.form.get('new_password', '')
            if len(new_password) < 6:
                conn.close()
                return render_template('admin_forgot.html', step='3', username=username,
                                       error='Password must be at least 6 characters.')
            hashed = generate_password_hash(new_password)
            conn.execute('UPDATE users SET password_hash = ?, force_password_change = 0 WHERE id = ?', (hashed, user['id']))
            conn.commit()
            conn.close()
            return redirect(url_for('auth.admin_login'))

    return render_template('admin_forgot.html', step='1')


@auth_bp.route('/admin/signup', methods=['GET', 'POST'])
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

        return redirect(url_for('auth.admin_login'))
    return render_template('admin_signup.html')
