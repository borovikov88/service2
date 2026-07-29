import hashlib

from django.db import migrations, models


def fill_credential_hashes(apps, schema_editor):
    WebAuthnCredential = apps.get_model("pool_service", "WebAuthnCredential")
    for credential in WebAuthnCredential.objects.all():
        credential.credential_id_hash = hashlib.sha256(
            credential.credential_id.encode("utf-8")
        ).hexdigest()
        credential.save(update_fields=["credential_id_hash"])


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0075_webauthn_credentials"),
    ]

    operations = [
        migrations.AddField(
            model_name="webauthncredential",
            name="credential_id_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(fill_credential_hashes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="webauthncredential",
            name="credential_id_hash",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="webauthncredential",
            name="credential_id",
            field=models.CharField(max_length=1024),
        ),
    ]
