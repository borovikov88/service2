from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0204_customer_reminders")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField(
                    model_name="customerreminderpolicy", old_name="supervisor", new_name="fallback_owner",
                ),
                migrations.AlterField(
                    model_name="customerreminderpolicy", name="fallback_owner",
                    field=models.ForeignKey(
                        db_column="supervisor_id", on_delete=django.db.models.deletion.PROTECT,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AlterField(
            model_name="customerreminderpolicy", name="duty_manager",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

