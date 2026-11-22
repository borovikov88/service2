from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0200_staff_departments")]

    operations = [
        migrations.AlterField(
            model_name="communicationchannel",
            name="kind",
            field=models.CharField(
                choices=[("avito", "Авито"), ("website", "Сайт"), ("megafon", "Мегафон"), ("max", "MAX")],
                max_length=24,
            ),
        ),
    ]
