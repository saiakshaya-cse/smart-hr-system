from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from datetime import date, timedelta
from .models import LeaveBalance, LeaveRequest, LeaveStatusHistory, PublicHoliday
from apps.attendance.models import Attendance
from apps.accounts.audit import log_audit
from apps.employees.models import Employee
from .events import emit_leave_cancelled, emit_leave_decision

ALLOWED_TRANSITIONS = {
    LeaveRequest.Status.PENDING: {
        LeaveRequest.Status.APPROVED,
        LeaveRequest.Status.REJECTED,
        LeaveRequest.Status.CANCELLED,
    },
}

BLOCKING_LEAVE_STATUSES = [
    LeaveRequest.Status.PENDING,
    LeaveRequest.Status.APPROVED,
]

def calculate_working_days(start_date, end_date):
    """
    Count working days inclusively, excluding weekends
    and configured public holidays.
    """

    if start_date > end_date:
        raise ValidationError(
            "Leave end date cannot be before the start date."
        )

    holidays = set(
        PublicHoliday.objects.filter(
            date__range=(start_date, end_date)
        ).values_list("date", flat=True)
    )

    working_days = 0
    current_date = start_date

    while current_date <= end_date:
        # weekday(): Monday = 0, Sunday = 6
        is_weekday = current_date.weekday() < 5
        is_public_holiday = current_date in holidays

        if is_weekday and not is_public_holiday:
            working_days += 1

        current_date += timedelta(days=1)

    return working_days

def calculate_working_days_by_year(start_date, end_date):
    """Return working-day counts grouped by calendar year."""

    if start_date > end_date:
        raise ValidationError(
            "Leave end date cannot be before the start date."
        )

    yearly_days = {}
    current_year = start_date.year

    while current_year <= end_date.year:
        year_start = max(
            start_date,
            date(current_year, 1, 1),
        )
        year_end = min(
            end_date,
            date(current_year, 12, 31),
        )

        yearly_days[current_year] = calculate_working_days(
            year_start,
            year_end,
        )

        current_year += 1

    return yearly_days
def get_unused_leave_days(leave_request):
    """
    Identify unused working days using attendance records.

    Return None if any relevant attendance record is missing
    or its status is unclear. Otherwise, return unused dates.
    """
    today = timezone.localdate()

    # Leave has not started: all approved working days are unused.
    if today < leave_request.start_date:
        unused_days = []

        current_date = leave_request.start_date
        while current_date <= leave_request.end_date:
            if calculate_working_days(current_date, current_date):
                unused_days.append(current_date)
            current_date += timedelta(days=1)

        return unused_days

    # Leave has started: inspect attendance records.
    working_dates = []
    current_date = leave_request.start_date

    while current_date <= min(leave_request.end_date, today):
        if calculate_working_days(current_date, current_date):
            working_dates.append(current_date)
        current_date += timedelta(days=1)

    attendance_records = {
        record.date: record
        for record in Attendance.objects.filter(
            employee=leave_request.employee,
            date__in=working_dates,
        )
    }

    unused_days = []

    for day in working_dates:
        record = attendance_records.get(day)

        if record is None:
            return None

        if record.status in {
            Attendance.Status.PRESENT,
            Attendance.Status.LATE,
        }:
            unused_days.append(day)

        elif record.status == Attendance.Status.ON_LEAVE:
            continue

        else:
            # ABSENT and HALF_DAY need HR interpretation.
            return None

    # Future working days have not yet been taken.
    current_date = max(leave_request.start_date, today + timedelta(days=1))

    while current_date <= leave_request.end_date:
        if calculate_working_days(current_date, current_date):
            unused_days.append(current_date)
        current_date += timedelta(days=1)

    return unused_days
@transaction.atomic
def restore_leave_balance(leave_request, unused_days):
    """
    Restore eligible unused leave days.
    If any required yearly balance is missing, restore nothing
    and flag the request for HR review.
    """
    if leave_request.restoration_status == (
        LeaveRequest.RestorationStatus.COMPLETED
    ):
        return True

    days_by_year = {}

    for day in unused_days:
        days_by_year[day.year] = days_by_year.get(day.year, 0) + 1

    balances = []

    # Validate every required balance before changing any balance.
    for year, days in days_by_year.items():
        balance = (
            LeaveBalance.objects.select_for_update()
            .filter(
                employee=leave_request.employee,
                leave_type=leave_request.leave_type,
                year=year,
            )
            .first()
        )

        if balance is None:
            leave_request.restoration_status = (
                LeaveRequest.RestorationStatus.HR_REVIEW
            )
            leave_request.restoration_review_reason = (
                f"Leave balance for {year} is missing. "
                "HR must review the restoration."
            )
            leave_request.save(
                update_fields=[
                    "restoration_status",
                    "restoration_review_reason",
                    "updated_at",
                ]
            )
            return False

        balances.append((balance, days))

    # Update balances only after all required balances are found.
    for balance, days in balances:
        balance.remaining_days += days
        balance.save(update_fields=["remaining_days", "updated_at"])

    leave_request.restoration_status = (
        LeaveRequest.RestorationStatus.COMPLETED
    )
    leave_request.restoration_review_reason = ""
    leave_request.restoration_completed_at = timezone.now()
    leave_request.save(
        update_fields=[
            "restoration_status",
            "restoration_review_reason",
            "restoration_completed_at",
            "updated_at",
        ]
    )

    return True
