from django.db import migrations, models


def _map_interval_to_frequency(interval):
    if not interval:
        return None
    try:
        value = int(interval)
    except (TypeError, ValueError):
        return None
    if value <= 7:
        return "weekly"
    if value <= 15:
        return "twice_monthly"
    if value <= 31:
        return "monthly"
    if value <= 62:
        return "bimonthly"
    if value <= 93:
        return "quarterly"
    if value <= 186:
        return "twice_yearly"
    return "yearly"


def forwards(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    for pool in Pool.objects.all().only("id", "service_interval_days", "service_frequency"):
        if pool.service_frequency:
            continue
        mapped = _map_interval_to_frequency(pool.service_interval_days)
        if mapped:
            Pool.objects.filter(pk=pool.pk).update(service_frequency=mapped)


def backwards(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    Pool.objects.filter(service_frequency__isnull=False).update(service_frequency=None)


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0037_organization_water_norms"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="service_frequency",
            field=models.CharField(blank=True, choices=[("weekly", "weekly"), ("twice_monthly", "twice_monthly"), ("monthly", "monthly"), ("bimonthly", "bimonthly"), ("quarterly", "quarterly"), ("twice_yearly", "twice_yearly"), ("yearly", "yearly")], max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="service_suspended",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(forwards, backwards),
    ]
