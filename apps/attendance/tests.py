from django.test import TestCase

from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError
from apps.employees.models import Employee
from apps.attendance.models import Attendance
from apps.attendance.services import check_in, check_out, correct_attendance , get_monthly_attendance_summary
from apps.attendance.events import attendance_corrected


class AttendanceCheckInTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="checkin_user@example.com",
            password="test-password",
        )
        self.employee = Employee.objects.create(
            employee_id="EMP001",
            full_name="Test Employee",
            email="test.employee@example.com",
            joining_date=date(2026, 1, 1),
            user=self.user,
        )
    def test_check_in_creates_attendance(self):
        attendance = check_in(self.employee, self.user)

        self.assertEqual(attendance.employee, self.employee)
        self.assertEqual(attendance.date, timezone.localdate())
        self.assertIsNotNone(attendance.check_in)
        self.assertEqual(attendance.status, Attendance.Status.PRESENT)
    def test_duplicate_check_in_is_rejected(self):
        check_in(self.employee,self.user)

        with self.assertRaises(ValidationError):
            check_in(self.employee,self.user)

        self.assertEqual(
            Attendance.objects.filter(employee=self.employee).count(),
            1,
        )

    def test_inactive_employee_cannot_check_in(self):
        self.employee.employment_status = Employee.EmploymentStatus.TERMINATED
        self.employee.save(update_fields=["employment_status"])

        with self.assertRaises(ValidationError):
            check_in(self.employee,self.user)

        self.assertFalse(
            Attendance.objects.filter(employee=self.employee).exists()
        )

    def test_check_out_after_check_in(self):
        attendance = check_in(self.employee,self.user)

        later_time = attendance.check_in + timezone.timedelta(seconds=1)

        with patch("apps.attendance.services.timezone.now", return_value=later_time):
            result = check_out(self.employee,self.user)

        self.assertEqual(result.pk, attendance.pk)
        self.assertIsNotNone(result.check_out)
        self.assertGreater(result.check_out, result.check_in)

    
    def test_duplicate_check_out_is_rejected(self):
        attendance = check_in(self.employee,self.user)

        later_time = attendance.check_in + timezone.timedelta(seconds=2)

        with patch(
        "apps.attendance.services.timezone.now",
        return_value=later_time,
    ):
         check_out(self.employee,self.user)

        with self.assertRaises(ValidationError):
           check_out(self.employee,self.user)

        attendance.refresh_from_db()
        self.assertIsNotNone(attendance.check_out)


    def test_check_out_without_check_in_is_rejected(self):
        with self.assertRaises(ValidationError):
            check_out(self.employee,self.user)

        self.assertFalse(
            Attendance.objects.filter(employee=self.employee).exists()
        )
    def test_check_in_integrity_error_becomes_validation_error(self):
        with patch(
            "apps.attendance.services.Attendance.objects.create",
            side_effect=IntegrityError,
        ):
            with self.assertRaises(ValidationError):
                check_in(self.employee,self.user)
    def test_user_cannot_check_in_for_another_employee(self):
        intruder = get_user_model().objects.create_user(
            email="intruder@example.com", password="test-password",
        )
        with self.assertRaises(ValidationError):
            check_in(self.employee, intruder)
        self.assertFalse(
            Attendance.objects.filter(employee=self.employee).exists()
        )

    def test_check_in_requires_an_actor(self):
        with self.assertRaises(ValidationError):
            check_in(self.employee, None)

    def test_user_cannot_check_out_for_another_employee(self):
        check_in(self.employee, self.user)
        intruder = get_user_model().objects.create_user(
            email="intruder2@example.com", password="test-password",
        )
        with self.assertRaises(ValidationError):
            check_out(self.employee, intruder)

class AttendanceCorrectionTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            employee_id="EMP002",
            full_name="Correction Test Employee",
            email="correction.employee@example.com",
            joining_date=date(2026, 1, 1),
        )
        User = get_user_model()

        self.hr_user = User.objects.create_user(
            email="hr@example.com",
            password="test-password",
            role=User.Role.HR,
        )
        self.regular_user = User.objects.create_user(
            email="employee@example.com",
            password="test-password",
            role=User.Role.EMPLOYEE,
        )

        self.attendance = Attendance.objects.create(
            employee=self.employee,
            date=timezone.localdate(),
            check_in=timezone.now(),
            status=Attendance.Status.PRESENT,
        )
    def test_check_in_correction_creates_single_audit_event(self):
        from apps.accounts.models import AuditEvent

        correct_attendance(
            self.attendance.pk,
            self.hr_user,
            check_in=self.attendance.check_in - timezone.timedelta(hours=1),
            reason="Fixed check-in time",
        )
        events = AuditEvent.objects.filter(
            actor=self.hr_user,
            object_id=self.attendance.pk,
            action="UPDATE",
        )
        self.assertEqual(events.count(), 1)

    def test_hr_can_correct_attendance_with_reason(self):
        result = correct_attendance(
            self.attendance.pk,
            self.hr_user,
            status=Attendance.Status.LATE,
            reason="HR corrected attendance after reviewing records",
        )

        self.assertEqual(result.status, Attendance.Status.LATE)
        self.assertEqual(result.corrected_by, self.hr_user)
        self.assertIsNotNone(result.corrected_at)
        self.assertEqual(
            result.correction_note,
            "HR corrected attendance after reviewing records",
        )

    def test_employee_cannot_correct_attendance(self):
        with self.assertRaises(ValidationError):
            correct_attendance(
                self.attendance.pk,
                self.regular_user,
                status=Attendance.Status.LATE,
                reason="Test correction",
            )

    def test_correction_requires_reason(self):
        with self.assertRaises(ValidationError):
            correct_attendance(
                self.attendance.pk,
                self.hr_user,
                status=Attendance.Status.LATE,
                reason="   ",
            )
    def test_hr_correction_creates_audit_event(self):
        from apps.accounts.models import AuditEvent
        from django.contrib.contenttypes.models import ContentType

        content_type = ContentType.objects.get_for_model(Attendance)

        correct_attendance(
        self.attendance.pk,
        self.hr_user,
        status=Attendance.Status.LATE,
        reason="Verified correction for audit test",
    )

        audit_exists = AuditEvent.objects.filter(
        actor=self.hr_user,
        content_type=content_type,
        object_id=self.attendance.pk,
        action="UPDATE",
    ).exists()

        self.assertTrue(
        audit_exists,
        "Attendance correction did not create a central audit event.",
    )

    def test_admin_cannot_correct_attendance(self):
        User = get_user_model()
        admin_user = User.objects.create_user(
            email="admin_corrector@example.com",
            password="test-password",
            role=User.Role.ADMIN,
        )
        with self.assertRaises(ValidationError):
            correct_attendance(
                self.attendance.pk, admin_user,
                status=Attendance.Status.LATE, reason="Admin attempt",
            )

    def test_correction_audit_event_records_old_and_new_values(self):
        from apps.accounts.models import AuditEvent

        old_status = self.attendance.status
        correct_attendance(
            self.attendance.pk, self.hr_user,
            status=Attendance.Status.LATE, reason="Late arrival confirmed",
        )
        event = AuditEvent.objects.get(
            actor=self.hr_user,
            object_id=self.attendance.pk,
            action="UPDATE",
        )
        self.assertEqual(event.changes["status"]["old"], old_status)
        self.assertEqual(event.changes["status"]["new"], Attendance.Status.LATE)
        
    def test_correction_emits_attendance_corrected_event(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        attendance_corrected.connect(receiver)
        self.addCleanup(attendance_corrected.disconnect, receiver)

        with self.captureOnCommitCallbacks(execute=True):
            correct_attendance(
                self.attendance.pk, self.hr_user,
                status=Attendance.Status.LATE, reason="Late arrival confirmed",
            )

        self.assertEqual(len(received), 1)
        event = received[0]
        self.assertEqual(event["attendance_id"], self.attendance.pk)
        self.assertEqual(event["employee_id"], self.employee.pk)
        self.assertEqual(event["corrected_by_id"], self.hr_user.pk)
        self.assertEqual(
            event["changes"]["status"], {"old": "PRESENT", "new": "LATE"}
        )

    def test_failed_correction_emits_no_event(self):
        received = []

        def receiver(sender, **kwargs):
            received.append(kwargs)

        attendance_corrected.connect(receiver)
        self.addCleanup(attendance_corrected.disconnect, receiver)

        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValidationError):
                correct_attendance(
                    self.attendance.pk, self.regular_user,
                    status=Attendance.Status.LATE, reason="Not allowed",
                )

        self.assertEqual(received, [])

    def test_failing_receiver_does_not_break_correction(self):
        def bad_receiver(sender, **kwargs):
            raise RuntimeError("email service down")

        attendance_corrected.connect(bad_receiver)
        self.addCleanup(attendance_corrected.disconnect, bad_receiver)

        with self.assertLogs("apps.attendance.events", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                result = correct_attendance(
                    self.attendance.pk, self.hr_user,
                    status=Attendance.Status.LATE, reason="Late arrival",
                )

        self.assertEqual(result.status, Attendance.Status.LATE)

class MonthlyAttendanceSummaryTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            employee_id="EMP003",
            full_name="Monthly Test Employee",
            email="monthly.employee@example.com",
            joining_date=date(2026, 1, 1),
        )

    def test_monthly_summary_counts_attendance_statuses(self):
        Attendance.objects.create(
            employee=self.employee,
            date=date(2026, 10, 1),
            status=Attendance.Status.PRESENT,
        )
        Attendance.objects.create(
            employee=self.employee,
            date=date(2026, 10, 2),
            status=Attendance.Status.LATE,
        )
        Attendance.objects.create(
            employee=self.employee,
            date=date(2026, 10, 3),
            status=Attendance.Status.ABSENT,
        )

        summary = get_monthly_attendance_summary(
            self.employee, 2026, 10
        )

        self.assertEqual(summary["PRESENT"], 1)
        self.assertEqual(summary["LATE"], 1)
        self.assertEqual(summary["ABSENT"], 1)
        self.assertEqual(summary["HALF_DAY"], 0)
        self.assertEqual(summary["ON_LEAVE"], 0)
        self.assertEqual(summary["total_days"], 3)

    def test_monthly_summary_excludes_other_months(self):
        Attendance.objects.create(
            employee=self.employee,
            date=date(2026, 10, 31),
            status=Attendance.Status.PRESENT,
        )
        Attendance.objects.create(
            employee=self.employee,
            date=date(2026, 11, 1),
            status=Attendance.Status.LATE,
        )

        summary = get_monthly_attendance_summary(
            self.employee, 2026, 10
        )

        self.assertEqual(summary["PRESENT"], 1)
        self.assertEqual(summary["LATE"], 0)
        self.assertEqual(summary["total_days"], 1)

    def test_invalid_month_is_rejected(self):
        for month in (0, 13):
            with self.subTest(month=month):
                with self.assertRaises(ValidationError):
                    get_monthly_attendance_summary(
                        self.employee, 2026, month
                    )
    def test_summary_counts_only_the_requested_month(self):
        employee = Employee.objects.create(
            employee_id="EMP-SUM-1", full_name="Summary Test",
            email="sum1@example.com", joining_date=date(2026, 1, 1),
        )
        for day, status in [
            (date(2026, 9, 30), Attendance.Status.PRESENT),
            (date(2026, 10, 1), Attendance.Status.PRESENT),
            (date(2026, 10, 31), Attendance.Status.LATE),
            (date(2026, 11, 1), Attendance.Status.ABSENT),
        ]:
            Attendance.objects.create(employee=employee, date=day, status=status)

        summary = get_monthly_attendance_summary(employee, 2026, 10)

        self.assertEqual(summary["total_days"], 2)
        self.assertEqual(summary[Attendance.Status.PRESENT], 1)
        self.assertEqual(summary[Attendance.Status.LATE], 1)
        self.assertEqual(summary[Attendance.Status.ABSENT], 0)

    def test_summary_ignores_other_employees_and_empty_month(self):
        employee = Employee.objects.create(
            employee_id="EMP-SUM-2", full_name="Summary Two",
            email="sum2@example.com", joining_date=date(2026, 1, 1),
        )
        other = Employee.objects.create(
            employee_id="EMP-SUM-3", full_name="Summary Three",
            email="sum3@example.com", joining_date=date(2026, 1, 1),
        )
        Attendance.objects.create(
            employee=other, date=date(2026, 10, 5),
            status=Attendance.Status.PRESENT,
        )
        summary = get_monthly_attendance_summary(employee, 2026, 10)
        self.assertEqual(summary["total_days"], 0)

    def test_summary_rejects_invalid_month(self):
        employee = Employee.objects.create(
            employee_id="EMP-SUM-4", full_name="Summary Four",
            email="sum4@example.com", joining_date=date(2026, 1, 1),
        )
        with self.assertRaises(ValidationError):
            get_monthly_attendance_summary(employee, 2026, 13)
    