"""Members (custom user model) and password-reset tokens.

Flask -> Django mapping:
  Members.member_id          -> Member.id
  Members.password_hash      -> Member.password (Django PBKDF2 hash)
  Members.locked_until (TEXT)-> DateTimeField
  PasswordResets             -> PasswordReset
"""
from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", "active"
    EXPIRED = "expired", "expired"


class Role(models.TextChoices):
    MEMBER = "member", "member"
    ADMIN = "admin", "admin"


class MemberManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, name, password, **extra):
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("Email is required.")
        member = self.model(email=email, name=name, **extra)
        member.set_password(password)
        member.save(using=self._db)
        return member

    def create_user(self, email, name="", password=None, **extra):
        extra.setdefault("role", Role.MEMBER)
        return self._create(email, name, password, **extra)

    def create_superuser(self, email, name="Admin", password=None, **extra):
        extra.setdefault("role", Role.ADMIN)
        extra["is_superuser"] = True
        return self._create(email, name, password, **extra)


class Member(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)  # stored and queried in lowercase
    membership_status = models.CharField(
        max_length=10, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("100.00"))
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    objects = MemberManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(membership_status__in=["active", "expired"]),
                name="member_status_valid"),
            models.CheckConstraint(
                condition=Q(role__in=["member", "admin"]), name="member_role_valid"),
        ]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    # Lets team admins into Django's built-in admin site (/django-admin/) as well.
    @property
    def is_staff(self):
        return self.is_superuser or self.is_admin

    def has_perm(self, perm, obj=None):
        return self.is_superuser or self.is_admin

    def has_module_perms(self, app_label):
        return self.is_superuser or self.is_admin


class PasswordReset(models.Model):
    token = models.CharField(max_length=64, primary_key=True)  # secrets.token_urlsafe(32)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="password_resets")
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    def __str__(self):
        return f"reset for {self.member_id} (used={self.used})"
