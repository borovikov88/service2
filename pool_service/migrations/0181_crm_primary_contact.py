from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0180_contact_business_categories")]

    operations = [
        migrations.AddField(
            model_name="crmlead",
            name="contact",
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contact_leads", to="pool_service.client"),
        ),
        migrations.AddField(
            model_name="crmdeal",
            name="contact",
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contact_deals", to="pool_service.client"),
        ),
    ]
