"""Automated tests for Gym Booking System (Flask + SQLite).

Run from the folder containing app.py:
    python -m unittest -v test_app

Each test uses its own temporary SQLite database; gym.db is not changed.

Tests 01-10: booking, login, access control.
Tests 11-20: cancellation, registration, admin training management.
Tests 21-31: Sprint 3 payments, refunds, RBAC, lockout, password reset, CSRF, occupancy.
Tests 32-39: Waitlist HTTP functional & edge-case tests.
ConcurrencyTests (15 tests): Multithreaded race-condition tests with threading.Barrier.
"""

import os
import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta

import app


class GymBookingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = app.DB
        app.DB = os.path.join(self.temp_dir.name, "test_gym.db")
        app.app.config.update(TESTING=True)

        # app.init_db() closes its own SQLite connection.
        app.init_db()
        self.client = app.app.test_client()

        # This session is safely in the future, even when tests run at night.
        self.future_date = (datetime.now() + timedelta(days=10)).date().isoformat()
        with closing(sqlite3.connect(app.DB)) as db:
            with db:
                cursor = db.execute(
                    """INSERT INTO Sessions
                       (title, trainer, session_date, start_time, capacity)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("Testing Session", "Trainer", self.future_date, "12:00", 2),
                )
                self.open_session = cursor.lastrowid

    def tearDown(self):
        # Test clients should not retain application contexts between tests.
        # All connections created directly by these tests are explicitly closed.
        app.DB = self.original_db
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def login(self, email="adil@kbtu.kz", password="1234"):
        return self.client.post(
            "/login", data={"email": email, "password": password}
        )

    def book(self, session_id):
        return self.client.post(
            "/book", data={"session_id": str(session_id)}, follow_redirects=True
        )

    def cancel(self, booking_id, **extra):
        data = {"booking_id": str(booking_id)}
        data.update(extra)
        return self.client.post("/cancel", data=data, follow_redirects=True)

    def register(self, name, email, password):
        return self.client.post(
            "/register",
            data={"name": name, "email": email, "password": password},
            follow_redirects=True,
        )

    def count_bookings(self, member_id, session_id):
        with closing(sqlite3.connect(app.DB)) as db:
            return db.execute(
                """SELECT COUNT(*) FROM Bookings
                   WHERE member_id=? AND session_id=?""",
                (member_id, session_id),
            ).fetchone()[0]

    def booking_id_of(self, member_id, session_id):
        with closing(sqlite3.connect(app.DB)) as db:
            row = db.execute(
                """SELECT booking_id FROM Bookings
                   WHERE member_id=? AND session_id=?""",
                (member_id, session_id),
            ).fetchone()
        return row[0] if row else None

    def insert_booking(self, member_id, session_id):
        """Insert a booking directly into the DB (bypasses business rules)."""
        with closing(sqlite3.connect(app.DB)) as db:
            with db:
                cursor = db.execute(
                    """INSERT INTO Bookings (member_id, session_id, status, booked_at)
                       VALUES (?, ?, 'confirmed', ?)""",
                    (member_id, session_id,
                     datetime.now().isoformat(timespec="seconds")),
                )
                return cursor.lastrowid

    def count_members_by_email(self, email):
        with closing(sqlite3.connect(app.DB)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM Members WHERE email=?", (email,)
            ).fetchone()[0]

    def count_sessions_by_title(self, title):
        with closing(sqlite3.connect(app.DB)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM Sessions WHERE title=?", (title,)
            ).fetchone()[0]

    # ------------------------------------------------------------------
    # Tests 01-10: booking, login, access control
    # ------------------------------------------------------------------
    def test_01_valid_booking_creates_one_record(self):
        self.login()
        response = self.book(self.open_session)
        self.assertIn(b"Booked: Testing Session", response.data)
        self.assertEqual(self.count_bookings(3, self.open_session), 1)

    def test_02_duplicate_booking_rejected(self):
        self.login()
        self.book(self.open_session)
        response = self.book(self.open_session)
        self.assertIn(b"already registered", response.data)
        self.assertEqual(self.count_bookings(3, self.open_session), 1)

    def test_03_full_session_rejected(self):
        self.login()
        response = self.book(1)  # Seeded Yoga: capacity 2, already 2 booked
        self.assertIn(b"session is full", response.data)
        self.assertEqual(self.count_bookings(3, 1), 0)

    def test_04_expired_membership_rejected(self):
        self.login("expired@kbtu.kz")
        response = self.book(self.open_session)
        self.assertIn(b"membership has expired", response.data)
        self.assertEqual(self.count_bookings(5, self.open_session), 0)

    def test_05_past_session_rejected(self):
        self.login()
        response = self.book(4)  # Seeded Yoga (past)
        self.assertIn(b"already started or finished", response.data)
        self.assertEqual(self.count_bookings(3, 4), 0)

    def test_06_wrong_password_rejected(self):
        response = self.login(password="incorrect")
        self.assertIn(b"Wrong email or password", response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn("member_id", session)

    def test_07_member_cannot_access_admin(self):
        self.login()
        response = self.client.get("/admin", follow_redirects=True)
        self.assertIn(b"Access denied", response.data)
        self.assertNotIn(b"All bookings", response.data)

    def test_08_anonymous_booking_requires_login(self):
        response = self.client.post(
            "/book", data={"session_id": self.open_session}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(self.count_bookings(3, self.open_session), 0)

    def test_09_admin_can_access_admin_page(self):
        self.login("aldair@kbtu.kz")
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"All bookings", response.data)

    def test_10_booking_uses_logged_in_identity(self):
        # Adil (id 3) is logged in but tries to book as member 2 (Iskander).
        self.login()
        self.client.post(
            "/book",
            data={"session_id": self.open_session, "member_id": "2"},
        )
        # The booking must belong to the logged-in user, not to the form value.
        self.assertEqual(self.count_bookings(3, self.open_session), 1)
        self.assertEqual(self.count_bookings(2, self.open_session), 0)

    # ------------------------------------------------------------------
    # Tests 11-15: booking cancellation
    # ------------------------------------------------------------------
    def test_11_member_can_cancel_own_booking(self):
        self.login()
        self.book(self.open_session)
        booking_id = self.booking_id_of(3, self.open_session)
        self.assertIsNotNone(booking_id)

        response = self.cancel(booking_id)

        self.assertIn(b"Cancelled: Testing Session", response.data)
        self.assertEqual(self.count_bookings(3, self.open_session), 0)

    def test_12_member_cannot_cancel_someone_elses_booking(self):
        # Seeded booking: Aldair (member 1) is registered for Yoga (session 1).
        self.login()  # Adil
        booking_id = self.booking_id_of(1, 1)

        response = self.cancel(booking_id)

        self.assertIn(b"only cancel your own bookings", response.data)
        self.assertEqual(self.count_bookings(1, 1), 1)

    def test_13_cancel_past_session_rejected(self):
        # Session 4 is seeded as "Yoga (past)"; put Adil on it directly in the DB.
        booking_id = self.insert_booking(3, 4)
        self.login()

        response = self.cancel(booking_id)

        self.assertIn(b"already started or finished", response.data)
        self.assertEqual(self.count_bookings(3, 4), 1)

    def test_14_admin_can_cancel_any_booking(self):
        # Iskander (member 2) is on the seeded Yoga session; admin cancels it.
        self.login("aldair@kbtu.kz")
        booking_id = self.booking_id_of(2, 1)

        response = self.cancel(booking_id, next="admin")

        self.assertIn(b"Cancelled: Yoga", response.data)
        self.assertEqual(self.count_bookings(2, 1), 0)

    def test_15_can_rebook_after_cancellation(self):
        self.login()
        self.book(self.open_session)
        self.cancel(self.booking_id_of(3, self.open_session))
        self.assertEqual(self.count_bookings(3, self.open_session), 0)

        response = self.book(self.open_session)

        self.assertIn(b"Booked: Testing Session", response.data)
        self.assertEqual(self.count_bookings(3, self.open_session), 1)

    # ------------------------------------------------------------------
    # Tests 16-18: registration
    # ------------------------------------------------------------------
    def test_16_registration_creates_active_member_who_can_log_in(self):
        response = self.register("New User", "new.user@kbtu.kz", "secret1")
        self.assertIn(b"Account created", response.data)

        with closing(sqlite3.connect(app.DB)) as db:
            row = db.execute(
                "SELECT membership_status, role FROM Members WHERE email=?",
                ("new.user@kbtu.kz",),
            ).fetchone()
        self.assertEqual(row, ("active", "member"))

        self.login("new.user@kbtu.kz", "secret1")
        with self.client.session_transaction() as session:
            self.assertIn("member_id", session)
            self.assertEqual(session["role"], "member")

    def test_17_registration_with_existing_email_rejected(self):
        # Same address as seeded Adil, written in upper case.
        response = self.register("Fake Adil", "ADIL@KBTU.KZ", "secret1")
        self.assertIn(b"already exists", response.data)
        self.assertEqual(self.count_members_by_email("adil@kbtu.kz"), 1)

    def test_18_registration_with_invalid_data_rejected(self):
        cases = [
            ("Bad Email", "not-an-email", "secret1", b"valid email"),
            ("Short Pw", "short.pw@kbtu.kz", "123", b"at least 4 characters"),
            ("", "no.name@kbtu.kz", "secret1", b"Name is required"),
        ]
        for name, email, password, expected in cases:
            with self.subTest(name=name or "<empty>", email=email):
                response = self.register(name, email, password)
                self.assertIn(expected, response.data)
                self.assertEqual(self.count_members_by_email(email), 0)

    # ------------------------------------------------------------------
    # Tests 19-20: admin manages trainings
    # ------------------------------------------------------------------
    def test_19_admin_can_create_training(self):
        self.login("aldair@kbtu.kz")
        response = self.client.post(
            "/admin/session/create",
            data={
                "title": "Pilates",
                "trainer": "Aigul",
                "session_date": self.future_date,
                "start_time": "15:30",
                "capacity": "8",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Training created: Pilates", response.data)
        self.assertEqual(self.count_sessions_by_title("Pilates"), 1)

    def test_20_capacity_cannot_be_lower_than_confirmed_bookings(self):
        # Seeded Yoga (session 1) has 2 confirmed bookings.
        self.login("aldair@kbtu.kz")
        response = self.client.post(
            "/admin/session/update",
            data={
                "session_id": "1",
                "title": "Yoga",
                "trainer": "Aigerim",
                "session_date": self.future_date,
                "start_time": "10:00",
                "capacity": "1",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Capacity cannot be lower than the 2 bookings", response.data)

        with closing(sqlite3.connect(app.DB)) as db:
            capacity = db.execute(
                "SELECT capacity FROM Sessions WHERE session_id=1"
            ).fetchone()[0]
        self.assertEqual(capacity, 2)  # unchanged

    # ------------------------------------------------------------------
    # Tests 21-31: Sprint 3 (payments, refunds, RBAC, security, occupancy)
    # ------------------------------------------------------------------
    def balance_of(self, member_id):
        with closing(sqlite3.connect(app.DB)) as db:
            return db.execute(
                "SELECT balance FROM Members WHERE member_id=?", (member_id,)
            ).fetchone()[0]

    def test_21_topup_increases_balance_and_shows_in_ui(self):
        self.login()
        before = self.balance_of(3)
        response = self.client.post(
            "/topup", data={"amount": "45.50"}, follow_redirects=True
        )
        self.assertIn(b"Balance topped up by $45.50", response.data)
        self.assertEqual(self.balance_of(3), round(before + 45.50, 2))

    def test_22_booking_deducts_balance_and_cancel_refunds_with_history(self):
        self.login()
        before = self.balance_of(3)
        # self.open_session uses default price $10.00
        book_resp = self.book(self.open_session)
        self.assertIn(b"$10.00 paid", book_resp.data)
        self.assertEqual(self.balance_of(3), round(before - 10.0, 2))

        bid = self.booking_id_of(3, self.open_session)
        cancel_resp = self.cancel(bid)
        self.assertIn(b"$10.00 refunded", cancel_resp.data)
        self.assertIn(b"refunded", cancel_resp.data)
        self.assertEqual(self.balance_of(3), before)

    def test_23_insufficient_balance_blocks_booking_via_http(self):
        with closing(sqlite3.connect(app.DB)) as db:
            with db:
                db.execute("UPDATE Members SET balance=2.0 WHERE member_id=3")
        self.login()
        response = self.book(self.open_session)
        self.assertIn(b"Insufficient balance", response.data)
        self.assertEqual(self.count_bookings(3, self.open_session), 0)
        self.assertEqual(self.balance_of(3), 2.0)

    def test_24_admin_member_management_toggle_status_and_role(self):
        self.login("aldair@kbtu.kz")
        # Expire Adil (member 3)
        resp = self.client.post(
            "/admin/member/toggle-status",
            data={"member_id": "3"},
            follow_redirects=True,
        )
        self.assertIn(b"expired", resp.data)
        # Cannot demote last admin (Aldair, member 1)
        resp2 = self.client.post(
            "/admin/member/toggle-role",
            data={"member_id": "1"},
            follow_redirects=True,
        )
        self.assertIn(b"last admin", resp2.data)
        # Promote Adil to admin, then Aldair can be demoted
        self.client.post(
            "/admin/member/toggle-role",
            data={"member_id": "3"},
            follow_redirects=True,
        )
        with closing(sqlite3.connect(app.DB)) as db:
            role_3 = db.execute(
                "SELECT role FROM Members WHERE member_id=3"
            ).fetchone()[0]
        self.assertEqual(role_3, "admin")

    def test_25_stale_cookie_role_rejected_after_db_demotion(self):
        self.login("aldair@kbtu.kz")
        # Demote Aldair directly in DB while session cookie still says role='admin'
        with closing(sqlite3.connect(app.DB)) as db:
            with db:
                db.execute("UPDATE Members SET role='member' WHERE member_id=1")
        resp = self.client.get("/admin", follow_redirects=True)
        self.assertIn(b"Access denied", resp.data)
        # Also verify cannot cancel Iskander's booking via stale admin cookie
        iskander_bid = self.booking_id_of(2, 1)
        cancel_resp = self.cancel(iskander_bid, next="admin")
        self.assertIn(b"only cancel your own bookings", cancel_resp.data)
        self.assertEqual(self.count_bookings(2, 1), 1)

    def test_26_deleted_user_session_cleared_and_redirected(self):
        self.login()
        with closing(sqlite3.connect(app.DB)) as db:
            with db:
                db.execute("DELETE FROM Members WHERE member_id=3")
        resp = self.client.get("/tests")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        with self.client.session_transaction() as sess:
            self.assertNotIn("member_id", sess)

    def test_27_login_lockout_after_five_failures_via_http(self):
        for _ in range(4):
            r = self.login(password="bad")
            self.assertIn(b"Wrong email or password", r.data)
        r5 = self.login(password="bad")
        self.assertIn(b"locked", r5.data.lower())
        # Correct password also blocked during lockout
        r6 = self.login(password="1234")
        self.assertIn(b"locked", r6.data.lower())
        with self.client.session_transaction() as sess:
            self.assertNotIn("member_id", sess)

    def test_28_forgot_and_reset_password_end_to_end(self):
        resp = self.client.post(
            "/forgot-password",
            data={"email": "adil@kbtu.kz"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        match = re.search(rb"/reset-password/([A-Za-z0-9_-]+)", resp.data)
        self.assertIsNotNone(match, "Reset link was not displayed on forgot-password page")
        token = match.group(1).decode()

        reset_resp = self.client.post(
            f"/reset-password/{token}",
            data={"password": "newsecret"},
            follow_redirects=True,
        )
        self.assertIn(b"Password updated successfully", reset_resp.data)

        # Old password rejected, new password works
        self.assertIn(b"Wrong email or password", self.login(password="1234").data)
        ok_login = self.login(password="newsecret")
        self.assertEqual(ok_login.status_code, 302)

        # Reusing the token is rejected
        reuse_resp = self.client.get(f"/reset-password/{token}")
        self.assertIn(b"already been used", reuse_resp.data)

    def test_29_csrf_blocks_missing_token_and_allows_valid_token(self):
        app.app.config["ENFORCE_CSRF_IN_TESTS"] = True
        try:
            # Missing CSRF token -> 400
            bad = self.client.post(
                "/login", data={"email": "adil@kbtu.kz", "password": "1234"}
            )
            self.assertEqual(bad.status_code, 400)

            # Fetch form with valid CSRF token -> succeeds
            page = self.client.get("/login")
            match = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', page.data)
            self.assertIsNotNone(match)
            token = match.group(1).decode()
            good = self.client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "email": "adil@kbtu.kz",
                    "password": "1234",
                },
            )
            self.assertEqual(good.status_code, 302)
        finally:
            app.app.config["ENFORCE_CSRF_IN_TESTS"] = False

    def test_30_occupancy_dashboard_renders_on_home_and_admin(self):
        self.login("aldair@kbtu.kz")
        home = self.client.get("/")
        self.assertIn(b"Club occupancy", home.data)
        self.assertIn(b'id="load-grid"', home.data)
        admin_page = self.client.get("/admin")
        self.assertIn(b"Club occupancy", admin_page.data)
        self.assertIn(b"Payments &amp; Revenue Overview", admin_page.data)

    def test_31_regular_member_blocked_from_admin_member_routes(self):
        self.login()  # Adil (regular member)
        r1 = self.client.post(
            "/admin/member/toggle-status",
            data={"member_id": "2"},
            follow_redirects=True,
        )
        r2 = self.client.post(
            "/admin/member/toggle-role",
            data={"member_id": "3"},
            follow_redirects=True,
        )
        self.assertIn(b"Access denied", r1.data)
        self.assertIn(b"Access denied", r2.data)
        with closing(sqlite3.connect(app.DB)) as db:
            role_3 = db.execute(
                "SELECT role FROM Members WHERE member_id=3"
            ).fetchone()[0]
        self.assertEqual(role_3, "member")

    # ------------------------------------------------------------------
    # Tests 32-39: Waitlist HTTP functional & edge-case tests
    # ------------------------------------------------------------------
    def count_waitlist(self, session_id, member_id=None):
        with closing(sqlite3.connect(app.DB)) as db:
            if member_id is None:
                return db.execute(
                    "SELECT COUNT(*) FROM Waitlist WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            return db.execute(
                "SELECT COUNT(*) FROM Waitlist WHERE session_id=? AND member_id=?",
                (session_id, member_id),
            ).fetchone()[0]

    def test_32_waitlist_join_and_ui_on_full_session(self):
        self.login()  # Adil (member 3)
        before_bal = self.balance_of(3)
        home_before = self.client.get("/")
        self.assertIn(b"Join Waitlist", home_before.data)

        resp = self.client.post(
            "/waitlist/join", data={"session_id": "1"}, follow_redirects=True
        )
        self.assertIn(b"Joined waitlist for Yoga", resp.data)
        self.assertIn(b"Position #1", resp.data)
        self.assertIn(b"My Waitlist", resp.data)
        self.assertIn(b"Leave Waitlist (#1)", resp.data)
        self.assertIn(b"Waitlist: 1", resp.data)
        self.assertEqual(self.balance_of(3), before_bal)
        self.assertEqual(self.count_waitlist(1, 3), 1)

    def test_33_waitlist_rejects_open_past_expired_and_duplicate(self):
        self.login()  # Adil (member 3)
        # Open session -> told to book directly
        r_open = self.client.post(
            "/waitlist/join", data={"session_id": "2"}, follow_redirects=True
        )
        self.assertIn(b"book directly", r_open.data.lower())
        self.assertEqual(self.count_waitlist(2), 0)

        # Past session -> rejected
        r_past = self.client.post(
            "/waitlist/join", data={"session_id": "4"}, follow_redirects=True
        )
        self.assertIn(b"already started", r_past.data.lower())
        self.assertEqual(self.count_waitlist(4), 0)

        # Duplicate join on full session 1 -> rejected
        self.client.post("/waitlist/join", data={"session_id": "1"})
        r_dup = self.client.post(
            "/waitlist/join", data={"session_id": "1"}, follow_redirects=True
        )
        self.assertIn(b"already on the waitlist", r_dup.data.lower())
        self.assertEqual(self.count_waitlist(1, 3), 1)

        # Expired member -> rejected
        self.login("expired@kbtu.kz")
        r_exp = self.client.post(
            "/waitlist/join", data={"session_id": "1"}, follow_redirects=True
        )
        self.assertIn(b"expired", r_exp.data.lower())
        self.assertEqual(self.count_waitlist(1, 5), 0)

    def test_34_waitlist_leave_own_and_block_non_admin_removing_others(self):
        # Zhanibek (4) joins Yoga waitlist
        self.login("zhanibek@kbtu.kz")
        self.client.post("/waitlist/join", data={"session_id": "1"})
        self.assertEqual(self.count_waitlist(1, 4), 1)

        # Adil (3, non-admin) tries to remove Zhanibek (4) -> blocked
        self.login("adil@kbtu.kz")
        r_block = self.client.post(
            "/waitlist/leave",
            data={"session_id": "1", "member_id": "4"},
            follow_redirects=True,
        )
        self.assertIn(b"only remove yourself", r_block.data.lower())
        self.assertEqual(self.count_waitlist(1, 4), 1)

        # Zhanibek leaves own waitlist -> succeeds
        self.login("zhanibek@kbtu.kz")
        r_leave = self.client.post(
            "/waitlist/leave", data={"session_id": "1"}, follow_redirects=True
        )
        self.assertIn(b"Left waitlist: Yoga", r_leave.data)
        self.assertEqual(self.count_waitlist(1, 4), 0)

    def test_35_waitlist_auto_promotion_on_cancel_via_http(self):
        # Adil (3) joins Yoga ($15) waitlist
        self.login("adil@kbtu.kz")
        self.client.post("/waitlist/join", data={"session_id": "1"})
        self.assertEqual(self.balance_of(3), 100.0)

        # Admin cancels Iskander's (2) Yoga booking -> Adil auto-promoted
        self.login("aldair@kbtu.kz")
        iskander_bid = self.booking_id_of(2, 1)
        resp = self.cancel(iskander_bid, next="admin")
        self.assertIn(b"Cancelled: Yoga", resp.data)
        self.assertIn(b"Waitlist auto-promoted: Adil", resp.data)
        self.assertEqual(self.count_bookings(2, 1), 0)
        self.assertEqual(self.count_bookings(3, 1), 1)
        self.assertEqual(self.count_waitlist(1, 3), 0)
        self.assertEqual(self.balance_of(2), 100.0)  # 85 + 15 refund
        self.assertEqual(self.balance_of(3), 85.0)   # 100 - 15 paid

    def test_36_admin_sees_waitlist_queue_and_can_remove_entry(self):
        self.login("adil@kbtu.kz")
        self.client.post("/waitlist/join", data={"session_id": "1"})

        self.login("aldair@kbtu.kz")
        admin_page = self.client.get("/admin")
        self.assertIn(b"Waitlist Queue", admin_page.data)
        self.assertIn(b"WL: 1", admin_page.data)
        self.assertIn(b"adil@kbtu.kz", admin_page.data)

        rem = self.client.post(
            "/waitlist/leave",
            data={"session_id": "1", "member_id": "3", "next": "admin"},
            follow_redirects=True,
        )
        self.assertIn(b"Removed Adil from waitlist", rem.data)
        self.assertEqual(self.count_waitlist(1, 3), 0)

    def test_37_delete_session_cascades_waitlist_via_http(self):
        self.login("adil@kbtu.kz")
        self.client.post("/waitlist/join", data={"session_id": "1"})
        self.assertEqual(self.count_waitlist(1), 1)

        self.login("aldair@kbtu.kz")
        resp = self.client.post(
            "/admin/session/delete", data={"session_id": "1"}, follow_redirects=True
        )
        self.assertIn(b"Training deleted: Yoga", resp.data)
        self.assertEqual(self.count_waitlist(1), 0)

    def test_38_waitlist_routes_enforce_csrf(self):
        self.login("adil@kbtu.kz")
        app.app.config["ENFORCE_CSRF_IN_TESTS"] = True
        try:
            # Missing token -> 400
            bad_join = self.client.post("/waitlist/join", data={"session_id": "1"})
            self.assertEqual(bad_join.status_code, 400)
            bad_leave = self.client.post("/waitlist/leave", data={"session_id": "1"})
            self.assertEqual(bad_leave.status_code, 400)

            # Valid token from home page -> succeeds
            home = self.client.get("/")
            match = re.search(rb'name="csrf_token" value="([0-9a-f]+)"', home.data)
            self.assertIsNotNone(match)
            token = match.group(1).decode()
            good_join = self.client.post(
                "/waitlist/join",
                data={"csrf_token": token, "session_id": "1"},
                follow_redirects=True,
            )
            self.assertIn(b"Joined waitlist for Yoga", good_join.data)
            good_leave = self.client.post(
                "/waitlist/leave",
                data={"csrf_token": token, "session_id": "1"},
                follow_redirects=True,
            )
            self.assertIn(b"Left waitlist: Yoga", good_leave.data)
        finally:
            app.app.config["ENFORCE_CSRF_IN_TESTS"] = False

    def test_39_capacity_increase_auto_promotes_waitlist_via_http(self):
        self.login("adil@kbtu.kz")
        self.client.post("/waitlist/join", data={"session_id": "1"})
        self.assertEqual(self.count_waitlist(1, 3), 1)

        self.login("aldair@kbtu.kz")
        resp = self.client.post(
            "/admin/session/update",
            data={
                "session_id": "1",
                "title": "Yoga",
                "trainer": "Aigerim",
                "session_date": self.future_date,
                "start_time": "10:00",
                "capacity": "3",
                "price": "15.00",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Waitlist auto-promoted: Adil", resp.data)
        self.assertEqual(self.count_bookings(3, 1), 1)
        self.assertEqual(self.count_waitlist(1, 3), 0)
        self.assertEqual(self.balance_of(3), 85.0)


class ConcurrencyTests(unittest.TestCase):
    """15 multithreaded race-condition tests using threading.Thread + threading.Barrier.
    Every test runs on its own isolated temporary file-backed SQLite DB in WAL mode.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = app.DB
        self.db_path = os.path.join(self.temp_dir.name, "conc_gym.db")
        app.DB = self.db_path
        app.app.config.update(TESTING=True)
        app.init_db(extended=False)

    def tearDown(self):
        app._initialized_dbs.discard(self.db_path)
        app.DB = self.original_db
        self.temp_dir.cleanup()

    def connect(self):
        return app.connect_db(self.db_path)

    def add_member(self, conn, idx, balance=100.0, status="active"):
        cur = conn.execute(
            "INSERT INTO Members(name,email,membership_status,password_hash,role,balance) "
            "VALUES (?,?,?,?,?,?)",
            (f"ThreadUser {idx}", f"tuser{idx}@kbtu.kz", status, app.DEMO_PW_HASH, "member", balance),
        )
        return cur.lastrowid

    def add_session(self, conn, title, capacity, price=15.0, days_ahead=5):
        cur = conn.execute(
            "INSERT INTO Sessions(title,trainer,session_date,start_time,capacity,price) "
            "VALUES (?,?,?,?,?,?)",
            (title, "Coach", app.day(days_ahead), "12:00", capacity, price),
        )
        return cur.lastrowid

    def run_barrier(self, callables):
        barrier = threading.Barrier(len(callables))
        results = [None] * len(callables)
        errors = []
        lock = threading.Lock()

        def worker(i, fn):
            try:
                results[i] = fn(barrier)
            except Exception as exc:
                with lock:
                    errors.append((i, type(exc).__name__, str(exc)))

        threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(callables)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"Unexpected thread errors: {errors}")
        return results

    def test_conc_01_last_seat_race_condition(self):
        with closing(self.connect()) as db:
            with db:
                mids = [self.add_member(db, i, 100.0) for i in range(10)]
                sid = self.add_session(db, "Last Seat", 1, 15.0)

        def make_task(mid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.book_session(conn, mid, sid)
            return task

        res = self.run_barrier([make_task(m) for m in mids])
        self.assertEqual(sum(1 for ok, _ in res if ok), 1)
        self.assertEqual(sum(1 for ok, _ in res if not ok), 9)
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 1)
            paid = db.execute(
                "SELECT COUNT(*) FROM Payments WHERE session_id=? AND status='paid'", (sid,)
            ).fetchone()[0]
            self.assertEqual(paid, 1)
            bals = [app.balance_of(db, m) for m in mids]
            self.assertEqual(bals.count(85.0), 1)
            self.assertEqual(bals.count(100.0), 9)

    def test_conc_02_multi_seat_exact_capacity_race(self):
        with closing(self.connect()) as db:
            with db:
                mids = [self.add_member(db, i, 100.0) for i in range(12)]
                sid = self.add_session(db, "Three Seats", 3, 20.0)

        def make_task(mid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.book_session(conn, mid, sid)
            return task

        res = self.run_barrier([make_task(m) for m in mids])
        self.assertEqual(sum(1 for ok, _ in res if ok), 3)
        self.assertEqual(sum(1 for ok, _ in res if not ok), 9)
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 3)
            paid = db.execute(
                "SELECT COUNT(*) FROM Payments WHERE session_id=? AND status='paid'", (sid,)
            ).fetchone()[0]
            self.assertEqual(paid, 3)

    def test_conc_03_same_user_double_book_race(self):
        with closing(self.connect()) as db:
            with db:
                sid = self.add_session(db, "Double Book", 5, 20.0)

        def task(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.book_session(conn, 3, sid)

        res = self.run_barrier([task for _ in range(5)])
        self.assertEqual(sum(1 for ok, _ in res if ok), 1)
        self.assertEqual(sum(1 for ok, _ in res if not ok), 4)
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 1)
            self.assertEqual(app.balance_of(db, 3), 80.0)

    def test_conc_04_double_cancel_double_refund_race(self):
        with closing(self.connect()) as db:
            self.assertTrue(app.book_session(db, 3, 2)[0])  # Boxing $20
            bid = app.booking_id_of(db, 3, 2)

        def task(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.cancel_booking(conn, 3, bid)

        res = self.run_barrier([task for _ in range(5)])
        self.assertEqual(sum(1 for ok, _ in res if ok), 1)
        self.assertEqual(sum(1 for ok, _ in res if not ok), 4)
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, 2), 0)
            self.assertEqual(app.balance_of(db, 3), 100.0)
            refunds = db.execute(
                "SELECT COUNT(*) FROM Payments WHERE member_id=3 AND status='refunded'"
            ).fetchone()[0]
            self.assertEqual(refunds, 1)

    def test_conc_05_concurrent_booking_insufficient_balance_race(self):
        with closing(self.connect()) as db:
            with db:
                db.execute("UPDATE Members SET balance=10.0 WHERE member_id=3")
                sids = [self.add_session(db, f"Budget {i}", 5, 10.0) for i in range(3)]

        def make_task(sid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.book_session(conn, 3, sid)
            return task

        res = self.run_barrier([make_task(s) for s in sids])
        self.assertEqual(sum(1 for ok, _ in res if ok), 1)
        self.assertEqual(sum(1 for ok, _ in res if not ok), 2)
        with closing(self.connect()) as db:
            bal = app.balance_of(db, 3)
            self.assertGreaterEqual(bal, 0.0)
            self.assertEqual(bal, 0.0)
            count = db.execute("SELECT COUNT(*) FROM Bookings WHERE member_id=3").fetchone()[0]
            self.assertEqual(count, 1)

    def test_conc_06_concurrent_topup_race(self):
        def task(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.topup_balance(conn, 3, 10.0)

        res = self.run_barrier([task for _ in range(10)])
        self.assertTrue(all(ok for ok, _ in res))
        with closing(self.connect()) as db:
            self.assertEqual(app.balance_of(db, 3), 200.0)

    def test_conc_07_concurrent_waitlist_join_same_user(self):
        def task(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.join_waitlist(conn, 3, 1)  # Full Yoga session

        res = self.run_barrier([task for _ in range(5)])
        self.assertEqual(sum(1 for ok, _ in res if ok), 1)
        self.assertEqual(sum(1 for ok, _ in res if not ok), 4)
        with closing(self.connect()) as db:
            cnt = db.execute(
                "SELECT COUNT(*) FROM Waitlist WHERE member_id=3 AND session_id=1"
            ).fetchone()[0]
            self.assertEqual(cnt, 1)

    def test_conc_08_concurrent_waitlist_join_multiple_users_fifo(self):
        with closing(self.connect()) as db:
            with db:
                mids = [self.add_member(db, i, 100.0) for i in range(8)]

        def make_task(mid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.join_waitlist(conn, mid, 1)
            return task

        res = self.run_barrier([make_task(m) for m in mids])
        self.assertTrue(all(ok for ok, _ in res))
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT waitlist_id, member_id FROM Waitlist WHERE session_id=1 "
                "ORDER BY created_at ASC, waitlist_id ASC"
            ).fetchall()
            self.assertEqual(len(rows), 8)
            self.assertEqual(len({r["member_id"] for r in rows}), 8)
            wids = [r["waitlist_id"] for r in rows]
            self.assertEqual(wids, sorted(wids))

    def test_conc_09_concurrent_cancel_and_waitlist_auto_promotion(self):
        with closing(self.connect()) as db:
            with db:
                sid = self.add_session(db, "Dual Cancel", 2, 15.0)
                a = self.add_member(db, 1, 100.0)
                b = self.add_member(db, 2, 100.0)
                w = [self.add_member(db, i, 100.0) for i in range(3, 7)]
            self.assertTrue(app.book_session(db, a, sid)[0])
            self.assertTrue(app.book_session(db, b, sid)[0])
            ba, bb = app.booking_id_of(db, a, sid), app.booking_id_of(db, b, sid)
            for wm in w:
                self.assertTrue(app.join_waitlist(db, wm, sid)[0])

        def make_cancel(mid, bid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.cancel_booking(conn, mid, bid)
            return task

        res = self.run_barrier([make_cancel(a, ba), make_cancel(b, bb)])
        self.assertTrue(all(ok for ok, _ in res))
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 2)
            booked = {
                r[0] for r in db.execute(
                    "SELECT member_id FROM Bookings WHERE session_id=? AND status='confirmed'",
                    (sid,),
                )
            }
            self.assertEqual(booked, {w[0], w[1]})
            remaining = [
                r[0] for r in db.execute(
                    "SELECT member_id FROM Waitlist WHERE session_id=? "
                    "ORDER BY created_at ASC, waitlist_id ASC",
                    (sid,),
                )
            ]
            self.assertEqual(remaining, [w[2], w[3]])
            self.assertEqual(app.balance_of(db, a), 100.0)
            self.assertEqual(app.balance_of(db, b), 100.0)
            self.assertEqual(app.balance_of(db, w[0]), 85.0)
            self.assertEqual(app.balance_of(db, w[1]), 85.0)

    def test_conc_10_concurrent_cancel_and_direct_booking_vs_waitlist(self):
        with closing(self.connect()) as db:
            with db:
                sid = self.add_session(db, "Cancel vs Direct", 1, 15.0)
                owner = self.add_member(db, 1, 100.0)
                waiter = self.add_member(db, 2, 100.0)
                outsider = self.add_member(db, 3, 100.0)
            self.assertTrue(app.book_session(db, owner, sid)[0])
            bid = app.booking_id_of(db, owner, sid)
            self.assertTrue(app.join_waitlist(db, waiter, sid)[0])

        def do_cancel(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.cancel_booking(conn, owner, bid)

        def do_book(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.book_session(conn, outsider, sid)

        res = self.run_barrier([do_cancel, do_book])
        self.assertTrue(res[0][0])
        self.assertFalse(res[1][0])
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 1)
            self.assertEqual(app.balance_of(db, owner), 100.0)
            self.assertEqual(app.balance_of(db, waiter), 85.0)
            self.assertEqual(app.balance_of(db, outsider), 100.0)

    def test_conc_11_concurrent_waitlist_leave_and_cancel_auto_promote(self):
        with closing(self.connect()) as db:
            with db:
                sid = self.add_session(db, "Leave vs Cancel", 1, 15.0)
                owner = self.add_member(db, 1, 100.0)
                w1 = self.add_member(db, 2, 100.0)
                w2 = self.add_member(db, 3, 100.0)
            self.assertTrue(app.book_session(db, owner, sid)[0])
            bid = app.booking_id_of(db, owner, sid)
            self.assertTrue(app.join_waitlist(db, w1, sid)[0])
            self.assertTrue(app.join_waitlist(db, w2, sid)[0])

        def do_leave(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.leave_waitlist(conn, w1, sid)

        def do_cancel(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.cancel_booking(conn, owner, bid)

        res = self.run_barrier([do_leave, do_cancel])
        self.assertTrue(res[1][0])
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 1)
            self.assertEqual(app.balance_of(db, owner), 100.0)
            booked_id = db.execute(
                "SELECT member_id FROM Bookings WHERE session_id=?", (sid,)
            ).fetchone()[0]
            self.assertIn(booked_id, (w1, w2))
            other = w2 if booked_id == w1 else w1
            self.assertEqual(app.balance_of(db, booked_id), 85.0)
            self.assertEqual(app.balance_of(db, other), 100.0)
            overlap = db.execute(
                "SELECT COUNT(*) FROM Bookings b JOIN Waitlist w "
                "ON b.member_id=w.member_id AND b.session_id=w.session_id WHERE b.session_id=?",
                (sid,),
            ).fetchone()[0]
            self.assertEqual(overlap, 0)

    def test_conc_12_concurrent_admin_capacity_reduction_vs_booking(self):
        d5 = app.day(5)
        with closing(self.connect()) as db:
            with db:
                sid = self.add_session(db, "Capacity Race", 5, 10.0, days_ahead=5)
                starter = self.add_member(db, 0, 100.0)
                mids = [self.add_member(db, i, 100.0) for i in range(1, 6)]
            self.assertTrue(app.book_session(db, starter, sid)[0])

        def do_reduce(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.update_session(conn, sid, "Capacity Race", "Coach", d5, "12:00", 2, 10.0)

        def make_book(mid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.book_session(conn, mid, sid)
            return task

        res = self.run_barrier([do_reduce] + [make_book(m) for m in mids])
        admin_ok = res[0][0]
        booked_new = sum(1 for ok, _ in res[1:] if ok)
        with closing(self.connect()) as db:
            cap = db.execute("SELECT capacity FROM Sessions WHERE session_id=?", (sid,)).fetchone()[0]
            taken = app.taken_of(db, sid)
            self.assertLessEqual(taken, cap)
            self.assertEqual(taken, 1 + booked_new)
            if admin_ok:
                self.assertEqual(cap, 2)
            else:
                self.assertEqual(cap, 5)
                self.assertGreater(taken, 2)

    def test_conc_13_concurrent_failed_login_bruteforce_lockout(self):
        def task(barrier):
            with closing(self.connect()) as conn:
                barrier.wait()
                return app.authenticate_with_status(conn, "adil@kbtu.kz", "wrongpw")

        res = self.run_barrier([task for _ in range(8)])
        self.assertTrue(all(m is None for m, _ in res))
        self.assertTrue(any("locked" in (err or "").lower() for _, err in res))
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT failed_attempts, locked_until FROM Members WHERE member_id=3"
            ).fetchone()
            self.assertEqual(row["failed_attempts"], app.MAX_LOGIN_ATTEMPTS)
            self.assertIsNotNone(row["locked_until"])
            self.assertGreater(datetime.fromisoformat(row["locked_until"]), datetime.now())
            m_ok, err_ok = app.authenticate_with_status(db, "adil@kbtu.kz", "1234")
            self.assertIsNone(m_ok)
            self.assertIn("locked", (err_ok or "").lower())

    def test_conc_14_concurrent_password_reset_token_reuse_http(self):
        with closing(self.connect()) as db:
            ok, token, _ = app.create_password_reset(db, "adil@kbtu.kz")
            self.assertTrue(ok)

        def make_task(idx):
            def task(barrier):
                client = app.app.test_client()
                barrier.wait()
                resp = client.post(
                    f"/reset-password/{token}",
                    data={"password": f"racepass{idx}"},
                    follow_redirects=True,
                )
                return b"Password updated successfully" in resp.data
            return task

        res = self.run_barrier([make_task(i) for i in range(5)])
        self.assertEqual(sum(1 for ok in res if ok), 1)
        self.assertEqual(sum(1 for ok in res if not ok), 4)
        winner_idx = res.index(True)
        with closing(self.connect()) as db:
            used = db.execute(
                "SELECT used FROM PasswordResets WHERE token=?", (token,)
            ).fetchone()[0]
            self.assertEqual(used, 1)
            self.assertIsNotNone(app.authenticate(db, "adil@kbtu.kz", f"racepass{winner_idx}"))

    def test_conc_15_waitlist_auto_promote_skips_ineligible_under_concurrency(self):
        with closing(self.connect()) as db:
            with db:
                sid = self.add_session(db, "Skip Ineligible", 2, 20.0)
                a = self.add_member(db, 1, 100.0)
                b = self.add_member(db, 2, 100.0)
                w_poor = self.add_member(db, 3, 5.0, "active")
                w_exp = self.add_member(db, 4, 100.0, "active")
                w_ok1 = self.add_member(db, 5, 100.0, "active")
                w_ok2 = self.add_member(db, 6, 100.0, "active")
            self.assertTrue(app.book_session(db, a, sid)[0])
            self.assertTrue(app.book_session(db, b, sid)[0])
            ba, bb = app.booking_id_of(db, a, sid), app.booking_id_of(db, b, sid)
            for wm in (w_poor, w_exp, w_ok1, w_ok2):
                self.assertTrue(app.join_waitlist(db, wm, sid)[0])
            with db:
                db.execute("UPDATE Members SET membership_status='expired' WHERE member_id=?", (w_exp,))

        def make_cancel(mid, bid):
            def task(barrier):
                with closing(self.connect()) as conn:
                    barrier.wait()
                    return app.cancel_booking(conn, mid, bid)
            return task

        res = self.run_barrier([make_cancel(a, ba), make_cancel(b, bb)])
        self.assertTrue(all(ok for ok, _ in res))
        with closing(self.connect()) as db:
            self.assertEqual(app.taken_of(db, sid), 2)
            booked = {
                r[0] for r in db.execute(
                    "SELECT member_id FROM Bookings WHERE session_id=? AND status='confirmed'",
                    (sid,),
                )
            }
            self.assertEqual(booked, {w_ok1, w_ok2})
            self.assertEqual(app.balance_of(db, w_poor), 5.0)
            self.assertEqual(app.balance_of(db, w_exp), 100.0)
            self.assertEqual(app.balance_of(db, w_ok1), 80.0)
            self.assertEqual(app.balance_of(db, w_ok2), 80.0)
            self.assertEqual(app.balance_of(db, a), 100.0)
            self.assertEqual(app.balance_of(db, b), 100.0)
            rem = [
                r[0] for r in db.execute(
                    "SELECT member_id FROM Waitlist WHERE session_id=? "
                    "ORDER BY created_at ASC, waitlist_id ASC",
                    (sid,),
                )
            ]
            self.assertEqual(rem, [w_poor, w_exp])


if __name__ == "__main__":
    unittest.main(verbosity=2)