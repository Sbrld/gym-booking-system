"""Gym Booking System - Flask + SQLite.
Run:  pip install -r requirements.txt   ->   python app.py   ->   open http://127.0.0.1:5000
Demo accounts: password is "1234" for everyone. aldair@kbtu.kz is admin.

TEAM TASKS (from the weekly table)
  Iskander  : DATABASE + ADMIN TOOLS:
              - schema rules: CHECK constraints, ON DELETE CASCADE, foreign keys ON (SCHEMA)
              - seed data in SEED_* constants + bigger demo data for the real gym.db (seed_extended)
              - schema check: python app.py --check, python app.py --reset
              - admin AUDIT LOG (AuditLog table, append-only) -> page /admin/audit
              - admin statistics (JOIN + GROUP BY) -> page /stats
              - club occupancy chart by weekday and hour on the main page (get_club_load)
              - friendlier look (nav, tables, empty states); 22 tests -> page /tests, group 5
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
import random  # ADDED (Iskander)
import re
import secrets
import sqlite3
import sys     # ADDED (Iskander)
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, abort, g, render_template_string, request, redirect,
                   url_for, flash, session)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "gym.db"))  # CHANGED (Iskander): fixed path next to app.py
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

DEMO_PW_HASH = generate_password_hash("1234")  # computed once (hashing is slow)
MIN_PASSWORD = 4  # demo value; use 8+ in a real system
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
RESET_TOKEN_MINUTES = 30
DEFAULT_MEMBER_BALANCE = 100.0
DEFAULT_SESSION_PRICE = 10.0
DB_TIMEOUT = 15.0

_initialized_dbs = set()
_init_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS Members (
    member_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    membership_status TEXT NOT NULL DEFAULT 'active'
        CHECK (membership_status IN ('active', 'expired')),     -- CHANGED (Iskander)
    password_hash TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'admin')),                    -- CHANGED (Iskander)
    balance REAL NOT NULL DEFAULT 100.0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
CREATE TABLE IF NOT EXISTS Sessions (
    session_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    trainer TEXT NOT NULL,
    session_date TEXT NOT NULL
        CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),  -- YYYY-MM-DD (CHANGED, Iskander)
    start_time TEXT NOT NULL
        CHECK (start_time GLOB '[0-9][0-9]:[0-9][0-9]'),                         -- HH:MM (CHANGED, Iskander)
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    price REAL NOT NULL DEFAULT 10.0 CHECK (price >= 0)
);
CREATE TABLE IF NOT EXISTS Bookings (
    booking_id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id) ON DELETE CASCADE,    -- CHANGED (Iskander)
    session_id INTEGER NOT NULL REFERENCES Sessions(session_id) ON DELETE CASCADE, -- CHANGED (Iskander)
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('confirmed', 'cancelled')),                              -- CHANGED (Iskander)
    booked_at TEXT NOT NULL,
    UNIQUE (member_id, session_id)
);
CREATE TABLE IF NOT EXISTS Payments (
    payment_id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id) ON DELETE CASCADE,    -- CHANGED (Iskander)
    session_id INTEGER NOT NULL,  -- CHANGED (Iskander): no FK on purpose, payment history survives deleting a training
    amount REAL NOT NULL CHECK (amount >= 0),
    status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'refunded')),
    created_at TEXT NOT NULL,
    refunded_at TEXT
);
CREATE TABLE IF NOT EXISTS PasswordResets (
    token TEXT PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id) ON DELETE CASCADE,    -- CHANGED (Iskander)
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
-- ADDED (Iskander): admin audit log. No FK on actor_id: the log must survive deleting a member.
CREATE TABLE IF NOT EXISTS AuditLog (
    log_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_id INTEGER,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action LIKE '%.%'),   -- 'kind.what', e.g. session.create
    target TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS Waitlist (
    waitlist_id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES Members(member_id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES Sessions(session_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE (member_id, session_id)
);
-- the log is append-only: the database itself refuses to change or delete a row
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON AuditLog
BEGIN SELECT RAISE(ABORT, 'AuditLog is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON AuditLog
BEGIN SELECT RAISE(ABORT, 'AuditLog is append-only'); END;
"""

# ===== ADDED (Iskander): SEED DATA ======================================
# CORE seed: small and fixed. It is loaded into gym.db AND into every in-memory test database,
# so the 53 team tests keep working. To plug in the real team data, edit these lists.
SEED_MEMBERS = [  # (name, email, membership_status, role, balance); password "1234" (DEMO_PW_HASH)
    ("Aldair", "aldair@kbtu.kz", "active", "admin", 85.0),
    ("Iskander", "iskander@kbtu.kz", "active", "member", 85.0),
    ("Adil", "adil@kbtu.kz", "active", "member", 100.0),
    ("Zhanibek", "zhanibek@kbtu.kz", "active", "member", 100.0),
    ("Expired Member", "expired@kbtu.kz", "expired", "member", 100.0),
]
SEED_SESSIONS = [  # (title, trainer, days_from_today, start_time, capacity, price)
    ("Yoga", "Aigerim", 1, "10:00", 2, 15.0),
    ("Boxing", "Daniyar", 2, "18:00", 3, 20.0),
    ("Fitness", "Madina", 3, "09:00", 4, 12.0),
    ("Yoga (past)", "Aigerim", -1, "10:00", 5, 15.0),
]
SEED_BOOKINGS = [(1, 1), (2, 1)]  # (member_id, session_id): Yoga is full; every booking also gets a paid Payment

# EXTENDED demo data: only for the real gym.db (python app.py), NOT for the tests.
# A realistic two-week timetable with bigger halls, 30 more members and their bookings,
# so the occupancy chart looks like a real club and 2 booked places are no longer "100%".
EXT_MEMBER_NAMES = [
    "Alua", "Bekzat", "Dana", "Yerlan", "Gulnara", "Timur", "Madi", "Aruzhan", "Nurlan", "Saltanat",
    "Dias", "Kamila", "Rustem", "Zarina", "Arman", "Ayana", "Miras", "Lyazzat", "Askar", "Dinara",
    "Olzhas", "Aigul", "Samat", "Nazerke", "Azamat", "Karina", "Ilyas", "Assel", "Ruslan", "Medina",
]
EXT_CAPACITY_UPGRADE = {"Yoga": 20, "Boxing": 25, "Fitness": 30, "Yoga (past)": 20}  # the 4 core sessions get real halls
# (title, trainer, weekdays 0=Mon..6=Sun, start_time, capacity, price, typical fill 0..1)
EXT_SCHEDULE = [
    ("Crossfit", "Daniyar", (0, 1, 3, 4), "07:00", 16, 15.0, 0.60),
    ("Morning Yoga", "Aigerim", (0, 2, 4), "08:00", 20, 12.0, 0.55),
    ("Weekend Yoga", "Aigerim", (5, 6), "09:00", 25, 12.0, 0.60),
    ("Stretch & Mobility", "Dana", (1, 3), "09:00", 18, 10.0, 0.50),
    ("Pilates", "Madina", (1, 3, 5), "10:00", 18, 14.0, 0.55),
    ("Weekend Fitness", "Madina", (5, 6), "10:00", 30, 12.0, 0.75),
    ("Core Training", "Sanzhar", (0, 2, 4), "11:00", 18, 13.0, 0.50),
    ("Weekend HIIT", "Daniyar", (5, 6), "11:00", 25, 13.0, 0.70),
    ("Lunch HIIT", "Daniyar", (0, 1, 2, 3, 4), "12:00", 20, 13.0, 0.50),
    ("Family Fitness", "Madina", (5, 6), "12:00", 25, 11.0, 0.55),
    ("Express Fitness", "Dana", (1, 2, 3), "13:00", 20, 11.0, 0.45),
    ("Open Boxing", "Daniyar", (5,), "14:00", 20, 15.0, 0.50),
    ("Strength Basics", "Sanzhar", (0, 2, 4), "15:00", 18, 14.0, 0.45),
    ("Dance Cardio", "Madina", (5, 6), "15:00", 25, 12.0, 0.50),
    ("Strength Club", "Sanzhar", (1, 3, 6), "16:00", 18, 14.0, 0.55),
    ("Weekend Spinning", "Dana", (5, 6), "16:00", 25, 14.0, 0.60),
    ("Teen Fitness", "Madina", (0, 1, 2, 3, 4), "17:00", 20, 10.0, 0.60),
    ("Spinning", "Dana", (0, 1, 2, 3, 4), "18:00", 25, 14.0, 0.85),
    ("Evening Crossfit", "Sanzhar", (0, 2, 4), "18:00", 18, 15.0, 0.80),
    ("Boxing Group", "Daniyar", (0, 2, 4), "19:00", 25, 20.0, 0.85),
    ("Zumba", "Madina", (1, 3), "19:00", 30, 14.0, 0.90),
    ("Functional Training", "Sanzhar", (0, 1, 2, 3, 4), "19:00", 22, 16.0, 0.80),
    ("Evening Yoga", "Aigerim", (0, 1, 2, 3, 6), "20:00", 25, 12.0, 0.65),
    ("Late HIIT", "Daniyar", (1, 3, 4), "20:00", 20, 13.0, 0.70),
    ("Weekend Zumba", "Madina", (5, 6), "18:00", 30, 14.0, 0.70),
    ("Weekend Boxing", "Daniyar", (5, 6), "19:00", 20, 18.0, 0.70),
    ("Stretching", "Aigerim", (0, 1, 2, 3, 4, 5), "21:00", 20, 10.0, 0.35),
]
EXT_DAYS_BACK, EXT_DAYS_AHEAD = 10, 3  # 14 days: every weekday appears twice; the page lists the next 4 days
EXT_START_BALANCE = 800.0  # money of every demo member before the demo bookings are paid


