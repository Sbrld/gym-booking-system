# Project Context: Gym Booking System

Technical reference for onboarding developers and AI assistants. All details are derived directly from the repository source files and runtime environment.

---

## 1. Project Overview & Current Functionality

**Gym Booking System** is a monolithic, server-rendered web application built with **Flask** and **SQLite**. It manages gym training schedules, member class reservations, wallet payments and refunds, visual occupancy analytics, role-based access control, and administrative operations.

### Problem Solved
Enforces gym booking and billing rules—validating membership status, session timing, duplicate registrations, class capacity, and wallet balance—while providing self-service booking/cancellation/refunds for members, occupancy visibility, security protections, and administrative management for staff.

### Implemented & Working Features
- **Authentication, Lockout & Password Reset (`app.py:231-410, 1802-1865`)**:
  - Email/password login with salted password hashing (`werkzeug.security`), signed cookie sessions, and self-service registration (`active` `member`, default `$100.00` balance).
  - Account lockout after `5` consecutive failed login attempts for `15` minutes (`failed_attempts`, `locked_until`), reset automatically on successful login or password reset.
  - Self-service password reset (`/forgot-password`, `/reset-password/<token>`) generating 32-byte URL-safe single-use tokens valid for `30` minutes (displayed in a demo/dev notice box since no SMTP server is configured).
- **Booking, Payments & Refunds (`app.py:533-717, 1887-1918`)**:
  - Sessions carry a configurable `price` (`>= 0`); members carry a wallet `balance` (default `$100.00`) and can top up via `POST /topup`.
  - Atomic `BEGIN IMMEDIATE` transactions in `book_session()` validate membership status, future start time, duplicates, capacity, and sufficient balance, then deduct `price`, insert `Bookings`, and record a `'paid'` entry in `Payments`.
  - Atomic `cancel_booking()` deletes the `Bookings` row (allowing rebooking under `UNIQUE(member_id, session_id)`), refunds the paid amount to the **booking owner's** `balance`, and updates the `Payments` record to `status='refunded'` with `refunded_at`.
  - Members view their transaction history in "My Payments & Refunds" on `/`; admins view revenue metrics (Net Revenue, Total Refunded, Gross Volume) and recent transactions on `/admin`.
- **RBAC & Member Permissions (`app.py:238-268, 481-527, 1969-1985`)**:
  - `@login_required`, `@admin_required`, and `/cancel` verify live DB user existence and `role` on every request (preventing stale cookie privilege escalation and auto-clearing sessions of deleted users).
  - Admin "Members & Permissions" table allows toggling `membership_status` (`active` / `expired`) and `role` (`member` / `admin`), blocking demotion of the last remaining admin.
- **Visual Gym Occupancy / Load Dashboard (`app.py:724-788, 1465-1513`)**:
  - Pure HTML/CSS/SVG occupancy dashboard rendered on both `/` and `/admin` (zero external CDN dependencies).
  - Displays overall upcoming occupancy percentage, booked/capacity counts, color-coded progress bar (green `< 60%` Low, amber `60-89%` Medium, red `>= 90%` High/Full), per-session visual load bars in the sessions table, and a compact SVG bar chart grouped by upcoming date.
- **Session Discovery & Admin CRUD (`app.py:416-478, 735-805, 1869-1967`)**:
  - Case-insensitive search by title/trainer, `Upcoming only` filter, `✓ Booked` / `past` badges, and inline Admin session create/edit/delete (including editable `price` and capacity-floor protection).
- **Security Hardening (`app.py:203-225`)**:
  - Per-session CSRF tokens on all `POST` forms via `@app.before_request` (automatically bypassed when `app.config["TESTING"]` is `True` unless `ENFORCE_CSRF_IN_TESTS` is enabled).
  - Atomic `BEGIN IMMEDIATE` write transactions in `book_session()`, `cancel_booking()`, and `reset_password_with_token()`.
