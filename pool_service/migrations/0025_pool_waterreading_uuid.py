import uuid

from django.db import migrations, models


def populate_uuids(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    WaterReading = apps.get_model("pool_service", "WaterReading")

    for pool in Pool.objects.filter(uuid__isnull=True):
        pool.uuid = uuid.uuid4()
        pool.save(update_fields=["uuid"])

    for reading in WaterReading.objects.filter(uuid__isnull=True):
        reading.uuid = uuid.uuid4()
        reading.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0024_pool_depth_pool_depth_max_pool_depth_min_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="uuid",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.AddField(
            model_name="waterreading",
            name="uuid",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(populate_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pool",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="waterreading",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
