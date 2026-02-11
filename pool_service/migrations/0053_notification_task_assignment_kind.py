from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0052_servicetask_end_time_servicetask_start_time"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("limits", "limits"),
                    ("missed_visit", "missed_visit"),
                    ("daily_missing", "daily_missing"),
                    ("new_company", "new_company"),
                    ("new_personal", "new_personal"),
                    ("task_assignment", "task_assignment"),
                ],
            ),
        ),
    ]
