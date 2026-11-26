# Project Context: Gym Booking System

Technical reference for onboarding developers and AI assistants. All details are derived directly from the repository source files and runtime environment.

---

## 1. Project Overview & Current Functionality

**Gym Booking System** is a monolithic, server-rendered web application built with **Flask** and **SQLite**. It manages gym training schedules, member class reservations, FIFO waitlists with automatic promotion, wallet payments and refunds, club occupancy analytics, admin audit logging, SQL statistics, role-based access control, and administrative operations.

### Problem Solved
Enforces gym booking, waitlist, and billing rules—validating membership status, session timing, duplicate registrations, class capacity, and wallet balance—while providing self-service booking/cancellation/refunds/waitlisting for members, weekday-by-hour club occupancy visibility, append-only audit trails, concurrency-safe SQLite transactions, and administrative management for staff.

### Implemented & Working Features
- **Authentication, Lockout & Password Reset (`app.py`)**:
  - Email/password login with salted password hashing (`werkzeug.security`), signed cookie sessions, and self-service registration (`active` `member`, default `$100.00` balance).
  - Concurrency-safe account lockout after `5` consecutive failed login attempts for `15` minutes (`failed_attempts`, `locked_until` updated atomically inside `BEGIN IMMEDIATE`), reset automatically on successful login or password reset. Admin logins and account lockouts are logged to `AuditLog`.
  - Self-service password reset (`/forgot-password`, `/reset-password/<token>`) generating 32-byte URL-safe single-use tokens valid for `30` minutes (displayed in an on-page demo/dev notice box since no SMTP server is configured). Single-use enforcement is guarded inside `BEGIN IMMEDIATE`.
- **Booking, Payments & Refunds (`app.py`)**:
  - Sessions carry a configurable `price` (`>= 0`); members carry a wallet `balance` (default `$100.00`) and can top up via `POST /topup` (atomic `BEGIN IMMEDIATE`).
  - Atomic `BEGIN IMMEDIATE` transactions in `book_session()` validate membership status, future start time, duplicates, capacity, and sufficient balance, then deduct `price`, insert `Bookings`, log a `'paid'` entry in `Payments`, and clean up any stale `Waitlist` entry for that `(member_id, session_id)`.
  - Atomic `cancel_booking()` deletes the `Bookings` row (allowing rebooking under `UNIQUE(member_id, session_id)`), refunds the paid amount to the **booking owner's** `balance`, updates the `Payments` record to `status='refunded'` with `refunded_at`, and automatically promotes the next eligible FIFO waitlisted member in the same transaction.
  - Members view their transaction history in "My Payments & Refunds" on `/`; admins view revenue metrics (Net Revenue, Total Refunded, Gross Volume) and recent transactions on `/admin`.
- **FIFO Waitlist System & Auto-Promotion (`app.py`)**:
  - `Waitlist` table with `UNIQUE(member_id, session_id)` and `ON DELETE CASCADE` on `Members` and `Sessions`, created idempotently on both fresh and existing `gym.db` databases.
  - `join_waitlist(db, member_id, session_id)` validates active membership, future session, not already booked/waitlisted, and that the session is actually full (`taken >= capacity`; rejects open sessions telling users to book directly), inserting inside `BEGIN IMMEDIATE` with zero charge.
  - `leave_waitlist(db, member_id, session_id, is_admin=False, target_member_id=None)` allows members to leave their own waitlist and admins to remove any member (logged to `AuditLog` via `leave_waitlist_audited`).
  - **Auto-Promotion Rule**: Inside the same `BEGIN IMMEDIATE` transaction in `cancel_booking()` (and when `update_session()` increases `capacity` on an upcoming session), `_promote_waitlist_in_tx()` iterates FIFO (`ORDER BY created_at ASC, waitlist_id ASC`), **skips ineligible (expired) or low-balance members while keeping them in the waitlist queue** (preserving their FIFO position if they renew/top up later), and promotes the first eligible member by removing their `Waitlist` row, deducting `session.price`, inserting a confirmed `Bookings` row, and logging a `'paid'` row in `Payments`. Flash messages report promoted member name(s).
  - Deleting a session (`delete_session()`) transactionally cascade-deletes its `Waitlist` and `Bookings` rows while preserving `Payments` history.
  - UI on `/` displays Join Waitlist / Leave Waitlist (`#N`) buttons on full upcoming sessions, waitlist counts, and a dedicated **"My Waitlist"** section; `/admin` displays per-session waitlist counts (`WL: N`) and a **"Waitlist Queue"** management table.
