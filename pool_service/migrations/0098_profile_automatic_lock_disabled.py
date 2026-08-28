from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0097_onecodatasyncrun")]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="automatic_lock_disabled",
            field=models.BooleanField(default=False),
        ),
    ]
