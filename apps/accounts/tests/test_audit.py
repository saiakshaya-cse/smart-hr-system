
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from apps.accounts.models import AuditEvent
from apps.employees.models import Department


class AuditSignalTests(TestCase):
    def setUp(self):
        self.content_type = ContentType.objects.get_for_model(Department)

    def get_events(self, department):
        return AuditEvent.objects.filter(
            content_type=self.content_type,
            object_id=department.pk,
        )

    def test_create_department_is_audited(self):
        department = Department.objects.create(name='Engineering')

        event = self.get_events(department).get(action='CREATE')

        self.assertEqual(event.object_repr, 'Engineering')
        self.assertIsNone(event.actor)

    def test_update_department_records_changed_fields(self):
        department = Department.objects.create(name='Operations')

        department.name = 'Finance'
        department.save()

        event = self.get_events(department).get(action='UPDATE')

        self.assertEqual(
            event.changes['name'],
            {'old': 'Operations', 'new': 'Finance'},
        )

    def test_delete_department_is_audited(self):
        department = Department.objects.create(name='Research')
        department_id = department.pk

        department.delete()

        event = AuditEvent.objects.get(
            content_type=self.content_type,
            object_id=department_id,
            action='DELETE',
        )

        self.assertEqual(event.object_repr, 'Research')
