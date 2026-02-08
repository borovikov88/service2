from django.db import migrations, models
from django.utils import timezone
from django.db.models import Min


def set_pool_created_at(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    WaterReading = apps.get_model("pool_service", "WaterReading")

    min_dates = (
        WaterReading.objects.values("pool_id")
        .annotate(min_date=Min("date"))
    )
    min_by_pool = {row["pool_id"]: row["min_date"] for row in min_dates if row["min_date"]}

    for pool in Pool.objects.all():
        if pool.id in min_by_pool:
            pool.created_at = min_by_pool[pool.id]
            pool.save(update_fields=["created_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0046_crmitemphoto"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="created_at",
            field=models.DateTimeField(default=timezone.now),
        ),
        migrations.RunPython(set_pool_created_at, reverse_code=migrations.RunPython.noop),
    ]
