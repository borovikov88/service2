from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0044_crmitem_urgency"),
    ]

    operations = [
        migrations.AddField(
            model_name="crmitem",
            name="photo",
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to="crm_issues/",
            ),
        ),
    ]
