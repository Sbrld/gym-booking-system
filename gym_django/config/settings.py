"""Django settings for the Gym Booking System (ported from Flask + SQLite).

Environment variables (all optional locally):
  DJANGO_SECRET_KEY     signing key for sessions / CSRF (set a strong value in production)
  DJANGO_DEBUG          "1"/"true"/"yes" (default "1" locally; "0" on Render)
  DJANGO_ALLOWED_HOSTS  comma-separated hosts (Render's own host is added automatically)
  DJANGO_TIME_ZONE      default Asia/Almaty
  DATABASE_PATH         SQLite file (default gym.db next to manage.py)
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", "1")

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = []
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _render_host:  # set automatically by Render
    ALLOWED_HOSTS.append(_render_host)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "gym",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",  # standard DTL
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

_db_path = Path(os.environ.get("DATABASE_PATH", "gym.db"))
if not _db_path.is_absolute():
    _db_path = BASE_DIR / _db_path
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": _db_path}}

AUTH_USER_MODEL = "accounts.Member"
LOGIN_URL = "/login"
# No AUTH_PASSWORD_VALIDATORS on purpose: the team's rule is MIN_PASSWORD = 4 (demo value, see gym/constants.py).

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Almaty")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if "test" in sys.argv:  # scrypt/PBKDF2 are slow on purpose; use a fast hasher only in tests
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
