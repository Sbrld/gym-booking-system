from django.contrib import admin

from .models import Member, PasswordReset


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "email", "membership_status", "role", "balance")
    list_filter = ("membership_status", "role")
    search_fields = ("name", "email")
    exclude = ("password", "groups", "user_permissions", "last_login")  # passwords are never edited here


@admin.register(PasswordReset)
class PasswordResetAdmin(admin.ModelAdmin):
    list_display = ("member", "expires_at", "used")
