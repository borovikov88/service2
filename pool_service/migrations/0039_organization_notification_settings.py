from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0038_pool_service_frequency"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="notify_limits",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_missed_visits",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="notify_pool_staff_daily",
            field=models.BooleanField(default=True),
        ),
    ]