- **Testing & Deployment Readiness (`app.py:812-1381`, `test_app.py`, `render.yaml`, `Procfile`, `DEPLOY.md`)**:
  - **53/53** in-app tests pass on `/tests` (34 original + 19 Sprint 3 tests covering payments, refunds, RBAC, lockout, password reset, and occupancy).
  - **31/31** HTTP integration tests pass in `test_app.py` (20 original + 11 Sprint 3 tests covering all new routes, stale-cookie RBAC, deleted-user handling, lockout, password reset, CSRF, and occupancy UI).
  - Environment-driven configuration, WSGI-safe automatic DB initialization (`ensure_db_initialized()`), idempotent SQLite migrations, `requirements.txt`, `Procfile`, `render.yaml`, `.gitignore`, and `DEPLOY.md`.

---

## 2. Tech Stack

| Layer | Technology | Exact Version | Source / Notes |
|---|---|---|---|
| Language / Runtime | Python | `3.13.1` | Active `.venv` (`render.yaml` pins `3.13.1`). Unused MSYS2 Python `3.12.7` venv also exists locally at `venv/`. |
| Web Framework | Flask | `3.1.3` | `requirements.txt` & `.venv` |
| Production WSGI Server | Gunicorn | `23.0.0` | `requirements.txt`, `Procfile`, `render.yaml` |
| WSGI / Password Hashing | Werkzeug | `3.1.9` | Flask dependency; used for `generate_password_hash` / `check_password_hash` |
| Templating | Jinja2 | `3.1.6` | Flask dependency; inline templates via `render_template_string` |
| HTML Escaping / Signing | MarkupSafe / itsdangerous | `3.0.4` / `2.2.0` | Flask transitive dependencies |
| CLI / Signals | click / blinker | `8.5.1` / `1.9.0` | Flask transitive dependencies |
| Database | SQLite (`sqlite3`) | `3.45.3` | Python stdlib; raw parameterized SQL (no ORM) |
| Frontend | HTML5, inline CSS, inline SVG | N/A | Zero external CDN dependencies, no bundler/build step |
| Testing | `unittest` (stdlib) + custom `@case` runner | Python `3.13.1` | `test_app.py` (HTTP) + `app.py` (`/tests` in-memory logic) |

---

## 3. Project Structure

Excluded from tree: `.venv/`, `venv/`, `__pycache__/`.

```text
bis/
├── app.py              # Monolithic Flask app (2,003 lines): env config, DDL/migrations,
│                       # CSRF/RBAC/lockout/reset security, payments/refunds, booking logic,
│                       # occupancy analytics, 53 in-app tests, inline HTML/CSS/SVG templates, routes.
├── test_app.py         # 31 HTTP integration tests via unittest + Flask test_client (515 lines).
├── gym.db              # Local SQLite database file (gitignored).
├── requirements.txt    # Python dependencies (Flask==3.1.3, gunicorn==23.0.0).
├── Procfile            # WSGI process definition (web: gunicorn app:app).
├── render.yaml         # Render.com Blueprint infrastructure-as-code manifest.
├── .gitignore          # Ignores virtualenvs, bytecode, *.db, and .env files.
├── DEPLOY.md           # Step-by-step Render.com deployment guide.
└── PROJECT_CONTEXT.md  # This documentation file.
```

