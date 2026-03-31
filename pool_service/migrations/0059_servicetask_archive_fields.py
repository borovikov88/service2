from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0058_servicetask_auto_created_servicetask_client_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="servicetask",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="servicetask",
            name="archived_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="archived_service_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="servicetask",
            name="archived_reason",
            field=models.CharField(
                blank=True,
                choices=[("completed", "Выполнена"), ("deleted", "Удалена"), ("manual", "В архиве")],
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="servicetask",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name="servicetask",
            index=models.Index(fields=["organization", "is_archived"], name="task_org_archived_idx"),
        ),
    ]
