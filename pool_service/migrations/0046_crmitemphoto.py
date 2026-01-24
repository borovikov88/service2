from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0045_crmitem_photo"),
    ]

    operations = [
        migrations.CreateModel(
            name="CrmItemPhoto",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("image", models.ImageField(upload_to="crm_issues/")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("item", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="photos", to="pool_service.crmitem")),
            ],
        ),
    ]
