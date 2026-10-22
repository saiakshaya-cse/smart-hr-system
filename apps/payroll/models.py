from decimal import Decimal

from django.db import models
from django.core.validators import MinValueValidator
from django.db.models import Q


class SalaryComponent(models.Model):
    class ComponentType(models.TextChoices):
        BASIC = "BASIC", "Basic Salary"
        ALLOWANCE = "ALLOWANCE", "Allowance"
        DEDUCTION = "DEDUCTION", "Deduction"

    name = models.CharField(max_length=100, unique=True)
    component_type = models.CharField(
        max_length=20,
        choices=ComponentType.choices,
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_component_type_display()})"


class PayrollPeriod(models.Model):
    name = models.CharField(max_length=100, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=models.F("start_date")),
                name="payroll_period_valid_dates",
            ),
        ]

    def __str__(self):
        return self.name


class PayrollRecord(models.Model):
    employee = models.ForeignKey(
        "employees.Employee",
        on_delete=models.PROTECT,
        related_name="payroll_records",
    )
    payroll_period = models.ForeignKey(
        PayrollPeriod,
        on_delete=models.PROTECT,
        related_name="payroll_records",
    )

    basic_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_allowances = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_deductions = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    gross_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    net_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "payroll_period"],
                name="unique_employee_payroll_period",
            ),
        ]

    def calculate_totals(self):
        self.gross_salary = self.basic_salary + self.total_allowances
        self.net_salary = self.gross_salary - self.total_deductions

    def save(self, *args, **kwargs):
        self.calculate_totals()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.employee} - {self.payroll_period}"


class Payslip(models.Model):
    class Status(models.TextChoices):
        GENERATED = "GENERATED", "Generated"
        FAILED = "FAILED", "Failed"

    payroll_record = models.OneToOneField(
        PayrollRecord,
        on_delete=models.CASCADE,
        related_name="payslip",
    )
    payslip_number = models.CharField(max_length=50, unique=True)
    pdf_file = models.FileField(upload_to="payslips/", blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.GENERATED,
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.payslip_number