### Internal Layout of `app.py`
| Line Range | Section | Responsibilities |
|---|---|---|
| `1–100` | Config & Schema | Env-driven config (`DB`, `app.secret_key`, constants), WSGI init lock, 5-table `SCHEMA` DDL. |
| `103–197` | DB Helpers & Migrations | `get_db`, `close_db`, `day`, idempotent `seed_db` (column migrations + demo seed), `init_db`, `ensure_db_initialized`, `_starts`. |
| `203–225` | Request Guards | `csrf_token`, `inject_security_helpers`, `before_request_guards` (DB init + CSRF verification). |
| `231–410` | Auth, RBAC & Reset | `current_user`, `@login_required`, `@admin_required`, `authenticate_with_status`, `authenticate`, `register_member`, `create_password_reset`, `validate_reset_token`, `reset_password_with_token`. |
| `416–527` | Admin CRUD & RBAC | `_check_session_fields`, `create_session`, `update_session`, `delete_session`, `get_all_members`, `toggle_member_status`, `toggle_member_role`. |
| `533–585` | Payments & Wallet | `topup_balance`, `get_member_payments`, `get_payment_overview`. |
| `592–717` | Booking & Cancellation | Atomic `book_session` (with payment deduction) and `cancel_booking` (with owner refund & payment status update). |
| `724–805` | Search & Occupancy | `_load_meta`, `search_sessions`, `get_gym_occupancy`, `get_member_bookings`. |
| `812–1381` | In-App Test Suite | 4 test groups (`G_ALDAIR` 20, `G_ADIL` 4, `G_TOREZHAN` 10, `G_SPRINT3` 19 = 53 total), `fresh_db`, `run_all_tests`. |
| `1387–1796` | Inline Templates | `STYLE`, `HEAD`, `NAV`, `OCCUPANCY_WIDGET`, `PAGE`, `LOGIN_PAGE`, `REGISTER_PAGE`, `FORGOT_PASSWORD_PAGE`, `RESET_PASSWORD_PAGE`, `ADMIN_PAGE`, `TESTS_PAGE`. |
| `1802–2003` | Routes & Entrypoint | 16 Flask route handlers and `__main__` entrypoint reading `HOST`, `PORT`, `FLASK_DEBUG`. |

---

## 4. Architecture & Data Flow

### System Interaction Pattern
- **Single-process monolith**: `app.py` keeps persistence, domain logic, inline Jinja2 templates, and routes in one module.
- **Pure DB-accepting service functions**: Domain functions accept a `sqlite3.Connection` (`db`) and return `(ok: bool, message: str)` tuples (or data dicts), allowing identical code paths for HTTP routes and `/tests` in-memory fixtures.
- **Request lifecycle**:
  1. `@app.before_request` (`before_request_guards`, `app.py:217-225`) ensures the SQLite schema/migrations are initialized via thread-safe `ensure_db_initialized()` and validates CSRF tokens on `POST` requests.
  2. `get_db()` (`app.py:103-107`) lazily opens a per-request SQLite connection (`flask.g.db`, `sqlite3.Row`); `close_db()` closes it on teardown.
  3. Mutating routes follow **Post-Redirect-Get (PRG)** with `flask.flash(msg, "ok" | "err")`.

### Routing & Endpoints

All endpoints serve HTML (`text/html`) or `302` redirects. No external APIs or JSON endpoints exist.

