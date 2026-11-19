"""31 automated tests for Gym Booking System (Flask + SQLite).

Run from the folder containing app.py:
    python -m unittest -v test_app

Each test uses its own temporary SQLite database; gym.db is not changed.

Tests 01-10: booking, login, access control.
Tests 11-20: cancellation, registration, admin training management.
Tests 21-31: Sprint 3 payments, refunds, RBAC, lockout, password reset, CSRF, occupancy.
"""

import os
import re
import sqlite3
import tempfile
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
        self.assertIn(b"Upcoming Gym Occupancy", home.data)
        self.assertIn(b"<svg", home.data)
        admin_page = self.client.get("/admin")
        self.assertIn(b"Upcoming Gym Occupancy", admin_page.data)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)