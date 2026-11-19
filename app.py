"""Gym Booking System - Flask + SQLite.
Run:  pip install -r requirements.txt   ->   python app.py   ->   open http://127.0.0.1:5000
Demo accounts: password is "1234" for everyone. aldair@kbtu.kz is admin.

NOTE: the code was generated with AI help. The "DONE BY" tags show which team
member is RESPONSIBLE for that part (explains it, tests it, answers questions).

TEAM TASKS (from the weekly table)
  Iskander  : absent (no contribution recorded)
  Aldair    : 20 test functions in the interface            -> page /tests, group 1
  Torezhan  : new user conveniences + test as user, 10 cases -> search, "My bookings",
              "Booked" badge, past badge, upcoming filter; page /tests, group 3
  Adil      : booking cancellation + test case               -> cancel_booking(), /cancel,
              Cancel buttons; page /tests, group 2. Also: base app, UI texts and style.
  Zhanibek  : register + new login, create / delete / change trainings
              -> /register, authenticate(), /admin (create, update, delete trainings)

SPRINT 3 ADDITIONS:
  - Payments, member balance, top-up, and automatic refund on cancellation (Payments table)
  - Live DB-verified RBAC & Admin Members/Permissions management
  - Visual Gym Occupancy / Load dashboard (summary card, per-session bars, SVG chart)
  - Security: 5-attempt login lockout, Password Reset flow (PasswordResets table),
    CSRF protection on POST forms, atomic SQLite transactions (BEGIN IMMEDIATE)
  - 19 new in-app tests + deployment readiness (env config, WSGI DB init, Render files)
"""
import math
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, abort, g, render_template_string, request, redirect,
                   url_for, flash, session)
from werkzeug.security import generate_password_hash, check_password_hash

DB = os.environ.get("DATABASE_PATH", "gym.db")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

DEMO_PW_HASH = generate_password_hash("1234")  # computed once (hashing is slow)
MIN_PASSWORD = 4  # demo value; use 8+ in a real system
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
RESET_TOKEN_MINUTES = 30
DEFAULT_MEMBER_BALANCE = 100.0
DEFAULT_SESSION_PRICE = 10.0

_initialized_dbs = set()
_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS Members (
    member_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    membership_status TEXT NOT NULL DEFAULT 'active',
    password_hash TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member',   -- 'member' | 'admin'
    balance REAL NOT NULL DEFAULT 100.0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
CREATE TABLE IF NOT EXISTS Sessions (
    session_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    trainer TEXT NOT NULL,
    session_date TEXT NOT NULL,   -- YYYY-MM-DD
    start_time TEXT NOT NULL,     -- HH:MM
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    price REAL NOT NULL DEFAULT 10.0 CHECK (price >= 0)
);
CREATE TABLE IF NOT EXISTS Bookings (
    booking_id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id),
    session_id INTEGER NOT NULL REFERENCES Sessions(session_id),
    status TEXT NOT NULL DEFAULT 'confirmed',
    booked_at TEXT NOT NULL,
    UNIQUE (member_id, session_id)
);
CREATE TABLE IF NOT EXISTS Payments (
    payment_id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id),
    session_id INTEGER NOT NULL REFERENCES Sessions(session_id),
    amount REAL NOT NULL CHECK (amount >= 0),
    status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'refunded')),
    created_at TEXT NOT NULL,
    refunded_at TEXT
);
CREATE TABLE IF NOT EXISTS PasswordResets (
    token TEXT PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id),
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""


# =====================================================================
# DATABASE HELPERS (base app, AI-generated; Iskander's real DB/seed goes here - absent)
# =====================================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def day(n):
    """ISO date n days from today (used for demo data and tests)."""
    return (datetime.now().date() + timedelta(days=n)).isoformat()


