from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0181_crm_primary_contact")]

    operations = [
        migrations.RemoveConstraint(model_name="crmlead", name="crm_lead_result"),
        migrations.AlterField(
            model_name="crmlead",
            name="result",
            field=models.CharField(blank=True, default="", max_length=16, choices=[
                ("", "Не определён"), ("qualified", "Квалифицирован в сделки"),
                ("customer_saved", "Контакты сохранены без сделки"),
                ("linked", "Привязан к текущей работе"), ("non_target", "Нецелевой"),
                ("spam", "Спам"), ("duplicate", "Дубль"), ("no_deal", "Закрыт без сделки"),
            ]),
        ),
        migrations.AddConstraint(
            model_name="crmlead",
            constraint=models.CheckConstraint(
                condition=models.Q(result__in=["", "qualified", "customer_saved", "linked", "non_target", "spam", "duplicate", "no_deal"]),
                name="crm_lead_result",
            ),
        ),
    ]
