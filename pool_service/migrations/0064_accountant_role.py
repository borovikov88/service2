from django.db import migrations, models


ROLE_CHOICES = [
    ("owner", "Владелец"),
    ("manager", "Менеджер"),
    ("service", "Сервис"),
    ("installer", "Монтажник"),
    ("accountant", "Бухгалтер"),
    ("admin", "Администратор"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0063_company_expenses"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organizationaccess",
            name="role",
            field=models.CharField(
                choices=ROLE_CHOICES,
                max_length=20,
                verbose_name="Роль",
            ),
        ),
        migrations.AlterField(
            model_name="organizationinvite",
            name="role",
            field=models.CharField(
                choices=ROLE_CHOICES,
                default="service",
                max_length=20,
            ),
        ),
    ]
