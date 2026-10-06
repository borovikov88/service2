from django.db import migrations, models
import django.db.models.deletion


def detach_manual_uploads(apps, schema_editor):
    PhoneCall = apps.get_model("pool_service", "PhoneCall")
    TelephonyConnection = apps.get_model("pool_service", "TelephonyConnection")

    manual_connections = TelephonyConnection.objects.filter(external_id="manual-upload")
    for connection in manual_connections.iterator():
        PhoneCall.objects.filter(connection_id=connection.pk).update(
            source_kind="uploaded",
            connection_id=None,
        )

    TelephonyConnection.objects.filter(
        external_id="manual-upload",
        calls__isnull=True,
    ).delete()


def restore_manual_uploads(apps, schema_editor):
    PhoneCall = apps.get_model("pool_service", "PhoneCall")
    TelephonyConnection = apps.get_model("pool_service", "TelephonyConnection")

    organization_ids = (
        PhoneCall.objects.filter(source_kind="uploaded")
        .values_list("organization_id", flat=True)
        .distinct()
    )
    for organization_id in organization_ids:
        connection, _ = TelephonyConnection.objects.get_or_create(
            organization_id=organization_id,
            external_id="manual-upload",
            defaults={
                "name": "Загруженные записи",
                "is_active": True,
            },
        )
        PhoneCall.objects.filter(
            organization_id=organization_id,
            source_kind="uploaded",
        ).update(
            source_kind="telephony",
            connection_id=connection.pk,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0129_backfill_inactive_employees"),
    ]

    operations = [
        migrations.AddField(
            model_name="phonecall",
            name="source_kind",
            field=models.CharField(
                choices=[
                    ("telephony", "Телефония"),
                    ("uploaded", "Загруженный аудиофайл"),
                ],
                db_index=True,
                default="telephony",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="phonecall",
            name="connection",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="calls",
                to="pool_service.telephonyconnection",
            ),
        ),
        migrations.RunPython(detach_manual_uploads, restore_manual_uploads),
    ]
