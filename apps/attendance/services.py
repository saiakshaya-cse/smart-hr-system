from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from apps.accounts.models import User
from .models import Attendance
from .events import emit_attendance_corrected
def _ensure_own_record(employee,actor):
    if actor is None or employee.user_id != actor.pk:
        raise ValidationError(
            "You can only record your own attendance."
        )
@transaction.atomic
def check_in(employee,actor):
    """Record one check-in per employee per day."""
    _ensure_own_record(employee, actor)
    today = timezone.localdate()


    if not employee.is_active_employee:
        raise ValidationError("Only active employees can check in.")

    existing = Attendance.objects.filter(
        employee=employee,
        date=today,
    ).first()

    if existing is not None:
        raise ValidationError(
            "Attendance already exists for this employee today."
        )

    try:
        attendance = Attendance.objects.create(
            employee=employee,
            date=today,
            check_in=timezone.now(),
            status=Attendance.Status.PRESENT,
        )
    except IntegrityError as exc:
        raise ValidationError(
            "Attendance already exists for this employee today."
        ) from exc

    return attendance


@transaction.atomic
def check_out(employee, actor):
    """Record one check-out after the employee has checked in."""
    _ensure_own_record(employee, actor)
    today = timezone.localdate()

    if not employee.is_active_employee:
        raise ValidationError("Only active employees can check out.")

    attendance = (
        Attendance.objects.select_for_update()
        .filter(employee=employee, date=today)
        .first()
    )

    if attendance is None or attendance.check_in is None:
        raise ValidationError(
            "You must check in before checking out."
        )

    if attendance.check_out is not None:
        raise ValidationError(
            "You have already checked out today."
        )

    checkout_time = timezone.now()

    if checkout_time <= attendance.check_in:
        raise ValidationError(
            "Check-out time must be later than check-in time."
        )

    attendance.check_out = checkout_time
    attendance.save(update_fields=["check_out", "updated_at"])

    return attendance


@transaction.atomic
def correct_attendance(
    attendance_id,
    actor,
    *,
    status=None,
    check_in=None,
    check_out=None,
    reason="",
):
    """Allow HR  to correct attendance with an audit record."""
    if actor is None or actor.role!=User.Role.HR:
        raise ValidationError(
            "Only HR  can correct attendance."
        )

    if not reason or not reason.strip():
        raise ValidationError(
            "A reason is required for attendance correction."
        )

    attendance = (
        Attendance.objects.select_for_update()
        .filter(pk=attendance_id)
        .first()
    )

    if attendance is None:
        raise ValidationError("Attendance record not found.")
    old_values = {
        "status": attendance.status,
        "check_in": attendance.check_in,
        "check_out": attendance.check_out,
    }
    if status is not None:
        if status not in Attendance.Status.values:
            raise ValidationError("Invalid attendance status.")
        attendance.status = status

    if check_in is not None:
        attendance.check_in = check_in

    if check_out is not None:
        attendance.check_out = check_out

    if (
        attendance.check_in is not None
        and attendance.check_out is not None
        and attendance.check_out <= attendance.check_in
    ):
        raise ValidationError(
            "Check-out time must be later than check-in time."
        )
    attendance.corrected_by = actor
    attendance.corrected_at = timezone.now()
    attendance.correction_note = reason.strip()

    # The audit event is recorded by AuditedModelMixin on save
    # (actor from audit_actor), so no separate log_audit call is needed.
    attendance.save(
        update_fields=[
            "status",
            "check_in",
            "check_out",
            "corrected_by",
            "corrected_at",
            "correction_note",
            "updated_at",
        ],
        audit_actor=actor,
    )

    emit_attendance_corrected(
        attendance, actor, old_values, attendance.correction_note
        )
    return attendance


def get_monthly_attendance_summary(employee, year, month):
    """Return attendance counts for one employee in a calendar month."""
    if not 1 <= month <= 12:
        raise ValidationError("Month must be between 1 and 12.")

    if year < 1 or year > 9999:
        raise ValidationError("Year must be between 1 and 9999.")

    start_date = date(year, month, 1)

    if month == 12:
        
        if year == 9999:
            end_date = None
        else:
            end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)

    records = Attendance.objects.filter(
        employee=employee,
        date__gte=start_date,
    )

    if end_date is not None:
        records = records.filter(date__lt=end_date)

    counts = records.values("status").annotate(total=Count("id"))

    summary = {
        status: 0
        for status, _label in Attendance.Status.choices
    }

    for item in counts:
        summary[item["status"]] = item["total"]

    summary["total_days"] = records.count()
    summary["employee_id"] = employee.employee_id
    summary["year"] = year
    summary["month"] = month

    return summary

