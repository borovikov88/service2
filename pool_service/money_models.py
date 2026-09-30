"""Persistent planning layer for management money forecasting.

The models deliberately live outside primary 1C documents.  Synced rows are
immutable snapshots of published 1C planning data; management rows are explicit
Service2 expectations with an audit trail.
"""
from django.conf import settings
from django.db import models

from pool_service.models import OneCODataSyncRun, Organization, Pool


class OneCMoneyForecastSnapshot(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="money_forecast_snapshots"
    )
    sync_run = models.ForeignKey(
        OneCODataSyncRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="money_forecast_snapshots",
    )
    source_at = models.DateTimeField()
    fetched_at = models.DateTimeField()
    activated_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)
    diagnostics = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "pool_service"
        ordering = ["-source_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="money_fc_org_active_idx"),
            models.Index(fields=["organization", "source_at"], name="money_fc_org_time_idx"),
        ]


class OneCMoneyForecastRow(models.Model):
    DIRECTION_RECEIPT = "receipt"
    DIRECTION_PAYMENT = "payment"
    DIRECTION_CHOICES = [
        (DIRECTION_RECEIPT, "Поступление"),
        (DIRECTION_PAYMENT, "Платёж"),
    ]

    KIND_ORDER_SCHEDULE = "order_schedule"
    KIND_ORDER_DUE = "order_due"
    KIND_SUPPLIER_SCHEDULE = "supplier_schedule"
    KIND_CHOICES = [
        (KIND_ORDER_SCHEDULE, "График оплаты заказа покупателя"),
        (KIND_ORDER_DUE, "Срок оплаты заказа покупателя"),
        (KIND_SUPPLIER_SCHEDULE, "График оплаты заказа поставщику"),
    ]

    PRECISION_EXACT = "exact"
    PRECISION_MONTH = "month"
    PRECISION_UNKNOWN = "unknown"
    PRECISION_CHOICES = [
        (PRECISION_EXACT, "Точная дата"),
        (PRECISION_MONTH, "Месяц"),
        (PRECISION_UNKNOWN, "Дата не назначена"),
    ]

    CONFIRMED = "confirmed"
    REVIEW = "review"
    POSSIBLE = "possible"
    EXCLUDED = "excluded"
    CONFIRMATION_CHOICES = [
        (CONFIRMED, "Подтверждено"),
        (REVIEW, "Требует проверки"),
        (POSSIBLE, "Возможная продажа"),
        (EXCLUDED, "Не входит в прогноз"),
    ]

    snapshot = models.ForeignKey(
        OneCMoneyForecastSnapshot, on_delete=models.CASCADE, related_name="rows"
    )
    direction = models.CharField(max_length=12, choices=DIRECTION_CHOICES)
    item_kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    source_identity = models.CharField(max_length=96)

    order_guid = models.UUIDField(null=True, blank=True)
    agreement_guid = models.UUIDField(null=True, blank=True)
    counterparty_guid = models.UUIDField(null=True, blank=True)
    counterparty_name = models.CharField(max_length=300, blank=True)
    order_number = models.CharField(max_length=80, blank=True)
    order_date = models.DateField(null=True, blank=True)
    order_state = models.CharField(max_length=120, blank=True)
    object_name = models.CharField(max_length=300, blank=True)
    responsible_name = models.CharField(max_length=300, blank=True)

    expected_amount = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    matched_paid_amount = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    remaining_amount = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    payment_match_status = models.CharField(max_length=32, default="not_checked")

    contractual_due_date = models.DateField(null=True, blank=True)
    expected_date = models.DateField(null=True, blank=True)
    expected_month = models.DateField(null=True, blank=True)
    date_precision = models.CharField(max_length=12, choices=PRECISION_CHOICES, default=PRECISION_UNKNOWN)
    basis = models.CharField(max_length=300, blank=True)
    confirmation_status = models.CharField(
        max_length=16, choices=CONFIRMATION_CHOICES, default=REVIEW
    )
    source_updated_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = "pool_service"
        ordering = ["expected_date", "expected_month", "counterparty_name", "order_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "source_identity"], name="money_fc_snapshot_identity_uq"
            )
        ]
        indexes = [
            models.Index(fields=["snapshot", "direction", "confirmation_status"], name="money_fc_dir_status_idx"),
            models.Index(fields=["snapshot", "expected_month"], name="money_fc_month_idx"),
            models.Index(fields=["snapshot", "order_guid"], name="money_fc_order_idx"),
        ]