- **Database Integrity, Extended Seed Data & Schema Check (Iskander)**:
  - `PRAGMA foreign_keys = ON` on all connections, `CHECK` constraints across all tables, `ON DELETE CASCADE` on `Bookings`, `PasswordResets`, `Payments(member_id)`, and `Waitlist`. `Payments.session_id` intentionally has no FK so payment history survives training deletion.
  - Core seed (`SEED_MEMBERS`, `SEED_SESSIONS`, `SEED_BOOKINGS`) for tests vs. deterministic extended two-week club schedule (`seed_extended`) for the real `gym.db` (`init_db(extended=None)` automatically disables extended seeding when `app.config["TESTING"]` is `True`).
  - CLI flags: `python app.py --check` verifies tables, columns, `UNIQUE` constraints, foreign keys, `CHECK` constraints, append-only triggers, and data invariants; `python app.py --reset` recreates `gym.db`.
- **Admin Audit Log, SQL Statistics & Club Occupancy Chart (Iskander)**:
  - Append-only `AuditLog` table protected by `BEFORE UPDATE` and `BEFORE DELETE` SQLite triggers (`audit_no_update`, `audit_no_delete`). Filterable `/admin/audit` page and recent activity widget on `/admin`.
  - `/stats` admin analytics page powered by SQL `JOIN + GROUP BY` queries (totals, fullest trainings, trainer rankings, idle members).
  - Interactive weekday-by-hour **Club occupancy** card (`get_club_load()`) rendered on `/` and `/admin` alongside per-session occupancy progress bars.
- **RBAC & Security Hardening (`app.py`)**:
  - `@login_required`, `@admin_required`, `/cancel`, and `/waitlist/leave` verify live DB user existence and `role` on every request (preventing stale cookie privilege escalation and auto-clearing sessions of deleted users).
  - Admin "Members & Permissions" table allows toggling `membership_status` (`active` / `expired`) and `role` (`member` / `admin`), blocking demotion of the last remaining admin.
  - Per-session CSRF tokens validated via `@app.before_request` on all `POST` routes (including `/waitlist/join` and `/waitlist/leave`).
  - **SQLite Concurrency Hardening**: `connect_db()` configures `timeout=15.0`, `PRAGMA busy_timeout = 15000`, `PRAGMA foreign_keys = ON`, and `PRAGMA journal_mode = WAL` for file-backed DBs. All mutating critical paths (`book_session`, `cancel_booking`, `join_waitlist`, `leave_waitlist`, `topup_balance`, `update_session`, `delete_session`, `authenticate_with_status`, `reset_password_with_token`) serialize cleanly via `BEGIN IMMEDIATE`.
- **Testing & Deployment Readiness (`app.py`, `test_app.py`, `render.yaml`, `Procfile`, `DEPLOY.md`)**:
  - **98/98** in-app tests on `/tests` across 7 groups: `G_ALDAIR` (20), `G_ADIL` (4), `G_TOREZHAN` (10), `G_SPRINT3` (19), `G_ISKANDER` (22), `G_WAITLIST` (8), and `G_CONCURRENCY` (15 multithreaded race-condition tests on isolated temp file-backed SQLite DBs).
  - **54/54** `unittest` tests in `test_app.py`: 39 HTTP integration/functional tests (including 8 new Waitlist & CSRF tests) + 15 multithreaded `ConcurrencyTests` using `threading.Thread` + `threading.Barrier`.
  - Environment-driven configuration, WSGI-safe `ensure_db_initialized()`, `requirements.txt`, `Procfile`, `render.yaml`, `.gitignore`, and `DEPLOY.md`.

