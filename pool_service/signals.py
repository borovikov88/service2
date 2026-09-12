from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import OneCImportBatch, OneCODataSyncRun, Profile, WaterReading
from .finance_imports.validators import delete_private_file
from .services.notifications import notify_reading_out_of_range
from .services.task_generation import create_supply_task_from_reading

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)


@receiver(post_save, sender=WaterReading)
def notify_out_of_range_on_reading_save(sender, instance, created, **kwargs):
    notify_reading_out_of_range(instance)
    if created:
        create_supply_task_from_reading(instance)


@receiver(post_delete, sender=OneCImportBatch)
def delete_onec_import_file(sender, instance, using, **kwargs):
    # The filesystem is not transactional. Delete only after the database
    # transaction that removed the batch has committed successfully.
    storage = instance.stored_file.storage
    name = instance.stored_file.name
    organization_id = instance.organization_id
    batch_id = instance.id
    transaction.on_commit(
        lambda: delete_private_file(
            storage, name, organization_id=organization_id, batch_id=batch_id
        ),
        using=using,
    )


@receiver(post_save, sender=OneCODataSyncRun)
def finalize_finance_position_after_auto_apply(sender, instance, using, **kwargs):
    """Use the same point-in-time finalizer for browser and scheduled AUTO_APPLY.

    The callback runs only after the unified transaction commits.  Retryable
    finance-position failures are deliberately not auto-recursed here; the
    scheduled worker or browser resume action can retry the shared finalizer.
    """
    if instance.mode != OneCODataSyncRun.MODE_AUTO_APPLY:
        return
    if instance.status != OneCODataSyncRun.STATUS_COMPLETED:
        return
    if (instance.progress or {}).get("finance_position_state") in {
        "completed",
        "failed",
        "retryable_error",
    }:
        return

    run_id = instance.pk

    def _finalize():
        from .finance_imports.odata_daily_sync import finalize_finance_position_step

        run = OneCODataSyncRun.objects.get(pk=run_id)
        finalize_finance_position_step(run)

    transaction.on_commit(_finalize, using=using)

#@receiver(post_save, sender=User)
#def save_user_profile(sender, instance, **kwargs):
#    instance.profile.save()
