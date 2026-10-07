from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0130_phonecall_uploaded_audio_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="phonecall",
            name="peer_employee",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="peer_phone_calls",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="phonecall",
            name="peer_employee_profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="peer_phone_calls",
                to="pool_service.employee",
            ),
        ),
        migrations.AddField(
            model_name="phonecall",
            name="peer_provider_extension",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="phonecall",
            name="peer_provider_user",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="phonecall",
            name="direction",
            field=models.CharField(
                choices=[
                    ("in", "Входящий"),
                    ("out", "Исходящий"),
                    ("internal", "Внутренний"),
                ],
                max_length=8,
            ),
        ),
    ]
