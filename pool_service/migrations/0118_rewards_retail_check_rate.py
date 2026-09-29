from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0117_reward_order_links"),
    ]

    operations = [
        migrations.AddField(
            model_name="rewardschemeversion",
            name="retail_check_rate",
            field=models.DecimalField(
                decimal_places=6,
                default=Decimal("0.010000"),
                max_digits=7,
            ),
        ),
    ]