def seed_db(db):
    """Create tables, apply idempotent migrations, and seed demo data (only if empty).
    Works for gym.db and for the in-memory databases used by the tests."""
    db.executescript(SCHEMA)

    # Idempotent migrations for older gym.db files
    member_cols = {r[1] for r in db.execute("PRAGMA table_info(Members)")}
    if "password_hash" not in member_cols:
        db.execute("ALTER TABLE Members ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    if "role" not in member_cols:
        db.execute("ALTER TABLE Members ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
    if "balance" not in member_cols:
        db.execute("ALTER TABLE Members ADD COLUMN balance REAL NOT NULL DEFAULT 100.0")
    if "failed_attempts" not in member_cols:
        db.execute("ALTER TABLE Members ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0")
    if "locked_until" not in member_cols:
        db.execute("ALTER TABLE Members ADD COLUMN locked_until TEXT")

    session_cols = {r[1] for r in db.execute("PRAGMA table_info(Sessions)")}
    if "price" not in session_cols:
        db.execute("ALTER TABLE Sessions ADD COLUMN price REAL NOT NULL DEFAULT 10.0")

    if db.execute("SELECT COUNT(*) FROM Members").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO Members(name,email,membership_status,password_hash,role,balance) "
            "VALUES (?,?,?,?,?,?)", [
                ("Aldair", "aldair@kbtu.kz", "active", DEMO_PW_HASH, "admin", 85.0),
                ("Iskander", "iskander@kbtu.kz", "active", DEMO_PW_HASH, "member", 85.0),
                ("Adil", "adil@kbtu.kz", "active", DEMO_PW_HASH, "member", 100.0),
                ("Zhanibek", "zhanibek@kbtu.kz", "active", DEMO_PW_HASH, "member", 100.0),
                ("Expired Member", "expired@kbtu.kz", "expired", DEMO_PW_HASH, "member", 100.0),
            ])
        db.executemany(
            "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) "
            "VALUES (?,?,?,?,?,?)", [
                ("Yoga", "Aigerim", day(1), "10:00", 2, 15.0),
                ("Boxing", "Daniyar", day(2), "18:00", 3, 20.0),
                ("Fitness", "Madina", day(3), "09:00", 4, 12.0),
                ("Yoga (past)", "Aigerim", day(-1), "10:00", 5, 15.0),
            ])
        now = datetime.now().isoformat(timespec="seconds")
        # Yoga is already full: members 1 and 2 (each paid 15.0 from initial 100.0)
        db.executemany(
            "INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (?,?,?,?)",
            [(1, 1, "confirmed", now), (2, 1, "confirmed", now)])
        db.executemany(
            "INSERT INTO Payments(member_id,session_id,amount,status,created_at) VALUES (?,?,?,?,?)",
            [(1, 1, 15.0, "paid", now), (2, 1, 15.0, "paid", now)])
    else:
        # Old members without a password get the demo password; make sure one admin exists
        db.execute("UPDATE Members SET password_hash=? WHERE password_hash=''", (DEMO_PW_HASH,))
        if db.execute("SELECT COUNT(*) FROM Members WHERE role='admin'").fetchone()[0] == 0:
            db.execute("UPDATE Members SET role='admin' WHERE member_id=1")
    db.commit()


def init_db():
    db = sqlite3.connect(DB)
    try:
        seed_db(db)
    finally:
        db.close()
    _initialized_dbs.add(DB)


def ensure_db_initialized():
    """Safe, idempotent DB startup guard for WSGI (Gunicorn) and local runs."""
    if DB not in _initialized_dbs or (DB != ":memory:" and not os.path.exists(DB)):
        with _init_lock:
            if DB not in _initialized_dbs or (DB != ":memory:" and not os.path.exists(DB)):
                init_db()


def _starts(row):
    """Start datetime of a session row."""
    return datetime.fromisoformat(f"{row['session_date']}T{row['start_time']}")


# =====================================================================
# SECURITY HELPERS (Sprint 3: CSRF, Lockout, Password Reset)
# =====================================================================
def csrf_token():
    """Return (and lazily create) a per-session CSRF token."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_security_helpers():
    return {"csrf_token": csrf_token}


@app.before_request
def before_request_guards():
    ensure_db_initialized()
    if request.method == "POST":
        if not app.config.get("TESTING") or app.config.get("ENFORCE_CSRF_IN_TESTS"):
            expected = session.get("_csrf_token")
            provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not provided or not secrets.compare_digest(expected, provided):
                abort(400, description="Invalid or missing CSRF token.")


# =====================================================================
# DONE BY: Zhanibek + Sprint 3 RBAC - authentication, roles, registration
# =====================================================================
def current_user():
    if "member_id" not in session:
        return None
    return get_db().execute("SELECT * FROM Members WHERE member_id=?",
                            (session["member_id"],)).fetchone()


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "member_id" not in session:
            return redirect(url_for("login"))
        user = current_user()
        if user is None:  # account was deleted while logged in
            session.clear()
            flash("Your account is no longer active. Please log in again.", "err")
            return redirect(url_for("login"))
        session["role"] = user["role"]
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "member_id" not in session:
            return redirect(url_for("login"))
        user = current_user()
        if user is None:
            session.clear()
            flash("Your account is no longer active. Please log in again.", "err")
            return redirect(url_for("login"))
        session["role"] = user["role"]
        if user["role"] != "admin":
            flash("Access denied.", "err")
            return redirect(url_for("index"))
        return f(*a, **kw)
    return wrapper


def authenticate_with_status(db, email, password):
    """Authenticate email+password with 5-attempt / 15-minute lockout enforcement.
    Returns (member_row | None, error_message | None)."""
    norm_email = (email or "").strip().lower()
    m = db.execute("SELECT * FROM Members WHERE email=?", (norm_email,)).fetchone()
    if m is None:
        return None, "Wrong email or password."

    now = datetime.now()
    locked_until = m["locked_until"]
    if locked_until:
        try:
            lock_dt = datetime.fromisoformat(locked_until)
        except ValueError:
            lock_dt = None
        if lock_dt and lock_dt > now:
            mins_left = max(1, math.ceil((lock_dt - now).total_seconds() / 60))
            return None, (f"Account is locked due to too many failed login attempts. "
                          f"Try again in {mins_left} minute(s).")

    if m["password_hash"] and check_password_hash(m["password_hash"], password or ""):
        if (m["failed_attempts"] or 0) > 0 or m["locked_until"]:
            with db:
                db.execute("UPDATE Members SET failed_attempts=0, locked_until=NULL WHERE member_id=?",
                           (m["member_id"],))
            m = db.execute("SELECT * FROM Members WHERE member_id=?", (m["member_id"],)).fetchone()
        return m, None

    attempts = (m["failed_attempts"] or 0) + 1
    if attempts >= MAX_LOGIN_ATTEMPTS:
        until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
        with db:
            db.execute("UPDATE Members SET failed_attempts=?, locked_until=? WHERE member_id=?",
                       (attempts, until, m["member_id"]))
        return None, (f"Too many failed login attempts. Account locked for "
                      f"{LOCKOUT_MINUTES} minutes.")

    with db:
        db.execute("UPDATE Members SET failed_attempts=? WHERE member_id=?",
                   (attempts, m["member_id"]))
    return None, "Wrong email or password."


def authenticate(db, email, password):
    """Return the member row if email+password are correct and not locked, otherwise None."""
    member, _err = authenticate_with_status(db, email, password)
    return member


def register_member(db, name, email, password):
    """Create a new member account. Returns (ok, message)."""
    name = (name or "").strip()
    email = (email or "").strip().lower()
    if not name:
        return False, "Name is required."
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return False, "Please enter a valid email address."
    if len(password or "") < MIN_PASSWORD:
        return False, f"Password must be at least {MIN_PASSWORD} characters."
    if db.execute("SELECT 1 FROM Members WHERE email=?", (email,)).fetchone():
        return False, "An account with this email already exists."
    try:
        with db:
            db.execute(
                "INSERT INTO Members(name,email,membership_status,password_hash,role,balance) "
                "VALUES (?,?,'active',?,'member',?)",
                (name, email, generate_password_hash(password), DEFAULT_MEMBER_BALANCE))
    except sqlite3.IntegrityError:
        return False, "An account with this email already exists."
    return True, "Account created. You can log in now."


def create_password_reset(db, email):
    """Generate a single-use password reset token valid for 30 minutes.
    Returns (ok, token | None, message)."""
    norm_email = (email or "").strip().lower()
    if not norm_email:
        return False, None, "Please enter your email address."
    m = db.execute("SELECT member_id FROM Members WHERE email=?", (norm_email,)).fetchone()
    if m is None:
        return False, None, "If an account with that email exists, a reset link has been generated."
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(minutes=RESET_TOKEN_MINUTES)).isoformat(timespec="seconds")
    with db:
        db.execute(
            "INSERT INTO PasswordResets(token, member_id, expires_at, used) VALUES (?,?,?,0)",
            (token, m["member_id"], expires_at))
    return True, token, f"Password reset link generated (valid for {RESET_TOKEN_MINUTES} minutes)."


def validate_reset_token(db, token):
    """Validate a password reset token. Returns (reset_row | None, error_message | None)."""
    if not token:
        return None, "Invalid password reset link."
    row = db.execute(
        """SELECT pr.token, pr.member_id, pr.expires_at, pr.used, m.email, m.name
           FROM PasswordResets pr JOIN Members m USING(member_id)
           WHERE pr.token=?""", (token,)).fetchone()
    if row is None:
        return None, "Invalid password reset link."
    if row["used"]:
        return None, "This password reset link has already been used."
    try:
        exp = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None, "Invalid password reset link."
    if exp <= datetime.now():
        return None, "This password reset link has expired. Please request a new one."
    return row, None


def reset_password_with_token(db, token, new_password):
    """Reset a member's password using a valid token, marking it used and clearing lockout."""
    row, err = validate_reset_token(db, token)
    if err:
        return False, err
    if len(new_password or "") < MIN_PASSWORD:
        return False, f"Password must be at least {MIN_PASSWORD} characters."
    pw_hash = generate_password_hash(new_password)
    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        # Re-check token inside write transaction to prevent double use
        cur = db.execute("SELECT used, expires_at FROM PasswordResets WHERE token=?", (token,)).fetchone()
        if cur is None or cur["used"] or datetime.fromisoformat(cur["expires_at"]) <= datetime.now():
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "This password reset link is no longer valid."
        db.execute(
            "UPDATE Members SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE member_id=?",
            (pw_hash, row["member_id"]))
        db.execute("UPDATE PasswordResets SET used=1 WHERE token=?", (token,))
        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    return True, "Password updated successfully. You can log in now."


# =====================================================================
# DONE BY: Zhanibek + Sprint 3 - create / change / delete trainings & member RBAC (admin)
# =====================================================================
def _check_session_fields(title, trainer, date, time, capacity, price=DEFAULT_SESSION_PRICE):
    """Validate training fields. Returns ((title, trainer, date, time, cap, price), None) or (None, error)."""
    title, trainer = (title or "").strip(), (trainer or "").strip()
    if not title or not trainer:
        return None, "Title and trainer are required."
    try:
        date = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
        time = datetime.strptime(time, "%H:%M").strftime("%H:%M")
    except (ValueError, TypeError):
        return None, "Date must be YYYY-MM-DD and time must be HH:MM."
    try:
        cap = int(capacity)
    except (ValueError, TypeError):
        return None, "Capacity must be a whole number."
    if cap <= 0:
        return None, "Capacity must be greater than 0."
    try:
        price_val = float(price)
    except (ValueError, TypeError):
        return None, "Price must be a valid number."
    if math.isnan(price_val) or math.isinf(price_val) or price_val < 0:
        return None, "Price must be 0 or greater."
    price_val = round(price_val, 2)
    return (title, trainer, date, time, cap, price_val), None


def create_session(db, title, trainer, date, time, capacity, price=DEFAULT_SESSION_PRICE):
    fields, err = _check_session_fields(title, trainer, date, time, capacity, price)
    if err:
        return False, err
    with db:
        db.execute("INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) "
                   "VALUES (?,?,?,?,?,?)", fields)
    return True, f"Training created: {fields[0]} (${fields[5]:.2f})."


def update_session(db, session_id, title, trainer, date, time, capacity, price=None):
    existing = db.execute("SELECT price FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
    if existing is None:
        return False, "Training not found."
    effective_price = existing["price"] if price in (None, "") else price
    fields, err = _check_session_fields(title, trainer, date, time, capacity, effective_price)
    if err:
        return False, err
    taken = db.execute("SELECT COUNT(*) FROM Bookings WHERE session_id=? AND status='confirmed'",
                       (session_id,)).fetchone()[0]
    if fields[4] < taken:
        return False, f"Capacity cannot be lower than the {taken} bookings already confirmed."
    with db:
        db.execute(
            "UPDATE Sessions SET title=?, trainer=?, session_date=?, start_time=?, capacity=?, price=? "
            "WHERE session_id=?", fields + (session_id,))
    return True, f"Training updated: {fields[0]}."


def delete_session(db, session_id):
    row = db.execute("SELECT title FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        return False, "Training not found."
    with db:  # one transaction: bookings of this training + the training itself
        db.execute("DELETE FROM Bookings WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM Sessions WHERE session_id=?", (session_id,))
    return True, f"Training deleted: {row['title']} (and its bookings)."


def get_all_members(db):
    rows = db.execute(
        """SELECT member_id, name, email, membership_status, role, balance,
                  failed_attempts, locked_until
           FROM Members ORDER BY member_id""").fetchall()
    now = datetime.now()
    result = []
    for r in rows:
        d = dict(r)
        is_locked = False
        if d["locked_until"]:
            try:
                is_locked = datetime.fromisoformat(d["locked_until"]) > now
            except ValueError:
                is_locked = False
        d["is_locked"] = is_locked
        result.append(d)
    return result


def toggle_member_status(db, target_member_id):
    m = db.execute("SELECT member_id, name, membership_status FROM Members WHERE member_id=?",
                   (target_member_id,)).fetchone()
    if m is None:
        return False, "Member not found."
    new_status = "expired" if m["membership_status"] == "active" else "active"
    with db:
        db.execute("UPDATE Members SET membership_status=? WHERE member_id=?",
                   (new_status, target_member_id))
    return True, f"Updated {m['name']}'s membership status to {new_status}."


def toggle_member_role(db, actor_member_id, target_member_id):
    m = db.execute("SELECT member_id, name, role FROM Members WHERE member_id=?",
                   (target_member_id,)).fetchone()
    if m is None:
        return False, "Member not found."
    new_role = "member" if m["role"] == "admin" else "admin"
    if m["role"] == "admin" and new_role == "member":
        admin_count = db.execute("SELECT COUNT(*) FROM Members WHERE role='admin'").fetchone()[0]
        if admin_count <= 1:
            if actor_member_id == target_member_id:
                return False, "You cannot demote yourself because you are the last admin."
            return False, "Cannot demote the last admin."
    with db:
        db.execute("UPDATE Members SET role=? WHERE member_id=?", (new_role, target_member_id))
    return True, f"Updated {m['name']}'s role to {new_role}."


# =====================================================================
# PAYMENTS & BALANCE HELPERS (Sprint 3)
# =====================================================================
def topup_balance(db, member_id, amount):
    """Add funds to a member's balance. Returns (ok, message)."""
    try:
        val = float(amount)
    except (ValueError, TypeError):
        return False, "Top-up amount must be a valid number."
    if math.isnan(val) or math.isinf(val) or val <= 0:
        return False, "Top-up amount must be greater than 0."
    if val > 10000:
        return False, "Top-up amount cannot exceed $10,000 at once."
    val = round(val, 2)
    m = db.execute("SELECT balance FROM Members WHERE member_id=?", (member_id,)).fetchone()
    if m is None:
        return False, "Member not found."
    with db:
        db.execute("UPDATE Members SET balance = ROUND(balance + ?, 2) WHERE member_id=?",
                   (val, member_id))
    new_balance = db.execute("SELECT balance FROM Members WHERE member_id=?", (member_id,)).fetchone()[0]
    return True, f"Balance topped up by ${val:.2f}. New balance: ${new_balance:.2f}."


def get_member_payments(db, member_id):
    rows = db.execute("""
        SELECT p.payment_id, p.member_id, p.session_id, p.amount, p.status,
               p.created_at, p.refunded_at,
               COALESCE(s.title, 'Deleted session #' || p.session_id) AS title,
               s.session_date, s.start_time
        FROM Payments p
        LEFT JOIN Sessions s ON p.session_id = s.session_id
        WHERE p.member_id=?
        ORDER BY p.payment_id DESC""", (member_id,)).fetchall()
    return [dict(r) for r in rows]


def get_payment_overview(db):
    summary = db.execute("""
        SELECT
            COUNT(CASE WHEN status='paid' THEN 1 END) AS paid_count,
            COUNT(CASE WHEN status='refunded' THEN 1 END) AS refunded_count,
            COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END), 0.0) AS net_revenue,
            COALESCE(SUM(CASE WHEN status='refunded' THEN amount ELSE 0 END), 0.0) AS total_refunded,
            COALESCE(SUM(amount), 0.0) AS gross_volume
        FROM Payments""").fetchone()
    recent = db.execute("""
        SELECT p.payment_id, p.amount, p.status, p.created_at, p.refunded_at,
               m.name AS member_name, m.email AS member_email,
               COALESCE(s.title, 'Deleted session #' || p.session_id) AS session_title
        FROM Payments p
        LEFT JOIN Members m ON p.member_id = m.member_id
        LEFT JOIN Sessions s ON p.session_id = s.session_id
        ORDER BY p.payment_id DESC
        LIMIT 50""").fetchall()
    return dict(summary), [dict(r) for r in recent]


# =====================================================================
# BASE BOOKING LOGIC + Sprint 3 Atomic Payment & Transaction Handling
# DONE BY: Adil - rejection messages rewritten
# =====================================================================
def book_session(db, member_id, session_id):
    """Implements the booking flow diagram with atomic payment deduction.
    Returns (ok: bool, message: str)."""
    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        member = db.execute("SELECT * FROM Members WHERE member_id=?", (member_id,)).fetchone()
        sess = db.execute("SELECT * FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
        if member is None or sess is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Member or session not found."

        # 1. Active membership?
        if member["membership_status"] != "active":
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Your membership has expired. Please renew it to book sessions."

        # 2. Session in future?
        if _starts(sess) <= datetime.now():
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "This session has already started or finished."

        # 3. Already booked?
        dup = db.execute(
            "SELECT 1 FROM Bookings WHERE member_id=? AND session_id=? AND status='confirmed'",
            (member_id, session_id)).fetchone()
        if dup:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "You are already registered for this session."

        # 4. Free places?
        taken = db.execute(
            "SELECT COUNT(*) FROM Bookings WHERE session_id=? AND status='confirmed'",
            (session_id,)).fetchone()[0]
        if taken >= sess["capacity"]:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Sorry, this session is full."

        # 5. Sufficient balance?
        price = round(float(sess["price"]), 2)
        balance = round(float(member["balance"]), 2)
        if balance < price:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, (f"Insufficient balance (${balance:.2f} available, "
                           f"${price:.2f} required). Please top up your balance.")

        # Atomically deduct balance, create booking, and log payment
        now = datetime.now().isoformat(timespec="seconds")
        db.execute("UPDATE Members SET balance = ROUND(balance - ?, 2) WHERE member_id=?",
                   (price, member_id))
        db.execute(
            "INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (?,?,?,?)",
            (member_id, session_id, "confirmed", now))
        db.execute(
            "INSERT INTO Payments(member_id,session_id,amount,status,created_at) VALUES (?,?,?,?,?)",
            (member_id, session_id, price, "paid", now))
        if started_tx:
            db.commit()
    except sqlite3.IntegrityError:
        if started_tx and db.in_transaction:
            db.rollback()
        return False, "Booking could not be saved. Please try again."
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    return True, (f"Booked: {sess['title']} on {sess['session_date']} "
                  f"at {sess['start_time']} (${price:.2f} paid).")


# =====================================================================
# DONE BY: Adil + Sprint 3 Refunds - BOOKING CANCELLATION
# The Bookings row is DELETED so UNIQUE(member_id, session_id) allows rebooking,
# while the Payments row is updated to status='refunded' for audit history.
# =====================================================================
def cancel_booking(db, member_id, booking_id, is_admin=False):
    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        b = db.execute("""SELECT b.booking_id, b.member_id, b.session_id,
                                 s.title, s.session_date, s.start_time
                          FROM Bookings b JOIN Sessions s USING(session_id)
                          WHERE b.booking_id=?""", (booking_id,)).fetchone()
        if b is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Booking not found."
        if b["member_id"] != member_id and not is_admin:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "You can only cancel your own bookings."
        if _starts(b) <= datetime.now():
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "You cannot cancel a session that has already started or finished."

        db.execute("DELETE FROM Bookings WHERE booking_id=?", (booking_id,))

        payment = db.execute("""
            SELECT payment_id, amount FROM Payments
            WHERE member_id=? AND session_id=? AND status='paid'
            ORDER BY payment_id DESC LIMIT 1""", (b["member_id"], b["session_id"])).fetchone()
        refund_note = ""
        if payment is not None:
            now = datetime.now().isoformat(timespec="seconds")
            amt = round(float(payment["amount"]), 2)
            db.execute("UPDATE Members SET balance = ROUND(balance + ?, 2) WHERE member_id=?",
                       (amt, b["member_id"]))
            db.execute("UPDATE Payments SET status='refunded', refunded_at=? WHERE payment_id=?",
                       (now, payment["payment_id"]))
            refund_note = f" (${amt:.2f} refunded)"

        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    return True, f"Cancelled: {b['title']} on {b['session_date']} at {b['start_time']}{refund_note}."


# =====================================================================
# DONE BY: Torezhan + Sprint 3 Occupancy - search, bookings, visual load metrics
# =====================================================================
def _load_meta(taken, capacity):
    pct = min(100, round((taken / capacity) * 100)) if capacity > 0 else 0
    if taken >= capacity and capacity > 0:
        return pct, "high", "Full"
    if pct >= 90:
        return pct, "high", "High"
    if pct >= 60:
        return pct, "medium", "Medium"
    return pct, "low", "Low"


def search_sessions(db, query="", only_upcoming=False):
    like = f"%{(query or '').strip()}%"
    rows = db.execute("""
        SELECT s.*, (SELECT COUNT(*) FROM Bookings b
                     WHERE b.session_id=s.session_id AND b.status='confirmed') AS taken
        FROM Sessions s WHERE s.title LIKE ? OR s.trainer LIKE ?
        ORDER BY session_date, start_time""", (like, like)).fetchall()
    now, result = datetime.now(), []
    for r in rows:
        d = dict(r)
        d["is_past"] = _starts(r) <= now
        pct, level, label = _load_meta(d["taken"], d["capacity"])
        d["occupancy_pct"] = pct
        d["load_level"] = level
        d["load_label"] = label
        if only_upcoming and d["is_past"]:
            continue
        result.append(d)
    return result


def get_gym_occupancy(db):
    """Compute overall upcoming occupancy and per-date load breakdown for visual charts."""
    upcoming = search_sessions(db, "", only_upcoming=True)
    total_booked = sum(s["taken"] for s in upcoming)
    total_capacity = sum(s["capacity"] for s in upcoming)
    overall_pct, overall_level, overall_label = _load_meta(total_booked, total_capacity)

    by_date_map = {}
    for s in upcoming:
        slot = by_date_map.setdefault(
            s["session_date"], {"date": s["session_date"], "booked": 0, "capacity": 0, "sessions": 0})
        slot["booked"] += s["taken"]
        slot["capacity"] += s["capacity"]
        slot["sessions"] += 1

    by_date = []
    for date_key in sorted(by_date_map.keys()):
        item = by_date_map[date_key]
        pct, level, label = _load_meta(item["booked"], item["capacity"])
        item["pct"] = pct
        item["level"] = level
        item["label"] = label
        by_date.append(item)

    return {
        "total_booked": total_booked,
        "total_capacity": total_capacity,
        "overall_pct": overall_pct,
        "overall_level": overall_level,
        "overall_label": overall_label,
        "upcoming_sessions_count": len(upcoming),
        "by_date": by_date,
    }


def get_member_bookings(db, member_id):
    rows = db.execute("""
        SELECT b.booking_id, b.session_id, b.booked_at, s.title, s.trainer,
               s.session_date, s.start_time, s.price
        FROM Bookings b JOIN Sessions s USING(session_id)
        WHERE b.member_id=? AND b.status='confirmed'
        ORDER BY s.session_date, s.start_time""", (member_id,)).fetchall()
    now, result = datetime.now(), []
    for r in rows:
        d = dict(r)
        d["is_past"] = _starts(r) <= now
        result.append(d)
    return result


# =====================================================================
# TESTS RUN IN THE INTERFACE (page /tests). Every test uses its own fresh
# in-memory database, so the real gym.db is never touched.
# Demo data: members 1 Aldair(admin) 2 Iskander 3 Adil 4 Zhanibek 5 Expired;
#            sessions 1 Yoga(full, $15) 2 Boxing($20) 3 Fitness($12) 4 Yoga(past, $15)
# =====================================================================
G_ALDAIR = "Aldair - 20 test functions"
G_ADIL = "Adil - cancellation case"
G_TOREZHAN = "Torezhan - 10 user cases (test as user)"
G_SPRINT3 = "Sprint 3 - Payments, RBAC, Occupancy & Security (19 cases)"
TESTS = []


def case(group):
    def deco(f):
        TESTS.append((group, f))
        return f
    return deco


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    seed_db(db)
    return db


def taken_of(db, session_id):
    return db.execute("SELECT COUNT(*) FROM Bookings WHERE session_id=? AND status='confirmed'",
                      (session_id,)).fetchone()[0]


def booking_id_of(db, member_id, session_id):
    return db.execute("SELECT booking_id FROM Bookings WHERE member_id=? AND session_id=?",
                      (member_id, session_id)).fetchone()[0]


def balance_of(db, member_id):
    return db.execute("SELECT balance FROM Members WHERE member_id=?",
                      (member_id,)).fetchone()[0]


# ---------- DONE BY: Aldair - 20 test functions ----------
@case(G_ALDAIR)
def booking_success():
    """Active member books a future session with free places"""
    db = fresh_db()
    ok, msg = book_session(db, 3, 2)
    assert ok, msg
    assert taken_of(db, 2) == 1


@case(G_ALDAIR)
def booking_inactive_membership():
    """Expired membership is rejected"""
    ok, msg = book_session(fresh_db(), 5, 3)
    assert not ok and "expired" in msg.lower(), msg


@case(G_ALDAIR)
def booking_past_session():
    """Session in the past is rejected"""
    ok, msg = book_session(fresh_db(), 3, 4)
    assert not ok and "already started" in msg.lower(), msg


@case(G_ALDAIR)
def booking_duplicate():
    """Second booking of the same session is rejected"""
    db = fresh_db()
    assert book_session(db, 3, 2)[0]
    ok, msg = book_session(db, 3, 2)
    assert not ok and "already registered" in msg.lower(), msg


@case(G_ALDAIR)
def booking_full_session():
    """Full session (Yoga) is rejected"""
    ok, msg = book_session(fresh_db(), 3, 1)
    assert not ok and "full" in msg.lower(), msg


@case(G_ALDAIR)
def booking_unknown_member():
    """Unknown member id is rejected"""
    assert not book_session(fresh_db(), 999, 1)[0]


@case(G_ALDAIR)
def booking_unknown_session():
    """Unknown session id is rejected"""
    assert not book_session(fresh_db(), 3, 999)[0]


@case(G_ALDAIR)
def database_unique_constraint():
    """UNIQUE(member_id, session_id) blocks a duplicate even without the code check"""
    db = fresh_db()
    sql = "INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (3,2,'confirmed','x')"
    db.execute(sql)
    try:
        db.execute(sql)
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate row was accepted by the database")


@case(G_ALDAIR)
def places_counter_increases():
    """Booked places counter grows by 1 after a booking"""
    db = fresh_db()
    before = taken_of(db, 2)
    book_session(db, 3, 2)
    assert taken_of(db, 2) == before + 1


@case(G_ALDAIR)
def login_correct_password():
    """Correct email and password log the member in"""
    m = authenticate(fresh_db(), "adil@kbtu.kz", "1234")
    assert m is not None and m["name"] == "Adil"


@case(G_ALDAIR)
def login_wrong_password():
    """Wrong password is rejected"""
    assert authenticate(fresh_db(), "adil@kbtu.kz", "wrong") is None


@case(G_ALDAIR)
def login_unknown_email():
    """Unknown email is rejected"""
    assert authenticate(fresh_db(), "nobody@kbtu.kz", "1234") is None


@case(G_ALDAIR)
def register_success():
    """New account can be created and used to log in"""
    db = fresh_db()
    ok, msg = register_member(db, "Test User", "new@kbtu.kz", "abcd")
    assert ok, msg
    assert authenticate(db, "new@kbtu.kz", "abcd") is not None


@case(G_ALDAIR)
def register_duplicate_email():
    """Registering an existing email is rejected"""
    assert not register_member(fresh_db(), "Copy", "adil@kbtu.kz", "abcd")[0]


@case(G_ALDAIR)
def register_short_password():
    """Too short password is rejected"""
    assert not register_member(fresh_db(), "Test", "short@kbtu.kz", "123")[0]


@case(G_ALDAIR)
def register_bad_email():
    """Email without a proper format is rejected"""
    assert not register_member(fresh_db(), "Test", "not-an-email", "abcd")[0]


@case(G_ALDAIR)
def training_create():
    """Admin function: a valid training is created"""
    db = fresh_db()
    ok, msg = create_session(db, "Pilates", "Aruzhan", day(5), "11:00", 10)
    assert ok, msg
    assert db.execute("SELECT COUNT(*) FROM Sessions").fetchone()[0] == 5


@case(G_ALDAIR)
def training_bad_capacity():
    """Admin function: capacity 0 or text is rejected"""
    db = fresh_db()
    assert not create_session(db, "Pilates", "Aruzhan", day(5), "11:00", 0)[0]
    assert not create_session(db, "Pilates", "Aruzhan", day(5), "11:00", "abc")[0]


@case(G_ALDAIR)
def training_update():
    """Admin function: a training can be changed"""
    db = fresh_db()
    ok, msg = update_session(db, 2, "Boxing Pro", "Daniyar", day(2), "19:00", 5)
    assert ok, msg
    row = db.execute("SELECT * FROM Sessions WHERE session_id=2").fetchone()
    assert row["title"] == "Boxing Pro" and row["start_time"] == "19:00" and row["capacity"] == 5


@case(G_ALDAIR)
def training_delete_removes_bookings():
    """Admin function: deleting a training also deletes its bookings"""
    db = fresh_db()
    ok, msg = delete_session(db, 1)
    assert ok, msg
    assert db.execute("SELECT COUNT(*) FROM Sessions WHERE session_id=1").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM Bookings WHERE session_id=1").fetchone()[0] == 0


# ---------- DONE BY: Adil - cancellation case ----------
@case(G_ADIL)
def cancel_own_booking():
    """Member cancels own booking: it disappears and free places grow by 1"""
    db = fresh_db()
    book_session(db, 3, 2)
    assert taken_of(db, 2) == 1
    ok, msg = cancel_booking(db, 3, booking_id_of(db, 3, 2))
    assert ok, msg
    assert taken_of(db, 2) == 0


@case(G_ADIL)
def cancel_someone_elses_booking():
    """Member cannot cancel another member's booking"""
    db = fresh_db()
    ok, msg = cancel_booking(db, 3, 1)  # booking 1 belongs to member 1
    assert not ok and "own" in msg.lower(), msg
    assert taken_of(db, 1) == 2


@case(G_ADIL)
def cancel_past_session():
    """A booking for a session that already started cannot be cancelled"""
    db = fresh_db()
    db.execute("INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (3,4,'confirmed','x')")
    ok, msg = cancel_booking(db, 3, booking_id_of(db, 3, 4))
    assert not ok and "already" in msg.lower(), msg


@case(G_ADIL)
def rebook_after_cancel():
    """After cancelling, the same session can be booked again"""
    db = fresh_db()
    book_session(db, 3, 2)
    cancel_booking(db, 3, booking_id_of(db, 3, 2))
    ok, msg = book_session(db, 3, 2)
    assert ok, msg


# ---------- DONE BY: Torezhan - 10 user cases ----------
@case(G_TOREZHAN)
def user_registers_and_logs_in():
    """New user registers, then logs in with the same data"""
    db = fresh_db()
    assert register_member(db, "Nurlan", "nurlan@kbtu.kz", "pass1")[0]
    assert authenticate(db, "nurlan@kbtu.kz", "pass1") is not None


@case(G_TOREZHAN)
def new_user_books_session():
    """Newly registered user books Fitness"""
    db = fresh_db()
    register_member(db, "Nurlan", "nurlan@kbtu.kz", "pass1")
    mid = authenticate(db, "nurlan@kbtu.kz", "pass1")["member_id"]
    ok, msg = book_session(db, mid, 3)
    assert ok, msg


@case(G_TOREZHAN)
def my_bookings_shows_booking():
    """'My bookings' lists the session the user booked"""
    db = fresh_db()
    book_session(db, 3, 2)
    rows = get_member_bookings(db, 3)
    assert len(rows) == 1 and rows[0]["title"] == "Boxing"


@case(G_TOREZHAN)
def my_bookings_empty_for_new_user():
    """A new user has an empty 'My bookings' list"""
    db = fresh_db()
    register_member(db, "Nurlan", "nurlan@kbtu.kz", "pass1")
    mid = authenticate(db, "nurlan@kbtu.kz", "pass1")["member_id"]
    assert get_member_bookings(db, mid) == []


@case(G_TOREZHAN)
def search_by_title():
    """Search 'box' finds only Boxing"""
    titles = [s["title"] for s in search_sessions(fresh_db(), "box")]
    assert titles == ["Boxing"], titles


@case(G_TOREZHAN)
def search_by_trainer():
    """Search by trainer name 'aigerim' finds both Yoga sessions"""
    assert len(search_sessions(fresh_db(), "aigerim")) == 2


@case(G_TOREZHAN)
def search_no_results():
    """Search with no match returns an empty list"""
    assert search_sessions(fresh_db(), "zzz") == []


@case(G_TOREZHAN)
def upcoming_filter_hides_past():
    """'Upcoming only' hides past sessions, normal view shows them"""
    db = fresh_db()
    assert all("(past)" not in s["title"] for s in search_sessions(db, "", True))
    assert any("(past)" in s["title"] for s in search_sessions(db, "", False))


@case(G_TOREZHAN)
def places_left_counter():
    """Places left for Boxing drops from 3 to 2 after one booking"""
    db = fresh_db()
    s = search_sessions(db, "Boxing")[0]
    assert s["capacity"] - s["taken"] == 3
    book_session(db, 3, 2)
    s = search_sessions(db, "Boxing")[0]
    assert s["capacity"] - s["taken"] == 2


@case(G_TOREZHAN)
def expired_user_gets_clear_message():
    """Expired user sees a clear message and nothing is booked"""
    db = fresh_db()
    ok, msg = book_session(db, 5, 3)
    assert not ok and "renew" in msg.lower(), msg
    assert taken_of(db, 3) == 0 and get_member_bookings(db, 5) == []


# ---------- SPRINT 3: Payments, Refunds, RBAC, Security, Occupancy (19 cases) ----------
@case(G_SPRINT3)
def payment_deducts_balance_and_logs_paid():
    """Booking Boxing ($20) deducts $20 from balance and creates a 'paid' Payment"""
    db = fresh_db()
    before = balance_of(db, 3)
    ok, msg = book_session(db, 3, 2)
    assert ok, msg
    assert balance_of(db, 3) == round(before - 20.0, 2)
    payments = get_member_payments(db, 3)
    assert len(payments) == 1 and payments[0]["status"] == "paid" and payments[0]["amount"] == 20.0


@case(G_SPRINT3)
def payment_insufficient_balance_rejected():
    """Booking is rejected when member balance is lower than session price"""
    db = fresh_db()
    with db:
        db.execute("UPDATE Members SET balance=5.0 WHERE member_id=3")
    ok, msg = book_session(db, 3, 2)  # Boxing costs $20
    assert not ok and "insufficient balance" in msg.lower(), msg
    assert balance_of(db, 3) == 5.0 and taken_of(db, 2) == 0


@case(G_SPRINT3)
def payment_topup_increases_balance():
    """Top-up increases member balance accurately"""
    db = fresh_db()
    ok, msg = topup_balance(db, 3, 50.5)
    assert ok, msg
    assert balance_of(db, 3) == 150.5


@case(G_SPRINT3)
def payment_topup_invalid_amount_rejected():
    """Zero, negative, or non-numeric top-up is rejected"""
    db = fresh_db()
    assert not topup_balance(db, 3, 0)[0]
    assert not topup_balance(db, 3, -25)[0]
    assert not topup_balance(db, 3, "abc")[0]
    assert balance_of(db, 3) == 100.0


@case(G_SPRINT3)
def payment_zero_price_session_succeeds():
    """Free ($0) session can be booked even with $0 balance and logs a $0 payment"""
    db = fresh_db()
    ok, msg = create_session(db, "Free Stretch", "Aigerim", day(4), "08:00", 5, price=0.0)
    assert ok, msg
    sid = db.execute("SELECT session_id FROM Sessions WHERE title='Free Stretch'").fetchone()[0]
    with db:
        db.execute("UPDATE Members SET balance=0.0 WHERE member_id=3")
    ok, msg = book_session(db, 3, sid)
    assert ok, msg
    assert balance_of(db, 3) == 0.0
    assert get_member_payments(db, 3)[0]["amount"] == 0.0


@case(G_SPRINT3)
def refund_on_member_cancel_restores_balance():
    """Cancelling own booking refunds the paid price back to member balance"""
    db = fresh_db()
    book_session(db, 3, 2)  # -$20 -> $80
    assert balance_of(db, 3) == 80.0
    ok, msg = cancel_booking(db, 3, booking_id_of(db, 3, 2))
    assert ok and "refunded" in msg.lower(), msg
    assert balance_of(db, 3) == 100.0


@case(G_SPRINT3)
def refund_marks_payment_status_refunded():
    """Cancellation updates the Payment record to 'refunded' with a refunded_at timestamp"""
    db = fresh_db()
    book_session(db, 3, 2)
    cancel_booking(db, 3, booking_id_of(db, 3, 2))
    payments = get_member_payments(db, 3)
    assert len(payments) == 1
    assert payments[0]["status"] == "refunded" and payments[0]["refunded_at"] is not None


@case(G_SPRINT3)
def rebook_after_refund_charges_again():
    """Rebooking a session after refund deducts balance again and creates a second Payment"""
    db = fresh_db()
    book_session(db, 3, 2)
    cancel_booking(db, 3, booking_id_of(db, 3, 2))
    ok, msg = book_session(db, 3, 2)
    assert ok, msg
    assert balance_of(db, 3) == 80.0
    payments = get_member_payments(db, 3)
    assert len(payments) == 2
    assert [p["status"] for p in payments] == ["paid", "refunded"]


@case(G_SPRINT3)
def admin_cancel_refunds_booking_owner_not_admin():
    """When admin cancels another member's booking, refund goes to the booking owner"""
    db = fresh_db()
    admin_before = balance_of(db, 1)
    owner_before = balance_of(db, 2)
    bid = booking_id_of(db, 2, 1)  # Iskander's seeded $15 Yoga booking
    ok, msg = cancel_booking(db, 1, bid, is_admin=True)
    assert ok, msg
    assert balance_of(db, 1) == admin_before
    assert balance_of(db, 2) == round(owner_before + 15.0, 2)


@case(G_SPRINT3)
def rbac_expired_member_rejected_without_deducting_balance():
    """Expired member booking rejection does not deduct balance or create a payment"""
    db = fresh_db()
    before = balance_of(db, 5)
    ok, _msg = book_session(db, 5, 3)
    assert not ok
    assert balance_of(db, 5) == before and get_member_payments(db, 5) == []


@case(G_SPRINT3)
def rbac_toggle_member_status_blocks_and_unblocks_booking():
    """Toggling membership_status to expired blocks booking; toggling back restores it"""
    db = fresh_db()
    ok, _ = toggle_member_status(db, 3)
    assert ok
    assert not book_session(db, 3, 2)[0]
    ok, _ = toggle_member_status(db, 3)
    assert ok
    assert book_session(db, 3, 2)[0]


@case(G_SPRINT3)
def rbac_cannot_demote_last_admin():
    """Admin cannot demote themselves when they are the sole admin, but can after promoting another"""
    db = fresh_db()
    ok, msg = toggle_member_role(db, actor_member_id=1, target_member_id=1)
    assert not ok and "last admin" in msg.lower(), msg
    assert toggle_member_role(db, actor_member_id=1, target_member_id=3)[0]  # promote Adil
    assert toggle_member_role(db, actor_member_id=1, target_member_id=1)[0]  # now allowed


@case(G_SPRINT3)
def rbac_non_admin_cannot_cancel_or_refund_others():
    """Non-admin cannot cancel another member's booking or trigger a refund"""
    db = fresh_db()
    owner_before = balance_of(db, 2)
    bid = booking_id_of(db, 2, 1)
    ok, _msg = cancel_booking(db, 3, bid, is_admin=False)
    assert not ok
    assert balance_of(db, 2) == owner_before and taken_of(db, 1) == 2


@case(G_SPRINT3)
def security_lockout_after_five_failed_logins():
    """Account locks for 15 minutes after 5 failed logins, blocking even the correct password"""
    db = fresh_db()
    for _ in range(4):
        m, err = authenticate_with_status(db, "adil@kbtu.kz", "wrong")
        assert m is None and "wrong" in err.lower()
    m, err = authenticate_with_status(db, "adil@kbtu.kz", "wrong")
    assert m is None and "locked" in err.lower(), err
    # Even the correct password must be rejected while locked
    m, err = authenticate_with_status(db, "adil@kbtu.kz", "1234")
    assert m is None and "locked" in err.lower(), err


@case(G_SPRINT3)
def security_successful_login_resets_failed_attempts():
    """A successful login resets failed_attempts back to 0"""
    db = fresh_db()
    for _ in range(3):
        authenticate_with_status(db, "adil@kbtu.kz", "wrong")
    assert db.execute("SELECT failed_attempts FROM Members WHERE member_id=3").fetchone()[0] == 3
    m, err = authenticate_with_status(db, "adil@kbtu.kz", "1234")
    assert m is not None and err is None
    assert db.execute("SELECT failed_attempts FROM Members WHERE member_id=3").fetchone()[0] == 0


@case(G_SPRINT3)
def security_password_reset_token_and_login():
    """Valid reset token updates password hash and allows login with the new password"""
    db = fresh_db()
    ok, token, msg = create_password_reset(db, "adil@kbtu.kz")
    assert ok and token, msg
    ok, msg = reset_password_with_token(db, token, "newpass99")
    assert ok, msg
    assert authenticate(db, "adil@kbtu.kz", "1234") is None
    assert authenticate(db, "adil@kbtu.kz", "newpass99") is not None


@case(G_SPRINT3)
def security_password_reset_expired_invalid_and_reused_rejected():
    """Unknown, expired, and already-used reset tokens are rejected"""
    db = fresh_db()
    assert not reset_password_with_token(db, "nonexistent-token", "newpass")[0]
    ok, token, _ = create_password_reset(db, "adil@kbtu.kz")
    assert ok
    assert reset_password_with_token(db, token, "newpass1")[0]
    ok_reuse, msg_reuse = reset_password_with_token(db, token, "newpass2")
    assert not ok_reuse and "used" in msg_reuse.lower(), msg_reuse

    # Expired token
    ok2, token2, _ = create_password_reset(db, "adil@kbtu.kz")
    assert ok2
    past_iso = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    with db:
        db.execute("UPDATE PasswordResets SET expires_at=? WHERE token=?", (past_iso, token2))
    ok_exp, msg_exp = reset_password_with_token(db, token2, "newpass3")
    assert not ok_exp and "expired" in msg_exp.lower(), msg_exp


@case(G_SPRINT3)
def security_password_reset_clears_lockout():
    """Completing a password reset clears login lockout and failed_attempts"""
    db = fresh_db()
    for _ in range(MAX_LOGIN_ATTEMPTS):
        authenticate_with_status(db, "adil@kbtu.kz", "wrong")
    assert authenticate(db, "adil@kbtu.kz", "1234") is None
    ok, token, _ = create_password_reset(db, "adil@kbtu.kz")
    assert ok and reset_password_with_token(db, token, "unlocked1")[0]
    m, err = authenticate_with_status(db, "adil@kbtu.kz", "unlocked1")
    assert m is not None and err is None


@case(G_SPRINT3)
def occupancy_metrics_calculation():
    """Gym occupancy aggregates only upcoming sessions and classifies load levels"""
    db = fresh_db()
    # Seeded upcoming: Yoga (2/2), Boxing (0/3), Fitness (0/4) -> total 2/9 = 22% (low)
    occ = get_gym_occupancy(db)
    assert occ["total_booked"] == 2 and occ["total_capacity"] == 9
    assert occ["overall_pct"] == 22 and occ["overall_level"] == "low"
    assert len(occ["by_date"]) == 3
    assert occ["by_date"][0]["pct"] == 100 and occ["by_date"][0]["level"] == "high"
    # Book Boxing twice -> 2/3 = 67% (medium)
    book_session(db, 3, 2)
    book_session(db, 4, 2)
    occ2 = get_gym_occupancy(db)
    assert occ2["total_booked"] == 4 and occ2["by_date"][1]["level"] == "medium"


def run_all_tests():
    groups = {}
    for group, f in TESTS:
        g_ = groups.setdefault(group, {"title": group, "results": [], "passed": 0})
        try:
            f()
            ok, err = True, ""
        except AssertionError as e:
            ok, err = False, f"FAIL: {e}"
        except Exception as e:  # unexpected crash
            ok, err = False, f"ERROR: {type(e).__name__}: {e}"
        g_["results"].append({"name": f.__name__, "doc": f.__doc__, "ok": ok, "err": err})
        g_["passed"] += ok
    return list(groups.values())


# =====================================================================
# PAGES (templates)
# =====================================================================
STYLE = """
<style>
 body{font-family:system-ui,-apple-system,sans-serif;max-width:1020px;margin:24px auto;padding:0 14px;color:#1f2937;background:#fafafa}
 h1,h2,h3{color:#111827}
 table{border-collapse:collapse;width:100%;margin:12px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.05)}
 th,td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left;vertical-align:middle}
 th{background:#eef3fb;font-weight:600}
 .ok{background:#e6f6ea;color:#14532d;padding:10px 12px;border-radius:6px;margin:8px 0;border:1px solid #bbf7d0}
 .err{background:#fdeaea;color:#7f1d1d;padding:10px 12px;border-radius:6px;margin:8px 0;border:1px solid #fecaca}
 .notice{background:#eff6ff;color:#1e3a8a;padding:12px 14px;border-radius:8px;margin:12px 0;border:1px solid #bfdbfe;word-break:break-all}
 select,button,input{padding:6px 10px;font-size:14px;border-radius:6px}
 input,select{border:1px solid #d1d5db;background:#fff}
 .bar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:8px 0}
 button{background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:500}
 button:hover{background:#1d4ed8}
 button.danger{background:#dc2626}
 button.danger:hover{background:#b91c1c}
 button.secondary{background:#4b5563}
 button.secondary:hover{background:#374151}
 .full td{background:#fef2f2}
 .past td{color:#9ca3af}
 .low-spots{color:#dc2626;font-weight:bold}
 .badge{display:inline-block;background:#dcfce7;color:#166534;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:600;margin-left:4px}
 .badge.grey{background:#e5e7eb;color:#4b5563}
 .badge.amber{background:#fef3c7;color:#92400e}
 .badge.red{background:#fee2e2;color:#991b1b}
 .badge.blue{background:#dbeafe;color:#1e40af}
 td input{width:100%;box-sizing:border-box;padding:4px 6px}
 .pass{color:#15803d;font-weight:bold}
 .fail{color:#b91c1c;font-weight:bold}
 .inline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:14px 0}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .card-title{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;margin-bottom:4px}
 .card-value{font-size:22px;font-weight:700;color:#111827}
 .progress-track{width:100%;height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin-top:6px}
 .progress-track.sm{width:90px;height:7px;display:inline-block;vertical-align:middle;margin:0 6px 0 0}
 .progress-fill{height:100%;border-radius:999px;transition:width .25s ease}
 .fill-low{background:#16a34a}
 .fill-medium{background:#d97706}
 .fill-high{background:#dc2626}
 .occ-grid{display:grid;grid-template-columns:1fr 1.6fr;gap:14px;margin:14px 0}
 @media (max-width:720px){.occ-grid{grid-template-columns:1fr}}
 .chart-wrap{width:100%;overflow-x:auto}
 .wallet-bar{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px;margin:12px 0;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
</style>
"""

HEAD = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} - Gym Booking System</title>
""" + STYLE + "</head><body>"

NAV = """
<h1>Gym Booking System</h1>
<div class="bar">
  <span>{% if user %}Logged in as <b>{{ user.name }}</b>
    <span class="badge {{ 'blue' if user.role == 'admin' else 'grey' }}">{{ user.role }}</span>
    <span class="badge {{ '' if user.membership_status == 'active' else 'red' }}">{{ user.membership_status }}</span>
    &middot; Balance: <b>${{ "%.2f"|format(user.balance) }}</b>
  {% endif %}</span>
  <span>
  {% if user %}
    <a href="{{ url_for('index') }}">Sessions</a> |
    {% if user.role == 'admin' %}<a href="{{ url_for('admin') }}">Admin</a> | {% endif %}
    <a href="{{ url_for('tests') }}">Tests</a> |
    <a href="{{ url_for('logout') }}">Logout</a>
  {% else %}
    <a href="{{ url_for('login') }}">Login</a> | <a href="{{ url_for('register') }}">Register</a>
  {% endif %}
  </span>
</div>
{% for cat, msg in get_flashed_messages(with_categories=true) %}
  <div class="{{ cat }}">{{ msg }}</div>
{% endfor %}
"""

OCCUPANCY_WIDGET = """
<div class="occ-grid">
  <div class="card">
    <div class="card-title">Upcoming Gym Occupancy</div>
    <div class="card-value">
      {{ occupancy.overall_pct }}%
      <span class="badge {{ 'red' if occupancy.overall_level == 'high' else ('amber' if occupancy.overall_level == 'medium' else '') }}">
        {{ occupancy.overall_label }}
      </span>
    </div>
    <div style="font-size:13px;color:#4b5563;margin-top:4px">
      <b>{{ occupancy.total_booked }}</b> / {{ occupancy.total_capacity }} spots booked across
      {{ occupancy.upcoming_sessions_count }} upcoming session(s)
    </div>
    <div class="progress-track">
      <div class="progress-fill fill-{{ occupancy.overall_level }}" style="width:{{ occupancy.overall_pct }}%"></div>
    </div>
    <div style="font-size:12px;color:#6b7280;margin-top:8px">
      Load legend: <span class="badge">Low &lt;60%</span>
      <span class="badge amber">Medium 60-89%</span>
      <span class="badge red">High &ge;90%</span>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Gym Load by Upcoming Date</div>
    {% if occupancy.by_date %}
    {% set chart_w = [occupancy.by_date|length * 88 + 24, 340]|max %}
    <div class="chart-wrap">
      <svg width="{{ chart_w }}" height="126" viewBox="0 0 {{ chart_w }} 126" role="img" aria-label="Gym occupancy chart by date">
        <line x1="8" y1="92" x2="{{ chart_w - 8 }}" y2="92" stroke="#d1d5db" stroke-width="1"/>
        {% for d in occupancy.by_date %}
          {% set x = 16 + loop.index0 * 88 %}
          {% set bar_h = (d.pct * 0.68)|round|int %}
          {% set bar_h = bar_h if bar_h > 4 else (4 if d.booked > 0 else 2) %}
          {% set y = 92 - bar_h %}
          {% set color = '#dc2626' if d.level == 'high' else ('#d97706' if d.level == 'medium' else '#16a34a') %}
          <text x="{{ x + 28 }}" y="{{ y - 5 }}" text-anchor="middle" font-size="11" font-weight="600" fill="#374151">{{ d.pct }}%</text>
          <rect x="{{ x + 8 }}" y="{{ y }}" width="40" height="{{ bar_h }}" rx="4" fill="{{ color }}"/>
          <text x="{{ x + 28 }}" y="106" text-anchor="middle" font-size="10" fill="#374151">{{ d.date[5:] }}</text>
          <text x="{{ x + 28 }}" y="119" text-anchor="middle" font-size="10" fill="#6b7280">{{ d.booked }}/{{ d.capacity }}</text>
        {% endfor %}
      </svg>
    </div>
    {% else %}
    <p style="font-size:13px;color:#6b7280;margin:8px 0 0">No upcoming sessions scheduled.</p>
    {% endif %}
  </div>
</div>
"""

PAGE = HEAD + NAV + OCCUPANCY_WIDGET + """
<div class="wallet-bar">
  <span>Wallet Balance: <b>${{ "%.2f"|format(user.balance) }}</b> &middot; My upcoming bookings: <b>{{ upcoming_count }}</b></span>
  <form method="post" action="{{ url_for('topup') }}" class="inline" style="margin:0">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label for="topup-amt" style="font-size:13px">Top Up ($):</label>
    <input id="topup-amt" type="number" step="0.01" min="1" max="10000" name="amount" value="50" required style="width:90px">
    <button type="submit">Top Up Balance</button>
  </form>
</div>
<!-- Torezhan: search + upcoming filter -->
<form method="get" action="{{ url_for('index') }}" class="bar">
  <span class="inline">
    <input name="q" placeholder="Search title or trainer" value="{{ q }}">
    <label><input type="checkbox" name="upcoming" value="1" {{ 'checked' if upcoming }}> Upcoming only</label>
    <button>Search</button>
  </span>
</form>
<form method="post" action="{{ url_for('book') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <h2>Sessions</h2>
  <table>
    <tr><th>Title</th><th>Trainer</th><th>Date &amp; Time</th><th>Price</th><th>Occupancy &amp; Places</th><th></th></tr>
    {% for s in sessions %}
    <tr class="{{ 'full' if s.taken >= s.capacity }} {{ 'past' if s.is_past }}">
      <td>{{ s.title }}
        {% if s.session_id in booked_ids %}<span class="badge">&#10003; Booked</span>{% endif %}
        {% if s.is_past %}<span class="badge grey">past</span>{% endif %}</td>
      <td>{{ s.trainer }}</td>
      <td>{{ s.session_date }} {{ s.start_time }}</td>
      <td><b>${{ "%.2f"|format(s.price) }}</b></td>
      <td>
        <span class="progress-track sm"><span class="progress-fill fill-{{ s.load_level }}" style="display:block;width:{{ s.occupancy_pct }}%"></span></span>
        <span class="badge {{ 'red' if s.load_level == 'high' else ('amber' if s.load_level == 'medium' else '') }}">{{ s.load_label }} ({{ s.occupancy_pct }}%)</span>
        <div style="font-size:12px;margin-top:2px" class="{{ 'low-spots' if s.capacity - s.taken <= 1 }}">
          {{ s.capacity - s.taken }} left ({{ s.taken }} / {{ s.capacity }})
        </div>
      </td>
      <td><button name="session_id" value="{{ s.session_id }}">Book</button></td>
    </tr>
    {% else %}
    <tr><td colspan="6">No sessions found.</td></tr>
    {% endfor %}
  </table>
</form>
<h2>My bookings</h2>
<table>
  <tr><th>Session</th><th>Trainer</th><th>Date</th><th>Time</th><th>Price</th><th>Booked at</th><th></th></tr>
  {% for b in my_bookings %}
  <tr class="{{ 'past' if b.is_past }}">
    <td>{{ b.title }}</td><td>{{ b.trainer }}</td><td>{{ b.session_date }}</td><td>{{ b.start_time }}</td>
    <td>${{ "%.2f"|format(b.price) }}</td><td>{{ b.booked_at }}</td>
    <td>
      {% if b.is_past %}Past{% else %}
      <!-- Adil: Cancel button -->
      <form method="post" action="{{ url_for('cancel') }}" style="margin:0">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="booking_id" value="{{ b.booking_id }}">
        <button class="danger">Cancel &amp; Refund</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% else %}
  <tr><td colspan="7">You have no bookings yet.</td></tr>
  {% endfor %}
</table>
<h2>My Payments &amp; Refunds</h2>
<table>
  <tr><th>ID</th><th>Session</th><th>Amount</th><th>Status</th><th>Paid at</th><th>Refunded at</th></tr>
  {% for p in my_payments %}
  <tr>
    <td>#{{ p.payment_id }}</td>
    <td>{{ p.title }}</td>
    <td>${{ "%.2f"|format(p.amount) }}</td>
    <td><span class="badge {{ '' if p.status == 'paid' else 'amber' }}">{{ p.status }}</span></td>
    <td>{{ p.created_at }}</td>
    <td>{{ p.refunded_at or '-' }}</td>
  </tr>
  {% else %}
  <tr><td colspan="6">No payment transactions yet.</td></tr>
  {% endfor %}
</table>
</body></html>
"""

LOGIN_PAGE = HEAD + NAV + """
<h2>Login</h2>
<form method="post" action="{{ url_for('login') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <p><label>Email:<br><input type="email" name="email" required></label></p>
  <p><label>Password:<br><input type="password" name="password" required></label></p>
  <button>Login</button>
</form>
<p><a href="{{ url_for('forgot_password') }}">Forgot password?</a> &middot;
   No account? <a href="{{ url_for('register') }}">Register</a></p>
</body></html>
"""

REGISTER_PAGE = HEAD + NAV + """
<h2>Register</h2>
<form method="post" action="{{ url_for('register') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <p><label>Name:<br><input name="name" value="{{ name }}" required></label></p>
  <p><label>Email:<br><input type="email" name="email" value="{{ email }}" required></label></p>
  <p><label>Password (min. """ + str(MIN_PASSWORD) + """ characters):<br><input type="password" name="password" required></label></p>
  <button>Create account</button>
</form>
<p>Already registered? <a href="{{ url_for('login') }}">Login</a></p>
</body></html>
"""

FORGOT_PASSWORD_PAGE = HEAD + NAV + """
<h2>Forgot Password</h2>
<p>Enter your account email to generate a password reset link (valid for """ + str(RESET_TOKEN_MINUTES) + """ minutes).</p>
<form method="post" action="{{ url_for('forgot_password') }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <p><label>Email:<br><input type="email" name="email" value="{{ email }}" required></label></p>
  <button>Generate Reset Link</button>
</form>
{% if reset_url %}
<div class="notice">
  <b>Demo / Dev Notice (No SMTP configured):</b><br>
  Use this reset link directly: <a href="{{ reset_url }}">{{ reset_url }}</a>
</div>
{% endif %}
<p><a href="{{ url_for('login') }}">Back to Login</a></p>
</body></html>
"""

RESET_PASSWORD_PAGE = HEAD + NAV + """
<h2>Reset Password</h2>
{% if valid_token %}
<p>Setting a new password for <b>{{ reset_email }}</b>.</p>
<form method="post" action="{{ url_for('reset_password', token=token) }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <p><label>New Password (min. """ + str(MIN_PASSWORD) + """ characters):<br>
    <input type="password" name="password" required></label></p>
  <button>Update Password</button>
</form>
{% else %}
<p><a href="{{ url_for('forgot_password') }}">Request a new password reset link</a></p>
{% endif %}
<p><a href="{{ url_for('login') }}">Back to Login</a></p>
</body></html>
"""

ADMIN_PAGE = HEAD + NAV + OCCUPANCY_WIDGET + """
<h2>Payments &amp; Revenue Overview</h2>
<div class="cards">
  <div class="card">
    <div class="card-title">Net Revenue (Paid)</div>
    <div class="card-value">${{ "%.2f"|format(pay_summary.net_revenue) }}</div>
    <div style="font-size:12px;color:#6b7280">{{ pay_summary.paid_count }} active payment(s)</div>
  </div>
  <div class="card">
    <div class="card-title">Total Refunded</div>
    <div class="card-value">${{ "%.2f"|format(pay_summary.total_refunded) }}</div>
    <div style="font-size:12px;color:#6b7280">{{ pay_summary.refunded_count }} refunded transaction(s)</div>
  </div>
  <div class="card">
    <div class="card-title">Gross Transaction Volume</div>
    <div class="card-value">${{ "%.2f"|format(pay_summary.gross_volume) }}</div>
    <div style="font-size:12px;color:#6b7280">{{ pay_summary.paid_count + pay_summary.refunded_count }} total transaction(s)</div>
  </div>
</div>

<h2>Trainings</h2>
<h3>Create training</h3>
<form method="post" action="{{ url_for('admin_session_create') }}" class="inline">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input name="title" placeholder="Title" required>
  <input name="trainer" placeholder="Trainer" required>
  <input type="date" name="session_date" required>
  <input type="time" name="start_time" required>
  <input type="number" name="capacity" min="1" value="10" required style="width:76px" title="Capacity">
  <input type="number" name="price" step="0.01" min="0" value="10.00" required style="width:86px" title="Price ($)">
  <button>Create</button>
</form>
<h3>Change / delete training</h3>
<table>
  <tr><th>Title</th><th>Trainer</th><th>Date</th><th>Time</th><th>Cap</th><th>Price ($)</th><th>Load</th><th></th></tr>
  {% for s in sessions %}
  <tr>
    <td><input form="f{{ s.session_id }}" name="title" value="{{ s.title }}"></td>
    <td><input form="f{{ s.session_id }}" name="trainer" value="{{ s.trainer }}"></td>
    <td><input form="f{{ s.session_id }}" type="date" name="session_date" value="{{ s.session_date }}"></td>
    <td><input form="f{{ s.session_id }}" type="time" name="start_time" value="{{ s.start_time }}"></td>
    <td><input form="f{{ s.session_id }}" type="number" min="1" name="capacity" value="{{ s.capacity }}" style="width:60px"></td>
    <td><input form="f{{ s.session_id }}" type="number" step="0.01" min="0" name="price" value="{{ '%.2f'|format(s.price) }}" style="width:74px"></td>
    <td style="white-space:nowrap">{{ s.taken }}/{{ s.capacity }} <span class="badge {{ 'red' if s.load_level == 'high' else ('amber' if s.load_level == 'medium' else '') }}">{{ s.occupancy_pct }}%</span></td>
    <td style="white-space:nowrap">
      <form id="f{{ s.session_id }}" method="post" style="display:none">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="session_id" value="{{ s.session_id }}">
      </form>
      <button form="f{{ s.session_id }}" formaction="{{ url_for('admin_session_update') }}">Save</button>
      <button class="danger" form="f{{ s.session_id }}" formaction="{{ url_for('admin_session_delete') }}"
              onclick="return confirm('Delete this training and all its bookings?')">Delete</button>
    </td>
  </tr>
  {% endfor %}
</table>

<h2>Members &amp; Permissions</h2>
<table>
  <tr><th>ID</th><th>Name</th><th>Email</th><th>Balance</th><th>Membership</th><th>Role</th><th>Security</th><th>Actions</th></tr>
  {% for m in members %}
  <tr>
    <td>{{ m.member_id }}</td>
    <td>{{ m.name }}</td>
    <td>{{ m.email }}</td>
    <td>${{ "%.2f"|format(m.balance) }}</td>
    <td><span class="badge {{ '' if m.membership_status == 'active' else 'red' }}">{{ m.membership_status }}</span></td>
    <td><span class="badge {{ 'blue' if m.role == 'admin' else 'grey' }}">{{ m.role }}</span></td>
    <td>{% if m.is_locked %}<span class="badge red">Locked</span>{% else %}<span class="badge grey">OK</span>{% endif %}</td>
    <td style="white-space:nowrap">
      <form method="post" action="{{ url_for('admin_member_toggle_status') }}" style="display:inline">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="member_id" value="{{ m.member_id }}">
        <button class="secondary">{{ 'Expire' if m.membership_status == 'active' else 'Activate' }}</button>
      </form>
      <form method="post" action="{{ url_for('admin_member_toggle_role') }}" style="display:inline">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="member_id" value="{{ m.member_id }}">
        <button>{{ 'Demote' if m.role == 'admin' else 'Make Admin' }}</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>

<h2>All bookings</h2>
<table>
  <tr><th>ID</th><th>Member</th><th>Email</th><th>Session</th><th>Date</th><th>Booked at</th><th></th></tr>
  {% for b in bookings %}
  <tr><td>{{ b.booking_id }}</td><td>{{ b.name }}</td><td>{{ b.email }}</td><td>{{ b.title }}</td>
      <td>{{ b.session_date }}</td><td>{{ b.booked_at }}</td>
      <td><form method="post" action="{{ url_for('cancel') }}" style="margin:0">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="booking_id" value="{{ b.booking_id }}">
            <input type="hidden" name="next" value="admin">
            <button class="danger">Cancel &amp; Refund</button></form></td></tr>
  {% endfor %}
</table>

<h2>Recent Payments &amp; Refunds</h2>
<table>
  <tr><th>ID</th><th>Member</th><th>Session</th><th>Amount</th><th>Status</th><th>Paid at</th><th>Refunded at</th></tr>
  {% for p in recent_payments %}
  <tr>
    <td>#{{ p.payment_id }}</td>
    <td>{{ p.member_name }} ({{ p.member_email }})</td>
    <td>{{ p.session_title }}</td>
    <td>${{ "%.2f"|format(p.amount) }}</td>
    <td><span class="badge {{ '' if p.status == 'paid' else 'amber' }}">{{ p.status }}</span></td>
    <td>{{ p.created_at }}</td>
    <td>{{ p.refunded_at or '-' }}</td>
  </tr>
  {% else %}
  <tr><td colspan="7">No payments recorded yet.</td></tr>
  {% endfor %}
</table>
</body></html>
"""

TESTS_PAGE = HEAD + NAV + """
<h2>Tests: {{ passed }} / {{ total }} passed</h2>
<p>Every test runs on a temporary in-memory database; the real <code>gym.db</code> is not changed.</p>
{% for grp in groups %}
<h3>{{ grp.title }} ({{ grp.passed }} / {{ grp.results|length }})</h3>
<table>
  <tr><th>#</th><th>Test</th><th>What it checks</th><th>Result</th></tr>
  {% for r in grp.results %}
  <tr><td>{{ loop.index }}</td><td>{{ r.name }}</td><td>{{ r.doc }}</td>
      <td class="{{ 'pass' if r.ok else 'fail' }}">{{ 'PASS' if r.ok else r.err }}</td></tr>
  {% endfor %}
</table>
{% endfor %}
</body></html>
"""


# =====================================================================
# ROUTES
# =====================================================================
# ---------- DONE BY: Zhanibek + Sprint 3 Security - auth & password reset ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        m, err = authenticate_with_status(
            get_db(), request.form.get("email", ""), request.form.get("password", ""))
        if m:
            session.clear()
            session["member_id"], session["role"] = m["member_id"], m["role"]
            csrf_token()
            return redirect(url_for("index"))
        flash(err or "Wrong email or password.", "err")
    return render_template_string(LOGIN_PAGE, title="Login", user=None)


@app.route("/register", methods=["GET", "POST"])
def register():
    name = email = ""
    if request.method == "POST":
        name, email = request.form.get("name", ""), request.form.get("email", "")
        ok, msg = register_member(get_db(), name, email, request.form.get("password", ""))
        flash(msg, "ok" if ok else "err")
        if ok:
            return redirect(url_for("login"))
    return render_template_string(REGISTER_PAGE, title="Register", user=None, name=name, email=email)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    email = ""
    reset_url = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        ok, token, msg = create_password_reset(get_db(), email)
        flash(msg, "ok" if ok else "err")
        if token:
            reset_url = url_for("reset_password", token=token, _external=True)
    return render_template_string(
        FORGOT_PASSWORD_PAGE, title="Forgot Password", user=None,
        email=email, reset_url=reset_url)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = get_db()
    row, err = validate_reset_token(db, token)
    if err:
        flash(err, "err")
        return render_template_string(
            RESET_PASSWORD_PAGE, title="Reset Password", user=None,
            valid_token=False, token=token, reset_email="")
    if request.method == "POST":
        ok, msg = reset_password_with_token(db, token, request.form.get("password", ""))
        flash(msg, "ok" if ok else "err")
        if ok:
            return redirect(url_for("login"))
    return render_template_string(
        RESET_PASSWORD_PAGE, title="Reset Password", user=None,
        valid_token=True, token=token, reset_email=row["email"])


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Main page (Torezhan + Sprint 3: occupancy, wallet, payments) ----------
@app.get("/")
@login_required
def index():
    db = get_db()
    user = current_user()
    q = request.args.get("q", "").strip()
    upcoming = request.args.get("upcoming") == "1"
    sessions = search_sessions(db, q, upcoming)
    my_bookings = get_member_bookings(db, user["member_id"])
    my_payments = get_member_payments(db, user["member_id"])
    occupancy = get_gym_occupancy(db)
    return render_template_string(
        PAGE, title="Sessions", user=user, q=q, upcoming=upcoming, sessions=sessions,
        my_bookings=my_bookings, my_payments=my_payments, occupancy=occupancy,
        booked_ids={b["session_id"] for b in my_bookings},
        upcoming_count=sum(1 for b in my_bookings if not b["is_past"]))


@app.post("/topup")
@login_required
def topup():
    user = current_user()
    ok, msg = topup_balance(get_db(), user["member_id"], request.form.get("amount"))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("index"))


