from django.apps import AppConfig
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models.signals import post_migrate

AUDIT_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON {t}
       BEGIN SELECT RAISE(ABORT, 'AuditLog is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON {t}
       BEGIN SELECT RAISE(ABORT, 'AuditLog is append-only'); END""",
]


def install_audit_triggers(sender, using=DEFAULT_DB_ALIAS, **kwargs):
    """The audit log is append-only: SQLite itself refuses to change or delete a row
    (same triggers as in the Flask version). Re-created after every `migrate`."""
    from .models import AuditLog

    conn = connections[using]
    if conn.vendor != "sqlite":
        return
    table = AuditLog._meta.db_table
    with conn.cursor() as cursor:
        if table not in conn.introspection.table_names(cursor):
            return
        for stmt in AUDIT_TRIGGERS:
            cursor.execute(stmt.format(t=table))


class GymConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gym"

    def ready(self):
        post_migrate.connect(install_audit_triggers, sender=self)