| Method | Path | Auth | Inputs (Query / Form) | Behavior / Response |
|---|---|---|---|---|
| `GET`, `POST` | `/login` | Public | Form: `email`, `password`, `csrf_token` | Authenticates with 5-attempt lockout check (`authenticate_with_status`); on success clears session, sets `member_id`/`role`, rotates CSRF token, redirects `/`. |
| `GET`, `POST` | `/register` | Public | Form: `name`, `email`, `password`, `csrf_token` | Registers active member (`$100.00` balance); redirects `/login` on success. |
| `GET`, `POST` | `/forgot-password` | Public | Form: `email`, `csrf_token` | Generates 30-min reset token (`create_password_reset`); renders on-page demo reset URL box if account exists. |
| `GET`, `POST` | `/reset-password/<token>` | Public | Path: `token`; Form: `password`, `csrf_token` | Validates token (`validate_reset_token`); updates password hash, marks token used, clears lockout, redirects `/login`. |
| `GET` | `/logout` | Public | None | Calls `session.clear()`, redirects `/login`. |
| `GET` | `/` | `@login_required` | Query: `q`, `upcoming=1` | Renders `PAGE` with `OCCUPANCY_WIDGET`, wallet/top-up bar, filtered sessions with load bars, `My bookings`, and `My Payments & Refunds`. |
| `POST` | `/topup` | `@login_required` | Form: `amount`, `csrf_token` | Credits logged-in member's `balance` via `topup_balance()`; redirects `/`. |
| `POST` | `/book` | `@login_required` | Form: `session_id`, `csrf_token` | Atomically books & charges `current_user()["member_id"]` via `book_session()`; redirects `/`. |
| `POST` | `/cancel` | `@login_required` | Form: `booking_id`, optional `next=admin`, `csrf_token` | Uses live DB `user["role"] == "admin"`; cancels booking and refunds booking owner via `cancel_booking()`; redirects `/admin` (if admin + `next=admin`) or `/`. |
| `GET` | `/admin` | `@admin_required` | None | Renders `ADMIN_PAGE` with `OCCUPANCY_WIDGET`, revenue overview cards, session CRUD table (with `price`), Members & Permissions table, `All bookings`, and `Recent Payments & Refunds`. |
| `POST` | `/admin/session/create` | `@admin_required` | Form: `title`, `trainer`, `session_date`, `start_time`, `capacity`, `price`, `csrf_token` | Creates session via `create_session()`; redirects `/admin`. |
| `POST` | `/admin/session/update` | `@admin_required` | Form: `session_id`, `title`, `trainer`, `session_date`, `start_time`, `capacity`, optional `price`, `csrf_token` | Updates session via `update_session()` (preserves existing price if omitted; blocks capacity `< confirmed`); redirects `/admin`. |
| `POST` | `/admin/session/delete` | `@admin_required` | Form: `session_id`, `csrf_token` | Transactionally deletes bookings and session (`delete_session()`); redirects `/admin`. |
| `POST` | `/admin/member/toggle-status` | `@admin_required` | Form: `member_id`, `csrf_token` | Toggles target member between `'active'` and `'expired'`; redirects `/admin`. |
| `POST` | `/admin/member/toggle-role` | `@admin_required` | Form: `member_id`, `csrf_token` | Toggles target member between `'member'` and `'admin'` (blocks demoting last admin); redirects `/admin`. |
| `GET` | `/tests` | `@login_required` | None | Runs all 53 in-memory tests via `run_all_tests()` and renders `TESTS_PAGE`. |

### Authentication, RBAC & Security Details
- **Live DB RBAC (`app.py:238-268, 1909-1915`)**: Every protected route queries `current_user()` from SQLite. Deleted accounts have their session cleared and redirect to `/login`; `@admin_required` and `cancel()` check `user["role"] == "admin"` from the DB rather than trusting cookie state.
- **CSRF Protection (`app.py:203-225`)**: `csrf_token()` stores a 32-byte hex secret in `session["_csrf_token"]` and exposes it to templates via `@app.context_processor`. `before_request_guards()` validates `csrf_token` (or `X-CSRF-Token` header) with `secrets.compare_digest` on all `POST` requests, bypassing only when `app.config["TESTING"]` is `True` and `ENFORCE_CSRF_IN_TESTS` is falsy.
- **Atomic Transactions (`app.py:595-666, 675-716, 390-409`)**: `book_session()`, `cancel_booking()`, and `reset_password_with_token()` start `BEGIN IMMEDIATE` transactions (when not already inside a transaction) and roll back on any validation failure or exception, preventing overbooking race conditions and double-use of reset tokens.

---

## 5. Data Models & Database

SQLite schema defined in `SCHEMA` (`app.py:53-100`).

### Tables

#### `Members`
| Column | Type | Constraints / Default | Notes |
|---|---|---|---|
| `member_id` | `INTEGER` | `PRIMARY KEY` | Auto-incrementing rowid |
| `name` | `TEXT` | `NOT NULL` | Display name |
| `email` | `TEXT` | `NOT NULL UNIQUE` | Stored & queried lowercase |
| `membership_status` | `TEXT` | `NOT NULL DEFAULT 'active'` | `'active'` or `'expired'` |
| `password_hash` | `TEXT` | `NOT NULL DEFAULT ''` | Werkzeug salted hash |
| `role` | `TEXT` | `NOT NULL DEFAULT 'member'` | `'member'` or `'admin'` |
| `balance` | `REAL` | `NOT NULL DEFAULT 100.0` | Wallet balance in USD |
| `failed_attempts` | `INTEGER` | `NOT NULL DEFAULT 0` | Consecutive failed login counter |
| `locked_until` | `TEXT` | Nullable | Local ISO-8601 expiry timestamp when locked |

