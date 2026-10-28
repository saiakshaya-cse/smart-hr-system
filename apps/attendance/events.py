import logging

from django.db import transaction
from django.dispatch import Signal

logger = logging.getLogger(__name__)

# Sent after an attendance correction has been committed.
# Keyword arguments: attendance_id, employee_id, corrected_by_id,
# changes ({field: {"old": ..., "new": ...}}), reason.
attendance_corrected = Signal()


def _plain(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def emit_attendance_corrected(attendance, corrected_by, old_values, reason):
    """Send the event after the surrounding transaction commits."""
    changes = {}
    for field, old in old_values.items():
        new = getattr(attendance, field)
        if old != new:
            changes[field] = {"old": _plain(old), "new": _plain(new)}

    payload = {
        "attendance_id": attendance.pk,
        "employee_id": attendance.employee_id,
        "corrected_by_id": corrected_by.pk,
        "changes": changes,
        "reason": reason,
    }

    def _send():
        for receiver, response in attendance_corrected.send_robust(
            sender=attendance.__class__, **payload
        ):
            if isinstance(response, Exception):
                logger.error(
                    "attendance_corrected receiver %r failed: %s",
                    receiver,
                    response,
                )

    transaction.on_commit(_send)