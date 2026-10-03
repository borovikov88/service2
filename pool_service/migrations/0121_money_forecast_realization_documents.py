from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0120_management_money_forecast"),
    ]

    operations = [
        migrations.AddField(
            model_name="onecmoneyforecastrow",
            name="document_guid",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="onecmoneyforecastrow",
            name="document_type",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AlterField(
            model_name="onecmoneyforecastrow",
            name="item_kind",
            field=models.CharField(
                choices=[
                    ("order_schedule", "График оплаты заказа покупателя"),
                    ("order_due", "Срок оплаты заказа покупателя"),
                    ("realization_due", "Срок оплаты реализации по договору"),
                    ("supplier_schedule", "График оплаты заказа поставщику"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="onecmoneyforecastrow",
            index=models.Index(
                fields=["snapshot", "document_guid"],
                name="money_fc_document_idx",
            ),
        ),
    ]
