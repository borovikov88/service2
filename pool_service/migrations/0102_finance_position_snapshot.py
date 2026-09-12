from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0101_financemcpclient_financemcpprincipal_financemcpgrant_and_more")]
    operations = [
        migrations.CreateModel(
            name="OneCFinancePositionSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("snapshot_at", models.DateTimeField()),
                ("source_timezone", models.CharField(max_length=64)),
                ("fetched_at", models.DateTimeField()),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=False)),
                ("content_hash", models.CharField(db_index=True, max_length=64)),
                ("diagnostics", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("batch", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="finance_position_snapshot", to="pool_service.onecimportbatch")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="finance_position_snapshots", to="pool_service.organization")),
            ],
            options={"ordering": ["-snapshot_at", "-id"], "indexes": [models.Index(fields=["organization", "is_active"], name="fp_snap_org_active_idx"), models.Index(fields=["organization", "snapshot_at"], name="fp_snap_org_time_idx")]},
        ),
        migrations.CreateModel(
            name="CashPositionRow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_kind", models.CharField(choices=[("regular", "Счета и кассы"), ("kkm", "ККМ"), ("in_transit", "В пути")], max_length=16)),
                ("organization_guid", models.UUIDField()), ("account_guid", models.UUIDField()),
                ("reference_type", models.CharField(max_length=80)), ("display_name", models.CharField(max_length=300)),
                ("currency_guid", models.UUIDField(blank=True, null=True)), ("agreement_guid", models.UUIDField(blank=True, null=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=24)),
                ("amount_currency", models.DecimalField(blank=True, decimal_places=2, max_digits=24, null=True)),
                ("transfer_document_guid", models.UUIDField(blank=True, null=True)),
                ("transfer_document_type", models.CharField(blank=True, max_length=80)),
                ("transfer_document_display", models.CharField(blank=True, max_length=300)),
                ("source_identity", models.CharField(max_length=64)),
                ("snapshot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cash_rows", to="pool_service.onecfinancepositionsnapshot")),
            ],
            options={"ordering": ["source_kind", "display_name", "id"], "indexes": [models.Index(fields=["snapshot", "source_kind"], name="fp_cash_snap_kind_idx")], "constraints": [models.UniqueConstraint(fields=("snapshot", "source_identity"), name="fp_cash_snapshot_identity_uq")]},
        ),
        migrations.CreateModel(
            name="SettlementPositionRow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("side", models.CharField(choices=[("customer", "Покупатель"), ("supplier", "Поставщик")], max_length=16)),
                ("settlement_type_raw", models.CharField(max_length=120)),
                ("management_classification", models.CharField(choices=[("receivable", "Нам должны"), ("customer_advance", "Аванс клиента"), ("payable", "Мы должны"), ("supplier_advance", "Наш аванс поставщику"), ("sign_anomaly", "Аномалия знака"), ("zero", "Нулевой остаток")], max_length=32)),
                ("organization_guid", models.UUIDField()), ("counterparty_guid", models.UUIDField()),
                ("counterparty_name", models.CharField(max_length=300)), ("agreement_guid", models.UUIDField(blank=True, null=True)),
                ("document_guid", models.UUIDField(blank=True, null=True)), ("document_type", models.CharField(blank=True, max_length=120)),
                ("order_guid", models.UUIDField(blank=True, null=True)), ("order_type", models.CharField(blank=True, max_length=120)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=24)),
                ("amount_currency", models.DecimalField(blank=True, decimal_places=2, max_digits=24, null=True)),
                ("amount_reg", models.DecimalField(blank=True, decimal_places=2, max_digits=24, null=True)),
                ("is_sign_anomaly", models.BooleanField(default=False)), ("source_identity", models.CharField(max_length=64)),
                ("snapshot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="settlement_rows", to="pool_service.onecfinancepositionsnapshot")),
            ],
            options={"ordering": ["side", "management_classification", "counterparty_name", "id"], "indexes": [models.Index(fields=["snapshot", "side", "management_classification"], name="fp_settle_snap_class_idx"), models.Index(fields=["snapshot", "counterparty_name"], name="fp_settle_party_idx")], "constraints": [models.UniqueConstraint(fields=("snapshot", "source_identity"), name="fp_settle_snapshot_identity_uq")]},
        ),
    ]
