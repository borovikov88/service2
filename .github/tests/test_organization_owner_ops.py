import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('organization_owner_ops',
    Path(__file__).resolve().parents[2] / 'scripts' / 'organization_owner_ops.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class OwnerOperatorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(EVENT_NAME='workflow_dispatch', REF='refs/heads/main',
            REPOSITORY=ops.REPOSITORY, COMMIT_SHA='a'*40, CONFIRM_MAIN_SHA='a'*40,
            CURRENT_MAIN_SHA='a'*40, OWNER_MODE='preview', ORGANIZATION_ID='41',
            OWNER_USER_ID='42', RUN_ID='123', RUN_ATTEMPT='1', EXPECTED_PLAN='')
        self.receipt = dict(schema_version=1, expected_sha='a'*40, organization_id=41,
            user_id=42, employee_id=43, mode='preview', status='ready', plan_digest='b'*64,
            eligible_calls=12, roles_added=0, snapshot_count=0, audit_id=None,
            run_id='123', run_attempt='1')

    def test_preview_request_is_nonmutating_and_apply_needs_plan_and_confirmation(self):
        self.assertEqual(ops.validate_request(self.env)['mode'], 'preview')
        for changes in [dict(OWNER_MODE='apply'), dict(OWNER_MODE='apply', EXPECTED_PLAN='b'*64)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ops.validate_request({**self.env, **changes})
        apply = {**self.env, 'OWNER_MODE':'apply', 'EXPECTED_PLAN':'b'*64, 'CONFIRM_SCOPE':'first-owner-approved'}
        self.assertEqual(ops.validate_request(apply)['mode'], 'apply')

    def test_nonmain_fork_stale_commit_invalid_ids_and_other_events_are_rejected(self):
        for changes in [dict(REF='refs/heads/test'), dict(REPOSITORY='synthetic/fork'),
                dict(EVENT_NAME='pull_request'), dict(CURRENT_MAIN_SHA='c'*40),
                dict(CONFIRM_MAIN_SHA='c'*40), dict(ORGANIZATION_ID='1;echo'),
                dict(OWNER_USER_ID='-2'), dict(OWNER_USER_ID='０１'), dict(RUN_ID='0')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ops.validate_request({**self.env, **changes})

    def test_valid_receipt_discards_unrelated_private_output(self):
        raw = 'synthetic private sentinel\n'+ops.PREFIX+json.dumps(self.receipt)
        self.assertEqual(ops.parse_receipt(raw, ops.context(self.env)), self.receipt)
        self.assertNotIn('sentinel', json.dumps(ops.parse_receipt(raw, ops.context(self.env))))

    def test_wrong_scope_mutating_preview_unknown_fields_and_multiple_receipts_are_rejected(self):
        for change in [dict(schema_version=True), dict(user_id=99), dict(organization_id=True), dict(expected_sha='c'*40),
                dict(roles_added=1), dict(snapshot_count=1), dict(private_label='synthetic sentinel')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                ops.parse_receipt(ops.PREFIX+json.dumps({**self.receipt, **change}), ops.context(self.env))
        raw = ops.PREFIX+json.dumps(self.receipt)
        with self.assertRaises(ValueError):
            ops.parse_receipt(raw+'\n'+raw, ops.context(self.env))

    def test_artifact_retains_only_validated_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'raw', Path(folder)/'result.json'
            source.write_text('synthetic private sentinel\n'+ops.PREFIX+json.dumps(self.receipt), encoding='utf-8')
            with patch.dict(ops.os.environ, self.env), redirect_stdout(StringIO()):
                self.assertEqual(ops.main(['receipt', str(source), str(target)]), 0)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), self.receipt)
            self.assertNotIn('sentinel', target.read_text(encoding='utf-8'))

    def test_apply_receipt_requires_matching_plan_and_audited_change(self):
        env = {**self.env, 'OWNER_MODE':'apply', 'EXPECTED_PLAN':'b'*64}
        value = {**self.receipt, 'mode':'apply', 'status':'applied', 'roles_added':1,
            'snapshot_count':12, 'audit_id':44}
        self.assertEqual(ops.parse_receipt(ops.PREFIX+json.dumps(value), ops.context(env)), value)
        for change in [dict(plan_digest='c'*64), dict(audit_id=None), dict(status='ready')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                ops.parse_receipt(ops.PREFIX+json.dumps({**value, **change}), ops.context(env))


if __name__ == '__main__':
    unittest.main()
