from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0040_push_subscription"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationinvite",
            name="roles",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
