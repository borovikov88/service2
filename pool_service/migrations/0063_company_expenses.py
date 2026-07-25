import django.db.models.deletion
import pool_service.models
import pool_service.storage
import uuid
from django.conf import settings
from django.db import migrations, models


DEFAULT_CATEGORIES = [
    "Материалы",
    "Доставка",
    "Вода",
    "Инструмент",
    "Транспорт",
    "Прочее",
]


def create_default_categories(apps, schema_editor):
    Organization = apps.get_model("pool_service", "Organization")
    ExpenseCategory = apps.get_model("pool_service", "ExpenseCategory")
    for organization in Organization.objects.iterator():
        for sort_order, name in enumerate(DEFAULT_CATEGORIES, start=1):
            ExpenseCategory.objects.get_or_create(
                organization=organization,
                name=name,
                defaults={"sort_order": sort_order},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0062_mysql_compatible_constraints"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                choices=[
                    ("limits", "limits"),
                    ("missed_visit", "missed_visit"),
                    ("daily_missing", "daily_missing"),
                    ("new_company", "new_company"),
                    ("new_personal", "new_personal"),
                    ("task_assignment", "task_assignment"),
                    ("finance", "finance"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="organizationaccess",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "Владелец"),
                    ("manager", "Менеджер"),
                    ("service", "Сервис"),
                    ("installer", "Монтажник"),
                    ("admin", "Администратор"),
                ],
                max_length=20,
                verbose_name="Роль",
            ),
        ),
        migrations.AlterField(
            model_name="organizationinvite",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "Владелец"),
                    ("manager", "Менеджер"),
                    ("service", "Сервис"),
                    ("installer", "Монтажник"),
                    ("admin", "Администратор"),
                ],
                default="service",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="ExpenseCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="expense_categories",
                        to="pool_service.organization",
                    ),
                ),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                (
                    "source",
                    models.CharField(
                        choices=[("accountable", "Подотчёт"), ("company_cash", "Касса компании")],
                        max_length=24,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("spent_on", models.DateField()),
                (
                    "destination_type",
                    models.CharField(
                        choices=[("office", "Офисные расходы"), ("client", "Клиент")],
                        max_length=16,
                    ),
                ),
                ("destination_name", models.CharField(max_length=255)),
                ("vendor", models.CharField(blank=True, max_length=255)),
                ("description", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "На проверке"),
                            ("approved", "Подтверждён"),
                            ("rejected", "Отклонён"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "client",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="expenses",
                        to="pool_service.client",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_expenses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="expenses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="expenses",
                        to="pool_service.organization",
                    ),
                ),
                (
                    "pool",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="expenses",
                        to="pool_service.pool",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reviewed_expenses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="expenses",
                        to="pool_service.expensecategory",
                    ),
                ),
            ],
            options={"ordering": ["-spent_on", "-id"]},
        ),
        migrations.CreateModel(
            name="ExpenseChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Создан"),
                            ("updated", "Изменён"),
                            ("approved", "Подтверждён"),
                            ("rejected", "Отклонён"),
                        ],
                        max_length=16,
                    ),
                ),
                ("note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="expense_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "expense",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="changes",
                        to="pool_service.expense",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="ExpensePeriod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("month", models.DateField()),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="closed_expense_periods",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="expense_periods",
                        to="pool_service.organization",
                    ),
                ),
            ],
            options={"ordering": ["-month"]},
        ),
        migrations.CreateModel(
            name="ExpenseReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "file",
                    models.FileField(
                        storage=pool_service.storage.PrivateMediaStorage(),
                        upload_to=pool_service.models.expense_receipt_upload_to,
                    ),
                ),
                ("original_name", models.CharField(max_length=255)),
                ("content_type", models.CharField(blank=True, max_length=100)),
                ("size", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "expense",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="receipts",
                        to="pool_service.expense",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="uploaded_expense_receipts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AccountableTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "transaction_type",
                    models.CharField(
                        choices=[
                            ("issue", "Выдача"),
                            ("return", "Возврат"),
                            ("adjustment_in", "Увеличение остатка"),
                            ("adjustment_out", "Уменьшение остатка"),
                        ],
                        max_length=24,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("occurred_on", models.DateField()),
                ("note", models.TextField(blank=True)),
                ("is_voided", models.BooleanField(default=False)),
                ("voided_at", models.DateTimeField(blank=True, null=True)),
                ("void_reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_accountable_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="accountable_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="accountable_transactions",
                        to="pool_service.organization",
                    ),
                ),
                (
                    "voided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="voided_accountable_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-occurred_on", "-id"],
                "indexes": [
                    models.Index(fields=["organization", "occurred_on"], name="acct_org_date_idx"),
                    models.Index(fields=["employee", "occurred_on"], name="acct_user_date_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="expensecategory",
            constraint=models.UniqueConstraint(
                fields=("organization", "name"),
                name="unique_expense_category_name",
            ),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["organization", "spent_on"], name="expense_org_date_idx"),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["organization", "status"], name="expense_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["employee", "status"], name="expense_user_status_idx"),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["client", "spent_on"], name="expense_client_date_idx"),
        ),
        migrations.AddIndex(
            model_name="expensechange",
            index=models.Index(fields=["expense", "created_at"], name="expense_change_idx"),
        ),
        migrations.AddConstraint(
            model_name="expenseperiod",
            constraint=models.UniqueConstraint(
                fields=("organization", "month"),
                name="unique_expense_period_month",
            ),
        ),
        migrations.RunPython(create_default_categories, migrations.RunPython.noop),
    ]