@app.post("/book")
@login_required
def book():
    user = current_user()
    session_id = request.form.get("session_id", type=int)
    ok, msg = book_session(get_db(), user["member_id"], session_id)
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("index"))


# ---------- DONE BY: Adil + Sprint 3 RBAC/Refunds - cancellation route ----------
@app.post("/cancel")
@login_required
def cancel():
    user = current_user()
    is_admin = user["role"] == "admin"
    ok, msg = cancel_booking(get_db(), user["member_id"],
                             request.form.get("booking_id", type=int), is_admin=is_admin)
    flash(msg, "ok" if ok else "err")
    if is_admin and request.form.get("next") == "admin":
        return redirect(url_for("admin"))
    return redirect(url_for("index"))


# ---------- DONE BY: Zhanibek + Sprint 3 - admin: trainings, members RBAC, payments ----------
@app.get("/admin")
@admin_required
def admin():
    db = get_db()
    bookings = db.execute("""
        SELECT b.booking_id, m.name, m.email, s.title, s.session_date, b.booked_at
        FROM Bookings b JOIN Members m USING(member_id) JOIN Sessions s USING(session_id)
        ORDER BY b.booked_at DESC""").fetchall()
    pay_summary, recent_payments = get_payment_overview(db)
    return render_template_string(
        ADMIN_PAGE, title="Admin", user=current_user(),
        sessions=search_sessions(db), bookings=bookings,
        members=get_all_members(db), occupancy=get_gym_occupancy(db),
        pay_summary=pay_summary, recent_payments=recent_payments)


