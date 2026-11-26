"""Stage 1 checks: models, constraints, seed data, append-only audit log.
Run:  python manage.py test gym.tests.test_stage1_models -v 2
"""
from datetime import time, timedelta
from decimal import Decimal

from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import Member
from gym.audit import log_action
from gym.models import AuditLog, Booking, Payment, TrainingSession
from gym.seed import seed_db


class SeedAndConstraintTests(TestCase):
    def setUp(self):
        seed_db(extended=False)
        self.adil = Member.objects.get(email="adil@kbtu.kz")
        self.boxing = TrainingSession.objects.get(title="Boxing")

    def test_core_seed_counts_and_demo_password(self):
        self.assertEqual(Member.objects.count(), 5)
        self.assertEqual(TrainingSession.objects.count(), 4)
        self.assertEqual(Booking.objects.count(), 2)
        self.assertEqual(Payment.objects.count(), 2)
        aldair = Member.objects.get(email="aldair@kbtu.kz")
        self.assertTrue(aldair.is_admin)
        self.assertTrue(aldair.check_password("1234"))
        yoga = TrainingSession.objects.get(title="Yoga")
        self.assertEqual(yoga.bookings.count(), yoga.capacity)  # Yoga is full

    def test_seed_is_idempotent(self):
        self.assertFalse(seed_db(extended=False))
        self.assertEqual(Member.objects.count(), 5)

    def test_unique_member_session(self):
        Booking.objects.create(member=self.adil, session=self.boxing)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(member=self.adil, session=self.boxing)

    def test_capacity_must_be_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrainingSession.objects.create(title="X", trainer="T", session_date=timezone.localdate(),
                                           start_time=time(10, 0), capacity=0)

    def test_bad_role_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Member.objects.filter(pk=self.adil.pk).update(role="superhero")

    def test_cascade_keeps_payment_history(self):
        Booking.objects.create(member=self.adil, session=self.boxing)
        sid = self.boxing.pk  # Django sets instance.pk to None after delete()
        Payment.objects.create(member=self.adil, session_id=sid, amount=Decimal("20"))
        self.boxing.delete()
        self.assertFalse(Booking.objects.filter(member=self.adil).exists())
        self.assertTrue(Payment.objects.filter(member=self.adil, session_id=sid).exists())


class AuditLogTests(TestCase):
    def test_log_action_and_append_only(self):
        entry = log_action(None, "session.create", "Yoga", "details")
        self.assertEqual(entry.actor_name, "system")
        entry.details = "changed"
        with self.assertRaises(RuntimeError):  # model-level guard
            entry.save()
        with self.assertRaises(RuntimeError):
            entry.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():  # SQLite trigger
            AuditLog.objects.filter(pk=entry.pk).update(details="hacked")
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).delete()

    def test_action_needs_a_dot(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditLog.objects.create(actor_name="x", action="nodot")


class ExtendedSeedTests(TestCase):
    def test_extended_demo_data_is_consistent(self):
        seed_db(extended=True)
        self.assertEqual(Member.objects.count(), 5 + 30)
        self.assertGreater(TrainingSession.objects.count(), 50)
        for s in TrainingSession.objects.all():
            self.assertLessEqual(s.bookings.count(), s.capacity, s.title)
        self.assertFalse(Member.objects.filter(balance__lt=0).exists())
        self.assertEqual(Booking.objects.count(), Payment.objects.count())
        self.assertTrue(AuditLog.objects.filter(action="system.seed").exists())
