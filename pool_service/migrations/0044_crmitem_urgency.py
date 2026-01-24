from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0043_crmitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="crmitem",
            name="urgency",
            field=models.CharField(
                blank=True,
                choices=[
                    ("low", "\u041d\u0435\u0441\u0440\u043e\u0447\u043d\u043e"),
                    ("required", "\u0422\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f"),
                    ("critical", "\u041a\u0440\u0438\u0442\u0438\u0447\u043d\u043e"),
                ],
                max_length=20,
            ),
        ),
    ]
