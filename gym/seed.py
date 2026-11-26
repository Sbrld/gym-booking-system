"""Demo data (port of seed_db / seed_extended from the Flask app).

seed_db(extended=True)  -> used by `manage.py seed_demo` (the real gym.db)
seed_db(extended=False) -> core data only (small and fixed), handy for tests
Both do nothing if there are already members (idempotent, safe to run on every start).
"""
import random
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone

from accounts.models import Member, Role
from .audit import log_action
from .constants import DEMO_PASSWORD
from .models import Booking, Payment, TrainingSession
from .seed_data import (
    EXT_CAPACITY_UPGRADE, EXT_DAYS_AHEAD, EXT_DAYS_BACK, EXT_MEMBER_NAMES, EXT_SCHEDULE,
    EXT_START_BALANCE, SEED_BOOKINGS, SEED_MEMBERS, SEED_SESSIONS,
)


def D(value):
    """float constant from seed_data -> Decimal (via str, so 12.0 -> Decimal('12.0'))."""
    return Decimal(str(value))


def day(n):
    """Date n days from today (local time zone)."""
    return timezone.localdate() + timedelta(days=n)


def seed_db(extended=False):
    """Create demo members / sessions / bookings / payments if the database is empty.
    Returns True if data was created."""
    if Member.objects.exists():
        # Safety net from the Flask version: make sure one admin exists.
        if not Member.objects.filter(role=Role.ADMIN).exists():
            first = Member.objects.order_by("id").first()
            if first:
                first.role = Role.ADMIN
                first.save(update_fields=["role"])
        return False

    demo_hash = make_password(DEMO_PASSWORD)  # computed once (hashing is slow)
    with transaction.atomic():
        members = [
            Member.objects.create(name=n, email=e, membership_status=st, role=r, balance=D(bal),
                                  password=demo_hash)
            for n, e, st, r, bal in SEED_MEMBERS]
        sessions = [
            TrainingSession.objects.create(title=t, trainer=tr, session_date=day(off),
                                           start_time=datetime.strptime(tm, "%H:%M").time(),
                                           capacity=cap, price=D(pr))
            for t, tr, off, tm, cap, pr in SEED_SESSIONS]
        now = timezone.now()
        for m, s in SEED_BOOKINGS:  # (member_id, session_id) are 1-based positions in the lists above
            Booking.objects.create(member=members[m - 1], session=sessions[s - 1], booked_at=now)
            Payment.objects.create(member=members[m - 1], session_id=sessions[s - 1].pk,
                                   amount=D(SEED_SESSIONS[s - 1][5]), created_at=now)
        if extended:
            seed_extended()
    return True


def seed_extended():
    """Fill a NEW database with a realistic two-week timetable, 30 more members and their
    bookings + payments. Deterministic (fixed random seed)."""
    rng = random.Random(2026)
    now = timezone.now()
    today = timezone.localdate()
    demo_hash = make_password(DEMO_PASSWORD)

    for title, cap in EXT_CAPACITY_UPGRADE.items():  # bigger halls for the 4 core sessions
        TrainingSession.objects.filter(title=title).update(capacity=cap)

    ext_members = [
        Member.objects.create(name=n, email=f"{n.lower()}@kbtu.kz", membership_status="active",
                              role=Role.MEMBER, balance=D(EXT_START_BALANCE), password=demo_hash)
        for n in EXT_MEMBER_NAMES]
    spent = {m.pk: Decimal("0") for m in ext_members}
    counts = {"sessions": 0, "bookings": 0}

    def add_session(title, trainer, date, time, cap, price, n_booked):
        price = D(price)
        t = datetime.strptime(time, "%H:%M").time()
        ts = TrainingSession.objects.create(title=title, trainer=trainer, session_date=date,
                                            start_time=t, capacity=cap, price=price)
        start = timezone.make_aware(datetime.combine(date, t))
        for m in rng.sample(ext_members, max(0, min(n_booked, cap, len(ext_members)))):
            if start > now:  # future session: booked during the last days
                booked = now - timedelta(hours=rng.randint(1, 24 * 6))
            else:            # past session: booked a few days before it
                booked = start - timedelta(hours=rng.randint(6, 72))
            Booking.objects.create(member=m, session=ts, booked_at=booked)
            Payment.objects.create(member=m, session_id=ts.pk, amount=price, created_at=booked)
            spent[m.pk] += price
            counts["bookings"] += 1
        counts["sessions"] += 1

    for offset in range(-EXT_DAYS_BACK, EXT_DAYS_AHEAD + 1):
        d = today + timedelta(days=offset)
        for title, trainer, weekdays, time, cap, price, fill in EXT_SCHEDULE:
            if d.weekday() in weekdays:
                n = round(cap * fill * rng.uniform(0.8, 1.15) * (0.75 if offset >= 0 else 1.0))
                add_session(title, trainer, d, time, cap, price, n)
    # a tiny private session that is already full, to demo the "session is full" message
    add_session("Private Coaching", "Daniyar", day(2), "16:00", 2, 40.0, 2)

    for m in ext_members:  # never negative
        m.balance = max(Decimal("20"), D(EXT_START_BALANCE) - spent[m.pk]).quantize(Decimal("0.01"))
    Member.objects.bulk_update(ext_members, ["balance"])
    log_action(None, "system.seed", "demo data",
               f"Loaded {len(ext_members)} members, {counts['sessions']} sessions, {counts['bookings']} bookings")
