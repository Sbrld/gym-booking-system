from django.contrib import admin

from .models import AuditLog, Booking, Payment, TrainingSession


@admin.register(TrainingSession)
class TrainingSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "trainer", "session_date", "start_time", "capacity", "price")
    search_fields = ("title", "trainer")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "member", "session", "status", "booked_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "member", "session_id", "amount", "status", "created_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only: the audit log is append-only."""
    list_display = ("id", "created_at", "actor_name", "action", "target")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
