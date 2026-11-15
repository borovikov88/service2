"""User-authorized CRM release: enable negative close for the existing pilot only."""
from django.db import migrations, transaction


APPROVAL = 'crm-prepared-release-20261115'


def enable_negative_close(apps, schema_editor):
    alias = schema_editor.connection.alias
    Organization = apps.get_model('pool_service', 'Organization')
    Audit = apps.get_model('pool_service', 'DataAuditLog')
    with transaction.atomic(using=alias):
        organization = Organization.objects.using(alias).select_for_update().filter(pk=1).first()
        if not organization or not organization.crm_v2_read or not organization.crm_v2_write:
            print('CRM_RELEASE_ACTIVATION: org=1 skipped; existing CRM read/write scope unavailable')
            return
        recorded = Audit.objects.using(alias).filter(organization_id=1,
            entity_type='crm_release_activation', entity_id=APPROVAL).exists()
        if not recorded:
            was_enabled = organization.crm_close
            if not was_enabled:
                Organization.objects.using(alias).filter(pk=1).update(crm_close=True)
            Audit.objects.using(alias).create(organization_id=1,
                entity_type='crm_release_activation', entity_id=APPROVAL, action='update',
                before={'crm_close': was_enabled}, after={'crm_close': True},
                changed_fields=[] if was_enabled else ['crm_close'])
        # A recorded activation must never re-enable a subsequently revoked flag.
        enabled = Organization.objects.using(alias).get(pk=1).crm_close
        print('CRM_RELEASE_ACTIVATION: org=1 close_enabled=' + str(enabled).lower())


class Migration(migrations.Migration):
    dependencies = [('pool_service', '0186_merge_crm_release')]
    # Rolling back schema is not an instruction to revoke a user's feature flag.
    operations = [migrations.RunPython(enable_negative_close, migrations.RunPython.noop)]
