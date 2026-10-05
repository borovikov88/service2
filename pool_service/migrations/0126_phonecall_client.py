from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0125_client_crm_foundation"),
    ]

    operations = [
        migrations.AddField(
            model_name="phonecall",
            name="client",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="phone_calls",
                to="pool_service.client",
            ),
        ),
    ]
