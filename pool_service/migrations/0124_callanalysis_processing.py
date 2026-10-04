from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0123_unified_employee_identity_mapping"),
    ]

    operations = [
        migrations.AddField(
            model_name="callanalysis",
            name="analysis_model",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="error",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="processing_token",
            field=models.CharField(blank=True, max_length=36),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="processed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Ожидает расшифровки"),
                    ("processing", "Обрабатывается"),
                    ("ready", "Готово"),
                    ("failed", "Ошибка"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="transcription_model",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="callanalysis",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
