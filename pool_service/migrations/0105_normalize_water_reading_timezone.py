from datetime import timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import migrations


def _profile_timezone_map(apps):
    Profile = apps.get_model("pool_service", "Profile")
    return dict(Profile.objects.values_list("user_id", "timezone"))


def normalize_legacy_water_reading_dates(apps, schema_editor):
    WaterReading = apps.get_model("pool_service", "WaterReading")
    timezone_by_user = _profile_timezone_map(apps)

    for reading in WaterReading.objects.exclude(added_by_id=None).iterator(chunk_size=500):
        timezone_name = timezone_by_user.get(reading.added_by_id)
        if not timezone_name or not reading.date:
            continue
        try:
            user_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            continue

        # Before this migration WaterReading.date was persisted as a local
        # wall-clock value while Django later interpreted that same value as
        # UTC. Reinterpret the stored UTC clock components in the author's
        # profile timezone, then persist the real UTC instant.
        stored_utc = reading.date
        if stored_utc.tzinfo is None:
            stored_utc = stored_utc.replace(tzinfo=datetime_timezone.utc)
        else:
            stored_utc = stored_utc.astimezone(datetime_timezone.utc)
        intended_wall_time = stored_utc.replace(tzinfo=None)
        corrected = intended_wall_time.replace(
            tzinfo=user_timezone
        ).astimezone(datetime_timezone.utc)
        WaterReading.objects.filter(pk=reading.pk).update(date=corrected)


def restore_legacy_water_reading_dates(apps, schema_editor):
    WaterReading = apps.get_model("pool_service", "WaterReading")
    timezone_by_user = _profile_timezone_map(apps)

    for reading in WaterReading.objects.exclude(added_by_id=None).iterator(chunk_size=500):
        timezone_name = timezone_by_user.get(reading.added_by_id)
        if not timezone_name or not reading.date:
            continue
        try:
            user_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            continue

        corrected_utc = reading.date
        if corrected_utc.tzinfo is None:
            corrected_utc = corrected_utc.replace(tzinfo=datetime_timezone.utc)
        local_wall_time = corrected_utc.astimezone(user_timezone).replace(tzinfo=None)
        legacy_value = local_wall_time.replace(tzinfo=datetime_timezone.utc)
        WaterReading.objects.filter(pk=reading.pk).update(date=legacy_value)


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0104_pool_service_status_history"),
    ]

    operations = [
        migrations.RunPython(
            normalize_legacy_water_reading_dates,
            restore_legacy_water_reading_dates,
        ),
    ]