---

## 2. Tech Stack

| Layer | Technology | Exact Version | Source / Notes |
|---|---|---|---|
| Language / Runtime | Python | `3.13.1` | Active `.venv` (`render.yaml` pins `3.13.1`). Unused MSYS2 Python `3.12.7` venv also exists locally at `venv/`. |
| Web Framework | Flask | `3.1.3` | `requirements.txt` & `.venv` |
| Production WSGI Server | Gunicorn | `23.0.0` | `requirements.txt`, `Procfile`, `render.yaml` |
| WSGI / Password Hashing | Werkzeug | `3.1.9` | Flask dependency; used for `generate_password_hash` / `check_password_hash` |
| Templating | Jinja2 | `3.1.6` | Flask dependency; inline templates via `render_template_string` |
| Database | SQLite (`sqlite3`) | `3.45.3` | Python stdlib; raw parameterized SQL, WAL mode, `busy_timeout=15000`, `BEGIN IMMEDIATE` |
| Frontend | HTML5, inline CSS, vanilla JS | N/A | Zero external CDN dependencies, no bundler/build step |
| Testing | `unittest` + `threading.Barrier` + custom `@case` runner | Python `3.13.1` | `test_app.py` (54 tests) + `app.py` (`/tests`, 98 tests) |

---

## 3. Project Structure

Excluded from tree: `.venv/`, `venv/`, `__pycache__/`.

```text
bis/
├── app.py              # Monolithic Flask app (~3,932 lines): env config, 7-table SCHEMA with
│                       # CHECK/FK/triggers, WAL/busy_timeout DB helpers, CSRF/RBAC/lockout/reset,
│                       # payments/refunds, booking/cancellation, FIFO waitlist & auto-promotion,
│                       # audit log, SQL stats, club occupancy chart, 98 in-app tests (incl. 15
│                       # multithreaded concurrency tests), inline templates, 20 Flask routes.
├── test_app.py         # 54 unittest tests (~1,176 lines): 39 HTTP integration/waitlist tests +
│                       # 15 multithreaded Barrier concurrency tests on isolated temp SQLite DBs.
├── gym.db              # Local SQLite database file (gitignored).
├── requirements.txt    # Python dependencies (Flask==3.1.3, gunicorn==23.0.0).
├── Procfile            # WSGI process definition (web: gunicorn app:app).
├── render.yaml         # Render.com Blueprint infrastructure-as-code manifest.
├── .gitignore          # Ignores virtualenvs, bytecode, *.db, and .env files.
├── DEPLOY.md           # Step-by-step Render.com deployment guide.
└── PROJECT_CONTEXT.md  # This documentation file.
```

---

## 4. Architecture & Data Flow

### System Interaction Pattern
- **Single-process monolith**: `app.py` keeps persistence, domain logic, inline Jinja2 templates, in-app test suite, and routes in one module.
- **Pure DB-accepting service functions**: Domain functions accept a `sqlite3.Connection` (`db`) and return `(ok: bool, message: str)` tuples (or structured dicts), allowing identical code paths for HTTP routes, `/tests`, and multithreaded concurrency tests.
- **Request lifecycle**:
  1. `@app.before_request` (`before_request_guards`) ensures SQLite schema/migrations are initialized via thread-safe `ensure_db_initialized()` and validates CSRF tokens on `POST` requests.
  2. `get_db()` lazily opens a per-request hardened SQLite connection via `connect_db(DB)` (`flask.g.db`, `sqlite3.Row`, `foreign_keys=ON`, `busy_timeout=15000`, `journal_mode=WAL`); `close_db()` closes it on teardown.
  3. Mutating routes follow **Post-Redirect-Get (PRG)** with `flask.flash(msg, "ok" | "err")`.

