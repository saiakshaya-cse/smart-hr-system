
"""
Signal handlers for audit logging.
Automatically logs changes to models using AuditedModelMixin.
"""

from django.apps import apps
from django.db.models.signals import pre_save, post_delete, post_save
from django.dispatch import receiver

from apps.accounts.audit import log_audit
from apps.accounts.mixins import AuditedModelMixin


def register_audit_signals():
    """Register pre-save and post-delete handlers for audited models."""
    for model in apps.get_models():
        if (
            model is not AuditedModelMixin
            and issubclass(model, AuditedModelMixin)
        ):
            pre_save.connect(
                create_audit_pre_save(model),
                sender=model,
                weak=False,
                dispatch_uid=f'audit_pre_save_{model._meta.label_lower}',
            )
            post_delete.connect(
                create_audit_post_delete(model),
                sender=model,
                weak=False,
                dispatch_uid=f'audit_post_delete_{model._meta.label_lower}',
            )


def create_audit_pre_save(model):
    """Capture the action and changed fields before saving."""
    def handler(sender, instance, **kwargs):
        if instance.pk is None:
            instance._audit_action = 'CREATE'
            instance._audit_changes = None
            return

        try:
            old_instance = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._audit_action = 'CREATE'
            instance._audit_changes = None
            return

        changes = {}

        for field in instance._meta.fields:
            if field.name in ('id', 'created_at', 'updated_at'):
                continue

            # Compare stored values, including foreign key IDs.
            field_name = field.attname
            old_value = getattr(old_instance, field_name)
            new_value = getattr(instance, field_name)

            if old_value != new_value:
                changes[field.name] = {
                    'old': old_value,
                    'new': new_value,
                }

        instance._audit_action = 'UPDATE'
        instance._audit_changes = changes or None

    return handler


def create_audit_post_delete(model):
    """Record the deletion of an audited object."""
    def handler(sender, instance, **kwargs):
        log_audit(
            actor=getattr(instance, '_audit_actor', None),
            action='DELETE',
            obj=instance,
            request=getattr(instance, '_audit_request', None),
        )

    return handler


@receiver(post_save)
def audit_post_save(sender, instance, created, **kwargs):
    """Record create and update events for audited objects."""
    if not isinstance(instance, AuditedModelMixin):
        return

    # The fallback handles cases where no pre-save action was assigned.
    action = (
        getattr(instance, '_audit_action', None)
        or ('CREATE' if created else 'UPDATE')
    )
    changes = getattr(instance, '_audit_changes', None)

    if action == 'CREATE' or (action == 'UPDATE' and changes):
        log_audit(
            actor=getattr(instance, '_audit_actor', None),
            action=action,
            obj=instance,
            changes=changes,
            request=getattr(instance, '_audit_request', None),
        )