#### `Sessions`
| Column | Type | Constraints / Default | Notes |
|---|---|---|---|
| `session_id` | `INTEGER` | `PRIMARY KEY` | Auto-incrementing rowid |
| `title` | `TEXT` | `NOT NULL` | Training title |
| `trainer` | `TEXT` | `NOT NULL` | Trainer name |
| `session_date` | `TEXT` | `NOT NULL` | `YYYY-MM-DD` |
| `start_time` | `TEXT` | `NOT NULL` | `HH:MM` (24-hour) |
| `capacity` | `INTEGER` | `NOT NULL CHECK (capacity > 0)` | Max bookings (`>= confirmed` on update) |
| `price` | `REAL` | `NOT NULL DEFAULT 10.0 CHECK (price >= 0)` | Session fee in USD |

#### `Bookings`
| Column | Type | Constraints / Default | Notes |
|---|---|---|---|
| `booking_id` | `INTEGER` | `PRIMARY KEY` | Auto-incrementing rowid |
| `member_id` | `INTEGER` | `NOT NULL REFERENCES Members(member_id)` | Owner |
| `session_id` | `INTEGER` | `NOT NULL REFERENCES Sessions(session_id)` | Target session |
| `status` | `TEXT` | `NOT NULL DEFAULT 'confirmed'` | Active bookings are `'confirmed'`; cancelled rows are deleted |
| `booked_at` | `TEXT` | `NOT NULL` | Local ISO-8601 timestamp |
| *Constraint* | — | `UNIQUE (member_id, session_id)` | Prevents duplicate active bookings |

#### `Payments`
| Column | Type | Constraints / Default | Notes |
|---|---|---|---|
| `payment_id` | `INTEGER` | `PRIMARY KEY` | Auto-incrementing rowid |
| `member_id` | `INTEGER` | `NOT NULL REFERENCES Members(member_id)` | Payer / refund recipient |
| `session_id` | `INTEGER` | `NOT NULL REFERENCES Sessions(session_id)` | Session paid for (queried with `LEFT JOIN` in case session is deleted) |
| `amount` | `REAL` | `NOT NULL CHECK (amount >= 0)` | Charged/refunded amount |
| `status` | `TEXT` | `NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'refunded'))` | Audit lifecycle state |
| `created_at` | `TEXT` | `NOT NULL` | Payment timestamp |
| `refunded_at` | `TEXT` | Nullable | Refund timestamp when cancelled |

#### `PasswordResets`
| Column | Type | Constraints / Default | Notes |
|---|---|---|---|
| `token` | `TEXT` | `PRIMARY KEY` | 32-byte URL-safe random token (`secrets.token_urlsafe(32)`) |
| `member_id` | `INTEGER` | `NOT NULL REFERENCES Members(member_id)` | Target account |
| `expires_at` | `TEXT` | `NOT NULL` | 30 minutes from creation |
| `used` | `INTEGER` | `NOT NULL DEFAULT 0` | `0` unused, `1` consumed |

### Relationships & Migrations
- `Members` 1:N `Bookings`, `Sessions` 1:N `Bookings` (M:N via `UNIQUE(member_id, session_id)`).
- `Members` 1:N `Payments`, `Sessions` 1:N `Payments` (preserved as audit trail even after booking deletion or session deletion).
- `Members` 1:N `PasswordResets`.
- **`seed_db(db)` (`app.py:122-175`)**: Idempotently executes `SCHEMA`, migrates older `gym.db` files via `PRAGMA table_info` + `ALTER TABLE` (`password_hash`, `role`, `balance`, `failed_attempts`, `locked_until` on `Members`; `price` on `Sessions`), and seeds 5 demo members, 4 sessions (`Yoga $15`, `Boxing $20`, `Fitness $12`, `Yoga (past) $15`), 2 bookings, and 2 matching `'paid'` payment rows when `Members` is empty.

