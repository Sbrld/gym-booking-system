"""python manage.py seed_demo [--core] [--reset]

  (no flags)  load the big demo data if the database has no members yet (idempotent)
  --core      load only the small fixed core data (5 members, 4 sessions)
  --reset     delete the SQLite file, migrate from scratch, then seed
              (replaces `python app.py --reset` from the Flask version)
"""
import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

from gym.seed import seed_db


class Command(BaseCommand):
    help = "Load demo data (members, sessions, bookings, payments) if the database is empty."

    def add_arguments(self, parser):
        parser.add_argument("--core", action="store_true", help="only the small core data set")
        parser.add_argument("--reset", action="store_true", help="recreate the SQLite database first")

    def handle(self, *args, **opts):
        if opts["reset"]:
            path = str(settings.DATABASES["default"]["NAME"])
            connection.close()
            if path != ":memory:" and os.path.exists(path):
                os.remove(path)
                self.stdout.write(f"Removed {path}")
            call_command("migrate", interactive=False, verbosity=0)
        created = seed_db(extended=not opts["core"])
        self.stdout.write(self.style.SUCCESS("Demo data loaded." if created else "Database already has data - nothing to do."))
