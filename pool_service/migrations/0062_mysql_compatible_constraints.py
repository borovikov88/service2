import hashlib

from django.db import migrations, models


def normalize_notification_dedupe_keys(apps, schema_editor):
    Notification = apps.get_model("pool_service", "Notification")
    Notification.objects.filter(dedupe_key="").update(dedupe_key=None)


def restore_notification_dedupe_keys(apps, schema_editor):
    Notification = apps.get_model("pool_service", "Notification")
    Notification.objects.filter(dedupe_key__isnull=True).update(dedupe_key="")


def populate_push_endpoint_hashes(apps, schema_editor):
    PushSubscription = apps.get_model("pool_service", "PushSubscription")
    for subscription in PushSubscription.objects.only("pk", "endpoint").iterator():
        endpoint_hash = hashlib.sha256(subscription.endpoint.encode("utf-8")).hexdigest()
        PushSubscription.objects.filter(pk=subscription.pk).update(endpoint_hash=endpoint_hash)


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0061_alter_crmitem_stage"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="organizationaccess",
            name="unique_owner_per_org",
        ),
        migrations.RemoveConstraint(
            model_name="notification",
            name="unique_notification_dedupe",
        ),
        migrations.AlterField(
            model_name="notification",
            name="dedupe_key",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.RunPython(
            normalize_notification_dedupe_keys,
            restore_notification_dedupe_keys,
        ),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.UniqueConstraint(
                fields=("user", "dedupe_key"),
                name="unique_notification_dedupe",
            ),
        ),
        migrations.AddField(
            model_name="pushsubscription",
            name="endpoint_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(
            populate_push_endpoint_hashes,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="pushsubscription",
            name="endpoint_hash",
            field=models.CharField(editable=False, max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="pushsubscription",
            name="endpoint",
            field=models.CharField(max_length=512),
        ),
    ]
