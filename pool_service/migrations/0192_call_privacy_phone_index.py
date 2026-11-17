"""Index existing number metadata; never replay intake or call processing."""
import re
from django.db import migrations, models


def _key(value):
    value = str(value or '').strip()
    if len(value) > 40 or not re.fullmatch(r'\+?[0-9 ()\t.\-]+', value):
        return ''
    digits = ''.join(ch for ch in value if ch.isdigit())
    if value.startswith('+'):
        if len(digits) == 11 and digits.startswith('7'):
            return digits[1:]
        return '+' + digits if 8 <= len(digits) <= 15 else ''
    if len(digits) == 11 and digits[0] in {'7', '8'}:
        return digits[1:]
    if len(digits) == 10:
        return digits
    return '+' + digits if 8 <= len(digits) <= 15 else ''


def populate_metadata(apps, schema_editor):
    calls = apps.get_model('pool_service', 'PhoneCall').objects.using(schema_editor.connection.alias)
    access = apps.get_model('pool_service', 'OrganizationAccess').objects.using(schema_editor.connection.alias)
    owners = set(access.filter(role='owner').values_list('organization_id', 'user_id'))
    cursor = 0
    while True:
        batch = list(calls.filter(pk__gt=cursor).order_by('pk').only('pk', 'phone_number',
            'organization_id', 'employee_id', 'peer_employee_id', 'owner_user_snapshot')[:200])
        if not batch:
            return
        for call in batch:
            call.privacy_phone_source = call.phone_number or ''
            call.privacy_phone_key = _key(call.privacy_phone_source)
            # Current ownership provides a conservative historical access floor.
            # It does not claim a historical role or classify a call as internal.
            participants = [uid for uid in (call.employee_id, call.peer_employee_id)
                if uid and (call.organization_id, uid) in owners]
            if call.owner_user_snapshot is None and participants:
                call.owner_user_snapshot = min(participants)
        calls.bulk_update(batch, ['privacy_phone_source', 'privacy_phone_key', 'owner_user_snapshot'], batch_size=200)
        cursor = batch[-1].pk


class Migration(migrations.Migration):
    dependencies = [('pool_service', '0191_telephone_assignment_revocation')]
    operations = [
        migrations.AddField(model_name='phonecall', name='privacy_phone_key',
            field=models.CharField(max_length=16, blank=True, editable=False, db_index=True)),
        migrations.AddField(model_name='phonecall', name='privacy_phone_source',
            field=models.CharField(max_length=40, blank=True, editable=False)),
        migrations.RunPython(populate_metadata, migrations.RunPython.noop),
    ]
