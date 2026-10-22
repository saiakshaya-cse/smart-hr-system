
"""
Audit logging helper for cross-app audit trail.
Can be imported by M3, M4, M5 to log audit events.
"""

from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.accounts.models import User


def log_audit(actor, action, obj, changes=None, request=None):
    """
    Create an audit log entry.

    Args:
        actor: User who performed the action (can be None for system actions)
        action: One of CREATE, UPDATE, DELETE, VIEW, EXPORT, APPROVE, REJECT
        obj: The model instance being audited
        changes: Dict of field changes {field: {'old': val, 'new': val}}
        request: HttpRequest object to capture IP and User-Agent

    Returns:
        AuditEvent instance
    """
    from apps.accounts.models import AuditEvent

    # Get content type for the object
    content_type = ContentType.objects.get_for_model(obj)

    # Prepare changes JSON
    changes_json = None

    if changes:
        def serialize_val(val):
            if val is None:
                return None
            if isinstance(val, (str, int, float, bool)):
                return val
            if isinstance(val, models.Model):
                return str(val)
            if hasattr(val, 'isoformat'):
                return val.isoformat()
            return str(val)

        serialized = {}

        for field, change in changes.items():
            serialized[field] = {
                'old': serialize_val(change.get('old')),
                'new': serialize_val(change.get('new')),
            }

        changes_json = serialized

    # Extract IP and User-Agent from request
    ip_address = None
    user_agent = ''

    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')

        if x_forwarded_for:
            ip_address = x_forwarded_for.split(',')[0].strip()
        else:
            ip_address = request.META.get('REMOTE_ADDR')

        user_agent = request.META.get('HTTP_USER_AGENT', '')[:500] or ''

    # Create object representation
    object_repr = str(obj)[:200]

    # Create audit event
    audit_event = AuditEvent.objects.create(
        actor=actor,
        action=action,
        content_type=content_type,
        object_id=obj.pk,
        object_repr=object_repr,
        changes=changes_json,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    return audit_event


class AuditLogger:
    """
    Context manager for batch audit logging.

    Usage:
        with AuditLogger(request.user, request) as audit:
            audit.log('CREATE', new_employee)
            audit.log('UPDATE', updated_employee, changes)
    """

    def __init__(self, actor, request=None):
        self.actor = actor
        self.request = request
        self.entries = []

    def log(self, action, obj, changes=None):
        """Log an audit event."""
        entry = log_audit(
            self.actor,
            action,
            obj,
            changes,
            self.request,
        )
        self.entries.append(entry)
        return entry

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass  # Entries are saved immediately
