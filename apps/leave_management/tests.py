from datetime import date, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from apps.employees.models import Employee
from apps.attendance.models import Attendance
from .models import LeaveBalance, LeaveRequest, LeaveStatusHistory, LeaveType, PublicHoliday
from .services import  (calculate_working_days,calculate_working_days_by_year, change_leave_status, create_leave_request,restore_leave_balance,)
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from .events import leave_cancelled, leave_decision_made
from django.db import(
    close_old_connections,
      connection,
      connections,
      transaction,
)
from django.test import TransactionTestCase




class LeaveStatusTransitionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        cls.user = User.objects.create_user(
            email="leave_test_user@example.com",
            password="test-password-123",
            role=User.Role.HR,
        )

        # Adjust required fields if your Employee model needs more data.
        cls.employee = Employee.objects.create(
            employee_id="EMP-TEST-001",
            joining_date=date(2025, 1, 1),
        )

        cls.leave_type = LeaveType.objects.create(
            name="Test Casual Leave",
        )
        
    def create_leave_request(self):
        return LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.leave_type,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 2),
            reason="Testing leave status transitions",
        )
    def create_leave_balance(self, remaining_days, year=2026):
        return LeaveBalance.objects.create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=year,
            remaining_days=remaining_days,
        )
    
    def test_pending_to_approved(self):
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.user,
            remarks="Approved for testing",
        )

        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.APPROVED)
        self.assertEqual(
            LeaveStatusHistory.objects.filter(
                leave_request=leave,
                old_status=LeaveRequest.Status.PENDING,
                new_status=LeaveRequest.Status.APPROVED,
            ).count(),
            1,
        )

    def test_pending_to_rejected(self):
        leave = self.create_leave_request()

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.REJECTED,
            changed_by=self.user,
            remarks="Rejected for testing",
        )

        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.REJECTED)

    def test_pending_to_cancelled(self):
        leave = self.create_leave_request()

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.CANCELLED,
            changed_by=self.user,
            remarks="Cancelled for testing",
        )

        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.CANCELLED)

    def test_approved_to_pending_is_rejected(self):
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)
        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.user,
            remarks="Approved for testing.",
        )

        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk,
                LeaveRequest.Status.PENDING,
                changed_by=self.user,
            )

    def test_status_change_creates_one_history_entry(self):
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)

        initial_count = LeaveStatusHistory.objects.filter(
            leave_request=leave
        ).count()

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.user,
            remarks="Approved for testing.",
        )


        final_count = LeaveStatusHistory.objects.filter(
            leave_request=leave
        ).count()

        self.assertEqual(final_count - initial_count, 1)
    
    def test_non_hr_user_cannot_approve(self):
        employee_user = get_user_model().objects.create_user(
            email="plain@example.com", password="test-password-123",
        )
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.APPROVED,
                changed_by=employee_user, remarks="Trying",
            )
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.PENDING)

    def test_hr_cannot_approve_own_leave(self):
        self.employee.user = self.user
        self.employee.save(update_fields=["user"])
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.APPROVED,
                changed_by=self.user, remarks="Self approval",
            )
    def test_employee_can_cancel_own_pending_leave(self):
        employee_user = get_user_model().objects.create_user(
            email="own_cancel@example.com",
            password="test-password-123",
        )
        self.employee.user = employee_user
        self.employee.save(update_fields=["user"])
        leave = self.create_leave_request()

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.CANCELLED,
            changed_by=employee_user,
            remarks="Plans changed",
        )

        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.CANCELLED)

    def test_invalid_transition_creates_no_history(self):
        leave = self.create_leave_request()
        change_leave_status(
            leave.pk, LeaveRequest.Status.REJECTED,
            changed_by=self.user, remarks="No",
        )
        before = LeaveStatusHistory.objects.filter(leave_request=leave).count()
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.APPROVED,
                changed_by=self.user, remarks="Again",
            )
        after = LeaveStatusHistory.objects.filter(leave_request=leave).count()
        self.assertEqual(before, after)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.REJECTED)

    def test_cancelling_twice_is_rejected(self):
        leave = self.create_leave_request()
        change_leave_status(
            leave.pk, LeaveRequest.Status.CANCELLED,
            changed_by=self.user, remarks="First",
        )
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED,
                changed_by=self.user, remarks="Second",
            )
    def test_missing_yearly_balance_prevents_partial_restoration(self):
        leave = LeaveRequest.objects.create(
                employee=self.employee,
                leave_type=self.leave_type,
                start_date=date(2026, 12, 31),
                end_date=date(2027, 1, 1),
                reason="Missing yearly balance safety test",
            )
    
            # The 2026 balance exists, but the 2027 balance does not.
        balance_2026 = self.create_leave_balance(
                remaining_days=5,
                year=2026,
            )
    
        unused_days = [
                date(2026, 12, 31),
                date(2027, 1, 1),
            ]
    
        result = restore_leave_balance(leave, unused_days)
    
        balance_2026.refresh_from_db()
        leave.refresh_from_db()
    
            # Restoration must not partially update the 2026 balance.
        self.assertFalse(result)
        self.assertEqual(balance_2026.remaining_days, 5)
        self.assertEqual(
                leave.restoration_status,
                LeaveRequest.RestorationStatus.HR_REVIEW,
            )
        self.assertTrue(leave.restoration_review_reason)
    def test_approved_leave_cannot_be_cancelled(self):
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)
        change_leave_status(
            leave.pk, LeaveRequest.Status.APPROVED,
            changed_by=self.user, remarks="Approved",
        )
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED,
                changed_by=self.user, remarks="Cancel",
            )
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.APPROVED)

    def test_user_cannot_cancel_another_employees_request(self):
        other_user = get_user_model().objects.create_user(
            email="other_emp@example.com", password="test-password-123",
        )
        leave = self.create_leave_request()
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED,
                changed_by=other_user, remarks="Not mine",
            )
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.PENDING)

    def test_cancel_without_user_is_rejected(self):
        leave = self.create_leave_request()
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED, remarks="No user",
            )

    def test_approval_creates_audit_event_with_status_change(self):
        from django.contrib.contenttypes.models import ContentType
        from apps.accounts.models import AuditEvent

        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)
        change_leave_status(
            leave.pk, LeaveRequest.Status.APPROVED,
            changed_by=self.user, remarks="Approved",
        )
        event = AuditEvent.objects.get(
            actor=self.user,
            action="APPROVE",
            content_type=ContentType.objects.get_for_model(LeaveRequest),
            object_id=leave.pk,
        )
        self.assertEqual(event.changes["status"]["old"], "PENDING")
        self.assertEqual(event.changes["status"]["new"], "APPROVED")
    def test_leave_balance_is_not_restored_twice(self):
        leave = LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.leave_type,
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 3),
            reason="Double restoration safety test",
        )
        balance = self.create_leave_balance(remaining_days=5, year=2026)
        unused_days = [date(2026, 11, 2), date(2026, 11, 3)]

        result = restore_leave_balance(leave, unused_days)
        balance.refresh_from_db()
        self.assertTrue(result)
        self.assertEqual(balance.remaining_days, 7)

        result = restore_leave_balance(leave, unused_days)
        balance.refresh_from_db()
        leave.refresh_from_db()
        self.assertTrue(result)
        self.assertEqual(balance.remaining_days, 7)
        self.assertEqual(
            leave.restoration_status,
            LeaveRequest.RestorationStatus.COMPLETED,
        )
    def _listen_for_decisions(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        leave_decision_made.connect(receiver)
        self.addCleanup(leave_decision_made.disconnect, receiver)
        return received

    def test_approval_emits_leave_decision_event(self):
        received = self._listen_for_decisions()
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=5)

        with self.captureOnCommitCallbacks(execute=True):
            change_leave_status(
                leave.pk, LeaveRequest.Status.APPROVED,
                changed_by=self.user, remarks="Approved",
            )

        self.assertEqual(len(received), 1)
        event = received[0]
        self.assertEqual(event["leave_request_id"], leave.pk)
        self.assertEqual(event["employee_id"], self.employee.pk)
        self.assertEqual(event["old_status"], "PENDING")
        self.assertEqual(event["new_status"], "APPROVED")
        self.assertEqual(event["decided_by_id"], self.user.pk)
        self.assertEqual(event["remarks"], "Approved")
        self.assertIsNotNone(event["decided_at"])

    def test_rejection_for_insufficient_balance_emits_rejected_event(self):
        received = self._listen_for_decisions()
        leave = self.create_leave_request()
        self.create_leave_balance(remaining_days=0)

        with self.captureOnCommitCallbacks(execute=True):
            change_leave_status(
                leave.pk, LeaveRequest.Status.APPROVED,
                changed_by=self.user, remarks="Approving",
            )

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["new_status"], "REJECTED")

    def test_cancellation_emits_no_decision_event(self):
        received = self._listen_for_decisions()
        leave = self.create_leave_request()

        with self.captureOnCommitCallbacks(execute=True):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED,
                changed_by=self.user, remarks="Cancelled",
            )

        self.assertEqual(received, [])

    def test_failed_decision_emits_no_event(self):
        received = self._listen_for_decisions()
        other_user = get_user_model().objects.create_user(
            email="not_hr@example.com", password="test-password-123",
        )
        leave = self.create_leave_request()

        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValidationError):
                change_leave_status(
                    leave.pk, LeaveRequest.Status.APPROVED,
                    changed_by=other_user, remarks="Not allowed",
                )

        self.assertEqual(received, [])
    def _listen_for_cancellations(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        leave_cancelled.connect(receiver)
        self.addCleanup(leave_cancelled.disconnect, receiver)
        return received

    def test_cancellation_emits_cancelled_event_not_decision(self):
        decisions = self._listen_for_decisions()
        cancellations = self._listen_for_cancellations()
        leave = self.create_leave_request()

        with self.captureOnCommitCallbacks(execute=True):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED,
                changed_by=self.user, remarks="Cancelled by HR",
            )

        self.assertEqual(decisions, [])
        self.assertEqual(len(cancellations), 1)
        event = cancellations[0]
        self.assertEqual(event["leave_request_id"], leave.pk)
        self.assertEqual(event["employee_id"], self.employee.pk)
        self.assertEqual(event["old_status"], "PENDING")
        self.assertEqual(event["new_status"], "CANCELLED")
        self.assertEqual(event["cancelled_by_id"], self.user.pk)
        self.assertFalse(event["cancelled_by_owner"])
        self.assertEqual(event["remarks"], "Cancelled by HR")
        self.assertIsNotNone(event["cancelled_at"])

    def test_cancellation_by_owner_is_flagged(self):
        cancellations = self._listen_for_cancellations()
        employee_user = get_user_model().objects.create_user(
            email="owner_cancel@example.com", password="test-password-123",
        )
        self.employee.user = employee_user
        self.employee.save(update_fields=["user"])
        leave = self.create_leave_request()

        with self.captureOnCommitCallbacks(execute=True):
            change_leave_status(
                leave.pk, LeaveRequest.Status.CANCELLED,
                changed_by=employee_user, remarks="Plans changed",
            )

        self.assertEqual(len(cancellations), 1)
        self.assertTrue(cancellations[0]["cancelled_by_owner"])

    def test_failed_cancellation_emits_no_event(self):
        cancellations = self._listen_for_cancellations()
        other_user = get_user_model().objects.create_user(
            email="stranger@example.com", password="test-password-123",
        )
        leave = self.create_leave_request()

        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValidationError):
                change_leave_status(
                    leave.pk, LeaveRequest.Status.CANCELLED,
                    changed_by=other_user, remarks="Not mine",
                )

        self.assertEqual(cancellations, [])
        
class LeaveApplicationValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.hr_user = User.objects.create_user(
            email="validation_hr@example.com",
            password="test-password-123",
            role=User.Role.HR,
        )

        cls.employee = Employee.objects.create(
            employee_id="EMP-TEST-002",
            email="employee002@example.com",
            joining_date=date(2025, 1, 1),
        )

        cls.other_employee = Employee.objects.create(
            employee_id="EMP-TEST-003",
            email="employee003@example.com",
            joining_date=date(2025, 1, 1),
        )

        cls.leave_type = LeaveType.objects.create(
            name="Validation Casual Leave",
        )
        for emp in (cls.employee, cls.other_employee):
            LeaveBalance.objects.create(
                employee=emp,
                leave_type=cls.leave_type,
                year=2026,
                remaining_days=20,
            )

    def create_request(self, employee=None, start=None, end=None):
        return create_leave_request(
            employee=employee or self.employee,
            leave_type=self.leave_type,
            start_date=start or date(2026, 11, 10),
            end_date=end or date(2026, 11, 12),
            reason="Testing validation",
        )

    def test_valid_request_is_created(self):
        leave = self.create_request()

        self.assertEqual(leave.status, LeaveRequest.Status.PENDING)
        self.assertEqual(leave.employee, self.employee)

    def test_end_date_before_start_date_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_request(
                start=date(2026, 11, 12),
                end=date(2026, 11, 10),
            )

    def test_overlapping_pending_request_is_rejected(self):
        self.create_request()

        with self.assertRaises(ValidationError):
            self.create_request(
                start=date(2026, 11, 11),
                end=date(2026, 11, 13),
            )

    def test_overlapping_approved_request_is_rejected(self):
        leave = self.create_request()
    
        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.hr_user,
            remarks="Approved for testing.",
        )

        with self.assertRaises(ValidationError):
            self.create_request(
                start=date(2026, 11, 11),
                end=date(2026, 11, 13),
            )

    def test_rejected_request_does_not_block_new_request(self):
        leave = self.create_request()

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.REJECTED,
            changed_by=self.hr_user,
            remarks="Rejected for testing.",
        )

        new_leave = self.create_request(
            start=date(2026, 11, 11),
            end=date(2026, 11, 13),
        )

        self.assertEqual(new_leave.status, LeaveRequest.Status.PENDING)

    def test_different_employee_can_request_same_dates(self):
        self.create_request()

        other_leave = self.create_request(
            employee=self.other_employee,
        )
        self.assertEqual(other_leave.employee, self.other_employee)
        
    def test_cancelled_request_does_not_block_new_request(self):
        leave = self.create_request()
        change_leave_status(
            leave.pk, LeaveRequest.Status.CANCELLED,
            changed_by=self.hr_user, remarks="Cancelled",
        )
        new_leave = self.create_request(
            start=date(2026, 11, 11), end=date(2026, 11, 13),
        )
        self.assertEqual(new_leave.status, LeaveRequest.Status.PENDING)

    def test_request_exceeding_balance_is_rejected_without_changes(self):
        balance = LeaveBalance.objects.get(employee=self.employee, year=2026)
        balance.remaining_days = 2   # 10 to 12 Nov is 3 working days
        balance.save()
        with self.assertRaises(ValidationError):
            self.create_request()
        self.assertFalse(
            LeaveRequest.objects.filter(employee=self.employee).exists()
        )
        balance.refresh_from_db()
        self.assertEqual(balance.remaining_days, 2)

    def test_request_without_balance_for_year_is_rejected(self):
        LeaveBalance.objects.filter(employee=self.employee).delete()
        with self.assertRaises(ValidationError):
            self.create_request()

    def test_terminated_employee_cannot_apply(self):
        self.employee.employment_status = Employee.EmploymentStatus.TERMINATED
        self.employee.save(update_fields=["employment_status"])
        with self.assertRaises(ValidationError):
            self.create_request()

    def test_inactive_leave_type_is_rejected(self):
        self.leave_type.is_active = False
        self.leave_type.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.create_request()

    def test_application_locks_the_employee_row(self):
        with patch.object(
            Employee.objects,
            "select_for_update",
            wraps=Employee.objects.select_for_update,
        ) as lock:
            self.create_request()
        lock.assert_called_once()
    
