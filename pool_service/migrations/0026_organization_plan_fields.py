from django.db import migrations, models
from django.utils import timezone


def set_trial_start(apps, schema_editor):
    Organization = apps.get_model("pool_service", "Organization")
    now = timezone.now()
    Organization.objects.filter(plan_type="COMPANY_TRIAL", trial_started_at__isnull=True).update(
        trial_started_at=now
    )


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0025_pool_waterreading_uuid"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="plan_type",
            field=models.CharField(
                choices=[
                    ("PERSONAL_FREE", "PERSONAL_FREE"),
                    ("COMPANY_TRIAL", "COMPANY_TRIAL"),
                    ("COMPANY_PAID", "COMPANY_PAID"),
                ],
                default="COMPANY_TRIAL",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="trial_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="paid_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(set_trial_start, reverse_code=migrations.RunPython.noop),
    ]