### Routing & Endpoints

| Method | Path | Auth | Inputs (Query / Form) | Behavior / Response |
|---|---|---|---|---|
| `GET`, `POST` | `/login` | Public | Form: `email`, `password`, `csrf_token` | Authenticates with atomic 5-attempt lockout (`authenticate_with_status`); logs admin sign-ins and lockouts to `AuditLog`. |
| `GET`, `POST` | `/register` | Public | Form: `name`, `email`, `password`, `csrf_token` | Registers active member (`$100.00` balance); redirects `/login` on success. |
| `GET`, `POST` | `/forgot-password` | Public | Form: `email`, `csrf_token` | Generates 30-min reset token (`create_password_reset`); renders demo reset URL box. |
| `GET`, `POST` | `/reset-password/<token>` | Public | Path: `token`; Form: `password`, `csrf_token` | Atomically consumes single-use token, updates password hash, clears lockout, redirects `/login`. |
| `GET` | `/logout` | Public | None | Calls `session.clear()`, redirects `/login`. |
| `GET` | `/` | `@login_required` | Query: `q`, `upcoming=1` | Renders `PAGE` with `Club occupancy` chart, wallet/top-up bar, sessions table (Book / Join Waitlist / Leave Waitlist `#N`), `My bookings`, `My Waitlist`, and `My Payments & Refunds`. |
| `POST` | `/topup` | `@login_required` | Form: `amount`, `csrf_token` | Atomically credits logged-in member's `balance` via `topup_balance()`; redirects `/`. |
| `POST` | `/book` | `@login_required` | Form: `session_id`, `csrf_token` | Atomically books & charges `current_user()["member_id"]` via `book_session()`; redirects `/`. |
| `POST` | `/cancel` | `@login_required` | Form: `booking_id`, optional `next=admin`, `csrf_token` | Live-DB RBAC check; cancels booking, refunds owner, atomically auto-promotes next eligible FIFO waitlisted member, logs admin cancels to `AuditLog`. |
| `POST` | `/waitlist/join` | `@login_required` | Form: `session_id`, `csrf_token` | Atomically adds logged-in active member to FIFO waitlist of a full upcoming session (`join_waitlist()`); redirects `/`. |
| `POST` | `/waitlist/leave` | `@login_required` | Form: `session_id`, optional `member_id`, `next=admin`, `csrf_token` | Removes member from waitlist (`leave_waitlist_audited()`); non-admins can only remove themselves, admins can remove anyone (audited). |
| `GET` | `/admin` | `@admin_required` | None | Renders `ADMIN_PAGE` with `Club occupancy`, revenue overview, recent audit activity, session CRUD (with waitlist counts), Members & Permissions, `All bookings`, `Waitlist Queue`, and `Recent Payments & Refunds`. |
| `POST` | `/admin/session/create` | `@admin_required` | Form: `title`, `trainer`, `session_date`, `start_time`, `capacity`, `price`, `csrf_token` | Creates session + audit log; redirects `/admin`. |
| `POST` | `/admin/session/update` | `@admin_required` | Form: `session_id`, `title`, `trainer`, `session_date`, `start_time`, `capacity`, optional `price`, `csrf_token` | Atomically updates session (enforces `capacity >= confirmed`, auto-promotes waitlist if capacity increased) + audit log; redirects `/admin`. |
| `POST` | `/admin/session/delete` | `@admin_required` | Form: `session_id`, `csrf_token` | Transactionally deletes waitlist entries, bookings, and session (preserving `Payments`) + audit log; redirects `/admin`. |
| `POST` | `/admin/member/toggle-status` | `@admin_required` | Form: `member_id`, `csrf_token` | Toggles `'active'` / `'expired'` + audit log; redirects `/admin`. |
| `POST` | `/admin/member/toggle-role` | `@admin_required` | Form: `member_id`, `csrf_token` | Toggles `'member'` / `'admin'` (blocks demoting last admin) + audit log; redirects `/admin`. |
| `GET` | `/stats` | `@admin_required` | None | Renders `STATS_PAGE` with SQL `JOIN + GROUP BY` statistics. |
| `GET` | `/admin/audit` | `@admin_required` | Query: `type`, `q`, `limit` | Renders filterable `AUDIT_PAGE` from append-only `AuditLog`. |
| `GET` | `/tests` | `@login_required` | None | Runs all 98 tests via `run_all_tests()` (in-memory & isolated temp DBs) and renders `TESTS_PAGE`. |