# =====================================================================
# DATABASE HELPERS (base app; DONE BY: Iskander - SEED_* data, foreign keys, extended demo data, schema check)
# =====================================================================
def connect_db(path=None, timeout=DB_TIMEOUT):
    """Open a SQLite connection hardened for concurrent multithreaded access."""
    target = DB if path is None else path
    conn = sqlite3.connect(target, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    if target != ":memory:":
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect_db(DB)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def day(n):
    """ISO date n days from today (used for demo data and tests)."""
    return (datetime.now().date() + timedelta(days=n)).isoformat()


def seed_db(db, extended=False):
    """Create tables, apply idempotent migrations, and seed demo data (only if empty).
    Works for gym.db and for the in-memory databases used by the tests."""
    db.execute("PRAGMA foreign_keys = ON")  # ADDED (Iskander): also for the in-memory test databases
    db.execute(f"PRAGMA busy_timeout = {int(DB_TIMEOUT * 1000)}")
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
        # CHANGED (Iskander): data comes from the SEED_* constants
        db.executemany(
            "INSERT INTO Members(name,email,membership_status,password_hash,role,balance) "
            "VALUES (?,?,?,?,?,?)",
            [(n, e, st, DEMO_PW_HASH, r, bal) for n, e, st, r, bal in SEED_MEMBERS])
        db.executemany(
            "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) "
            "VALUES (?,?,?,?,?,?)",
            [(t, tr, day(off), tm, cap, pr) for t, tr, off, tm, cap, pr in SEED_SESSIONS])
        now = datetime.now().isoformat(timespec="seconds")
        db.executemany(
            "INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (?,?,?,?)",
            [(m, s, "confirmed", now) for m, s in SEED_BOOKINGS])
        db.executemany(
            "INSERT INTO Payments(member_id,session_id,amount,status,created_at) VALUES (?,?,?,?,?)",
            [(m, s, SEED_SESSIONS[s - 1][5], "paid", now) for m, s in SEED_BOOKINGS])
        if extended:  # ADDED (Iskander): only the real gym.db, never the test databases
            seed_extended(db)
    else:
        # Old members without a password get the demo password; make sure one admin exists
        db.execute("UPDATE Members SET password_hash=? WHERE password_hash=''", (DEMO_PW_HASH,))
        if db.execute("SELECT COUNT(*) FROM Members WHERE role='admin'").fetchone()[0] == 0:
            db.execute("UPDATE Members SET role='admin' WHERE member_id=1")
    db.commit()


def seed_extended(db):
    """ADDED (Iskander): fill a NEW gym.db with a realistic two-week timetable, 30 more members and
    their bookings + payments. Deterministic (fixed random seed). Called only by init_db()."""
    rng = random.Random(2026)
    now = datetime.now()
    for title, cap in EXT_CAPACITY_UPGRADE.items():  # bigger halls for the 4 core sessions
        db.execute("UPDATE Sessions SET capacity=? WHERE title=?", (cap, title))

    first_id = db.execute("SELECT COALESCE(MAX(member_id), 0) FROM Members").fetchone()[0] + 1
    db.executemany(
        "INSERT INTO Members(name,email,membership_status,password_hash,role,balance) "
        "VALUES (?,?,'active',?,'member',?)",
        [(n, f"{n.lower()}@kbtu.kz", DEMO_PW_HASH, EXT_START_BALANCE) for n in EXT_MEMBER_NAMES])
    ext_ids = list(range(first_id, first_id + len(EXT_MEMBER_NAMES)))
    spent = {m: 0.0 for m in ext_ids}
    counts = {"sessions": 0, "bookings": 0}

    def add_session(title, trainer, date, time, cap, price, n_booked):
        cur = db.execute("INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) "
                         "VALUES (?,?,?,?,?,?)", (title, trainer, date, time, cap, price))
        sid = cur.lastrowid
        start = datetime.fromisoformat(f"{date}T{time}")
        for m in rng.sample(ext_ids, max(0, min(n_booked, cap, len(ext_ids)))):
            if start > now:  # future session: booked during the last days
                booked = now - timedelta(hours=rng.randint(1, 24 * 6))
            else:            # past session: booked a few days before it
                booked = start - timedelta(hours=rng.randint(6, 72))
            stamp = booked.isoformat(timespec="seconds")
            db.execute("INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (?,?,'confirmed',?)",
                       (m, sid, stamp))
            db.execute("INSERT INTO Payments(member_id,session_id,amount,status,created_at) "
                       "VALUES (?,?,?,'paid',?)", (m, sid, price, stamp))
            spent[m] += price
            counts["bookings"] += 1
        counts["sessions"] += 1

    for offset in range(-EXT_DAYS_BACK, EXT_DAYS_AHEAD + 1):
        d = now.date() + timedelta(days=offset)
        for title, trainer, weekdays, time, cap, price, fill in EXT_SCHEDULE:
            if d.weekday() in weekdays:
                n = round(cap * fill * rng.uniform(0.8, 1.15) * (0.75 if offset >= 0 else 1.0))
                add_session(title, trainer, d.isoformat(), time, cap, price, n)
    # a tiny private session that is already full, to demo the "session is full" message
    add_session("Private Coaching", "Daniyar", day(2), "16:00", 2, 40.0, 2)

    db.executemany("UPDATE Members SET balance=ROUND(?,2) WHERE member_id=?",
                   [(max(20.0, EXT_START_BALANCE - s), m) for m, s in spent.items()])  # never negative
    log_action(db, None, "system.seed", "demo data",
               f"Loaded {len(ext_ids)} members, {counts['sessions']} sessions, {counts['bookings']} bookings")


def init_db(extended=None):
    if extended is None:
        extended = not app.config.get("TESTING", False)
    db = connect_db(DB)
    try:
        seed_db(db, extended=extended)  # CHANGED (Iskander): bigger demo data for the real gym.db
    finally:
        db.close()
    _initialized_dbs.add(DB)


def ensure_db_initialized():
    """Safe, idempotent DB startup guard for WSGI (Gunicorn) and local runs."""
    if DB not in _initialized_dbs or (DB != ":memory:" and not os.path.exists(DB)):
        with _init_lock:
            if DB not in _initialized_dbs or (DB != ":memory:" and not os.path.exists(DB)):
                init_db()


# =====================================================================
# DONE BY: Iskander - schema check (python app.py --check)
# =====================================================================
EXPECTED_SCHEMA = {  # table -> {column: (type, NOT NULL)}
    "Members": {"member_id": ("INTEGER", 0), "name": ("TEXT", 1), "email": ("TEXT", 1),
                "membership_status": ("TEXT", 1), "password_hash": ("TEXT", 1), "role": ("TEXT", 1),
                "balance": ("REAL", 1), "failed_attempts": ("INTEGER", 1), "locked_until": ("TEXT", 0)},
    "Sessions": {"session_id": ("INTEGER", 0), "title": ("TEXT", 1), "trainer": ("TEXT", 1),
                 "session_date": ("TEXT", 1), "start_time": ("TEXT", 1), "capacity": ("INTEGER", 1),
                 "price": ("REAL", 1)},
    "Bookings": {"booking_id": ("INTEGER", 0), "member_id": ("INTEGER", 1), "session_id": ("INTEGER", 1),
                 "status": ("TEXT", 1), "booked_at": ("TEXT", 1)},
    "Payments": {"payment_id": ("INTEGER", 0), "member_id": ("INTEGER", 1), "session_id": ("INTEGER", 1),
                 "amount": ("REAL", 1), "status": ("TEXT", 1), "created_at": ("TEXT", 1),
                 "refunded_at": ("TEXT", 0)},
    "PasswordResets": {"token": ("TEXT", 0), "member_id": ("INTEGER", 1), "expires_at": ("TEXT", 1),
                       "used": ("INTEGER", 1)},
    "AuditLog": {"log_id": ("INTEGER", 0), "created_at": ("TEXT", 1), "actor_id": ("INTEGER", 0),
                 "actor_name": ("TEXT", 1), "action": ("TEXT", 1), "target": ("TEXT", 1),
                 "details": ("TEXT", 1)},
    "Waitlist": {"waitlist_id": ("INTEGER", 0), "member_id": ("INTEGER", 1),
                 "session_id": ("INTEGER", 1), "created_at": ("TEXT", 1)},
}
EXPECTED_FKS = {  # table -> {(parent table, column, on delete)}
    "Bookings": {("Members", "member_id", "CASCADE"), ("Sessions", "session_id", "CASCADE")},
    "Payments": {("Members", "member_id", "CASCADE")},  # no FK to Sessions: payment history survives
    "PasswordResets": {("Members", "member_id", "CASCADE")},
    "AuditLog": set(),                                  # no FKs: the log survives deleting anything
    "Waitlist": {("Members", "member_id", "CASCADE"), ("Sessions", "session_id", "CASCADE")},
}
# bad rows that the database itself must reject (CHECK constraints)
BAD_ROWS = [
    ("membership_status only active/expired",
     "INSERT INTO Members(name,email,membership_status) VALUES ('x','chk1@x.kz','banned')"),
    ("role only member/admin",
     "INSERT INTO Members(name,email,role) VALUES ('x','chk2@x.kz','boss')"),
    ("Bookings.status only confirmed/cancelled",
     "INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (1,1,'maybe','x')"),
    ("Payments.status only paid/refunded",
     "INSERT INTO Payments(member_id,session_id,amount,status,created_at) VALUES (1,1,1,'lost','x')"),
    ("session date must look like YYYY-MM-DD",
     "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity) VALUES ('a','b','12.11.2026','10:00',5)"),
    ("session time must look like HH:MM",
     "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity) VALUES ('a','b','2030-01-01','10am',5)"),
    ("session capacity must be > 0",
     "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity) VALUES ('a','b','2030-01-01','10:00',0)"),
    ("session price must be >= 0",
     "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) VALUES ('a','b','2030-01-01','10:00',5,-1)"),
    ("audit action must look like kind.what",
     "INSERT INTO AuditLog(created_at,actor_name,action) VALUES ('x','x','nodot')"),
]


def check_schema():
    """Compare gym.db with EXPECTED_SCHEMA, test the DB rules and the data. Returns 0 if OK, else 1."""
    problems = []

    def check(cond, ok, bad):
        print(("[OK]   " + ok) if cond else ("[FAIL] " + bad))
        if not cond:
            problems.append(bad)

    db = connect_db(DB)
    hint = " (old gym.db? run: python app.py --reset)"
    # 1. tables, columns, types
    for table, cols in EXPECTED_SCHEMA.items():
        info = {r[1]: (r[2].upper(), r[3]) for r in db.execute(f"PRAGMA table_info({table})")}
        check(bool(info), f"table {table} exists", f"table {table} is missing" + hint)
        for col, (typ, nn) in cols.items():
            check(info.get(col) == (typ, nn), f"{table}.{col} {typ}",
                  f"{table}.{col}: expected {typ}, NOT NULL={nn}, found {info.get(col)}" + hint)
    # 2. UNIQUE and foreign keys (with ON DELETE CASCADE)
    uniq = [[c[2] for c in db.execute(f"PRAGMA index_info({i[1]})")]
            for i in db.execute("PRAGMA index_list(Bookings)")]
    check(["member_id", "session_id"] in uniq, "Bookings: UNIQUE(member_id, session_id)",
          "Bookings: UNIQUE(member_id, session_id) is missing")
    w_uniq = [[c[2] for c in db.execute(f"PRAGMA index_info({i[1]})")]
              for i in db.execute("PRAGMA index_list(Waitlist)")]
    check(["member_id", "session_id"] in w_uniq, "Waitlist: UNIQUE(member_id, session_id)",
          "Waitlist: UNIQUE(member_id, session_id) is missing")
    for table, expected in EXPECTED_FKS.items():
        fks = {(r[2], r[3], r[6]) for r in db.execute(f"PRAGMA foreign_key_list({table})")}
        check(fks == expected, f"foreign keys of {table}: {sorted(expected) or 'none'}",
              f"foreign keys of {table} are wrong: {sorted(fks)}" + hint)
    # 3. the database itself rejects bad rows (each insert is rolled back)
    for rule, sql in BAD_ROWS:
        try:
            db.execute(sql)
            rejected = False
        except sqlite3.IntegrityError as e:
            rejected = "CHECK" in str(e)
        finally:
            db.rollback()
        check(rejected, f"DB rejects bad data: {rule}", f"DB accepts bad data: {rule}" + hint)
    # 3b. the audit log is append-only
    try:
        db.execute("INSERT INTO AuditLog(created_at,actor_name,action) VALUES ('x','x','system.check')")
        db.execute("UPDATE AuditLog SET target='tampered' WHERE action='system.check'")
        blocked = False
    except sqlite3.IntegrityError as e:
        blocked = "append-only" in str(e)
    finally:
        db.rollback()
    check(blocked, "AuditLog rows cannot be changed (append-only trigger)",
          "AuditLog rows can be changed: the append-only trigger is missing" + hint)
    # 4. data integrity
    q = lambda sql: db.execute(sql).fetchall()
    check(not q("PRAGMA foreign_key_check"), "no broken references", "rows point to missing members/sessions")
    check(bool(q("SELECT 1 FROM Members WHERE role='admin'")), "at least one admin", "no admin in Members")
    check(not q("SELECT 1 FROM Members WHERE password_hash=''"),
          "every member has a password hash", "members without a password")
    check(not q("SELECT 1 FROM Members WHERE balance < 0"), "no negative balances", "a member has a negative balance")
    check(not q("""SELECT 1 FROM Sessions s WHERE (SELECT COUNT(*) FROM Bookings b
                   WHERE b.session_id=s.session_id AND b.status='confirmed') > s.capacity"""),
          "no session is over capacity", "a session has more bookings than capacity")
    db.close()
    print("\nRESULT:", "matches the schema" if not problems else f"{len(problems)} problem(s) found")
    return 1 if problems else 0


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
    Uses BEGIN IMMEDIATE for atomic attempt counting and lockout under concurrency.
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

    # Verify password outside the write lock (scrypt hashing is CPU-intensive)
    pw_ok = bool(m["password_hash"] and check_password_hash(m["password_hash"], password or ""))

    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("SELECT * FROM Members WHERE member_id=?", (m["member_id"],)).fetchone()
        if cur is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return None, "Wrong email or password."

        now = datetime.now()
        if cur["locked_until"]:
            try:
                lock_dt = datetime.fromisoformat(cur["locked_until"])
            except ValueError:
                lock_dt = None
            if lock_dt and lock_dt > now:
                if started_tx and db.in_transaction:
                    db.rollback()
                mins_left = max(1, math.ceil((lock_dt - now).total_seconds() / 60))
                return None, (f"Account is locked due to too many failed login attempts. "
                              f"Try again in {mins_left} minute(s).")

        if pw_ok:
            if (cur["failed_attempts"] or 0) > 0 or cur["locked_until"]:
                db.execute("UPDATE Members SET failed_attempts=0, locked_until=NULL WHERE member_id=?",
                           (cur["member_id"],))
                cur = db.execute("SELECT * FROM Members WHERE member_id=?", (cur["member_id"],)).fetchone()
            if started_tx:
                db.commit()
            return cur, None

        attempts = (cur["failed_attempts"] or 0) + 1
        if attempts >= MAX_LOGIN_ATTEMPTS:
            until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="seconds")
            db.execute("UPDATE Members SET failed_attempts=?, locked_until=? WHERE member_id=?",
                       (attempts, until, cur["member_id"]))
            if started_tx:
                db.commit()
            return None, (f"Too many failed login attempts. Account locked for "
                          f"{LOCKOUT_MINUTES} minutes.")

        db.execute("UPDATE Members SET failed_attempts=? WHERE member_id=?",
                   (attempts, cur["member_id"]))
        if started_tx:
            db.commit()
        return None, "Wrong email or password."
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise


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
    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute("SELECT capacity, price FROM Sessions WHERE session_id=?",
                              (session_id,)).fetchone()
        if existing is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Training not found."
        effective_price = existing["price"] if price in (None, "") else price
        fields, err = _check_session_fields(title, trainer, date, time, capacity, effective_price)
        if err:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, err
        taken = db.execute("SELECT COUNT(*) FROM Bookings WHERE session_id=? AND status='confirmed'",
                           (session_id,)).fetchone()[0]
        if fields[4] < taken:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, f"Capacity cannot be lower than the {taken} bookings already confirmed."
        db.execute(
            "UPDATE Sessions SET title=?, trainer=?, session_date=?, start_time=?, capacity=?, price=? "
            "WHERE session_id=?", fields + (session_id,))
        promoted = []
        if fields[4] > existing["capacity"]:
            promoted = _promote_waitlist_in_tx(db, session_id)
        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    promo_note = ""
    if promoted:
        promo_note = f" Waitlist auto-promoted: {', '.join(p['name'] for p in promoted)}."
    return True, f"Training updated: {fields[0]}.{promo_note}"


