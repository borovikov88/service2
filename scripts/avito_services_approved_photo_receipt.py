"""Closed public result for the one owner-authorised photo publication."""
from datetime import datetime
import json
import re
import sys

FRAME = 'AVITO_SERVICES_APPROVED_PHOTO_RECEIPT '
MAX_BYTES = 32768
CODES = {
    'approved_photo_prepared', 'approved_photo_published', 'approved_photo_verified',
    'approved_photo_scope_invalid', 'approved_photo_actor_invalid',
    'approved_photo_changes_invalid', 'approved_photo_history_invalid',
    'approved_photo_asset_invalid', 'approved_photo_preview_invalid',
    'approved_photo_binding_changed', 'approved_photo_busy',
    'approved_photo_prior_result_unknown', 'approved_photo_preflight_failed',
    'approved_photo_public_readback_failed', 'approved_photo_internal_error',
    'approved_photo_source_not_current_stable',
}

def require(value):
    if not value:
        raise ValueError('receipt_invalid')

def integer(value, maximum=10**9, nullable=False):
    if value is None and nullable:
        return None
    require(type(value) is int and 0 < value <= maximum)
    return value

def timestamp(value, nullable=False):
    if value is None and nullable:
        return None
    require(isinstance(value, str) and len(value) <= 40)
    require(datetime.fromisoformat(value).utcoffset() is not None)
    return value

def flag(value, nullable=False):
    require(type(value) is bool or nullable and value is None)
    return value

def text_code(value):
    require(isinstance(value, str) and re.fullmatch('[a-z0-9_]{1,90}', value))
    return value

def reference(row, *, diagnostic=False):
    require(isinstance(row, dict))
    identifier = row.get('avito_id')
    require(isinstance(identifier, str) and re.fullmatch('[1-9][0-9]{0,19}', identifier))
    digest = row.get('url_sha256')
    require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest))
    result = {'avito_id': identifier, 'photo_number': integer(row.get('photo_number'), 10),
        'url_sha256': digest}
    if not diagnostic:
        require(row.get('code') == 'published_reference_retained')
        return result | {'code': 'published_reference_retained'}
    result['checked_at'] = timestamp(row.get('checked_at'))
    result['reason'] = text_code(row.get('reason'))
    result['http_status'] = integer(row.get('http_status'), 599, nullable=True)
    if result['http_status'] is not None:
        require(result['http_status'] >= 100)
    result['cached'] = flag(row.get('cached'))
    host = row.get('source_host')
    require(isinstance(host, str) and len(host) <= 253 and (not host or re.fullmatch('[a-z0-9.-]+', host)))
    result['source_host'] = host
    if 'probe' in row:
        result['probe'] = text_code(row['probe'])
    return result

