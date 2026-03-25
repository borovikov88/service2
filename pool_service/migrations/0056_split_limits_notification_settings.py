from django.db import migrations, models


def copy_limits_settings(apps, schema_editor):
    Organization = apps.get_model("pool_service", "Organization")
    for org in Organization.objects.all():
        org.notify_limits_pool_staff = org.notify_limits
        org.notify_limits_pool_staff_push = org.notify_limits_push
        org.notify_limits_service_staff = org.notify_limits
        org.notify_limits_service_staff_push = org.notify_limits_push
        org.save(
            update_fields=[
                "notify_limits_pool_staff",
                "notify_limits_pool_staff_push",
                "notify_limits_service_staff",
                "notify_limits_service_staff_push",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0055_organization_push_notification_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="notify_limits_pool_staff",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_limits_pool_staff_push",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_limits_service_staff",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_limits_service_staff_push",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(copy_limits_settings, migrations.RunPython.noop),
    ]
