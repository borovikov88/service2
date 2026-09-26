from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_test_scheme(apps, schema_editor):
    Organization = apps.get_model("pool_service", "Organization")
    Scheme = apps.get_model("pool_service", "EmployeeRewardScheme")
    Rule = apps.get_model("pool_service", "EmployeeRewardRule")

    rules = [
        ("client_manager", "client_manager", "information", None, None),
        ("paperwork", "paperwork_retail_check", "fixed", Decimal("50.00"), None),
        ("paperwork", "paperwork_sales_package", "fixed", Decimal("200.00"), None),
        ("sale", "sale", "percent", None, Decimal("10.0000")),
        ("project", "project", "percent", None, Decimal("5.0000")),
        ("work", "work", "percent", None, Decimal("40.0000")),
    ]
    for organization in Organization.objects.all().iterator():
        scheme, _ = Scheme.objects.get_or_create(
            organization=organization,
            name="Тестовая схема №1",
            version=1,
            defaults={
                "effective_from": date(2026, 1, 1),
                "is_active": True,
            },
        )
        for role, unit, kind, fixed_amount, rate_percent in rules:
            Rule.objects.get_or_create(
                scheme=scheme,
                role=role,
                unit_kind=unit,
                defaults={
                    "calculation_kind": kind,
                    "fixed_amount": fixed_amount,
                    "rate_percent": rate_percent,
                },
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0108_employee_compensation_month"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="organization",
            options={
                "permissions": [
                    ("view_cashflow", "Can view cash flow"),
                    ("import_cashflow", "Can import cash flow"),
                    ("manage_cashflow_classification", "Can manage cash flow classification"),
                    ("view_payroll_summary", "Can view payroll summary"),
                    ("view_payroll_personal", "Can view personal payroll data"),
                    ("import_payroll", "Can import payroll"),
                    ("manage_employee_mapping", "Can manage employee mapping"),
                    ("view_employee_rewards", "Can view employee reward summaries"),
                    ("manage_employee_rewards", "Can manage employee reward participation"),
                    ("manage_reward_rules", "Can manage employee reward rules"),
                    ("close_reward_month", "Can close employee reward month"),
                ]
            },
        ),
        migrations.CreateModel(
            name="EmployeeOneCUserIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("onec_user_id", models.UUIDField()),
                ("display_name", models.CharField(max_length=500)),
                ("status", models.CharField(choices=[("needs_confirmation", "Требует сопоставления"), ("confirmed", "Сопоставлен"), ("technical", "Техническая учётная запись")], default="needs_confirmation", max_length=24)),
                ("comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_employee_onec_user_identities", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="onec_user_identities", to="pool_service.employee")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_onec_user_identities", to="pool_service.organization")),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["display_name", "id"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardScheme",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("version", models.PositiveIntegerField(default=1)),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_employee_reward_schemes", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_reward_schemes", to="pool_service.organization")),
            ],
            options={"ordering": ["organization_id", "-effective_from", "-version"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("client_manager", "Менеджер клиента"), ("sale", "Продажа"), ("paperwork", "Оформление"), ("project", "Проект / расчёт"), ("work", "Выполнение работ")], max_length=24)),
                ("unit_kind", models.CharField(choices=[("client_manager", "Менеджер клиента"), ("sale", "Продажа"), ("paperwork_retail_check", "Самостоятельный розничный чек"), ("paperwork_sales_package", "Комплект документов / самостоятельный этап"), ("project", "Проект / расчёт"), ("work", "Выполнение работ")], max_length=40)),
                ("calculation_kind", models.CharField(choices=[("information", "Информационная"), ("fixed", "Фиксированная сумма"), ("percent", "Процент от ВП")], max_length=16)),
                ("fixed_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True)),
                ("rate_percent", models.DecimalField(blank=True, decimal_places=4, max_digits=9, null=True)),
                ("scheme", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rules", to="pool_service.employeerewardscheme")),
            ],
            options={"ordering": ["scheme_id", "role", "unit_kind"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_month", models.DateField()),
                ("source_document_type", models.CharField(max_length=120)),
                ("source_document_guid", models.UUIDField()),
                ("source_document_label", models.CharField(blank=True, max_length=500)),
                ("scope_key", models.CharField(max_length=240)),
                ("role", models.CharField(choices=[("client_manager", "Менеджер клиента"), ("sale", "Продажа"), ("paperwork", "Оформление"), ("project", "Проект / расчёт"), ("work", "Выполнение работ")], max_length=24)),
                ("share_percent", models.DecimalField(decimal_places=2, default=100, max_digits=7)),
                ("status", models.CharField(choices=[("required", "Требуется назначение"), ("proposed", "Назначено, ожидает подтверждения"), ("confirmed", "Подтверждено"), ("not_applicable", "Не применяется")], default="proposed", max_length=24)),
                ("source_kind", models.CharField(choices=[("manual", "Вручную"), ("client_template", "Шаблон клиента"), ("object_template", "Шаблон объекта"), ("order", "Заказ"), ("task_suggestion", "Связанная задача"), ("onec_author", "Автор документа 1С")], default="manual", max_length=24)),
                ("basis_note", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_employee_reward_assignments", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reward_assignments", to="pool_service.employee")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_reward_assignments", to="pool_service.organization")),
                ("proposed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="proposed_employee_reward_assignments", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["period_month", "source_document_type", "source_document_guid", "role", "id"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardAssignmentLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_identity", models.CharField(max_length=80)),
                ("nomenclature", models.CharField(blank=True, max_length=500)),
                ("nomenclature_type", models.CharField(blank=True, max_length=100)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="pool_service.employeerewardassignment")),
            ],
            options={"ordering": ["assignment_id", "source_identity"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_customer_guid", models.UUIDField(blank=True, null=True)),
                ("role", models.CharField(choices=[("client_manager", "Менеджер клиента"), ("sale", "Продажа"), ("paperwork", "Оформление"), ("project", "Проект / расчёт"), ("work", "Выполнение работ")], max_length=24)),
                ("share_percent", models.DecimalField(decimal_places=2, default=100, max_digits=7)),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reward_templates", to="pool_service.client")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_employee_reward_templates", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reward_templates", to="pool_service.employee")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_reward_templates", to="pool_service.organization")),
                ("pool", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reward_templates", to="pool_service.pool")),
            ],
            options={"ordering": ["organization_id", "role", "employee_id", "id"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardAssignmentChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=80)),
                ("before", models.JSONField(blank=True, default=dict)),
                ("after", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="employee_reward_assignment_changes", to=settings.AUTH_USER_MODEL)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="changes", to="pool_service.employeerewardassignment")),
            ],
            options={"ordering": ["assignment_id", "created_at", "id"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardMonthClose",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_month", models.DateField()),
                ("result_data", models.JSONField(default=dict)),
                ("total_amount", models.DecimalField(decimal_places=2, default=0, max_digits=20)),
                ("closed_at", models.DateTimeField(auto_now_add=True)),
                ("closed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="closed_employee_reward_months", to=settings.AUTH_USER_MODEL)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_reward_month_closes", to="pool_service.organization")),
                ("scheme", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="month_closes", to="pool_service.employeerewardscheme")),
            ],
            options={"ordering": ["-period_month", "organization_id"]},
        ),
        migrations.CreateModel(
            name="EmployeeRewardAdjustment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_month", models.DateField()),
                ("amount", models.DecimalField(decimal_places=2, max_digits=20)),
                ("reason", models.CharField(max_length=1000)),
                ("status", models.CharField(choices=[("proposed", "Предложена"), ("confirmed", "Подтверждена"), ("rejected", "Отклонена")], default="proposed", max_length=16)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_employee_reward_adjustments", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_employee_reward_adjustments", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reward_adjustments", to="pool_service.employee")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="employee_reward_adjustments", to="pool_service.organization")),
            ],
            options={"ordering": ["period_month", "employee_id", "id"]},
        ),
        migrations.AddConstraint(
            model_name="employeeonecuseridentity",
            constraint=models.UniqueConstraint(fields=("organization", "onec_user_id"), name="unique_reward_onec_user_org"),
        ),
        migrations.AddIndex(
            model_name="employeeonecuseridentity",
            index=models.Index(fields=["organization", "status"], name="reward_user_org_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardscheme",
            constraint=models.UniqueConstraint(fields=("organization", "name", "version"), name="unique_reward_scheme_ver"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardrule",
            constraint=models.UniqueConstraint(fields=("scheme", "role", "unit_kind"), name="unique_reward_rule_unit"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardrule",
            constraint=models.CheckConstraint(condition=models.Q(("fixed_amount__isnull", True), ("fixed_amount__gte", 0), _connector="OR"), name="reward_rule_fixed_nonneg"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardrule",
            constraint=models.CheckConstraint(condition=models.Q(("rate_percent__isnull", True), ("rate_percent__gte", 0), _connector="OR"), name="reward_rule_rate_nonneg"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardassignment",
            constraint=models.UniqueConstraint(fields=("organization", "period_month", "source_document_type", "source_document_guid", "scope_key", "role", "employee"), name="unique_reward_assignment"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardassignment",
            constraint=models.CheckConstraint(condition=models.Q(("share_percent__gt", 0), ("share_percent__lte", 100)), name="reward_share_valid"),
        ),
        migrations.AddIndex(
            model_name="employeerewardassignment",
            index=models.Index(fields=["organization", "period_month", "status"], name="reward_assign_period_idx"),
        ),
        migrations.AddIndex(
            model_name="employeerewardassignment",
            index=models.Index(fields=["organization", "source_document_guid"], name="reward_assign_doc_idx"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardassignmentline",
            constraint=models.UniqueConstraint(fields=("assignment", "source_identity"), name="unique_reward_assign_line"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardtemplate",
            constraint=models.CheckConstraint(condition=models.Q(("client__isnull", False), ("pool__isnull", False), ("source_customer_guid__isnull", False), _connector="OR"), name="reward_template_has_target"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardtemplate",
            constraint=models.CheckConstraint(condition=models.Q(("share_percent__gt", 0), ("share_percent__lte", 100)), name="reward_template_share_valid"),
        ),
        migrations.AddConstraint(
            model_name="employeerewardmonthclose",
            constraint=models.UniqueConstraint(fields=("organization", "period_month"), name="unique_reward_month_close"),
        ),
        migrations.AddIndex(
            model_name="employeerewardadjustment",
            index=models.Index(fields=["organization", "period_month", "status"], name="reward_adjust_period_idx"),
        ),
        migrations.RunPython(seed_test_scheme, noop_reverse),
    ]