class ManagementMoneyPlan(models.Model):
    DIRECTION_CHOICES = OneCMoneyForecastRow.DIRECTION_CHOICES
    PRECISION_CHOICES = OneCMoneyForecastRow.PRECISION_CHOICES
    CONFIRMATION_CHOICES = OneCMoneyForecastRow.CONFIRMATION_CHOICES

    SOURCE_ORDER = "order"
    SOURCE_SERVICE = "service"
    SOURCE_PAYMENT = "payment"
    SOURCE_OWNER = "owner"
    SOURCE_TAX = "tax"
    SOURCE_PAYROLL = "payroll"
    SOURCE_RENT = "rent"
    SOURCE_LOAN = "loan"
    SOURCE_CHOICES = [
        (SOURCE_ORDER, "Заказ/договор"),
        (SOURCE_SERVICE, "Сезонное обслуживание"),
        (SOURCE_PAYMENT, "Прочее обязательство"),
        (SOURCE_OWNER, "Согласованная выплата собственнику"),
        (SOURCE_TAX, "Налог"),
        (SOURCE_PAYROLL, "Зарплата"),
        (SOURCE_RENT, "Аренда"),
        (SOURCE_LOAN, "Кредит"),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="management_money_plans"
    )
    direction = models.CharField(max_length=12, choices=DIRECTION_CHOICES)
    source_type = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    linked_order_guid = models.UUIDField(null=True, blank=True)
    pool = models.ForeignKey(
        Pool, on_delete=models.SET_NULL, null=True, blank=True, related_name="money_plans"
    )

    counterparty_name = models.CharField(max_length=300, blank=True)
    order_reference = models.CharField(max_length=160, blank=True)
    amount = models.DecimalField(max_digits=24, decimal_places=2)
    contractual_due_date = models.DateField(null=True, blank=True)
    expected_date = models.DateField(null=True, blank=True)
    expected_month = models.DateField(null=True, blank=True)
    date_precision = models.CharField(max_length=12, choices=PRECISION_CHOICES)
    basis = models.CharField(max_length=300)
    confirmation_status = models.CharField(
        max_length=16, choices=CONFIRMATION_CHOICES, default=OneCMoneyForecastRow.CONFIRMED
    )
    note = models.TextField(blank=True)

    service_period_start = models.DateField(null=True, blank=True)
    service_period_end = models.DateField(null=True, blank=True)
    service_active_months = models.JSONField(default=list, blank=True)
    service_payment_offset_months = models.SmallIntegerField(null=True, blank=True)
    service_price_rule = models.CharField(max_length=300, blank=True)
    service_exceptions = models.JSONField(default=list, blank=True)
    price_effective_from = models.DateField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="created_management_money_plans",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="updated_management_money_plans",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "pool_service"
        ordering = ["expected_date", "expected_month", "id"]
        indexes = [
            models.Index(fields=["organization", "is_active", "direction"], name="money_plan_org_dir_idx"),
            models.Index(fields=["organization", "linked_order_guid"], name="money_plan_order_idx"),
            models.Index(fields=["organization", "expected_month"], name="money_plan_month_idx"),
        ]


class ManagementMoneyPlanChange(models.Model):
    plan = models.ForeignKey(
        ManagementMoneyPlan, on_delete=models.CASCADE, related_name="changes"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="management_money_plan_changes",
    )
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "pool_service"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["plan", "created_at"], name="money_plan_change_idx")
        ]
