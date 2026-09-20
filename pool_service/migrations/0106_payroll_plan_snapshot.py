from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0105_normalize_water_reading_timezone"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PayrollPlanSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_month", models.DateField()),
                ("source_type", models.CharField(default="odata", max_length=20)),
                ("source_hash", models.CharField(max_length=64)),
                ("source_rows", models.PositiveIntegerField(default=0)),
                ("source_organization_guids", models.JSONField(default=list)),
                ("currency_guid", models.UUIDField()),
                ("fetched_at", models.DateTimeField(auto_now_add=True)),
                ("fetched_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fetched_payroll_plan_snapshots", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payroll_plan_snapshots", to="pool_service.organization")),
            ],
            options={"ordering": ["-fetched_at", "-id"]},
        ),
        migrations.CreateModel(
            name="PayrollPlanItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("onec_employee_id", models.CharField(max_length=120)),
                ("employee_raw_name", models.CharField(max_length=500)),
                ("accrual_type_id", models.CharField(max_length=120)),
                ("accrual_type_name", models.CharField(max_length=300)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=20)),
                ("is_base_salary", models.BooleanField(default=False)),
                ("source_period", models.DateField()),
                ("source_organization_guid", models.UUIDField()),
                ("employee_identity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payroll_plan_items", to="pool_service.employeeonecidentity")),
                ("snapshot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="pool_service.payrollplansnapshot")),
            ],
            options={"ordering": ["employee_raw_name", "accrual_type_name", "id"]},
        ),
        migrations.AddIndex(
            model_name="payrollplansnapshot",
            index=models.Index(fields=["organization", "period_month", "-fetched_at"], name="pay_plan_org_month_idx"),
        ),
        migrations.AddConstraint(
            model_name="payrollplansnapshot",
            constraint=models.UniqueConstraint(fields=("organization", "period_month", "source_hash"), name="unique_payroll_plan_snapshot"),
        ),
        migrations.AddIndex(
            model_name="payrollplanitem",
            index=models.Index(fields=["snapshot", "employee_identity"], name="pay_plan_snap_emp_idx"),
        ),
        migrations.AddConstraint(
            model_name="payrollplanitem",
            constraint=models.UniqueConstraint(fields=("snapshot", "source_organization_guid", "onec_employee_id", "accrual_type_id"), name="unique_payroll_plan_item"),
        ),
    ]
