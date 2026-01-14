from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0029_profile_phone_verification"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="email_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
