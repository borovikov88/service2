from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0098_profile_automatic_lock_disabled")]

    operations = [
        migrations.AddField(
            model_name="onecodatasyncrun",
            name="mode",
            field=models.CharField(
                choices=[
                    ("preview", "Проверка без применения"),
                    ("auto_apply", "Обновление и применение"),
                ],
                default="preview",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="onecodatasyncrun",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="onecodatasyncrun",
            name="apply_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="onecodatasyncrun",
            name="applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="onecimportbatch",
            name="sync_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="candidate_batches",
                to="pool_service.onecodatasyncrun",
                verbose_name="Автоматическое обновление 1С",
            ),
        ),
    ]
