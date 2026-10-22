from datetime import date
from decimal import Decimal
from django.db import IntegrityError, transaction

from django.test import TestCase

from apps.employees.models import Employee
from apps.payroll.models import (
    SalaryComponent,
    PayrollPeriod,
    PayrollRecord,
    Payslip,
)


class SalaryComponentTest(TestCase):
    def test_create_salary_component(self):
        component = SalaryComponent.objects.create(
            name="House Rent Allowance",
            component_type=SalaryComponent.ComponentType.ALLOWANCE,
            amount=Decimal("5000.00"),
        )

        self.assertEqual(component.amount, Decimal("5000.00"))
        self.assertTrue(component.is_active)


class PayrollPeriodTest(TestCase):
    def test_create_payroll_period(self):
        period = PayrollPeriod.objects.create(
            name="October 2026",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        )

        self.assertEqual(period.name, "October 2026")
        self.assertFalse(period.is_processed)

    def test_end_date_cannot_be_before_start_date(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayrollPeriod.objects.create(
                    name="Invalid Period",
                    start_date=date(2026, 11, 30),
                    end_date=date(2026, 11, 1),
                )


class PayrollRecordTest(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            employee_id="EMP001",
            full_name="Test Employee",
            email="test.employee@example.com",
            joining_date=date(2026, 1, 1),
            base_salary=Decimal("25000.00"),
        )

        self.period = PayrollPeriod.objects.create(
            name="November 2026",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 30),
        )

    def test_calculate_salary_totals(self):
        record = PayrollRecord.objects.create(
            employee=self.employee,
            payroll_period=self.period,
            basic_salary=Decimal("25000.00"),
            total_allowances=Decimal("5000.00"),
            total_deductions=Decimal("2000.00"),
        )

        self.assertEqual(record.gross_salary, Decimal("30000.00"))
        self.assertEqual(record.net_salary, Decimal("28000.00"))

    def test_duplicate_employee_period_is_rejected(self):
        PayrollRecord.objects.create(
            employee=self.employee,
            payroll_period=self.period,
            basic_salary=Decimal("25000.00"),
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PayrollRecord.objects.create(
                    employee=self.employee,
                    payroll_period=self.period,
                    basic_salary=Decimal("25000.00"),
                )


class PayslipTest(TestCase):
    def test_create_payslip(self):
        employee = Employee.objects.create(
            employee_id="EMP002",
            full_name="Another Employee",
            email="another.employee@example.com",
            joining_date=date(2026, 1, 1),
            base_salary=Decimal("30000.00"),
        )

        period = PayrollPeriod.objects.create(
            name="December 2026",
            start_date=date(2026, 12, 1),
            end_date=date(2026, 12, 31),
        )

        record = PayrollRecord.objects.create(
            employee=employee,
            payroll_period=period,
            basic_salary=Decimal("30000.00"),
        )

        payslip = Payslip.objects.create(
            payroll_record=record,
            payslip_number="PAY-2026-001",
        )

        self.assertEqual(payslip.payslip_number, "PAY-2026-001")
        self.assertEqual(payslip.status, Payslip.Status.GENERATED)