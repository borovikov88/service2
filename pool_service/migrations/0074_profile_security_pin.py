from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0073_expense_kkm_cash_source_clear_counts"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="security_pin_changed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="security_pin_failed_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="profile",
            name="security_pin_hash",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
