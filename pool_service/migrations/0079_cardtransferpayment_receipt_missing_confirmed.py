from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0078_cardtransferpayment_cardtransferattachment_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="cardtransferpayment",
            name="receipt_missing_confirmed",
            field=models.BooleanField(default=False),
        ),
    ]
