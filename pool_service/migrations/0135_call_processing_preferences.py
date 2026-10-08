from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0134_operations_push_queue"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="CallProcessingRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mode", models.CharField(choices=[("manual", "Only manually"), ("all_except", "All except personal"), ("allowlist", "Only rules")], default="manual", max_length=16)),
                ("include_staff", models.BooleanField(default=True)),
                ("work_numbers", models.JSONField(blank=True, default=list)),
                ("effective_from", models.DateTimeField(blank=True, null=True)),
                ("revision", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization")),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="call_processing_rules", to=settings.AUTH_USER_MODEL)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="changed_call_processing_rules", to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("organization", "employee"), name="call_rule_org_employee_uniq")]},
        ),
        migrations.CreateModel(
            name="CallPrivateNumber",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField(max_length=120)),
                ("phone_key", models.CharField(max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="private_call_numbers", to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("organization", "owner", "phone_key"), name="call_private_owner_phone_uniq")]},
        ),
        migrations.CreateModel(
            name="CallProcessingRuleAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_user_id", models.PositiveBigIntegerField()),
                ("action", models.CharField(max_length=32)),
                ("details", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization")),
                ("actor", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="call_processing_audits", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
