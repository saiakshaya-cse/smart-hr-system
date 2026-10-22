
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User


class DashboardAccessTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin_dashboard@test.com",
            password="TestPassword123!",
        )
        self.hr = User.objects.create_user(
            email="hr_dashboard@test.com",
            password="TestPassword123!",
            role=User.Role.HR,
        )
        self.employee = User.objects.create_user(
            email="employee_dashboard@test.com",
            password="TestPassword123!",
            role=User.Role.EMPLOYEE,
        )

    def test_admin_can_access_all_dashboards(self):
        self.client.force_login(self.admin)

        for url_name in (
            "admin-dashboard",
            "hr-dashboard",
            "my-dashboard",
        ):
            with self.subTest(dashboard=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)

    def test_hr_can_access_hr_and_employee_dashboards(self):
        self.client.force_login(self.hr)

        for url_name in ("hr-dashboard", "my-dashboard"):
            with self.subTest(dashboard=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)

    def test_hr_cannot_access_admin_dashboard(self):
        self.client.force_login(self.hr)
        response = self.client.get(reverse("admin-dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_employee_can_access_employee_dashboard(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("my-dashboard"))

        self.assertEqual(response.status_code, 200)

    def test_employee_cannot_access_admin_dashboard(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("admin-dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_hr_dashboard(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("hr-dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_anonymous_user_is_redirected_to_login(self):
        for url_name in (
            "admin-dashboard",
            "hr-dashboard",
            "my-dashboard",
        ):
            with self.subTest(dashboard=url_name):
                response = self.client.get(reverse(url_name))
                self.assertRedirects(
                    response,
                    f"{reverse('accounts:login')}?next={reverse(url_name)}",
                )