@transaction.atomic
def create_leave_request(
    employee,
    leave_type,
    start_date,
    end_date,
    reason="",
):
    # Serialize concurrent applications for the same employee, so two
    # overlapping requests cannot both pass the overlap check.
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    """Validate and create a leave application."""
    if employee.employment_status == employee.EmploymentStatus.TERMINATED:
        raise ValidationError(
            "A terminated employee cannot apply for leave."
        )

    if not leave_type.is_active:
        raise ValidationError("This leave type is not active.")
    if start_date > end_date:
        raise ValidationError(
            "Leave end date cannot be before the start date."
        )

    overlapping_request = LeaveRequest.objects.filter(
        employee=employee,
        status__in=BLOCKING_LEAVE_STATUSES,
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists()

    if overlapping_request:
        raise ValidationError(
            "You already have a pending or approved leave "
            "request overlapping these dates."
        )

    yearly_days = calculate_working_days_by_year(start_date, end_date)

    for year, days in yearly_days.items():
        if days == 0:
            continue

        balance = LeaveBalance.objects.filter(
            employee=employee,
            leave_type=leave_type,
            year=year,
        ).first()

        if balance is None:
            raise ValidationError(
                f"No leave balance is configured for {year}."
            )

        if balance.remaining_days < days:
            raise ValidationError(
                f"Insufficient leave balance for {year}."
            )

    return LeaveRequest.objects.create(
        employee=employee,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
    )

def deduct_leave_balance(leave_request):
    """Deduct leave only when every year's balance is sufficient."""

    yearly_days = calculate_working_days_by_year(
        leave_request.start_date,
        leave_request.end_date,
    )

    balances = []

    # First, validate every year's balance.
    for year, days in yearly_days.items():
        if days == 0:
            continue

        balance = (
            LeaveBalance.objects.select_for_update()
            .filter(
                employee=leave_request.employee,
                leave_type=leave_request.leave_type,
                year=year,
            )
            .first()
        )

        if balance is None or balance.remaining_days < days:
            return False

        balances.append((balance, days))

    # Deduct only after all years pass validation.
    for balance, days in balances:
        balance.remaining_days -= days
        balance.save(
            update_fields=["remaining_days", "updated_at"]
        )

    return True

@transaction.atomic
def change_leave_status(
    leave_request_id,
    new_status,
    changed_by=None,
    remarks="",
):
    leave_request = (
        LeaveRequest.objects.select_for_update()
        .get(pk=leave_request_id)
    )

    old_status = leave_request.status

    if new_status == old_status:
        raise ValidationError(
            "The leave request already has this status."
        )

    if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ValidationError(
            f"Cannot change leave status from {old_status} "
            f"to {new_status}."
        )

    # Only HR/Admin can approve or reject, never their own request,
    # and remarks are required.
    if new_status in {
        LeaveRequest.Status.APPROVED,
        LeaveRequest.Status.REJECTED,
    }:
        if changed_by is None or not changed_by.is_hr:
            raise ValidationError(
                "Only HR or Admin can approve or reject leave."
            )

        if leave_request.employee.user_id == changed_by.pk:
            raise ValidationError(
                "You cannot approve or reject your own leave request."
            )

        if not remarks.strip():
            raise ValidationError(
                "Remarks are required to approve or reject leave."
            )

    # The owner or HR/Admin can cancel a Pending request.
    if new_status == LeaveRequest.Status.CANCELLED:
        is_owner = (
            changed_by is not None
            and leave_request.employee.user_id == changed_by.pk
        )
        if changed_by is None or not (is_owner or changed_by.is_hr):
            raise ValidationError(
                "Only the employee who owns the request, "
                "or HR/Admin, can cancel it."
            )

    # Re-check the balance at approval and deduct it.
    if new_status == LeaveRequest.Status.APPROVED:
        if not deduct_leave_balance(leave_request):
            new_status = LeaveRequest.Status.REJECTED
            remarks = (
                f"{remarks} Insufficient leave balance."
            ).strip()

    leave_request.status = new_status
    update_fields = ["status", "updated_at"]

    if new_status in {
        LeaveRequest.Status.APPROVED,
        LeaveRequest.Status.REJECTED,
    }:
        leave_request.approver = changed_by
        leave_request.approver_remarks = remarks
        leave_request.decided_at = timezone.now()
        update_fields += ["approver", "approver_remarks", "decided_at"]

    leave_request.save(update_fields=update_fields)

    LeaveStatusHistory.objects.create(
        leave_request=leave_request,
        old_status=old_status,
        new_status=new_status,
        changed_by=changed_by,
        remarks=remarks,
    )

    audit_actions = {
        LeaveRequest.Status.APPROVED: "APPROVE",
        LeaveRequest.Status.REJECTED: "REJECT",
        LeaveRequest.Status.CANCELLED: "UPDATE",
    }
    log_audit(
        changed_by,
        audit_actions[new_status],
        leave_request,
        changes={
            "status": {"old": old_status, "new": new_status},
            "remarks": {"old": None, "new": remarks},
        },
    )
    if new_status in {
        LeaveRequest.Status.APPROVED,
        LeaveRequest.Status.REJECTED,
    }:
        emit_leave_decision(leave_request, old_status, changed_by)
    elif new_status == LeaveRequest.Status.CANCELLED:
        emit_leave_cancelled(leave_request, old_status, changed_by, remarks)
    return leave_request