class WorkingDayCalculationTests(TestCase):
    def test_monday_to_friday_counts_five_days(self):
        days = calculate_working_days(
            date(2026, 10, 19),  # Monday
            date(2026, 10, 23),  # Friday
        )
        self.assertEqual(days, 5)

    def test_weekend_counts_zero_days(self):
        days = calculate_working_days(
            date(2026, 10, 24),  # Saturday
            date(2026, 10, 25),  # Sunday
        )
        self.assertEqual(days, 0)

    def test_public_holiday_is_excluded(self):
        PublicHoliday.objects.create(
            date=date(2026, 10, 21),  # Wednesday
            name="Test Holiday",
        )

        days = calculate_working_days(
            date(2026, 10, 19),
            date(2026, 10, 23),
        )
        self.assertEqual(days, 4)

    def test_dates_spanning_two_years(self):
        days = calculate_working_days(
            date(2026, 12, 31),  # Thursday
            date(2027, 1, 4),    # Monday
        )
        self.assertEqual(days, 3)

    def test_end_date_before_start_date_is_rejected(self):
        with self.assertRaises(ValidationError):
            calculate_working_days(
                date(2026, 10, 23),
                date(2026, 10, 19),
            )
    def test_working_days_are_split_by_calendar_year(self):
        yearly_days = calculate_working_days_by_year(
            date(2026, 12, 31),  # Thursday
            date(2027, 1, 4),    # Monday
        )

        self.assertEqual(yearly_days, {
            2026: 1,
            2027: 2,
        })

    def test_yearly_breakdown_rejects_invalid_dates(self):
        with self.assertRaises(ValidationError):
            calculate_working_days_by_year(
                date(2027, 1, 5),
                date(2027, 1, 1),
            )
class LeaveBalanceApprovalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.hr_user = User.objects.create_user(
            email="balance_hr@example.com",
            password="test-password-123",
            role=User.Role.HR,
        )
        cls.employee = Employee.objects.create(
            employee_id="EMP-BAL-001",
            email="balance_test@example.com",
            joining_date=date(2025, 1, 1),
        )
        cls.leave_type = LeaveType.objects.create(
            name="Balance Test Leave",
        )

    def create_balance(self, year, remaining_days):
        return LeaveBalance.objects.create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=year,
            remaining_days=remaining_days,
        )

    def create_pending_request(self, start_date, end_date):
        # Created directly, so the approval-time checks can be tested
        # even when the submission-time balance check would reject it.
        return LeaveRequest.objects.create(
            employee=self.employee,
            leave_type=self.leave_type,
            start_date=start_date,
            end_date=end_date,
            reason="Testing leave balance",
        )

    def test_pending_request_does_not_deduct_balance(self):
        balance = self.create_balance(2026, 10)

        leave = create_leave_request(
            employee=self.employee,
            leave_type=self.leave_type,
            start_date=date(2026, 11, 10),
            end_date=date(2026, 11, 12),
            reason="Testing pending balance",
        )

        leave.refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.PENDING)
        self.assertEqual(balance.remaining_days, 10)

    def test_approval_deducts_working_days_from_balance(self):
        balance = self.create_balance(2026, 10)
        leave = create_leave_request(
            employee=self.employee,
            leave_type=self.leave_type,
            start_date=date(2026, 11, 10),
            end_date=date(2026, 11, 12),
            reason="Testing approval deduction",
        )

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.hr_user,
            remarks="Approved for testing.",
        )

        leave.refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.APPROVED)
        self.assertEqual(balance.remaining_days, 7)

    def test_insufficient_balance_rejects_approval(self):
        balance = self.create_balance(2026, 2)
        leave = self.create_pending_request(
            date(2026, 11, 10), date(2026, 11, 12)
        )

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.hr_user,
            remarks="Approval requested for testing.",
        )

        leave.refresh_from_db()
        balance.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.REJECTED)
        self.assertEqual(balance.remaining_days, 2)

    def test_cross_year_leave_deducts_from_each_year(self):
        balance_2026 = self.create_balance(2026, 5)
        balance_2027 = self.create_balance(2027, 5)
        leave = create_leave_request(
            employee=self.employee,
            leave_type=self.leave_type,
            start_date=date(2026, 12, 31),
            end_date=date(2027, 1, 4),
            reason="Testing cross-year deduction",
        )

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.hr_user,
            remarks="Approved for testing.",
        )

        leave.refresh_from_db()
        balance_2026.refresh_from_db()
        balance_2027.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.APPROVED)
        self.assertEqual(balance_2026.remaining_days, 4)
        self.assertEqual(balance_2027.remaining_days, 3)

    def test_cross_year_insufficient_balance_deducts_nothing(self):
        balance_2026 = self.create_balance(2026, 5)
        balance_2027 = self.create_balance(2027, 1)
        leave = self.create_pending_request(
            date(2026, 12, 31), date(2027, 1, 4)
        )

        change_leave_status(
            leave.pk,
            LeaveRequest.Status.APPROVED,
            changed_by=self.hr_user,
            remarks="Approval requested for testing.",
        )

        leave.refresh_from_db()
        balance_2026.refresh_from_db()
        balance_2027.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.Status.REJECTED)
        self.assertEqual(balance_2026.remaining_days, 5)
        self.assertEqual(balance_2027.remaining_days, 1)

    def test_approval_without_remarks_raises_validation_error(self):
        leave = self.create_pending_request(
            date(2026, 11, 10), date(2026, 11, 10)
        )
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk,
                LeaveRequest.Status.APPROVED,
                changed_by=self.hr_user,
            )

    def test_rejection_without_remarks_raises_validation_error(self):
        leave = self.create_pending_request(
            date(2026, 11, 10), date(2026, 11, 10)
        )
        with self.assertRaises(ValidationError):
            change_leave_status(
                leave.pk,
                LeaveRequest.Status.REJECTED,
                changed_by=self.hr_user,
            )
