"""Gym domain models.

Flask -> Django mapping:
  Sessions  -> TrainingSession   (renamed so it is not confused with the HTTP session)
  Bookings  -> Booking           (UNIQUE(member, session) kept)
  Payments  -> Payment           (session_id is a plain integer on purpose: payment history
                                  survives deleting a training - same as in the Flask schema)
  AuditLog  -> AuditLog          (append-only: model guard + SQLite triggers, see apps.py)
"""
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .constants import DEFAULT_SESSION_PRICE


class TrainingSession(models.Model):
    title = models.CharField(max_length=120)
    trainer = models.CharField(max_length=120)
    session_date = models.DateField()
    start_time = models.TimeField()
    capacity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=8, decimal_places=2, default=DEFAULT_SESSION_PRICE)

    class Meta:
        ordering = ["session_date", "start_time", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(capacity__gt=0), name="session_capacity_positive"),
            models.CheckConstraint(condition=Q(price__gte=0), name="session_price_non_negative"),
        ]

    def __str__(self):
        return f"{self.title} {self.session_date} {self.start_time:%H:%M}"

    @property
    def starts_at(self):
        """Timezone-aware start of the training (used for the 'is it in the future?' rule)."""
        return timezone.make_aware(datetime.combine(self.session_date, self.start_time))

    @property
    def is_past(self):
        return self.starts_at <= timezone.now()


class BookingStatus(models.TextChoices):
    CONFIRMED = "confirmed", "confirmed"
    CANCELLED = "cancelled", "cancelled"


class Booking(models.Model):
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings")
    session = models.ForeignKey(TrainingSession, on_delete=models.CASCADE, related_name="bookings")
    status = models.CharField(max_length=10, choices=BookingStatus.choices, default=BookingStatus.CONFIRMED)
    booked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-booked_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["member", "session"], name="booking_unique_member_session"),
            models.CheckConstraint(condition=Q(status__in=["confirmed", "cancelled"]), name="booking_status_valid"),
        ]

    def __str__(self):
        return f"{self.member_id} -> {self.session_id} ({self.status})"


class PaymentStatus(models.TextChoices):
    PAID = "paid", "paid"
    REFUNDED = "refunded", "refunded"


class Payment(models.Model):
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    session_id = models.IntegerField()  # no FK on purpose: history survives deleting a training
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    status = models.CharField(max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.PAID)
    created_at = models.DateTimeField(default=timezone.now)
    refunded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=Decimal("0")), name="payment_amount_non_negative"),
            models.CheckConstraint(condition=Q(status__in=["paid", "refunded"]), name="payment_status_valid"),
        ]

    def __str__(self):
        return f"{self.member_id}: {self.amount} ({self.status})"


class AuditLog(models.Model):
    created_at = models.DateTimeField(default=timezone.now)
    actor_id = models.IntegerField(null=True, blank=True)  # no FK: the log must survive deleting a member
    actor_name = models.CharField(max_length=120)
    action = models.CharField(max_length=60)  # 'kind.what', e.g. session.create
    target = models.CharField(max_length=255, blank=True, default="")
    details = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(condition=Q(action__contains="."), name="audit_action_has_dot"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("AuditLog is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("AuditLog is append-only")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_name} {self.action}"
