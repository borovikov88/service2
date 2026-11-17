"""Allowlisted operator request/receipt boundary; no private rows in CI output."""
import json
import os
from pathlib import Path
import re
import sys

REPOSITORY = 'borovikov88/service2'
PREFIX = 'ORG_OWNER_BOOTSTRAP_V1 '


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,17}', value):
        raise ValueError('invalid_scope')
    return int(value)


def context(env):
    sha = env.get('COMMIT_SHA', '')
    mode = env.get('OWNER_MODE', '')
    plan = env.get('EXPECTED_PLAN', '')
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or mode not in {'preview', 'apply'}:
        raise ValueError('invalid_request')
    if mode == 'apply' and not re.fullmatch(r'[a-f0-9]{64}', plan):
        raise ValueError('reviewed_plan_required')
    if mode == 'preview' and plan:
        raise ValueError('preview_plan_must_be_empty')
    run_id, attempt = env.get('RUN_ID', ''), env.get('RUN_ATTEMPT', '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', run_id) or not re.fullmatch(r'[1-9][0-9]{0,5}', attempt):
        raise ValueError('invalid_execution')
    return dict(expected_sha=sha, organization_id=identifier(env.get('ORGANIZATION_ID', '')),
        user_id=identifier(env.get('OWNER_USER_ID', '')), mode=mode, plan_digest=plan,
        run_id=run_id, run_attempt=attempt)


def validate_request(env):
    result = context(env)
    if (env.get('EVENT_NAME') != 'workflow_dispatch' or env.get('REF') != 'refs/heads/main'
            or env.get('REPOSITORY') != REPOSITORY or env.get('CONFIRM_MAIN_SHA') != result['expected_sha']
            or env.get('CURRENT_MAIN_SHA') != result['expected_sha']):
        raise ValueError('exact_main_required')
    if result['mode'] == 'apply' and env.get('CONFIRM_SCOPE') != 'first-owner-approved':
        raise ValueError('owner_confirmation_required')
    return result


def parse_receipt(raw, expected):
    if len(raw) > 65536:
        raise ValueError('invalid_receipt')
    rows = [line[len(PREFIX):] for line in raw.splitlines() if line.startswith(PREFIX)]
    if len(rows) != 1:
        raise ValueError('invalid_receipt')
    value = json.loads(rows[0])
    fields = {'schema_version', 'expected_sha', 'organization_id', 'user_id', 'employee_id',
        'mode', 'status', 'plan_digest', 'eligible_calls', 'roles_added', 'snapshot_count',
        'audit_id', 'run_id', 'run_attempt'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('invalid_receipt')
    for key in ['expected_sha', 'organization_id', 'user_id', 'mode', 'run_id', 'run_attempt']:
        if type(value[key]) is not type(expected[key]) or value[key] != expected[key]:
            raise ValueError('receipt_scope_mismatch')
    for key in ['employee_id', 'eligible_calls', 'roles_added', 'snapshot_count']:
        if type(value[key]) is not int or value[key] < (1 if key == 'employee_id' else 0):
            raise ValueError('invalid_receipt')
    if value['audit_id'] is not None and (type(value['audit_id']) is not int or value['audit_id'] < 1):
        raise ValueError('invalid_receipt')
    if (not isinstance(value['plan_digest'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['plan_digest'])
            or value['roles_added'] not in {0, 1}):
        raise ValueError('invalid_receipt')
    if value['mode'] == 'preview':
        if (value['status'] not in {'ready', 'already_owner'} or value['roles_added']
                or value['snapshot_count'] or value['audit_id'] is not None):
            raise ValueError('invalid_receipt')
    elif (value['plan_digest'] != expected['plan_digest']
            or value['status'] not in {'applied', 'already_owner'}
            or value['roles_added'] != int(value['status'] == 'applied')
            or (value['roles_added'] or value['snapshot_count']) and value['audit_id'] is None):
        raise ValueError('invalid_receipt')
    return value


def main(argv):
    try:
        if argv == ['validate-request']:
            validate_request(os.environ)
            print('ORG_OWNER_REQUEST_ACCEPTED')
        elif len(argv) in (2, 3) and argv[0] == 'receipt':
            raw = Path(argv[1]).read_text(encoding='utf-8')
            result = parse_receipt(raw, context(os.environ))
            if len(argv) == 3:
                # Only validated protocol metadata is retained. Raw command
                # output and backups never become workflow artifacts.
                Path(argv[2]).write_text(json.dumps(result, sort_keys=True)+'\n', encoding='utf-8')
            print(PREFIX + json.dumps(result, sort_keys=True))
        else:
            raise ValueError('invalid_arguments')
    except Exception:
        print('ORG_OWNER_OPERATION_UNAVAILABLE')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