---

## 6. Setup & Environment

### Environment Variables
Variable names read by `app.py` (all optional locally with safe defaults):

- `FLASK_SECRET_KEY` — Flask session & CSRF signing key (set to a strong random value in production; auto-generated in `render.yaml`).
- `DATABASE_PATH` — Path to SQLite database file (default: `gym.db`).
- `PORT` — HTTP port when running `app.py` directly (default: `5000`; Gunicorn reads `PORT` automatically on Render).
- `FLASK_DEBUG` — Enables Flask debug mode (`"1"` / `"true"` / `"yes"`; default `"1"` locally, set to `"0"` in `render.yaml`).
- `HOST` — Bind interface when running `app.py` directly (default: `127.0.0.1`).

### Local Setup, Run & Test Commands (Windows PowerShell)

No frontend build step exists.

```powershell
# Activate existing virtual environment (or create one and run: pip install -r requirements.txt)
.\.venv\Scripts\Activate.ps1

# Run locally (auto-migrates/seeds gym.db; opens on http://127.0.0.1:5000)
.\.venv\Scripts\python.exe app.py

# Reset local demo DB to regenerate fresh future session dates (optional)
Remove-Item gym.db
.\.venv\Scripts\python.exe app.py

# Run all 31 HTTP integration tests (isolated temporary SQLite DBs)
.\.venv\Scripts\python.exe -m unittest -v test_app

# Run all 53 in-app tests via CLI (or visit http://127.0.0.1:5000/tests after logging in)
.\.venv\Scripts\python.exe -c "import app; g=app.run_all_tests(); print(sum(x['passed'] for x in g), '/', sum(len(x['results']) for x in g))"
```

For production deployment on Render.com (Blueprint or manual Gunicorn setup), see `DEPLOY.md`.

---

## 7. Current State & Known Gaps

### Demo / Simulated Behavior
- **Simulated Payments & Top-Up**: Wallet top-ups (`POST /topup`) and session payments are simulated internally in SQLite; no real payment gateway (Stripe, etc.) is integrated.
- **No SMTP Delivery for Password Reset**: `/forgot-password` displays the reset URL in an on-page demo/dev notice box rather than emailing it.
- **Demo Credentials**: Default demo accounts are seeded when `Members` is empty (`app.py:42, 141-165`), and `MIN_PASSWORD = 4` (`app.py:43`) is a demo threshold (raise to `8+` in production).
- **Local `gym.db` Dates**: The local `gym.db` file (if not deleted/regenerated) retains the dates from when it was first seeded; delete `gym.db` and restart `app.py` to seed fresh relative dates.

### Remaining Security & Architectural Limitations
- **GET-based logout**: `GET /logout` clears session state via `GET` rather than `POST` + CSRF token.
- **SQLite `PRAGMA foreign_keys` not enabled**: Foreign key constraints are declared in `SCHEMA` but not enforced by SQLite connections (`get_db()`), so referential integrity relies on application logic (notably keeping `Payments` audit records readable via `LEFT JOIN` if a session is deleted).
- **No refund cascade on session deletion**: `delete_session()` deletes a session and its bookings without automatically refunding future paid bookings for that session.
- **No IP-based rate limiting**: Lockout protects individual accounts (`5` failures / `15` min), but there is no IP throttling on `/login`, `/register`, or `/forgot-password`.
- **`/tests` accessible to all authenticated members**: Any logged-in member can visit `/tests`, which synchronously executes 53 tests (including scrypt password hashing) per request.
- **Ephemeral SQLite on Render Free Tier**: Unless configured with a Render Persistent Disk or PostgreSQL, SQLite data resets across deploys/restarts (documented in `DEPLOY.md`).
- **Monolithic file structure**: Templates, CSS, service logic, tests, and routes remain in a single `app.py` file (2,003 lines), and an unused local `venv/` folder sits alongside `.venv/`.
