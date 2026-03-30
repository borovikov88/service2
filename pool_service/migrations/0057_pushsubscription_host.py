from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0056_split_limits_notification_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="pushsubscription",
            name="host",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
