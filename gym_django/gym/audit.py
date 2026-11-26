from .models import AuditLog


def log_action(actor, action, target="", details=""):
    """Append one row to AuditLog. actor is a Member, or None for the system."""
    return AuditLog.objects.create(
        actor_id=actor.pk if actor is not None else None,
        actor_name=actor.name if actor is not None else "system",
        action=action, target=target, details=details)
