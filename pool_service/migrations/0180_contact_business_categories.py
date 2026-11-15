from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0171_merge_materials_board")]

    operations = [
        migrations.AlterField(
            model_name="clientbusinessrole",
            name="role",
            field=models.CharField(max_length=20, choices=[
                ("supplier", "Поставщик"), ("personal", "Личный"),
                ("internal", "Внутренний"), ("landlord", "Арендодатель"),
                ("partner", "Партнёр"), ("other", "Другой"),
            ]),
        ),
    ]
