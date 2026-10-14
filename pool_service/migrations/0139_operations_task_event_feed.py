from django.db import migrations, models


def seed_counter(apps, schema_editor):
    Counter = apps.get_model("pool_service", "OperationsTaskEventCounter")
    Counter.objects.using(schema_editor.connection.alias).get_or_create(
        key="crm_tasks",
        defaults={"next_sequence": 1},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0138_avito_scheduler_heartbeat"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicetaskchange",
            name="operations_feed_sequence",
            field=models.PositiveBigIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.CreateModel(
            name="OperationsTaskEventCounter",
            fields=[
                ("key", models.CharField(max_length=32, primary_key=True, serialize=False)),
                ("next_sequence", models.PositiveBigIntegerField(default=1)),
            ],
        ),
        migrations.RunPython(seed_counter, migrations.RunPython.noop),
    ]
