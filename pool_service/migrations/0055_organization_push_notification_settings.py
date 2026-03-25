from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0054_profile_notification_preferences"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="notify_limits_push",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_missed_visits_push",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_pool_staff_daily_push",
            field=models.BooleanField(default=True),
        ),
    ]