---

## 5. Data Models & Database

SQLite schema defined in `SCHEMA` (`app.py`): 7 tables (`Members`, `Sessions`, `Bookings`, `Payments`, `PasswordResets`, `AuditLog`, `Waitlist`) plus 2 append-only triggers on `AuditLog`.

### Tables Summary
- **`Members`**: `member_id`, `name`, `email (UNIQUE)`, `membership_status (CHECK active/expired)`, `password_hash`, `role (CHECK member/admin)`, `balance`, `failed_attempts`, `locked_until`.
- **`Sessions`**: `session_id`, `title`, `trainer`, `session_date (GLOB YYYY-MM-DD)`, `start_time (GLOB HH:MM)`, `capacity (CHECK > 0)`, `price (CHECK >= 0)`.
- **`Bookings`**: `booking_id`, `member_id (FK CASCADE)`, `session_id (FK CASCADE)`, `status (CHECK confirmed/cancelled)`, `booked_at`, `UNIQUE(member_id, session_id)`.
- **`Payments`**: `payment_id`, `member_id (FK CASCADE)`, `session_id` (no FK by design so history survives training deletion), `amount (CHECK >= 0)`, `status (CHECK paid/refunded)`, `created_at`, `refunded_at`.
- **`PasswordResets`**: `token (PK)`, `member_id (FK CASCADE)`, `expires_at`, `used`.
- **`AuditLog`**: `log_id`, `created_at`, `actor_id` (no FK), `actor_name`, `action (CHECK LIKE '%.%')`, `target`, `details`; protected by `audit_no_update` and `audit_no_delete` triggers.
- **`Waitlist`**: `waitlist_id (PK)`, `member_id (NOT NULL FK Members CASCADE)`, `session_id (NOT NULL FK Sessions CASCADE)`, `created_at (TEXT NOT NULL)`, `UNIQUE(member_id, session_id)`.

### Concurrency Test Coverage (15 Multithreaded Tests)
Both `test_app.ConcurrencyTests` and `app.py` (`G_CONCURRENCY`) run 15 multithreaded race-condition tests using `threading.Thread` + `threading.Barrier` on isolated temporary file-backed SQLite DBs in WAL mode:
1. Last-seat race (10 threads, capacity=1)
2. Multi-seat exact capacity race (12 threads, capacity=3)
3. Same user double-book race (5 threads, 1 member)
4. Double-cancel / double-refund race (5 threads, 1 booking)
5. Concurrent booking + insufficient balance race (3 sessions, balance=$10)
6. Concurrent top-up race (10 threads x +$10, zero lost updates)
7. Concurrent waitlist join — same user (5 threads -> 1 row)
8. Concurrent waitlist join — 8 users FIFO (all 8 inserted, sequential IDs)
9. Concurrent cancel + waitlist FIFO auto-promotion (2 cancels, 4 waitlisted)
10. Concurrent cancel + direct booking vs waitlist (capacity invariant & waiter priority)
11. Concurrent waitlist leave + cancel auto-promote (no double-book or negative balance)
12. Concurrent admin capacity reduction vs booking (`taken <= capacity` always holds)
13. Concurrent failed login brute-force lockout (8 threads -> exact lockout, zero lost updates)
14. Concurrent password reset token reuse (5 threads -> strictly 1 succeeds, 4 fail; tested at both DB and HTTP `/reset-password/<token>` levels)
15. Waitlist auto-promotion skips low-balance/expired users under concurrency (promotes eligible next-in-line without charging ineligible users)