class LeaveRequestConcurrencyTests(TransactionTestCase):
    """
    Verify that PostgreSQL serializes overlapping leave applications
    for the same employee.
    """

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest(
                "This integration test requires PostgreSQL."
            )

        self.employee = Employee.objects.create(
            employee_id="EMP-CONCURRENCY-001",
            email="concurrency_test@example.com",
            full_name="Concurrency Test Employee",
            joining_date=date(2025, 1, 1),
        )

        self.leave_type = LeaveType.objects.create(
            name="Concurrency Test Leave",
        )

        LeaveBalance.objects.create(
            employee=self.employee,
            leave_type=self.leave_type,
            year=2026,
            remaining_days=20,
        )

    def test_overlapping_requests_are_serialized(self):
        first_created = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        employee_id = self.employee.pk
        leave_type_id = self.leave_type.pk

        first_app = "leave_concurrency_first"
        second_app = "leave_concurrency_second"

        outcomes = {}

        def set_application_name(app_name):
            with connections["default"].cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('application_name', %s, false)",
                    [app_name],
                )

        def first_request():
            close_old_connections()

            try:
                if connections["default"].vendor != "postgresql":
                    raise AssertionError(
                        "First worker is not using PostgreSQL."
                    )

                set_application_name(first_app)

                employee = Employee.objects.get(pk=employee_id)
                leave_type = LeaveType.objects.get(pk=leave_type_id)

                with transaction.atomic():
                    create_leave_request(
                        employee=employee,
                        leave_type=leave_type,
                        start_date=date(2026, 11, 10),
                        end_date=date(2026, 11, 12),
                        reason="First concurrent request",
                    )

                    # The service has returned, but the outer transaction
                    # still holds the employee-row lock.
                    first_created.set()

                    if not release_first.wait(timeout=20):
                        raise TimeoutError(
                            "First transaction was not released."
                        )

                outcomes["first"] = "success"

            except Exception as exc:
                outcomes["first_error"] = repr(exc)

                # Unblock the second worker if the first worker fails.
                first_created.set()

            finally:
                close_old_connections()

        def second_request():
            close_old_connections()

            try:
                if connections["default"].vendor != "postgresql":
                    raise AssertionError(
                        "Second worker is not using PostgreSQL."
                    )

                set_application_name(second_app)

                if not first_created.wait(timeout=10):
                    raise TimeoutError(
                        "First request did not reach the synchronization point."
                    )

                employee = Employee.objects.get(pk=employee_id)
                leave_type = LeaveType.objects.get(pk=leave_type_id)

                # Signal immediately before the service attempts the lock.
                second_started.set()

                create_leave_request(
                    employee=employee,
                    leave_type=leave_type,
                    start_date=date(2026, 11, 11),
                    end_date=date(2026, 11, 13),
                    reason="Second concurrent request",
                )

                outcomes["second"] = "success"

            except ValidationError:
                outcomes["second"] = "rejected"

            except Exception as exc:
                outcomes["second_error"] = repr(exc)

            finally:
                close_old_connections()

        def second_is_blocked_by_first():
            deadline = time.monotonic() + 15

            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_stat_activity AS blocked
                            CROSS JOIN LATERAL
                                unnest(
                                    pg_blocking_pids(blocked.pid)
                                ) AS blocker(pid)
                            JOIN pg_stat_activity AS blocking
                                ON blocking.pid = blocker.pid
                            WHERE blocked.application_name = %s
                              AND blocking.application_name = %s
                              AND blocked.wait_event_type = 'Lock'
                        )
                        """,
                        [second_app, first_app],
                    )

                    if cursor.fetchone()[0]:
                        return True

                time.sleep(0.05)

            return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(first_request)
            second_future = None

            try:
                self.assertTrue(
                    first_created.wait(timeout=10),
                    "First request did not reach the synchronization point.",
                )

                # Do not start the second worker if the first already failed.
                self.assertNotIn(
                    "first_error",
                    outcomes,
                    f"First worker failed: {outcomes.get('first_error')}",
                )

                second_future = executor.submit(second_request)

                self.assertTrue(
                    second_started.wait(timeout=10),
                    "Second request did not start.",
                )

                self.assertTrue(
                    second_is_blocked_by_first(),
                    (
                        "PostgreSQL did not report the second transaction "
                        "waiting on a lock held by the first."
                    ),
                )

            finally:
                # Always allow the first transaction to finish.
                release_first.set()

            first_future.result(timeout=25)

            if second_future is not None:
                second_future.result(timeout=25)

        self.assertNotIn(
            "first_error",
            outcomes,
            f"First worker failed: {outcomes.get('first_error')}",
        )

        self.assertNotIn(
            "second_error",
            outcomes,
            f"Second worker failed: {outcomes.get('second_error')}",
        )

        self.assertEqual(outcomes.get("first"), "success")
        self.assertEqual(outcomes.get("second"), "rejected")

        blocking_requests = LeaveRequest.objects.filter(
            employee_id=employee_id,
            status__in=[
                LeaveRequest.Status.PENDING,
                LeaveRequest.Status.APPROVED,
            ],
            start_date__lte=date(2026, 11, 13),
            end_date__gte=date(2026, 11, 10),
        )

        self.assertEqual(blocking_requests.count(), 1)