from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from .models import OneCImportBatch, Profile, WaterReading
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

#@receiver(post_save, sender=User)
#def save_user_profile(sender, instance, **kwargs):
#    instance.profile.save()
