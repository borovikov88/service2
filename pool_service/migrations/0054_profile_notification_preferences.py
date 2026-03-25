from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0053_notification_task_assignment_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="in_app_notifications_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="push_notifications_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