def normalize(value):
    require(isinstance(value, dict) and value.get('schema_version') == 1
        and value.get('kind') == 'avito_services_approved_photo'
        and value.get('generation') == 'services-approved-photo18-20261025'
        and type(value.get('connection_id')) is int and value['connection_id'] == 4
        and value.get('account_id') == '446312402'
        and value.get('target_avito_id') == '8343962135'
        and type(value.get('expected_draft_revision')) is int and value['expected_draft_revision'] == 18
        and type(value.get('expected_previous_published_revision')) is int
        and value['expected_previous_published_revision'] == 15)
    result = {key: value[key] for key in (
        'schema_version', 'kind', 'generation', 'connection_id', 'account_id', 'target_avito_id',
        'expected_draft_revision', 'expected_previous_published_revision')}
    require(value.get('state') in {'blocked', 'unknown', 'published', 'verified'})
    require(value.get('code') in CODES)
    result.update(state=value['state'], code=value['code'],
        checked_at=timestamp(value.get('checked_at')),
        published_at=timestamp(value.get('published_at'), nullable=True),
        readback_checked_at=timestamp(value.get('readback_checked_at'), nullable=True),
        version_id=integer(value.get('version_id'), 2**63 - 1, nullable=True))
    revision = value.get('published_revision')
    require(revision is None or type(revision) is int and revision == 18)
    active = value.get('active_count')
    require(active is None or type(active) is int and active == 11)
    result.update(published_revision=revision, active_count=active)
    for key in ('asset_storage_verified', 'authorized_preview_verified'):
        result[key] = flag(value.get(key))
    for key in ('xml_public_verified', 'image_public_verified', 'source_matches_current_stable', 'configured_source_public_verified'):
        result[key] = flag(value.get(key), nullable=True)
    require(value.get('source_mode') == 'stable_current')
    result['source_mode'] = 'stable_current'
    require(value.get('configured_source_format') in {'xml', 'xlsx', 'unknown'})
    result['configured_source_format'] = value['configured_source_format']
    for key, maximum in (('legacy_diagnostics', 2), ('retained_references', 110)):
        rows = value.get(key)
        require(isinstance(rows, list) and len(rows) <= maximum)
        result[key] = [reference(row, diagnostic=key == 'legacy_diagnostics') for row in rows]
    report = value.get('provider_report')
    require(isinstance(report, dict) and report.get('state') in {
        'not_checked', 'unavailable', 'predates_publication', 'later_unfinished', 'later_completed'})
    upload = report.get('upload_id')
    require(upload is None or isinstance(upload, str) and re.fullmatch('[1-9][0-9]{0,31}', upload))
    require(report.get('upload_status') in {'', 'success', 'success_warning', 'error', 'processing', 'in_progress', 'created', 'pending'})
    projected_report = {'state': report['state'], 'upload_id': upload,
        'started_at': timestamp(report.get('started_at'), nullable=True),
        'complete': flag(report.get('complete'), nullable=True), 'upload_status': report['upload_status'],
        'target': None}
    target = report.get('target')
    if target is not None:
        require(isinstance(target, dict) and target.get('avito_id') == '8343962135'
            and target.get('status') in {None, 'active', 'old', 'archived', 'removed', 'blocked', 'rejected'})
        messages = target.get('messages')
        require(isinstance(messages, list) and len(messages) <= 50)
        projected_messages = []
        for row in messages:
            require(isinstance(row, dict) and row.get('type') in {'error', 'alarm', 'warning', 'info'}
                and type(row.get('code')) is int and -2**31 <= row['code'] < 2**31)
            projected_messages.append({'type': row['type'], 'code': row['code']})
        projected_report['target'] = {'avito_id': '8343962135',
            'status': target['status'], 'messages': projected_messages}
    if report['state'] in {'predates_publication', 'later_unfinished', 'later_completed'}:
        require(upload is not None and projected_report['started_at'] is not None
            and result['published_at'] is not None)
        later = datetime.fromisoformat(projected_report['started_at']) > datetime.fromisoformat(result['published_at'])
        require(later == (report['state'] != 'predates_publication'))
        if report['state'] == 'later_completed':
            require(projected_report['complete'] is True
                and report['upload_status'] in {'success', 'success_warning', 'error'})
    result['provider_report'] = projected_report
    facts = value.get('facts')
    require(isinstance(facts, dict))
    result['facts'] = {key: flag(facts.get(key)) for key in (
        'file_published', 'upload_found', 'source_confirmed', 'rows_processed', 'live_content_verified')}
    require(result['facts']['live_content_verified'] is False)
    if result['state'] in {'published', 'verified'}:
        require(result['version_id'] is not None and revision == 18 and active == 11
            and result['published_at'] is not None and result['facts']['file_published'])
    if result['state'] == 'verified':
        require(result['code'] == 'approved_photo_verified'
            and result['xml_public_verified'] is True and result['image_public_verified'] is True
            and result['source_matches_current_stable'] is True
            and result['configured_source_public_verified'] is True)
    require(len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_BYTES)
    return result

def main():
    try:
        with open(sys.argv[1], 'rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        frames = [line[len(FRAME):] for line in raw.decode().splitlines() if line.startswith(FRAME)]
        require(len(frames) == 1)
        result = normalize(json.loads(frames[0]))
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        print('AVITO_SERVICES_APPROVED_PHOTO_UNAVAILABLE receipt_invalid')
        return 0
    print('AVITO_SERVICES_APPROVED_PHOTO_RESULT ' + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0

if __name__ == '__main__':
    sys.exit(main())
