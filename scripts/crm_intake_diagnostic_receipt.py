"""Only allowlisted aggregate values may leave the host in CI logs."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PREFIX='CRM_INTAKE_DIAGNOSTICS_V1 '
STATES={'pending','retry','awaiting_consumer','claimed','failed','blocked'}


def normalize(text, sha):
    if not re.fullmatch('[a-f0-9]{40}',sha):raise ValueError('sha')
    lines=[line[len(PREFIX):] for line in text.splitlines() if line.startswith(PREFIX)]
    if not lines:
        statuses=[]
        for line in text.splitlines():
            try: candidate=json.loads(line)
            except (ValueError,TypeError):continue
            if type(candidate)==dict and set(candidate)=={'status','processed'} and candidate['status'] in {'busy','deploying','release_pending'} and type(candidate['processed'])==int and candidate['processed']==0:
                statuses.append(candidate['status'])
        if len(statuses)==1:return {'expected_sha':sha,'status':statuses[0],'diagnostics_collected':False}
    if len(lines)!=1:raise ValueError('missing_receipt')
    value=json.loads(lines[0])
    keys={'schema_version','organization_id','observed_at','crm_v2_read','crm_v2_write','process_enabled',
        'event_wakeup_enabled','activation_present','cutoff','last_attempt_at','last_attempt_error',
        'scheduler_registration','scheduler_ticks','buckets'}
    if type(value)!=dict or set(value)!=keys:raise ValueError('shape')
    if type(value['schema_version'])!=int or value['schema_version']!=1 or type(value['organization_id'])!=int or value['organization_id']!=1:raise ValueError('scope')
    for key in ('crm_v2_read','crm_v2_write','process_enabled','event_wakeup_enabled','activation_present','last_attempt_error'):
        if type(value[key])!=bool:raise ValueError('flag')
    for key in ('observed_at','cutoff','last_attempt_at'):
        stamp=value[key]
        if stamp is None and key!='observed_at':continue
        if type(stamp)!=str or len(stamp)>40 or datetime.fromisoformat(stamp).tzinfo is None:raise ValueError('timestamp')
    if value['scheduler_registration']!='not_observable' or value['scheduler_ticks']!='not_recorded':raise ValueError('scheduler')
    if type(value['buckets'])!=dict or set(value['buckets'])!=STATES:raise ValueError('states')
    for row in value['buckets'].values():
        if type(row)!=dict or set(row)!={'sample_count','truncated','due_in_sample','expired_in_sample','oldest_due_age_seconds'}:raise ValueError('bucket')
        if type(row['truncated'])!=bool or type(row['sample_count'])!=int or not 0<=row['sample_count']<=100:raise ValueError('count')
        for key in ('due_in_sample','expired_in_sample','oldest_due_age_seconds'):
            if row[key] is not None and (type(row[key])!=int or not 0<=row[key]<=(row['sample_count'] if key!='oldest_due_age_seconds' else 10**10)):raise ValueError('range')
    return {'expected_sha':sha,**value}


if __name__=='__main__':
    try:
        path=Path(sys.argv[1])
        if path.stat().st_size>65536:raise ValueError('size')
        result=normalize(path.read_text(),sys.argv[2])
    except Exception:
        print('CRM_INTAKE_DIAGNOSTICS_UNAVAILABLE')
        raise SystemExit(2)
    print(PREFIX+json.dumps(result,sort_keys=True))
