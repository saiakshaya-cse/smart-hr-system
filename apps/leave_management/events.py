
import logging

from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone

logger = logging.getLogger(__name__)

# Sent after a leave request is approved or rejected and the change has
# been committed. Keyword arguments: leave_request_id, employee_id,
# old_status, new_status, decided_by_id, remarks, decided_at (ISO text).
leave_decision_made = Signal()

# Sent after a Pending leave request is cancelled and the change has been
# committed. Keyword arguments: leave_request_id, employee_id, old_status,
# new_status, cancelled_by_id, cancelled_by_owner, remarks, cancelled_at.
leave_cancelled = Signal()


def _send_after_commit(signal, sender, payload):
    def _send():
        for receiver, response in signal.send_robust(sender=sender, **payload):
            if isinstance(response, Exception):
                logger.error(
                    "%r receiver %r failed: %s", signal, receiver, response
                )

    transaction.on_commit(_send)


def emit_leave_decision(leave_request, old_status, decided_by):
    payload = {
        "leave_request_id": leave_request.pk,
        "employee_id": leave_request.employee_id,
        "old_status": str(old_status),
        "new_status": str(leave_request.status),
        "decided_by_id": decided_by.pk,
        "remarks": leave_request.approver_remarks,
        "decided_at": (
            leave_request.decided_at.isoformat()
            if leave_request.decided_at
            else None
        ),
    }
    _send_after_commit(leave_decision_made, leave_request.__class__, payload)


def emit_leave_cancelled(leave_request, old_status, cancelled_by, remarks):
    payload = {
        "leave_request_id": leave_request.pk,
        "employee_id": leave_request.employee_id,
        "old_status": str(old_status),
        "new_status": str(leave_request.status),
        "cancelled_by_id": cancelled_by.pk,
        "cancelled_by_owner": (
            leave_request.employee.user_id == cancelled_by.pk
        ),
        "remarks": remarks,
        "cancelled_at": timezone.now().isoformat(),
    }
    _send_after_commit(leave_cancelled, leave_request.__class__, payload)