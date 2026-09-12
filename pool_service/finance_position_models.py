"""Persistent point-in-time finance-position snapshots."""

from django.db import models
from pool_service.models import OneCImportBatch, Organization


class OneCFinancePositionSnapshot(models.Model):
    batch = models.OneToOneField(OneCImportBatch, on_delete=models.PROTECT, related_name="finance_position_snapshot")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="finance_position_snapshots")
    snapshot_at = models.DateTimeField()
    source_timezone = models.CharField(max_length=64)
    fetched_at = models.DateTimeField()
    activated_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)
    content_hash = models.CharField(max_length=64, db_index=True)
    diagnostics = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "pool_service"
        ordering = ["-snapshot_at", "-id"]
        indexes = [
            models.Index(fields=["organization", "is_active"], name="fp_snap_org_active_idx"),
            models.Index(fields=["organization", "snapshot_at"], name="fp_snap_org_time_idx"),
        ]


class CashPositionRow(models.Model):
    SOURCE_REGULAR = "regular"
    SOURCE_KKM = "kkm"
    SOURCE_IN_TRANSIT = "in_transit"
    SOURCE_CHOICES = [(SOURCE_REGULAR, "Счета и кассы"), (SOURCE_KKM, "ККМ"), (SOURCE_IN_TRANSIT, "В пути")]
    snapshot = models.ForeignKey(OneCFinancePositionSnapshot, on_delete=models.CASCADE, related_name="cash_rows")
    source_kind = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    organization_guid = models.UUIDField()
    account_guid = models.UUIDField()
    reference_type = models.CharField(max_length=80)
    display_name = models.CharField(max_length=300)
    currency_guid = models.UUIDField(null=True, blank=True)
    agreement_guid = models.UUIDField(null=True, blank=True)
    amount = models.DecimalField(max_digits=24, decimal_places=2)
    amount_currency = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    transfer_document_guid = models.UUIDField(null=True, blank=True)
    transfer_document_type = models.CharField(max_length=80, blank=True)
    transfer_document_display = models.CharField(max_length=300, blank=True)
    source_identity = models.CharField(max_length=64)

    class Meta:
        app_label = "pool_service"
        ordering = ["source_kind", "display_name", "id"]
        constraints = [models.UniqueConstraint(fields=["snapshot", "source_identity"], name="fp_cash_snapshot_identity_uq")]
        indexes = [models.Index(fields=["snapshot", "source_kind"], name="fp_cash_snap_kind_idx")]


class SettlementPositionRow(models.Model):
    SIDE_CUSTOMER = "customer"
    SIDE_SUPPLIER = "supplier"
    SIDE_CHOICES = [(SIDE_CUSTOMER, "Покупатель"), (SIDE_SUPPLIER, "Поставщик")]
    CLASS_RECEIVABLE = "receivable"
    CLASS_CUSTOMER_ADVANCE = "customer_advance"
    CLASS_PAYABLE = "payable"
    CLASS_SUPPLIER_ADVANCE = "supplier_advance"
    CLASS_SIGN_ANOMALY = "sign_anomaly"
    CLASS_ZERO = "zero"
    CLASS_CHOICES = [
        (CLASS_RECEIVABLE, "Нам должны"), (CLASS_CUSTOMER_ADVANCE, "Аванс клиента"),
        (CLASS_PAYABLE, "Мы должны"), (CLASS_SUPPLIER_ADVANCE, "Наш аванс поставщику"),
        (CLASS_SIGN_ANOMALY, "Аномалия знака"), (CLASS_ZERO, "Нулевой остаток"),
    ]
    snapshot = models.ForeignKey(OneCFinancePositionSnapshot, on_delete=models.CASCADE, related_name="settlement_rows")
    side = models.CharField(max_length=16, choices=SIDE_CHOICES)
    settlement_type_raw = models.CharField(max_length=120)
    management_classification = models.CharField(max_length=32, choices=CLASS_CHOICES)
    organization_guid = models.UUIDField()
    counterparty_guid = models.UUIDField()
    counterparty_name = models.CharField(max_length=300)
    agreement_guid = models.UUIDField(null=True, blank=True)
    document_guid = models.UUIDField(null=True, blank=True)
    document_type = models.CharField(max_length=120, blank=True)
    order_guid = models.UUIDField(null=True, blank=True)
    order_type = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=24, decimal_places=2)
    amount_currency = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    amount_reg = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    is_sign_anomaly = models.BooleanField(default=False)
    source_identity = models.CharField(max_length=64)

    class Meta:
        app_label = "pool_service"
        ordering = ["side", "management_classification", "counterparty_name", "id"]
        constraints = [models.UniqueConstraint(fields=["snapshot", "source_identity"], name="fp_settle_snapshot_identity_uq")]
        indexes = [
            models.Index(fields=["snapshot", "side", "management_classification"], name="fp_settle_snap_class_idx"),
            models.Index(fields=["snapshot", "counterparty_name"], name="fp_settle_party_idx"),
        ]
