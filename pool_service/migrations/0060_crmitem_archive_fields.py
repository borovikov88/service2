from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0059_servicetask_archive_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="crmitem",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="crmitem",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="crmitem",
            name="archived_reason",
            field=models.CharField(
                blank=True,
                choices=[("completed", "Выполнено"), ("deleted", "Удалено"), ("manual", "В архиве")],
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="crmitem",
            name="archived_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="archived_crm_items",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="crmitem",
            index=models.Index(fields=["organization", "is_archived"], name="crm_org_archived_idx"),
        ),
    ]
