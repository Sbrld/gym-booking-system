"""URL configuration. Stage 3 adds the app routes (/, /login, /book, /admin ...).

Django's built-in admin site lives at /django-admin/ so that /admin stays free
for the team's own admin dashboard (same URL as in the Flask version).
"""
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("django-admin/", admin.site.urls),
]
