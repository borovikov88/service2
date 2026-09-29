from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0118_rewards_retail_check_rate"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RewardCustomerManagerRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("onec_customer_id", models.CharField(max_length=120)),
                ("raw_name", models.CharField(blank=True, max_length=500)),
                ("share", models.DecimalField(decimal_places=6, default=Decimal("1.000000"), max_digits=7)),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_reward_customer_manager_rules", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reward_customer_manager_rules", to="pool_service.employee")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reward_customer_manager_rules", to="pool_service.organization")),
            ],
        ),
        migrations.AddIndex(
            model_name="rewardcustomermanagerrule",
            index=models.Index(fields=["organization", "onec_customer_id", "effective_from"], name="reward_cust_mgr_lookup_idx"),
        ),
    ]
