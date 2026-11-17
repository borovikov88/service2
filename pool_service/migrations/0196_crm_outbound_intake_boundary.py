from django.db import migrations


def start_outbound(apps, schema_editor):
    # Single rollout marker, not a replay or rewrite of existing communications.
    apps.get_model("pool_service", "CrmInboxBoundary").objects.using(
        schema_editor.connection.alias).get_or_create(key="outbound_calls")


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0195_crm_lead_identity_origin")]
    operations = [migrations.RunPython(start_outbound, migrations.RunPython.noop)]
