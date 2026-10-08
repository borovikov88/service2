from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0134_call_processing_preferences"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CallProcessingBudget",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("monthly_limit_usd", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("revision", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="changed_call_processing_budgets", to=settings.AUTH_USER_MODEL)),
                ("organization", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="call_processing_budget", to="pool_service.organization")),
            ],
        ),
        migrations.CreateModel(
            name="CallProcessingUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attempt_key", models.CharField(max_length=64)),
                ("stage", models.CharField(choices=[("transcription", "Transcription"), ("analysis", "Analysis")], max_length=16)),
                ("model", models.CharField(max_length=80)),
                ("status", models.CharField(choices=[("reserved", "Reserved"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("released", "Released")], default="reserved", max_length=16)),
                ("duration_seconds", models.PositiveIntegerField(blank=True, null=True)),
                ("input_tokens", models.PositiveBigIntegerField(blank=True, null=True)),
                ("output_tokens", models.PositiveBigIntegerField(blank=True, null=True)),
                ("reserved_cost_usd", models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True)),
                ("estimated_cost_usd", models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True)),
                ("usage_cost_usd", models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True)),
                ("confirmed_cost_usd", models.DecimalField(blank=True, decimal_places=6, max_digits=12, null=True)),
                ("tariff_version", models.CharField(blank=True, max_length=64)),
                ("error_code", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("call", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="processing_usage", to="pool_service.phonecall")),
                ("employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="call_processing_usage", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="call_processing_usage", to="pool_service.organization")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["organization", "created_at"], name="call_usage_org_created_idx"),
                    models.Index(fields=["organization", "status", "created_at"], name="call_usage_org_status_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("call", "attempt_key", "stage"), name="call_usage_attempt_stage_uniq"),
                ],
            },
        ),
    ]