@app.post("/admin/session/create")
@admin_required
def admin_session_create():
    f = request.form
    ok, msg = create_session(
        get_db(), f.get("title"), f.get("trainer"),
        f.get("session_date"), f.get("start_time"), f.get("capacity"),
        f.get("price", DEFAULT_SESSION_PRICE))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/session/update")
@admin_required
def admin_session_update():
    f = request.form
    ok, msg = update_session(
        get_db(), f.get("session_id", type=int), f.get("title"), f.get("trainer"),
        f.get("session_date"), f.get("start_time"), f.get("capacity"),
        f.get("price"))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/session/delete")
@admin_required
def admin_session_delete():
    ok, msg = delete_session(get_db(), request.form.get("session_id", type=int))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/member/toggle-status")
@admin_required
def admin_member_toggle_status():
    ok, msg = toggle_member_status(get_db(), request.form.get("member_id", type=int))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/member/toggle-role")
@admin_required
def admin_member_toggle_role():
    actor = current_user()
    ok, msg = toggle_member_role(
        get_db(), actor["member_id"], request.form.get("member_id", type=int))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


# ---------- DONE BY: Aldair (+ Adil, Torezhan, Sprint 3 groups) - tests in the interface ----------
@app.get("/tests")
@login_required
def tests():
    groups = run_all_tests()
    total = sum(len(g_["results"]) for g_ in groups)
    passed = sum(g_["passed"] for g_ in groups)
    return render_template_string(TESTS_PAGE, title="Tests", user=current_user(),
                                  groups=groups, total=total, passed=passed)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
