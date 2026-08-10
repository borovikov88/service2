from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0088_alter_expensechange_action")]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                choices=[
                    ("limits", "limits"),
                    ("missed_visit", "missed_visit"),
                    ("daily_missing", "daily_missing"),
                    ("new_company", "new_company"),
                    ("new_personal", "new_personal"),
                    ("task_assignment", "task_assignment"),
                    ("finance", "finance"),
                    ("development", "development"),
                ],
                max_length=32,
            ),
        )
    ]