---

## 6. Setup & Environment

### Environment Variables
- `FLASK_SECRET_KEY` — Flask session & CSRF signing key (set to a strong random value in production).
- `DATABASE_PATH` — Path to SQLite database file (default: `gym.db` next to `app.py`).
- `PORT` — HTTP port when running `app.py` directly (default: `5000`).
- `FLASK_DEBUG` — Enables Flask debug mode (`"1"` / `"true"` / `"yes"`; default `"1"` locally, `"0"` in `render.yaml`).
- `HOST` — Bind interface when running `app.py` directly (default: `127.0.0.1`).

### Local Setup, Run & Test Commands (Windows PowerShell)

```powershell
# Activate existing virtual environment
.\.venv\Scripts\Activate.ps1

# Run locally (auto-migrates/seeds gym.db; opens on http://127.0.0.1:5000)
.\.venv\Scripts\python.exe app.py

# Recreate gym.db with fresh extended demo data and/or verify schema rules
.\.venv\Scripts\python.exe app.py --reset --check

# Run all 54 unittest tests (39 HTTP + 15 multithreaded concurrency tests)
.\.venv\Scripts\python.exe -m unittest -v test_app

# Run only the 15 multithreaded concurrency tests
.\.venv\Scripts\python.exe -m unittest -v test_app.ConcurrencyTests

# Run all 98 in-app tests via CLI (or visit http://127.0.0.1:5000/tests after logging in)
.\.venv\Scripts\python.exe -c "import app; g=app.run_all_tests(); print(sum(x['passed'] for x in g), '/', sum(len(x['results']) for x in g))"
```

For production deployment on Render.com (Blueprint or manual Gunicorn setup), see `DEPLOY.md`.

---

## 7. Current State & Known Gaps

### Demo / Simulated Behavior
- **Simulated Payments & Top-Up**: Wallet top-ups (`POST /topup`) and session payments/auto-promotion charges are simulated internally in SQLite; no real payment gateway is integrated.
- **No SMTP Delivery for Password Reset**: `/forgot-password` displays the reset URL in an on-page demo/dev notice box rather than emailing it.
- **Demo Credentials**: Default demo accounts are seeded when `Members` is empty, and `MIN_PASSWORD = 4` is a demo threshold (raise to `8+` in production).
- **Local `gym.db` Dates**: Delete `gym.db` or run `python app.py --reset` to seed fresh relative dates and the latest schema constraints/triggers.

### Remaining Security & Architectural Limitations
- **GET-based logout**: `GET /logout` clears session state via `GET` rather than `POST` + CSRF token.
- **No refund cascade on session deletion**: `delete_session()` removes bookings and waitlist entries without automatically refunding future paid bookings for that session.
- **No IP-based rate limiting**: Lockout protects individual accounts (`5` failures / `15` min), but there is no IP throttling on `/login`, `/register`, or `/forgot-password`.
- **`/tests` accessible to all authenticated members**: Any logged-in member can visit `/tests`, which synchronously executes 98 tests (including 15 multithreaded concurrency tests) per request.
- **Ephemeral SQLite on Render Free Tier**: Unless configured with a Render Persistent Disk or PostgreSQL, SQLite data resets across deploys/restarts (documented in `DEPLOY.md`).
- **Monolithic file structure**: Templates, CSS, service logic, tests, and routes remain in a single `app.py` file (~3,932 lines), and an unused local `venv/` folder sits alongside `.venv/`.