def delete_session(db, session_id):
    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT title FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Training not found."
        db.execute("DELETE FROM Waitlist WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM Bookings WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM Sessions WHERE session_id=?", (session_id,))
        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
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
    """Add funds to a member's balance atomically. Returns (ok, message)."""
    try:
        val = float(amount)
    except (ValueError, TypeError):
        return False, "Top-up amount must be a valid number."
    if math.isnan(val) or math.isinf(val) or val <= 0:
        return False, "Top-up amount must be greater than 0."
    if val > 10000:
        return False, "Top-up amount cannot exceed $10,000 at once."
    val = round(val, 2)
    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT balance FROM Members WHERE member_id=?", (member_id,)).fetchone()
        if m is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Member not found."
        db.execute("UPDATE Members SET balance = ROUND(balance + ?, 2) WHERE member_id=?",
                   (val, member_id))
        new_balance = db.execute("SELECT balance FROM Members WHERE member_id=?", (member_id,)).fetchone()[0]
        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
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
        db.execute("DELETE FROM Waitlist WHERE member_id=? AND session_id=?",
                   (member_id, session_id))
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
# WAITLIST SYSTEM (FIFO queue, atomic join/leave, auto-promotion on cancel & capacity increase)
# Rule for ineligible/low-balance waitlisted members during auto-promotion:
#   Ineligible (expired) or low-balance members are SKIPPED and KEPT in the waitlist queue
#   (preserving their FIFO position if they later renew/top up), while the next eligible
#   member in FIFO order is promoted and charged.
# =====================================================================
def _promote_waitlist_in_tx(db, session_id):
    """Promote eligible FIFO waitlisted members into open spots for an upcoming session.
    MUST be called inside an active BEGIN IMMEDIATE transaction.
    Returns list of promoted dicts: [{'member_id', 'name', 'price'}, ...]."""
    sess = db.execute("SELECT * FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
    if sess is None or _starts(sess) <= datetime.now():
        return []
    taken = db.execute(
        "SELECT COUNT(*) FROM Bookings WHERE session_id=? AND status='confirmed'",
        (session_id,)).fetchone()[0]
    spots = sess["capacity"] - taken
    if spots <= 0:
        return []

    price = round(float(sess["price"]), 2)
    candidates = db.execute("""
        SELECT w.waitlist_id, w.member_id, m.name, m.membership_status, m.balance
        FROM Waitlist w
        JOIN Members m ON m.member_id = w.member_id
        WHERE w.session_id = ?
        ORDER BY w.created_at ASC, w.waitlist_id ASC""", (session_id,)).fetchall()

    promoted = []
    for cand in candidates:
        if spots <= 0:
            break
        already = db.execute(
            "SELECT 1 FROM Bookings WHERE member_id=? AND session_id=? AND status='confirmed'",
            (cand["member_id"], session_id)).fetchone()
        if already:
            db.execute("DELETE FROM Waitlist WHERE waitlist_id=?", (cand["waitlist_id"],))
            continue
        bal = round(float(cand["balance"]), 2)
        if cand["membership_status"] != "active" or bal < price:
            # Skip ineligible / low-balance member and keep them in the waitlist queue
            continue

        now = datetime.now().isoformat(timespec="seconds")
        db.execute("DELETE FROM Waitlist WHERE waitlist_id=?", (cand["waitlist_id"],))
        db.execute("UPDATE Members SET balance = ROUND(balance - ?, 2) WHERE member_id=?",
                   (price, cand["member_id"]))
        db.execute(
            "INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (?,?,?,?)",
            (cand["member_id"], session_id, "confirmed", now))
        db.execute(
            "INSERT INTO Payments(member_id,session_id,amount,status,created_at) VALUES (?,?,?,?,?)",
            (cand["member_id"], session_id, price, "paid", now))
        promoted.append({"member_id": cand["member_id"], "name": cand["name"], "price": price})
        spots -= 1
    return promoted


def join_waitlist(db, member_id, session_id):
    """Add an active member to the FIFO waitlist of a full upcoming session (no charge).
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

        if member["membership_status"] != "active":
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Your membership has expired. Please renew it to join the waitlist."

        if _starts(sess) <= datetime.now():
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "This session has already started or finished."

        dup_book = db.execute(
            "SELECT 1 FROM Bookings WHERE member_id=? AND session_id=? AND status='confirmed'",
            (member_id, session_id)).fetchone()
        if dup_book:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "You are already registered for this session."

        dup_wait = db.execute(
            "SELECT 1 FROM Waitlist WHERE member_id=? AND session_id=?",
            (member_id, session_id)).fetchone()
        if dup_wait:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "You are already on the waitlist for this session."

        taken = db.execute(
            "SELECT COUNT(*) FROM Bookings WHERE session_id=? AND status='confirmed'",
            (session_id,)).fetchone()[0]
        if taken < sess["capacity"]:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Spots are still available for this session. Please book directly instead."

        now = datetime.now().isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO Waitlist(member_id, session_id, created_at) VALUES (?,?,?)",
            (member_id, session_id, now))
        pos = db.execute(
            "SELECT COUNT(*) FROM Waitlist WHERE session_id=?", (session_id,)).fetchone()[0]
        if started_tx:
            db.commit()
    except sqlite3.IntegrityError:
        if started_tx and db.in_transaction:
            db.rollback()
        return False, "You are already on the waitlist for this session."
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    return True, (f"Joined waitlist for {sess['title']} on {sess['session_date']} "
                  f"at {sess['start_time']} (Position #{pos}).")


def leave_waitlist(db, member_id, session_id, is_admin=False, target_member_id=None):
    """Remove a member from a session's waitlist.
    Members can only remove themselves; admins can remove anyone (via target_member_id or member_id).
    Returns (ok: bool, message: str)."""
    target = member_id if target_member_id is None else target_member_id
    if target != member_id and not is_admin:
        return False, "You can only remove yourself from the waitlist."

    started_tx = not db.in_transaction
    if started_tx:
        db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("""
            SELECT w.waitlist_id, w.member_id, m.name AS member_name,
                   s.title, s.session_date, s.start_time
            FROM Waitlist w
            JOIN Members m ON m.member_id = w.member_id
            JOIN Sessions s ON s.session_id = w.session_id
            WHERE w.member_id=? AND w.session_id=?""", (target, session_id)).fetchone()
        if row is None:
            if started_tx and db.in_transaction:
                db.rollback()
            return False, "Waitlist entry not found."
        db.execute("DELETE FROM Waitlist WHERE waitlist_id=?", (row["waitlist_id"],))
        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    if target != member_id:
        return True, (f"Removed {row['member_name']} from waitlist: {row['title']} "
                      f"on {row['session_date']} at {row['start_time']}.")
    return True, (f"Left waitlist: {row['title']} on {row['session_date']} "
                  f"at {row['start_time']}.")


def get_member_waitlist(db, member_id):
    """Return a member's waitlisted sessions with 1-based FIFO queue position."""
    rows = db.execute("""
        SELECT w.waitlist_id, w.session_id, w.created_at,
               s.title, s.trainer, s.session_date, s.start_time, s.price, s.capacity,
               (SELECT COUNT(*) FROM Bookings b
                WHERE b.session_id = s.session_id AND b.status = 'confirmed') AS taken,
               1 + (SELECT COUNT(*) FROM Waitlist w2
                    WHERE w2.session_id = w.session_id
                      AND (w2.created_at < w.created_at
                           OR (w2.created_at = w.created_at AND w2.waitlist_id < w.waitlist_id))) AS position
        FROM Waitlist w
        JOIN Sessions s ON s.session_id = w.session_id
        WHERE w.member_id = ?
        ORDER BY s.session_date, s.start_time, w.waitlist_id""", (member_id,)).fetchall()
    now, result = datetime.now(), []
    for r in rows:
        d = dict(r)
        d["is_past"] = _starts(r) <= now
        result.append(d)
    return result


def get_all_waitlist_entries(db):
    """Return all waitlist entries across sessions in FIFO order for the admin dashboard."""
    rows = db.execute("""
        SELECT w.waitlist_id, w.member_id, w.session_id, w.created_at,
               m.name AS member_name, m.email AS member_email,
               m.membership_status, m.balance,
               s.title AS session_title, s.session_date, s.start_time, s.price,
               1 + (SELECT COUNT(*) FROM Waitlist w2
                    WHERE w2.session_id = w.session_id
                      AND (w2.created_at < w.created_at
                           OR (w2.created_at = w.created_at AND w2.waitlist_id < w.waitlist_id))) AS position
        FROM Waitlist w
        JOIN Members m ON m.member_id = w.member_id
        JOIN Sessions s ON s.session_id = w.session_id
        ORDER BY s.session_date, s.start_time, w.created_at, w.waitlist_id""").fetchall()
    return [dict(r) for r in rows]


# =====================================================================
# DONE BY: Adil + Sprint 3 Refunds - BOOKING CANCELLATION
# The Bookings row is DELETED so UNIQUE(member_id, session_id) allows rebooking,
# while the Payments row is updated to status='refunded' for audit history.
# Also atomically auto-promotes the next eligible FIFO waitlisted member.
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

        promoted = _promote_waitlist_in_tx(db, b["session_id"])

        if started_tx:
            db.commit()
    except Exception:
        if started_tx and db.in_transaction:
            db.rollback()
        raise
    promo_note = ""
    if promoted:
        promo_note = f" Waitlist auto-promoted: {', '.join(p['name'] for p in promoted)}."
    return True, f"Cancelled: {b['title']} on {b['session_date']} at {b['start_time']}{refund_note}.{promo_note}"


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
        SELECT s.*,
               (SELECT COUNT(*) FROM Bookings b
                WHERE b.session_id=s.session_id AND b.status='confirmed') AS taken,
               (SELECT COUNT(*) FROM Waitlist w
                WHERE w.session_id=s.session_id) AS waitlist_count
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
# DONE BY: Iskander - ADMIN AUDIT LOG
#   Every admin action is written to the AuditLog table (who, what, which object, details).
#   The database itself blocks changing or deleting a log row (triggers in SCHEMA).
#   The admin_* wrappers below call the original functions and log only when they succeeded.
# =====================================================================
AUDIT_KINDS = {  # action prefix -> name of the filter on the audit page
    "session": "Trainings", "member": "Members", "booking": "Bookings",
    "waitlist": "Waitlist", "auth": "Sign-ins", "security": "Security alerts", "system": "System",
}
AUDIT_ACTIONS = {  # action -> (human text, badge colour class)
    "session.create": ("Created training", ""),
    "session.update": ("Changed training", "blue"),
    "session.delete": ("Deleted training", "red"),
    "member.status": ("Changed membership", "amber"),
    "member.role": ("Changed role", "amber"),
    "booking.cancel": ("Cancelled a booking", "red"),
    "waitlist.remove": ("Removed from waitlist", "amber"),
    "auth.admin_login": ("Admin sign-in", "grey"),
    "security.lockout": ("Account locked", "red"),
    "system.seed": ("Demo data loaded", "grey"),
}


def log_action(db, actor, action, target="", details=""):
    """Append one row to AuditLog. actor is a Members row/dict, or None for the system."""
    actor_id = actor["member_id"] if actor is not None else None
    actor_name = actor["name"] if actor is not None else "system"
    with db:
        db.execute("INSERT INTO AuditLog(created_at,actor_id,actor_name,action,target,details) "
                   "VALUES (?,?,?,?,?,?)",
                   (datetime.now().isoformat(timespec="seconds"), actor_id, actor_name,
                    action, target, details))


def get_audit_log(db, kind="", q="", limit=100):
    """Newest first. kind = prefix from AUDIT_KINDS ('' = all), q = text searched in admin/target/details."""
    kind = kind if kind in AUDIT_KINDS else ""
    like = f"%{(q or '').strip()}%"
    rows = db.execute("""
        SELECT * FROM AuditLog
        WHERE (? = '' OR action LIKE ? || '.%')
          AND (actor_name LIKE ? OR target LIKE ? OR details LIKE ?)
        ORDER BY log_id DESC LIMIT ?""", (kind, kind, like, like, like, max(1, int(limit)))).fetchall()
    return [dict(r) for r in rows]


def get_audit_summary(db):
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    top = db.execute("""SELECT actor_name, COUNT(*) AS n FROM AuditLog
                        GROUP BY actor_name ORDER BY n DESC, actor_name LIMIT 1""").fetchone()
    by_kind = db.execute("""SELECT substr(action, 1, instr(action, '.') - 1) AS kind, COUNT(*) AS n
                            FROM AuditLog GROUP BY kind ORDER BY n DESC""").fetchall()
    return {
        "total": db.execute("SELECT COUNT(*) FROM AuditLog").fetchone()[0],
        "last_24h": db.execute("SELECT COUNT(*) FROM AuditLog WHERE created_at >= ?", (since,)).fetchone()[0],
        "top_actor": dict(top) if top else None,
        "by_kind": [{"kind": r["kind"], "label": AUDIT_KINDS.get(r["kind"], r["kind"]), "n": r["n"]}
                    for r in by_kind],
    }


def describe_booking(db, booking_id):
    """Who owns a booking and what it is (read BEFORE the booking is cancelled)."""
    r = db.execute("""SELECT b.member_id, m.name, s.title, s.session_date
                      FROM Bookings b JOIN Members m USING(member_id) JOIN Sessions s USING(session_id)
                      WHERE b.booking_id=?""", (booking_id,)).fetchone()
    if r is None:
        return None
    return {"member_id": r["member_id"], "label": f"{r['name']}: {r['title']} {r['session_date']}"}


def admin_create_session(db, actor, title, trainer, date, time, capacity, price=DEFAULT_SESSION_PRICE):
    ok, msg = create_session(db, title, trainer, date, time, capacity, price)
    if ok:
        log_action(db, actor, "session.create", (title or "").strip(), msg)
    return ok, msg


def admin_update_session(db, actor, session_id, title, trainer, date, time, capacity, price=None):
    old = db.execute("SELECT * FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
    ok, msg = update_session(db, session_id, title, trainer, date, time, capacity, price)
    if ok:
        new = db.execute("SELECT * FROM Sessions WHERE session_id=?", (session_id,)).fetchone()
        keys = ("title", "trainer", "session_date", "start_time", "capacity", "price")
        changes = [f"{k}: {old[k]} -> {new[k]}" for k in keys if old[k] != new[k]]
        log_action(db, actor, "session.update", f"{new['title']} (#{session_id})",
                   "; ".join(changes) or "no field changed")
    return ok, msg


def admin_delete_session(db, actor, session_id):
    row = db.execute("SELECT title, session_date, start_time FROM Sessions WHERE session_id=?",
                     (session_id,)).fetchone()
    n_bookings = db.execute("SELECT COUNT(*) FROM Bookings WHERE session_id=?", (session_id,)).fetchone()[0]
    ok, msg = delete_session(db, session_id)
    if ok:
        log_action(db, actor, "session.delete", f"{row['title']} (#{session_id})",
                   f"{row['session_date']} {row['start_time']}, {n_bookings} booking(s) removed")
    return ok, msg


def admin_toggle_status(db, actor, member_id):
    m = db.execute("SELECT name FROM Members WHERE member_id=?", (member_id,)).fetchone()
    ok, msg = toggle_member_status(db, member_id)
    if ok:
        log_action(db, actor, "member.status", f"{m['name']} (#{member_id})", msg)
    return ok, msg


def admin_toggle_role(db, actor, member_id):
    m = db.execute("SELECT name FROM Members WHERE member_id=?", (member_id,)).fetchone()
    ok, msg = toggle_member_role(db, actor["member_id"], member_id)
    if ok:
        log_action(db, actor, "member.role", f"{m['name']} (#{member_id})", msg)
    return ok, msg


def cancel_booking_audited(db, actor, booking_id, is_admin):
    """cancel_booking + a log row when an admin cancels somebody else's booking."""
    info = describe_booking(db, booking_id) if is_admin else None
    ok, msg = cancel_booking(db, actor["member_id"], booking_id, is_admin=is_admin)
    if ok and info and info["member_id"] != actor["member_id"]:
        log_action(db, actor, "booking.cancel", info["label"], msg)
    return ok, msg


def leave_waitlist_audited(db, actor, session_id, target_member_id, is_admin):
    """leave_waitlist + an audit row when an admin removes another member from a waitlist."""
    target = actor["member_id"] if target_member_id is None else target_member_id
    ok, msg = leave_waitlist(db, actor["member_id"], session_id,
                             is_admin=is_admin, target_member_id=target)
    if ok and is_admin and target != actor["member_id"]:
        log_action(db, actor, "waitlist.remove", f"member #{target} / session #{session_id}", msg)
    return ok, msg


# =====================================================================
# DONE BY: Iskander - statistics for the admin (page /stats)
#   Every number comes from an SQL query with JOIN + GROUP BY, not from Python loops.
# =====================================================================
def get_stats(db):
    sessions = [dict(r) for r in db.execute("""
        SELECT s.session_id, s.title, s.trainer, s.session_date, s.start_time, s.capacity,
               COUNT(b.booking_id) AS taken,
               ROUND(100.0 * COUNT(b.booking_id) / s.capacity) AS load_pct
        FROM Sessions s
        LEFT JOIN Bookings b ON b.session_id = s.session_id AND b.status = 'confirmed'
        GROUP BY s.session_id
        ORDER BY load_pct DESC, s.session_date, s.start_time""")]
    trainers = [dict(r) for r in db.execute("""
        SELECT s.trainer, COUNT(DISTINCT s.session_id) AS sessions, COUNT(b.booking_id) AS bookings
        FROM Sessions s
        LEFT JOIN Bookings b ON b.session_id = s.session_id AND b.status = 'confirmed'
        GROUP BY s.trainer
        ORDER BY bookings DESC, s.trainer""")]
    idle_members = [dict(r) for r in db.execute("""
        SELECT m.name, m.email, m.membership_status
        FROM Members m
        LEFT JOIN Bookings b ON b.member_id = m.member_id AND b.status = 'confirmed'
        WHERE b.booking_id IS NULL
        ORDER BY m.name""")]
    totals = {
        "members": db.execute("SELECT COUNT(*) FROM Members").fetchone()[0],
        "sessions": len(sessions),
        "bookings": sum(s["taken"] for s in sessions),
        "avg_load": round(sum(s["load_pct"] for s in sessions) / len(sessions)) if sessions else 0,
    }
    return {"sessions": sessions[:12], "trainers": trainers, "idle_members": idle_members, "totals": totals}


# =====================================================================
# DONE BY: Iskander - club occupancy (chart on the main page, replaces the old date chart)
#   Estimated from the confirmed bookings, like "popular times": for every weekday and hour we count
#   the booked people and divide by CLUB_CAPACITY (people the club holds at once = 100%).
#   So 2 booked people in a club for 60 are about 3%, not "100%".
# =====================================================================
CLUB_OPEN, CLUB_CLOSE = 6, 24  # working hours of the club -> columns 6:00 ... 23:00
CLUB_CAPACITY = 50             # people the club holds at once = 10 segments (100%)
SESSION_HOURS = 1              # Sessions has no duration column: every session is assumed to last 1 hour
LOAD_STEPS = 10                # a column has 10 segments


def load_label(level):
    """Word for a load level 0..10 (shown under the title and in the tooltips)."""
    if level >= 7:
        return "High"
    if level >= 4:
        return "Medium"
    if level >= 1:
        return "Low"
    return "No bookings"


def get_club_load(db, now=None, club_capacity=None):
    """Data for the occupancy chart: levels[weekday 0=Mon][hour index] = 0..10."""
    now = now or datetime.now()
    cap = club_capacity or CLUB_CAPACITY
    hours = list(range(CLUB_OPEN, CLUB_CLOSE))
    people = [[0.0] * len(hours) for _ in range(7)]  # booked people per weekday and hour
    dates = [set() for _ in range(7)]                # dates of each weekday that have sessions
    rows = db.execute("""
        SELECT s.session_date, s.start_time, COUNT(b.booking_id) AS taken
        FROM Sessions s
        LEFT JOIN Bookings b ON b.session_id = s.session_id AND b.status = 'confirmed'
        GROUP BY s.session_id""").fetchall()
    for r in rows:
        try:
            d = datetime.strptime(r["session_date"], "%Y-%m-%d").date()
            h, m = map(int, r["start_time"].split(":"))
        except ValueError:
            continue  # a broken date/time must not break the page
        wd = d.weekday()
        dates[wd].add(d)
        start = h * 60 + m
        end = start + SESSION_HOURS * 60
        for i, hour in enumerate(hours):  # the session occupies every hour it overlaps
            if start < (hour + 1) * 60 and end > hour * 60:
                people[wd][i] += r["taken"]
    for wd in range(7):  # average over the dates of that weekday, so one busy date does not rule
        if dates[wd]:
            people[wd] = [p / len(dates[wd]) for p in people[wd]]
    levels = [[0 if p == 0 else max(1, min(LOAD_STEPS, round(LOAD_STEPS * p / cap))) for p in row]
              for row in people]
    i_now = now.hour - CLUB_OPEN
    in_hours = 0 <= i_now < len(hours)
    now_level = levels[now.weekday()][i_now] if in_hours else 0
    return {
        "hours": hours, "close": CLUB_CLOSE, "steps": LOAD_STEPS, "today": now.weekday(),
        "capacity": cap, "levels": levels,
        "labels": [[load_label(l) for l in row] for row in levels],
        "people": [[round(p, 1) for p in row] for row in people],
        "now_level": now_level,
        "now_label": load_label(now_level) if in_hours else "Closed",
    }


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
G_ISKANDER = "Iskander - database rules, audit log, statistics, club occupancy (22 tests)"  # ADDED (Iskander)
G_WAITLIST = "Waitlist - functional & edge cases (8 tests)"
G_CONCURRENCY = "Concurrency - 15 multithreaded race-condition tests"
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


# ---------- DONE BY: Iskander - database rules, audit log, statistics, club occupancy ----------
def rejected(db, sql, kind):
    """True if the database refuses this statement with an IntegrityError of the given kind."""
    try:
        db.execute(sql)
    except sqlite3.IntegrityError as e:
        return kind in str(e)
    return False


def admin_of(db):
    return db.execute("SELECT * FROM Members WHERE member_id=1").fetchone()  # Aldair


@case(G_ISKANDER)
def db_rejects_bad_membership_status():
    """CHECK: membership_status other than active/expired is rejected by the database"""
    assert rejected(fresh_db(), "INSERT INTO Members(name,email,membership_status) "
                                "VALUES ('X','x@kbtu.kz','banned')", "CHECK")


@case(G_ISKANDER)
def db_rejects_bad_role():
    """CHECK: role other than member/admin is rejected by the database"""
    assert rejected(fresh_db(), "INSERT INTO Members(name,email,role) VALUES ('X','x@kbtu.kz','boss')", "CHECK")


@case(G_ISKANDER)
def db_rejects_bad_booking_status():
    """CHECK: Bookings.status other than confirmed/cancelled is rejected"""
    assert rejected(fresh_db(), "INSERT INTO Bookings(member_id,session_id,status,booked_at) "
                                "VALUES (3,2,'maybe','x')", "CHECK")


@case(G_ISKANDER)
def db_rejects_bad_date_format():
    """CHECK: a session date that is not YYYY-MM-DD is rejected"""
    assert rejected(fresh_db(), "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity) "
                                "VALUES ('A','B','12.11.2026','10:00',5)", "CHECK")


@case(G_ISKANDER)
def db_foreign_key_blocks_unknown_member():
    """Foreign key: a booking for a member who does not exist is rejected"""
    assert rejected(fresh_db(), "INSERT INTO Bookings(member_id,session_id,status,booked_at) "
                                "VALUES (999,2,'confirmed','x')", "FOREIGN KEY")


@case(G_ISKANDER)
def db_cascade_deletes_session_and_member_bookings():
    """ON DELETE CASCADE: deleting a session or a member removes their bookings automatically"""
    db = fresh_db()
    db.execute("DELETE FROM Sessions WHERE session_id=1")
    assert taken_of(db, 1) == 0
    db = fresh_db()
    db.execute("DELETE FROM Members WHERE member_id=1")
    assert db.execute("SELECT COUNT(*) FROM Bookings WHERE member_id=1").fetchone()[0] == 0


@case(G_ISKANDER)
def db_payment_history_survives_session_deletion():
    """With foreign keys ON, deleting a training still works and the payments stay as history"""
    db = fresh_db()
    ok, msg = delete_session(db, 1)
    assert ok, msg
    pays = get_member_payments(db, 2)
    assert len(pays) == 1 and pays[0]["title"].startswith("Deleted session"), pays


@case(G_ISKANDER)
def db_extended_demo_data_is_consistent():
    """Extended demo data: valid, no overbooking, no negative balances, expired members never book"""
    db = fresh_db()
    seed_extended(db)
    assert db.execute("SELECT COUNT(*) FROM Members").fetchone()[0] >= 30
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert not db.execute("SELECT 1 FROM Members WHERE balance < 0").fetchall()
    assert not db.execute("""SELECT 1 FROM Sessions s WHERE (SELECT COUNT(*) FROM Bookings b
                             WHERE b.session_id=s.session_id) > s.capacity""").fetchall()
    assert taken_of(db, 1) == 2 and db.execute("SELECT capacity FROM Sessions WHERE session_id=1").fetchone()[0] == 20


@case(G_ISKANDER)
def audit_logs_session_create_update_delete():
    """Audit: creating, changing and deleting a training are logged with the admin and the details"""
    db = fresh_db()
    a = admin_of(db)
    assert admin_create_session(db, a, "Spin", "Dana", day(5), "18:00", 10, 9.0)[0]
    sid = db.execute("SELECT MAX(session_id) FROM Sessions").fetchone()[0]
    assert admin_update_session(db, a, sid, "Spin", "Dana", day(5), "18:00", 20, 9.0)[0]
    assert admin_delete_session(db, a, sid)[0]
    log = get_audit_log(db, kind="session")
    assert [e["action"] for e in log] == ["session.delete", "session.update", "session.create"], log
    assert "capacity: 10 -> 20" in log[1]["details"] and log[0]["actor_name"] == "Aldair"


@case(G_ISKANDER)
def audit_failed_action_is_not_logged():
    """Audit: an action that was rejected (capacity 0) leaves no log row"""
    db = fresh_db()
    ok, _ = admin_create_session(db, admin_of(db), "Bad", "T", day(5), "18:00", 0, 9.0)
    assert not ok and get_audit_summary(db)["total"] == 0


@case(G_ISKANDER)
def audit_logs_member_changes():
    """Audit: changing a membership status and a role are logged"""
    db = fresh_db()
    a = admin_of(db)
    assert admin_toggle_status(db, a, 3)[0] and admin_toggle_role(db, a, 3)[0]
    assert [e["action"] for e in get_audit_log(db, kind="member")] == ["member.role", "member.status"]


@case(G_ISKANDER)
def audit_logs_admin_cancel_of_other_members_booking_only():
    """Audit: an admin cancelling someone else's booking is logged; cancelling their own is not"""
    db = fresh_db()
    a = admin_of(db)
    assert cancel_booking_audited(db, a, booking_id_of(db, 1, 1), True)[0]  # admin's own booking
    assert get_audit_summary(db)["total"] == 0
    assert cancel_booking_audited(db, a, booking_id_of(db, 2, 1), True)[0]  # Iskander's booking
    log = get_audit_log(db)
    assert len(log) == 1 and log[0]["action"] == "booking.cancel" and "Iskander" in log[0]["target"], log


@case(G_ISKANDER)
def audit_log_is_append_only():
    """Audit: the database refuses to change or delete a log row"""
    db = fresh_db()
    log_action(db, admin_of(db), "system.test", "x", "y")
    assert rejected(db, "UPDATE AuditLog SET details='tampered'", "append-only")
    assert rejected(db, "DELETE FROM AuditLog", "append-only")


@case(G_ISKANDER)
def audit_survives_member_deletion_and_filters_work():
    """Audit: log rows stay after the admin is deleted; the type filter and the search work"""
    db = fresh_db()
    a = admin_of(db)
    admin_toggle_status(db, a, 3)
    admin_create_session(db, a, "Spin", "Dana", day(5), "18:00", 10, 9.0)
    db.execute("DELETE FROM Members WHERE member_id=1")
    assert get_audit_summary(db)["total"] == 2
    assert [e["action"] for e in get_audit_log(db, kind="member")] == ["member.status"]
    assert len(get_audit_log(db, q="Spin")) == 1 and get_audit_log(db, q="nothing-like-this") == []


@case(G_ISKANDER)
def stats_load_percent():
    """Stats: Yoga load is 100%, Boxing 0%, and Boxing becomes 33% after one booking"""
    db = fresh_db()
    load = lambda: {s["title"]: int(s["load_pct"]) for s in get_stats(db)["sessions"]}
    assert load()["Yoga"] == 100 and load()["Boxing"] == 0, load()
    book_session(db, 3, 2)
    assert load()["Boxing"] == 33, load()


@case(G_ISKANDER)
def stats_trainer_ranking():
    """Stats: the trainer with the most bookings (Aigerim, 2) is listed first"""
    t = get_stats(fresh_db())["trainers"]
    assert t[0]["trainer"] == "Aigerim" and t[0]["bookings"] == 2, t


@case(G_ISKANDER)
def stats_members_without_bookings():
    """Stats: members with no bookings are listed, and a member disappears after booking"""
    db = fresh_db()
    names = lambda: {m["name"] for m in get_stats(db)["idle_members"]}
    assert names() == {"Adil", "Zhanibek", "Expired Member"}, names()
    book_session(db, 3, 2)
    assert names() == {"Zhanibek", "Expired Member"}, names()


@case(G_ISKANDER)
def club_load_two_people_are_not_full():
    """Club load: 2 booked people in a club for CLUB_CAPACITY are level 1 (Low), not a full column"""
    db = fresh_db()
    wd = datetime.strptime(day(1), "%Y-%m-%d").weekday()  # Yoga is tomorrow at 10:00, 2 booked
    lv = get_club_load(db)["levels"][wd]
    assert lv[10 - CLUB_OPEN] == 1 and lv[11 - CLUB_OPEN] == 0, lv
    assert load_label(lv[10 - CLUB_OPEN]) == "Low"


@case(G_ISKANDER)
def club_load_scale_follows_club_capacity():
    """Club load: the same 2 people fill the whole column (10) when the club holds only 2; no bookings -> 0"""
    db = fresh_db()
    wd = datetime.strptime(day(1), "%Y-%m-%d").weekday()
    assert get_club_load(db, club_capacity=2)["levels"][wd][10 - CLUB_OPEN] == LOAD_STEPS
    db.execute("DELETE FROM Bookings")
    assert all(l == 0 for row in get_club_load(db)["levels"] for l in row)


@case(G_ISKANDER)
def club_load_session_covers_two_hours():
    """Club load: a session at 18:30 (1 hour long) fills both the 18:00 and the 19:00 columns"""
    db = fresh_db()
    d = day(4)
    db.execute("INSERT INTO Sessions(title,trainer,session_date,start_time,capacity) "
               "VALUES ('Late','T',?,'18:30',5)", (d,))
    sid = db.execute("SELECT MAX(session_id) FROM Sessions").fetchone()[0]
    db.execute("INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (3,?,'confirmed','x')", (sid,))
    lv = get_club_load(db)["levels"][datetime.strptime(d, "%Y-%m-%d").weekday()]
    at = lambda h: lv[h - CLUB_OPEN]
    assert at(18) > 0 and at(19) > 0 and at(17) == 0 and at(20) == 0, lv


@case(G_ISKANDER)
def club_load_ignores_closed_hours():
    """Club load: a session at 03:00 (club closed) does not appear in the chart and does not crash"""
    db = fresh_db()
    d = day(5)
    db.execute("INSERT INTO Sessions(title,trainer,session_date,start_time,capacity) "
               "VALUES ('Night','T',?,'03:00',5)", (d,))
    sid = db.execute("SELECT MAX(session_id) FROM Sessions").fetchone()[0]
    db.execute("INSERT INTO Bookings(member_id,session_id,status,booked_at) VALUES (3,?,'confirmed','x')", (sid,))
    assert sum(get_club_load(db)["levels"][datetime.strptime(d, "%Y-%m-%d").weekday()]) == 0


@case(G_ISKANDER)
def club_load_labels():
    """Club load: levels 0, 1-3, 4-6, 7-10 are called No bookings, Low, Medium, High"""
    assert [load_label(x) for x in (0, 1, 3, 4, 6, 7, 10)] == \
        ["No bookings", "Low", "Low", "Medium", "Medium", "High", "High"]


# ---------- WAITLIST: functional & edge cases (8 tests) ----------
@case(G_WAITLIST)
def waitlist_join_full_session_no_charge():
    """Active member joins waitlist of full session without being charged"""
    db = fresh_db()
    before = balance_of(db, 3)
    ok, msg = join_waitlist(db, 3, 1)  # Yoga (session 1) is full (2/2)
    assert ok and "position #1" in msg.lower(), msg
    assert balance_of(db, 3) == before
    wl = get_member_waitlist(db, 3)
    assert len(wl) == 1 and wl[0]["session_id"] == 1 and wl[0]["position"] == 1


@case(G_WAITLIST)
def waitlist_rejects_open_session():
    """Joining waitlist fails when session still has free spots"""
    db = fresh_db()
    ok, msg = join_waitlist(db, 3, 2)  # Boxing has 3 free spots
    assert not ok and "book directly" in msg.lower(), msg
    assert get_member_waitlist(db, 3) == []


@case(G_WAITLIST)
def waitlist_rejects_past_and_expired_and_booked():
    """Waitlist rejects past sessions, expired members, and already-booked members"""
    db = fresh_db()
    # Past session (fill it first or test directly - past check happens first)
    ok_past, msg_past = join_waitlist(db, 3, 4)
    assert not ok_past and "already started" in msg_past.lower(), msg_past
    # Expired member on full session 1
    ok_exp, msg_exp = join_waitlist(db, 5, 1)
    assert not ok_exp and "expired" in msg_exp.lower(), msg_exp
    # Already-booked member (member 1 is booked on Yoga)
    ok_dup, msg_dup = join_waitlist(db, 1, 1)
    assert not ok_dup and "already registered" in msg_dup.lower(), msg_dup


@case(G_WAITLIST)
def waitlist_leave_and_admin_remove():
    """Member can leave own waitlist; non-admin cannot remove others; admin can"""
    db = fresh_db()
    assert join_waitlist(db, 3, 1)[0]
    assert join_waitlist(db, 4, 1)[0]
    # Non-admin Adil (3) cannot remove Zhanibek (4)
    ok_deny, _ = leave_waitlist(db, 3, 1, is_admin=False, target_member_id=4)
    assert not ok_deny
    # Adil leaves his own waitlist -> Zhanibek moves to #1
    assert leave_waitlist(db, 3, 1)[0]
    wl4 = get_member_waitlist(db, 4)
    assert len(wl4) == 1 and wl4[0]["position"] == 1
    # Admin Aldair (1) removes Zhanibek (4)
    assert leave_waitlist(db, 1, 1, is_admin=True, target_member_id=4)[0]
    assert get_member_waitlist(db, 4) == []


@case(G_WAITLIST)
def waitlist_auto_promote_fifo_on_cancel():
    """Cancelling a booking auto-promotes the first FIFO waitlisted member and charges them"""
    db = fresh_db()
    assert join_waitlist(db, 3, 1)[0]  # #1 Adil ($100)
    assert join_waitlist(db, 4, 1)[0]  # #2 Zhanibek ($100)
    bid = booking_id_of(db, 2, 1)      # Iskander's Yoga ($15)
    ok, msg = cancel_booking(db, 2, bid)
    assert ok and "auto-promoted" in msg.lower() and "adil" in msg.lower(), msg
    assert taken_of(db, 1) == 2
    assert balance_of(db, 2) == 100.0  # Iskander refunded $15 (85 -> 100)
    assert balance_of(db, 3) == 85.0   # Adil charged $15 (100 -> 85)
    assert balance_of(db, 4) == 100.0  # Zhanibek still waiting
    assert get_member_waitlist(db, 3) == []
    wl4 = get_member_waitlist(db, 4)
    assert len(wl4) == 1 and wl4[0]["position"] == 1


@case(G_WAITLIST)
def waitlist_auto_promote_skips_low_balance_and_expired():
    """Auto-promotion skips low-balance and expired members (keeping them queued) and promotes next eligible"""
    db = fresh_db()
    # Add extra active member who will be #3
    assert register_member(db, "Eligible User", "elig@kbtu.kz", "1234")[0]
    m_elig = db.execute("SELECT member_id FROM Members WHERE email='elig@kbtu.kz'").fetchone()[0]
    assert join_waitlist(db, 3, 1)[0]       # #1 Adil -> will lower balance below $15
    assert join_waitlist(db, 4, 1)[0]       # #2 Zhanibek -> will expire membership
    assert join_waitlist(db, m_elig, 1)[0]  # #3 Eligible ($100, active)
    with db:
        db.execute("UPDATE Members SET balance=5.0 WHERE member_id=3")
        db.execute("UPDATE Members SET membership_status='expired' WHERE member_id=4")
    ok, msg = cancel_booking(db, 2, booking_id_of(db, 2, 1))
    assert ok and "eligible user" in msg.lower(), msg
    assert balance_of(db, 3) == 5.0
    assert balance_of(db, 4) == 100.0
    assert balance_of(db, m_elig) == 85.0
    assert taken_of(db, 1) == 2
    # Skipped members remain in waitlist queue
    assert len(get_member_waitlist(db, 3)) == 1
    assert len(get_member_waitlist(db, 4)) == 1
    assert get_member_waitlist(db, m_elig) == []


@case(G_WAITLIST)
def waitlist_delete_session_cascades_entries():
    """Deleting a session removes all its waitlist entries transactionally"""
    db = fresh_db()
    assert join_waitlist(db, 3, 1)[0]
    assert join_waitlist(db, 4, 1)[0]
    assert delete_session(db, 1)[0]
    assert db.execute("SELECT COUNT(*) FROM Waitlist WHERE session_id=1").fetchone()[0] == 0


@case(G_WAITLIST)
def waitlist_capacity_increase_auto_promotes():
    """Increasing capacity of a full upcoming session auto-promotes eligible waitlisted members"""
    db = fresh_db()
    assert join_waitlist(db, 3, 1)[0]
    assert join_waitlist(db, 4, 1)[0]
    ok, msg = update_session(db, 1, "Yoga", "Aigerim", day(1), "10:00", 3, 15.0)
    assert ok and "adil" in msg.lower(), msg
    assert taken_of(db, 1) == 3
    assert balance_of(db, 3) == 85.0
    assert get_member_waitlist(db, 3) == []
    assert len(get_member_waitlist(db, 4)) == 1


# ---------- CONCURRENCY: 15 multithreaded race-condition tests ----------
def _make_temp_db(temp_dir):
    db_path = os.path.join(temp_dir, "conc_test.db")
    with closing(connect_db(db_path)) as conn:
        seed_db(conn, extended=False)
    return db_path


def _add_test_member(conn, idx, balance=100.0, status="active"):
    cur = conn.execute(
        "INSERT INTO Members(name,email,membership_status,password_hash,role,balance) "
        "VALUES (?,?,?,?,?,?)",
        (f"Worker {idx}", f"worker{idx}@kbtu.kz", status, DEMO_PW_HASH, "member", balance))
    return cur.lastrowid


def _add_test_session(conn, title, capacity, price=15.0, days_ahead=5):
    cur = conn.execute(
        "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) "
        "VALUES (?,?,?,?,?,?)",
        (title, "Coach", day(days_ahead), "12:00", capacity, price))
    return cur.lastrowid


def _run_concurrent(tasks):
    """Run callables concurrently using a Barrier so every thread collides simultaneously."""
    barrier = threading.Barrier(len(tasks))
    results = [None] * len(tasks)
    errors = []
    err_lock = threading.Lock()

    def runner(i, fn):
        try:
            results[i] = fn(barrier)
        except Exception as exc:
            with err_lock:
                errors.append((i, type(exc).__name__, str(exc)))

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(tasks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"Thread errors: {errors}"
    return results


@case(G_CONCURRENCY)
def conc_01_last_seat_race():
    """10 threads book capacity=1 simultaneously -> strictly 1 succeeds, 1 payment deducted"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                mids = [_add_test_member(db, i, 100.0) for i in range(10)]
                sid = _add_test_session(db, "Last Seat", 1, 15.0)

        def worker(mid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return book_session(conn, mid, sid)
            return run

        res = _run_concurrent([worker(m) for m in mids])
        assert sum(1 for ok, _ in res if ok) == 1, res
        assert sum(1 for ok, _ in res if not ok) == 9, res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 1
            assert db.execute("SELECT COUNT(*) FROM Payments WHERE session_id=? AND status='paid'",
                              (sid,)).fetchone()[0] == 1
            bals = [balance_of(db, m) for m in mids]
            assert bals.count(85.0) == 1 and bals.count(100.0) == 9, bals


@case(G_CONCURRENCY)
def conc_02_multi_seat_exact_capacity_race():
    """12 threads book capacity=3 simultaneously -> strictly 3 succeed, 3 payments deducted"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                mids = [_add_test_member(db, i, 100.0) for i in range(12)]
                sid = _add_test_session(db, "Three Seats", 3, 20.0)

        def worker(mid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return book_session(conn, mid, sid)
            return run

        res = _run_concurrent([worker(m) for m in mids])
        assert sum(1 for ok, _ in res if ok) == 3, res
        assert sum(1 for ok, _ in res if not ok) == 9, res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 3
            assert db.execute("SELECT COUNT(*) FROM Payments WHERE session_id=? AND status='paid'",
                              (sid,)).fetchone()[0] == 3


@case(G_CONCURRENCY)
def conc_03_same_user_double_book_race():
    """1 member sends 5 concurrent bookings for the same session -> 1 booking, 1 charge"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                sid = _add_test_session(db, "Single User Race", 5, 20.0)

        def run(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return book_session(conn, 3, sid)

        res = _run_concurrent([run for _ in range(5)])
        assert sum(1 for ok, _ in res if ok) == 1, res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 1
            assert balance_of(db, 3) == 80.0
            assert db.execute("SELECT COUNT(*) FROM Payments WHERE member_id=3 AND session_id=?",
                              (sid,)).fetchone()[0] == 1


@case(G_CONCURRENCY)
def conc_04_double_cancel_double_refund_race():
    """5 threads cancel the same booking_id -> strictly 1 succeeds, 1 refund"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            assert book_session(db, 3, 2)[0]  # Boxing $20 -> balance $80
            bid = booking_id_of(db, 3, 2)

        def run(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return cancel_booking(conn, 3, bid)

        res = _run_concurrent([run for _ in range(5)])
        assert sum(1 for ok, _ in res if ok) == 1, res
        assert sum(1 for ok, _ in res if not ok) == 4, res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, 2) == 0
            assert balance_of(db, 3) == 100.0
            assert db.execute("SELECT COUNT(*) FROM Payments WHERE member_id=3 AND status='refunded'").fetchone()[0] == 1


@case(G_CONCURRENCY)
def conc_05_insufficient_balance_multi_session_race():
    """Member with $10 concurrently books 3 different $10 sessions -> strictly 1 succeeds, balance >= 0"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                db.execute("UPDATE Members SET balance=10.0 WHERE member_id=3")
                sids = [_add_test_session(db, f"Budget {i}", 5, 10.0) for i in range(3)]

        def worker(sid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return book_session(conn, 3, sid)
            return run

        res = _run_concurrent([worker(s) for s in sids])
        assert sum(1 for ok, _ in res if ok) == 1, res
        assert sum(1 for ok, _ in res if not ok) == 2, res
        with closing(connect_db(db_path)) as db:
            assert balance_of(db, 3) == 0.0
            assert db.execute("SELECT COUNT(*) FROM Bookings WHERE member_id=3").fetchone()[0] == 1


@case(G_CONCURRENCY)
def conc_06_concurrent_topup_no_lost_updates():
    """10 threads simultaneously top up +$10 -> balance increases by exact $100"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)

        def run(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return topup_balance(conn, 3, 10.0)

        res = _run_concurrent([run for _ in range(10)])
        assert all(ok for ok, _ in res), res
        with closing(connect_db(db_path)) as db:
            assert balance_of(db, 3) == 200.0


@case(G_CONCURRENCY)
def conc_07_waitlist_join_same_user_race():
    """5 threads for same member join waitlist of full session -> strictly 1 row"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)

        def run(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return join_waitlist(conn, 3, 1)  # Yoga (full)

        res = _run_concurrent([run for _ in range(5)])
        assert sum(1 for ok, _ in res if ok) == 1, res
        assert sum(1 for ok, _ in res if not ok) == 4, res
        with closing(connect_db(db_path)) as db:
            assert db.execute("SELECT COUNT(*) FROM Waitlist WHERE member_id=3 AND session_id=1").fetchone()[0] == 1


@case(G_CONCURRENCY)
def conc_08_waitlist_join_multiple_users_fifo():
    """8 members simultaneously join full session's waitlist -> all 8 inserted with unique sequential order"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                mids = [_add_test_member(db, i, 100.0) for i in range(8)]

        def worker(mid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return join_waitlist(conn, mid, 1)
            return run

        res = _run_concurrent([worker(m) for m in mids])
        assert all(ok for ok, _ in res), res
        with closing(connect_db(db_path)) as db:
            rows = db.execute(
                "SELECT waitlist_id, member_id FROM Waitlist WHERE session_id=1 "
                "ORDER BY created_at ASC, waitlist_id ASC").fetchall()
            assert len(rows) == 8
            assert len({r["member_id"] for r in rows}) == 8
            wids = [r["waitlist_id"] for r in rows]
            assert wids == sorted(wids) and len(set(wids)) == 8


@case(G_CONCURRENCY)
def conc_09_concurrent_cancel_and_fifo_auto_promote():
    """2 booked users cancel simultaneously with 4 waitlisted -> first 2 promoted FIFO, charged, others refunded"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                sid = _add_test_session(db, "Promo Class", 2, 15.0)
                a = _add_test_member(db, 1, 100.0)
                b = _add_test_member(db, 2, 100.0)
                w = [_add_test_member(db, i, 100.0) for i in range(3, 7)]
            assert book_session(db, a, sid)[0]
            assert book_session(db, b, sid)[0]
            ba, bb = booking_id_of(db, a, sid), booking_id_of(db, b, sid)
            for wm in w:
                assert join_waitlist(db, wm, sid)[0]

        def cancel_task(mid, bid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return cancel_booking(conn, mid, bid)
            return run

        res = _run_concurrent([cancel_task(a, ba), cancel_task(b, bb)])
        assert all(ok for ok, _ in res), res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 2
            booked = {r[0] for r in db.execute(
                "SELECT member_id FROM Bookings WHERE session_id=? AND status='confirmed'", (sid,))}
            assert booked == {w[0], w[1]}, booked
            remaining = [r[0] for r in db.execute(
                "SELECT member_id FROM Waitlist WHERE session_id=? ORDER BY created_at ASC, waitlist_id ASC", (sid,))]
            assert remaining == [w[2], w[3]], remaining
            assert balance_of(db, a) == 100.0 and balance_of(db, b) == 100.0
            assert balance_of(db, w[0]) == 85.0 and balance_of(db, w[1]) == 85.0
            assert balance_of(db, w[2]) == 100.0 and balance_of(db, w[3]) == 100.0


@case(G_CONCURRENCY)
def conc_10_cancel_vs_direct_booking_with_waitlist():
    """Cancel vs direct booking while user is waitlisted -> capacity never exceeded, balances consistent"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                sid = _add_test_session(db, "Contested Spot", 1, 15.0)
                owner = _add_test_member(db, 1, 100.0)
                waiter = _add_test_member(db, 2, 100.0)
                outsider = _add_test_member(db, 3, 100.0)
            assert book_session(db, owner, sid)[0]
            bid = booking_id_of(db, owner, sid)
            assert join_waitlist(db, waiter, sid)[0]

        def do_cancel(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return cancel_booking(conn, owner, bid)

        def do_book(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return book_session(conn, outsider, sid)

        res = _run_concurrent([do_cancel, do_book])
        assert res[0][0] is True, res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 1
            # Atomic auto-promotion inside cancel_booking ensures waiter gets the spot and outsider cannot overbook
            assert res[1][0] is False, res
            assert balance_of(db, owner) == 100.0
            assert balance_of(db, waiter) == 85.0
            assert balance_of(db, outsider) == 100.0
            assert db.execute("SELECT COUNT(*) FROM Waitlist WHERE session_id=?", (sid,)).fetchone()[0] == 0


@case(G_CONCURRENCY)
def conc_11_waitlist_leave_vs_cancel_auto_promote():
    """Waitlisted #1 leaves at exact moment of cancellation -> no crash, no double-book, consistent state"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                sid = _add_test_session(db, "Leave vs Promote", 1, 15.0)
                owner = _add_test_member(db, 1, 100.0)
                w1 = _add_test_member(db, 2, 100.0)
                w2 = _add_test_member(db, 3, 100.0)
            assert book_session(db, owner, sid)[0]
            bid = booking_id_of(db, owner, sid)
            assert join_waitlist(db, w1, sid)[0]
            assert join_waitlist(db, w2, sid)[0]

        def do_leave(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return leave_waitlist(conn, w1, sid)

        def do_cancel(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return cancel_booking(conn, owner, bid)

        res = _run_concurrent([do_leave, do_cancel])
        assert res[1][0] is True, res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 1
            assert balance_of(db, owner) == 100.0
            booked_id = db.execute("SELECT member_id FROM Bookings WHERE session_id=?", (sid,)).fetchone()[0]
            assert booked_id in (w1, w2)
            # No member is both booked and on waitlist
            overlap = db.execute("""
                SELECT COUNT(*) FROM Bookings b
                JOIN Waitlist w ON b.member_id=w.member_id AND b.session_id=w.session_id
                WHERE b.session_id=?""", (sid,)).fetchone()[0]
            assert overlap == 0
            # Winner charged $15, other unchanged, no negative balances
            other = w2 if booked_id == w1 else w1
            assert balance_of(db, booked_id) == 85.0
            assert balance_of(db, other) == 100.0


@case(G_CONCURRENCY)
def conc_12_admin_capacity_reduction_vs_booking():
    """Admin reduces capacity while 5 users book concurrently -> taken <= capacity invariant always holds"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        d5 = day(5)
        with closing(connect_db(db_path)) as db:
            with db:
                sid = _add_test_session(db, "Shrink Hall", 5, 10.0, days_ahead=5)
                init_m = _add_test_member(db, 0, 100.0)
                mids = [_add_test_member(db, i, 100.0) for i in range(1, 6)]
            assert book_session(db, init_m, sid)[0]

        def do_reduce(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return update_session(conn, sid, "Shrink Hall", "Coach", d5, "12:00", 2, 10.0)

        def do_book(mid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return book_session(conn, mid, sid)
            return run

        res = _run_concurrent([do_reduce] + [do_book(m) for m in mids])
        admin_ok = res[0][0]
        book_ok_count = sum(1 for ok, _ in res[1:] if ok)
        with closing(connect_db(db_path)) as db:
            cap = db.execute("SELECT capacity FROM Sessions WHERE session_id=?", (sid,)).fetchone()[0]
            taken = taken_of(db, sid)
            assert taken <= cap, f"Invariant violated: taken={taken} > cap={cap}"
            assert taken == 1 + book_ok_count
            if admin_ok:
                assert cap == 2 and taken <= 2
            else:
                assert cap == 5 and taken > 2


@case(G_CONCURRENCY)
def conc_13_failed_login_bruteforce_lockout():
    """8 threads submit wrong passwords simultaneously -> reliable increment & lockout without lost updates"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)

        def run(barrier):
            with closing(connect_db(db_path)) as conn:
                barrier.wait()
                return authenticate_with_status(conn, "adil@kbtu.kz", "wrongpw")

        res = _run_concurrent([run for _ in range(8)])
        assert all(m is None for m, _ in res)
        assert any("locked" in (err or "").lower() for _, err in res), res
        with closing(connect_db(db_path)) as db:
            row = db.execute("SELECT failed_attempts, locked_until FROM Members WHERE member_id=3").fetchone()
            assert row["failed_attempts"] == MAX_LOGIN_ATTEMPTS, dict(row)
            assert row["locked_until"] is not None and datetime.fromisoformat(row["locked_until"]) > datetime.now()
            m_ok, err_ok = authenticate_with_status(db, "adil@kbtu.kz", "1234")
            assert m_ok is None and "locked" in (err_ok or "").lower()


@case(G_CONCURRENCY)
def conc_14_password_reset_token_single_use_race():
    """5 threads submit the same valid reset token simultaneously -> strictly 1 succeeds, 4 fail"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            ok, token, _ = create_password_reset(db, "adil@kbtu.kz")
            assert ok and token

        def worker(idx):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return reset_password_with_token(conn, token, f"newpass{idx}")
            return run

        res = _run_concurrent([worker(i) for i in range(5)])
        winners = [i for i, (ok, _) in enumerate(res) if ok]
        assert len(winners) == 1, res
        assert sum(1 for ok, _ in res if not ok) == 4, res
        with closing(connect_db(db_path)) as db:
            used = db.execute("SELECT used FROM PasswordResets WHERE token=?", (token,)).fetchone()[0]
            assert used == 1
            assert authenticate(db, "adil@kbtu.kz", f"newpass{winners[0]}") is not None


@case(G_CONCURRENCY)
def conc_15_waitlist_promote_skips_ineligible_under_concurrency():
    """Concurrent cancels skip low-balance/expired waitlisted users and cleanly promote eligible ones"""
    with tempfile.TemporaryDirectory() as td:
        db_path = _make_temp_db(td)
        with closing(connect_db(db_path)) as db:
            with db:
                sid = _add_test_session(db, "Eligibility Race", 2, 20.0)
                a = _add_test_member(db, 1, 100.0)
                b = _add_test_member(db, 2, 100.0)
                w_poor = _add_test_member(db, 3, 5.0, "active")
                w_exp = _add_test_member(db, 4, 100.0, "active")
                w_ok1 = _add_test_member(db, 5, 100.0, "active")
                w_ok2 = _add_test_member(db, 6, 100.0, "active")
            assert book_session(db, a, sid)[0]
            assert book_session(db, b, sid)[0]
            ba, bb = booking_id_of(db, a, sid), booking_id_of(db, b, sid)
            for wm in (w_poor, w_exp, w_ok1, w_ok2):
                assert join_waitlist(db, wm, sid)[0]
            # Expire w_exp after joining waitlist
            with db:
                db.execute("UPDATE Members SET membership_status='expired' WHERE member_id=?", (w_exp,))

        def cancel_task(mid, bid):
            def run(barrier):
                with closing(connect_db(db_path)) as conn:
                    barrier.wait()
                    return cancel_booking(conn, mid, bid)
            return run

        res = _run_concurrent([cancel_task(a, ba), cancel_task(b, bb)])
        assert all(ok for ok, _ in res), res
        with closing(connect_db(db_path)) as db:
            assert taken_of(db, sid) == 2
            booked = {r[0] for r in db.execute(
                "SELECT member_id FROM Bookings WHERE session_id=? AND status='confirmed'", (sid,))}
            assert booked == {w_ok1, w_ok2}, booked
            assert balance_of(db, w_poor) == 5.0
            assert balance_of(db, w_exp) == 100.0
            assert balance_of(db, w_ok1) == 80.0
            assert balance_of(db, w_ok2) == 80.0
            assert balance_of(db, a) == 100.0 and balance_of(db, b) == 100.0
            rem = [r[0] for r in db.execute(
                "SELECT member_id FROM Waitlist WHERE session_id=? ORDER BY created_at ASC, waitlist_id ASC", (sid,))]
            assert rem == [w_poor, w_exp], rem


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
 /* Iskander: visual polish (nav pills, hover, focus, tables that scroll on a phone) */
 a{color:#2563eb;text-decoration:none}
 a:hover{text-decoration:underline}
 .nav-links{display:flex;gap:4px;flex-wrap:wrap}
 .nav-links a{padding:6px 12px;border-radius:999px;color:#374151}
 .nav-links a:hover{background:#e5e7eb;text-decoration:none}
 .nav-links a.active{background:#dbeafe;color:#1e40af;font-weight:600}
 table tr:hover td{background:#f9fafb}
 button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid #93c5fd;outline-offset:2px}
 .table-wrap{overflow-x:auto}
 .muted{color:#6b7280;font-size:13px}
 .filters{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 12px}
 .filters input[name=q]{min-width:220px}
 .empty{text-align:center;color:#6b7280;padding:18px}
 .mono{font-variant-numeric:tabular-nums;white-space:nowrap}
 /* Iskander: club occupancy card */
 .load-card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:16px 18px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
 .load-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
 .load-title{font-size:20px;color:#111827}
 .load-sub{color:#6b7280;margin-top:2px}
 .load-now{display:flex;gap:4px}
 .load-now i{width:10px;height:22px;border-radius:4px;background:#e5e5ea;display:block}
 .load-now i.on{background:#4a7f2d}
 .load-tabs{display:flex;justify-content:space-between;margin:14px 0 12px;gap:2px}
 .load-tabs button{background:none;color:#6b7280;font-weight:600;border-radius:16px;padding:8px 12px}
 .load-tabs button:hover{background:#f1f1f4}
 .load-tabs button.active{background:#f1f1f4;color:#111827}
 .load-grid{display:grid;grid-template-columns:repeat(18,1fr);gap:4px}
 .load-col{display:flex;flex-direction:column-reverse;gap:3px}
 .load-col i{height:11px;border-radius:4px;background:#e5e5ea;display:block}
 .load-col.l1 i.on{background:#b6d9a5}
 .load-col.l2 i.on{background:#96c883}
 .load-col.l3 i.on{background:#4a7f2d}
 .load-axis{position:relative;height:22px;margin-top:6px;font-size:14px}
 .load-axis span{position:absolute;transform:translateX(-50%)}
 .load-note{color:#6b7280;font-size:12px;margin-top:8px}
 @media (max-width:520px){.load-tabs button{padding:6px 7px;font-size:13px}.load-grid{gap:2px}}
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
  <span class="nav-links">
  {% if user %}
    <a href="{{ url_for('index') }}" class="{{ 'active' if request.endpoint == 'index' }}">Sessions</a>
    {% if user.role == 'admin' %}
    <a href="{{ url_for('admin') }}" class="{{ 'active' if request.endpoint == 'admin' }}">Admin</a>
    <a href="{{ url_for('stats') }}" class="{{ 'active' if request.endpoint == 'stats' }}">Stats</a>
    <a href="{{ url_for('audit_log') }}" class="{{ 'active' if request.endpoint == 'audit_log' }}">Audit log</a>
    {% endif %}
    <a href="{{ url_for('tests') }}" class="{{ 'active' if request.endpoint == 'tests' }}">Tests</a>
    <a href="{{ url_for('logout') }}">Logout</a>
  {% else %}
    <a href="{{ url_for('login') }}">Login</a> <a href="{{ url_for('register') }}">Register</a>
  {% endif %}
  </span>
</div>
{% for cat, msg in get_flashed_messages(with_categories=true) %}
  <div class="{{ cat }}">{{ msg }}</div>
{% endfor %}
"""

OCCUPANCY_WIDGET = """
<!-- Iskander: club occupancy card (data from get_club_load(), drawn by the script below) -->
<div class="load-card">
  <div class="load-head">
    <div>
      <b class="load-title">Club occupancy</b>
      <div class="load-sub">Now: {{ club_load.now_label }}
        &middot; upcoming sessions: {{ occupancy.total_booked }} of {{ occupancy.total_capacity }} places booked</div>
    </div>
    <div class="load-now" title="Occupancy now">{% for i in range(club_load.steps) %}<i class="{{ 'on' if i < club_load.now_level }}"></i>{% endfor %}</div>
  </div>
  <div class="load-tabs" id="load-tabs"></div>
  <div class="load-grid" id="load-grid"></div>
  <div class="load-axis" id="load-axis"></div>
  <div class="load-note">Estimated from confirmed bookings (average per weekday, one session = 1 hour).
    A full column = {{ club_load.capacity }} people in the club at once. Hover a column for details.</div>
</div>
<script>
(function () {
  var D = {{ club_load|tojson }};
  var DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  var tabs = document.getElementById("load-tabs");
  var grid = document.getElementById("load-grid");
  var axis = document.getElementById("load-axis");
  var current = D.today;
  function pad(h) { return (h < 10 ? "0" : "") + h + ":00"; }
  function draw() {
    grid.innerHTML = "";
    D.hours.forEach(function (h, i) {
      var lvl = D.levels[current][i];
      var col = document.createElement("div");
      col.className = "load-col " + (lvl >= 7 ? "l3" : lvl >= 4 ? "l2" : "l1");
      col.title = DAYS[current] + " " + pad(h) + ": " + D.labels[current][i] +
                  (D.people[current][i] ? " (about " + D.people[current][i] + " of " + D.capacity + " people)" : "");
      for (var s = 0; s < D.steps; s++) {
        var seg = document.createElement("i");
        if (s < lvl) { seg.className = "on"; }
        col.appendChild(seg);
      }
      grid.appendChild(col);
    });
    Array.prototype.forEach.call(tabs.children, function (b, d) { b.className = (d === current ? "active" : ""); });
  }
  DAYS.forEach(function (name, d) {
    var b = document.createElement("button");
    b.type = "button"; b.textContent = name;
    b.onclick = function () { current = d; draw(); };
    tabs.appendChild(b);
  });
  var n = D.hours.length;
  D.hours.forEach(function (h, i) {
    if (h % 3 === 0) {
      var t = document.createElement("span");
      t.textContent = h; t.style.left = ((i + 0.5) / n * 100) + "%";
      axis.appendChild(t);
    }
  });
  var end = document.createElement("span");
  end.textContent = D.close; end.style.left = "100%"; end.style.transform = "translateX(-100%)";
  axis.appendChild(end);
  draw();
})();
</script>
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
<h2>Sessions</h2>
<table>
  <tr><th>Title</th><th>Trainer</th><th>Date &amp; Time</th><th>Price</th><th>Occupancy &amp; Places</th><th></th></tr>
  {% for s in sessions %}
  <tr class="{{ 'full' if s.taken >= s.capacity }} {{ 'past' if s.is_past }}">
    <td>{{ s.title }}
      {% if s.session_id in booked_ids %}<span class="badge">&#10003; Booked</span>{% endif %}
      {% if s.session_id in waitlist_positions %}<span class="badge amber">Waitlist #{{ waitlist_positions[s.session_id] }}</span>{% endif %}
      {% if s.is_past %}<span class="badge grey">past</span>{% endif %}</td>
    <td>{{ s.trainer }}</td>
    <td>{{ s.session_date }} {{ s.start_time }}</td>
    <td><b>${{ "%.2f"|format(s.price) }}</b></td>
    <td>
      <span class="progress-track sm"><span class="progress-fill fill-{{ s.load_level }}" style="display:block;width:{{ s.occupancy_pct }}%"></span></span>
      <span class="badge {{ 'red' if s.load_level == 'high' else ('amber' if s.load_level == 'medium' else '') }}">{{ s.load_label }} ({{ s.occupancy_pct }}%)</span>
      {% if s.waitlist_count > 0 %}<span class="badge blue">Waitlist: {{ s.waitlist_count }}</span>{% endif %}
      <div style="font-size:12px;margin-top:2px" class="{{ 'low-spots' if s.capacity - s.taken <= 1 }}">
        {{ s.capacity - s.taken }} left ({{ s.taken }} / {{ s.capacity }})
      </div>
    </td>
    <td style="white-space:nowrap">
      {% if not s.is_past and s.taken >= s.capacity and s.session_id not in booked_ids %}
        {% if s.session_id in waitlist_positions %}
        <form method="post" action="{{ url_for('waitlist_leave') }}" style="margin:0">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <input type="hidden" name="session_id" value="{{ s.session_id }}">
          <button class="secondary">Leave Waitlist (#{{ waitlist_positions[s.session_id] }})</button>
        </form>
        {% else %}
        <form method="post" action="{{ url_for('waitlist_join') }}" style="margin:0">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <input type="hidden" name="session_id" value="{{ s.session_id }}">
          <button class="secondary">Join Waitlist</button>
        </form>
        {% endif %}
      {% else %}
      <form method="post" action="{{ url_for('book') }}" style="margin:0">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button name="session_id" value="{{ s.session_id }}">Book</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% else %}
  <tr><td colspan="6">No sessions found.</td></tr>
  {% endfor %}
</table>
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
<h2>My Waitlist</h2>
<table>
  <tr><th>Session</th><th>Trainer</th><th>Date &amp; Time</th><th>Price</th><th>Position</th><th>Joined at</th><th></th></tr>
  {% for w in my_waitlist %}
  <tr class="{{ 'past' if w.is_past }}">
    <td>{{ w.title }}</td>
    <td>{{ w.trainer }}</td>
    <td class="mono">{{ w.session_date }} {{ w.start_time }}</td>
    <td>${{ "%.2f"|format(w.price) }}</td>
    <td><span class="badge amber">#{{ w.position }}</span></td>
    <td class="mono">{{ w.created_at }}</td>
    <td>
      <form method="post" action="{{ url_for('waitlist_leave') }}" style="margin:0">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="session_id" value="{{ w.session_id }}">
        <button class="secondary">Leave Waitlist</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="7" class="empty">You are not on any waitlists.</td></tr>
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

<h2>Recent admin activity</h2>
<div class="table-wrap"><table>
  <tr><th>When</th><th>Admin</th><th>Action</th><th>Object</th></tr>
  {% for e in recent_audit %}
  {% set a = audit_actions.get(e.action, (e.action, 'grey')) %}
  <tr><td class="mono">{{ e.created_at.replace('T', ' ') }}</td><td>{{ e.actor_name }}</td>
      <td><span class="badge {{ a[1] }}">{{ a[0] }}</span></td><td>{{ e.target }}</td></tr>
  {% else %}<tr><td colspan="4" class="empty">Nothing yet. Create, change or delete a training to see it here.</td></tr>{% endfor %}
</table></div>
<p class="muted"><a href="{{ url_for('audit_log') }}">Open the full audit log &rarr;</a></p>

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
    <td style="white-space:nowrap">{{ s.taken }}/{{ s.capacity }} <span class="badge {{ 'red' if s.load_level == 'high' else ('amber' if s.load_level == 'medium' else '') }}">{{ s.occupancy_pct }}%</span>{% if s.waitlist_count > 0 %} <span class="badge blue">WL: {{ s.waitlist_count }}</span>{% endif %}</td>
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

<h2>Waitlist Queue</h2>
<div class="table-wrap"><table>
  <tr><th>Session</th><th>Date &amp; Time</th><th>Pos</th><th>Member</th><th>Status</th><th>Balance</th><th>Joined at</th><th></th></tr>
  {% for w in waitlist_entries %}
  <tr>
    <td>{{ w.session_title }}</td>
    <td class="mono">{{ w.session_date }} {{ w.start_time }}</td>
    <td><span class="badge amber">#{{ w.position }}</span></td>
    <td>{{ w.member_name }} <span class="muted">({{ w.member_email }})</span></td>
    <td><span class="badge {{ '' if w.membership_status == 'active' else 'red' }}">{{ w.membership_status }}</span></td>
    <td class="mono">${{ "%.2f"|format(w.balance) }}</td>
    <td class="mono">{{ w.created_at }}</td>
    <td>
      <form method="post" action="{{ url_for('waitlist_leave') }}" style="margin:0">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <input type="hidden" name="session_id" value="{{ w.session_id }}">
        <input type="hidden" name="member_id" value="{{ w.member_id }}">
        <input type="hidden" name="next" value="admin">
        <button class="danger">Remove</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="8" class="empty">No members currently on any waitlist.</td></tr>
  {% endfor %}
</table></div>

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

STATS_PAGE = HEAD + NAV + """
<h2>Statistics</h2>
<p class="muted">Every number below comes from an SQL query (JOIN + GROUP BY) on the live database.</p>
<div class="cards">
  <div class="card"><div class="card-title">Members</div><div class="card-value">{{ totals.members }}</div></div>
  <div class="card"><div class="card-title">Trainings</div><div class="card-value">{{ totals.sessions }}</div></div>
  <div class="card"><div class="card-title">Confirmed bookings</div><div class="card-value">{{ totals.bookings }}</div></div>
  <div class="card"><div class="card-title">Average training fill</div><div class="card-value">{{ totals.avg_load }}%</div></div>
</div>
<h3>Fullest trainings (top 12)</h3>
<div class="table-wrap"><table>
  <tr><th>Training</th><th>Trainer</th><th>Date</th><th>Booked</th><th style="width:30%">Fill</th></tr>
  {% for s in sessions %}
  <tr><td>{{ s.title }}</td><td>{{ s.trainer }}</td><td class="mono">{{ s.session_date }} {{ s.start_time }}</td>
      <td class="mono">{{ s.taken }} / {{ s.capacity }}</td>
      <td><div class="progress-track" style="margin:0 0 3px"><div class="progress-fill {{ 'fill-high' if s.load_pct >= 90 else ('fill-medium' if s.load_pct >= 60 else 'fill-low') }}" style="width:{{ [s.load_pct|int, 100]|min }}%"></div></div>
          <span class="muted">{{ s.load_pct|int }}%</span></td></tr>
  {% else %}<tr><td colspan="5" class="empty">No trainings.</td></tr>{% endfor %}
</table></div>
<h3>Trainers</h3>
<div class="table-wrap"><table>
  <tr><th>Trainer</th><th>Trainings</th><th>Bookings</th></tr>
  {% for t in trainers %}<tr><td>{{ t.trainer }}</td><td>{{ t.sessions }}</td><td>{{ t.bookings }}</td></tr>{% endfor %}
</table></div>
<h3>Members with no bookings</h3>
<div class="table-wrap"><table>
  <tr><th>Name</th><th>Email</th><th>Membership</th></tr>
  {% for m in idle_members %}<tr><td>{{ m.name }}</td><td>{{ m.email }}</td>
      <td><span class="badge {{ '' if m.membership_status == 'active' else 'red' }}">{{ m.membership_status }}</span></td></tr>
  {% else %}<tr><td colspan="3" class="empty">Every member has at least one booking.</td></tr>{% endfor %}
</table></div>
</body></html>
"""

AUDIT_PAGE = HEAD + NAV + """
<h2>Admin audit log</h2>
<p class="muted">Every admin action is recorded here. Rows cannot be edited or deleted: the database blocks it.</p>
<div class="cards">
  <div class="card"><div class="card-title">Events recorded</div><div class="card-value">{{ summary.total }}</div></div>
  <div class="card"><div class="card-title">Last 24 hours</div><div class="card-value">{{ summary.last_24h }}</div></div>
  <div class="card"><div class="card-title">Most active</div>
    <div class="card-value">{{ summary.top_actor.actor_name if summary.top_actor else '-' }}</div>
    <div class="muted">{{ summary.top_actor.n ~ ' event(s)' if summary.top_actor else 'no events yet' }}</div></div>
  <div class="card"><div class="card-title">By type</div>
    {% for k in summary.by_kind %}<span class="badge grey">{{ k.label }}: {{ k.n }}</span> {% else %}<span class="muted">-</span>{% endfor %}</div>
</div>
<form method="get" action="{{ url_for('audit_log') }}" class="inline filters">
  <select name="type" title="Type of action">
    <option value="">All actions</option>
    {% for key, label in kinds.items() %}<option value="{{ key }}" {{ 'selected' if key == kind }}>{{ label }}</option>{% endfor %}
  </select>
  <input name="q" placeholder="Search admin, object or details" value="{{ q }}">
  <select name="limit" title="How many rows">
    {% for n in (50, 100, 200) %}<option value="{{ n }}" {{ 'selected' if n == limit }}>Show {{ n }}</option>{% endfor %}
  </select>
  <button>Filter</button>
  <a href="{{ url_for('audit_log') }}">Reset</a>
</form>
<div class="table-wrap"><table>
  <tr><th>When</th><th>Admin</th><th>Action</th><th>Object</th><th>Details</th></tr>
  {% for e in entries %}
  {% set a = actions.get(e.action, (e.action, 'grey')) %}
  <tr>
    <td class="mono">{{ e.created_at.replace('T', ' ') }}</td>
    <td>{{ e.actor_name }}</td>
    <td><span class="badge {{ a[1] }}">{{ a[0] }}</span></td>
    <td>{{ e.target }}</td>
    <td class="muted">{{ e.details }}</td>
  </tr>
  {% else %}
  <tr><td colspan="5" class="empty">No events match. Admin actions will appear here.</td></tr>
  {% endfor %}
</table></div>
<p class="muted">Showing {{ entries|length }} newest matching event(s).</p>
</body></html>
"""

TESTS_PAGE = HEAD + NAV + """
<h2>Tests: {{ passed }} / {{ total }} passed</h2>
<p>Every test runs on an isolated temporary database (in-memory or temp file); the real <code>gym.db</code> is never changed.</p>
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
            if m["role"] == "admin":  # ADDED (Iskander): audit log
                log_action(get_db(), m, "auth.admin_login", m["email"], "Admin signed in")
            return redirect(url_for("index"))
        if err and err.startswith("Too many failed"):  # ADDED (Iskander): audit log
            log_action(get_db(), None, "security.lockout", request.form.get("email", "").strip().lower(), err)
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
    # CHANGED (Iskander): with the bigger timetable the page opens on upcoming sessions only
    first_visit = "q" not in request.args and "upcoming" not in request.args
    upcoming = True if first_visit else request.args.get("upcoming") == "1"
    sessions = search_sessions(db, q, upcoming)
    my_bookings = get_member_bookings(db, user["member_id"])
    my_waitlist = get_member_waitlist(db, user["member_id"])
    my_payments = get_member_payments(db, user["member_id"])
    occupancy = get_gym_occupancy(db)
    return render_template_string(
        PAGE, title="Sessions", user=user, q=q, upcoming=upcoming, sessions=sessions,
        my_bookings=my_bookings, my_waitlist=my_waitlist,
        waitlist_positions={w["session_id"]: w["position"] for w in my_waitlist},
        my_payments=my_payments, occupancy=occupancy,
        booked_ids={b["session_id"] for b in my_bookings},
        upcoming_count=sum(1 for b in my_bookings if not b["is_past"]),
        club_load=get_club_load(db))  # ADDED (Iskander)


@app.post("/waitlist/join")
@login_required
def waitlist_join():
    user = current_user()
    session_id = request.form.get("session_id", type=int)
    ok, msg = join_waitlist(get_db(), user["member_id"], session_id)
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("index"))


@app.post("/waitlist/leave")
@login_required
def waitlist_leave():
    user = current_user()
    is_admin = user["role"] == "admin"
    session_id = request.form.get("session_id", type=int)
    target_member_id = request.form.get("member_id", type=int)
    ok, msg = leave_waitlist_audited(get_db(), user, session_id, target_member_id, is_admin)
    flash(msg, "ok" if ok else "err")
    if is_admin and request.form.get("next") == "admin":
        return redirect(url_for("admin"))
    return redirect(url_for("index"))


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
    ok, msg = cancel_booking_audited(get_db(), user, request.form.get("booking_id", type=int), is_admin)
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
        waitlist_entries=get_all_waitlist_entries(db),
        members=get_all_members(db), occupancy=get_gym_occupancy(db),
        club_load=get_club_load(db), recent_audit=get_audit_log(db, limit=5), audit_actions=AUDIT_ACTIONS,  # ADDED (Iskander)
        pay_summary=pay_summary, recent_payments=recent_payments)


@app.post("/admin/session/create")
@admin_required
def admin_session_create():
    f = request.form
    ok, msg = admin_create_session(
        get_db(), current_user(), f.get("title"), f.get("trainer"),
        f.get("session_date"), f.get("start_time"), f.get("capacity"),
        f.get("price", DEFAULT_SESSION_PRICE))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/session/update")
@admin_required
def admin_session_update():
    f = request.form
    ok, msg = admin_update_session(
        get_db(), current_user(), f.get("session_id", type=int), f.get("title"), f.get("trainer"),
        f.get("session_date"), f.get("start_time"), f.get("capacity"),
        f.get("price"))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/session/delete")
@admin_required
def admin_session_delete():
    ok, msg = admin_delete_session(get_db(), current_user(), request.form.get("session_id", type=int))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/member/toggle-status")
@admin_required
def admin_member_toggle_status():
    ok, msg = admin_toggle_status(get_db(), current_user(), request.form.get("member_id", type=int))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


@app.post("/admin/member/toggle-role")
@admin_required
def admin_member_toggle_role():
    actor = current_user()
    ok, msg = admin_toggle_role(get_db(), actor, request.form.get("member_id", type=int))
    flash(msg, "ok" if ok else "err")
    return redirect(url_for("admin"))


# ---------- DONE BY: Iskander - statistics and audit log pages for the admin ----------
@app.get("/stats")
@admin_required
def stats():
    return render_template_string(STATS_PAGE, title="Stats", user=current_user(), **get_stats(get_db()))


@app.get("/admin/audit")
@admin_required
def audit_log():
    db = get_db()
    kind = request.args.get("type", "")
    q = request.args.get("q", "").strip()
    limit = request.args.get("limit", 100, type=int)
    limit = limit if limit in (50, 100, 200) else 100
    return render_template_string(
        AUDIT_PAGE, title="Audit log", user=current_user(), entries=get_audit_log(db, kind, q, limit),
        summary=get_audit_summary(db), kinds=AUDIT_KINDS, actions=AUDIT_ACTIONS,
        kind=kind, q=q, limit=limit)


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
    # ADDED (Iskander): python app.py --reset (recreate gym.db), python app.py --check (check the schema).
    # WERKZEUG_RUN_MAIN is set in the debug auto-reloader child, so the reset happens only once.
    if "--reset" in sys.argv and DB != ":memory:" and os.path.exists(DB) \
            and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        os.remove(DB)
        _initialized_dbs.discard(DB)
    init_db()
    if "--check" in sys.argv:
        sys.exit(check_schema())
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
