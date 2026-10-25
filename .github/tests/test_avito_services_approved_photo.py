"""Offline guards for the fixed, explicitly authorised photo publication."""
import contextlib
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('photo_receipt',
    ROOT / 'scripts/avito_services_approved_photo_receipt.py')
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)
BASE_SPEC = importlib.util.spec_from_file_location('photo_remote_base',
    ROOT / '.github/tests/test_avito_readonly_workflow.py')
BASE = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(BASE)


def sample():
    return {'schema_version': 1, 'kind': 'avito_services_approved_photo',
        'generation': 'services-approved-photo18-20261025', 'connection_id': 4,
        'account_id': '446312402', 'target_avito_id': '8343962135',
        'expected_draft_revision': 18, 'expected_previous_published_revision': 15,
        'checked_at': '2026-10-25T05:00:00+00:00', 'state': 'blocked',
        'code': 'approved_photo_scope_invalid', 'version_id': None,
        'published_revision': None, 'published_at': None, 'readback_checked_at': None,
        'active_count': None, 'asset_storage_verified': False, 'authorized_preview_verified': False,
        'local_publication_committed': False, 'current_publication_verified': None, 'media_failure': None,
        'xml_public_verified': None, 'image_public_verified': None,
        'legacy_diagnostics': [], 'retained_references': [],
        'provider_report': {'state': 'not_checked', 'upload_id': None, 'started_at': None,
            'complete': None, 'upload_status': '', 'target': None},
        'source_mode': 'stable_current', 'source_matches_current_stable': None,
        'configured_source_format': 'unknown', 'configured_source_public_verified': None,
        'facts': dict.fromkeys(('file_published', 'upload_found', 'source_confirmed',
                               'rows_processed', 'live_content_verified'), False)}


class ReceiptTests(unittest.TestCase):
    def test_closed_projection_rejects_false_success_and_drops_private_fields(self):
        value = sample() | {'private_token': 'PRIVATE', 'raw_url': 'https://private.invalid/cap'}
        self.assertNotIn('PRIVATE', json.dumps(RECEIPT.normalize(value)))
        for mutation in ({'connection_id': 1}, {'account_id': 'foreign'},
                         {'state': 'verified'}, {'published_revision': True},
                         {'active_count': 10}, {'source_mode': 'immutable_pin'}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                RECEIPT.normalize(value | mutation)
        value['facts']['live_content_verified'] = True
        with self.assertRaises(ValueError):
            RECEIPT.normalize(value)

    def test_safe_legacy_location_does_not_claim_an_unprobed_photo_failed(self):
        value = sample()
        value['legacy_diagnostics'] = [{
            'avito_id': '8346503274', 'photo_number': 1, 'url_sha256': 'a' * 64,
            'checked_at': value['checked_at'], 'reason': 'not_probed_same_host',
            'http_status': None, 'cached': False, 'source_host': 'photos.invalid',
            'raw_url': 'PRIVATE'}]
        normalized = RECEIPT.normalize(value)
        row = normalized['legacy_diagnostics'][0]
        self.assertIsNone(row['http_status'])
        self.assertNotIn('raw_url', row)
        for key, bad in (('photo_number', 11), ('url_sha256', 'PRIVATE'),
                         ('source_host', 'https://private.invalid/path')):
            invalid = deepcopy(value)
            invalid['legacy_diagnostics'][0][key] = bad
            with self.assertRaises(ValueError):
                RECEIPT.normalize(invalid)

    def test_framing_never_outputs_greetings_or_raw_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stdout'
            for raw, valid in (
                ('PRIVATE\n' + RECEIPT.FRAME + json.dumps(sample()) + '\n', True),
                ('PRIVATE', False),
                (RECEIPT.FRAME + '{}\n' + RECEIPT.FRAME + '{}\n', False)):
                path.write_text(raw)
                output = io.StringIO()
                with patch.object(RECEIPT.sys, 'argv', ['receipt', str(path)]), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(RECEIPT.main(), 0)
                self.assertNotIn('PRIVATE', output.getvalue())
                self.assertIn('PHOTO_RESULT' if valid else 'receipt_invalid', output.getvalue())


class RemoteTests(BASE.RemoteTests):
    def setUp(self):
        super().setUp()
        path = self.base / 'venv/bin/python'
        source = path.read_text().replace(
            'manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15',
            'manage.py publish_avito_approved_photo --expected-sha ' + BASE.SHA +
            ' --generation services-approved-photo18-20261025')
        source = source.replace('touch "$AVITO_TEST_MARKER"',
            '[[ "$SERVICE2_AVITO_APPROVED_PHOTO" == 1 ]]\ntouch "$AVITO_TEST_MARKER"')
        path.write_text(source)

    def run_remote(self, **updates):
        return subprocess.run(['bash', str(ROOT / '.github/scripts/avito_services_approved_photo_remote.sh'),
            BASE.SHA, str(self.app)], env=self.env | updates, capture_output=True, text=True, timeout=10)


class WorkflowTests(unittest.TestCase):
    def test_exact_marker_after_health_never_starts_for_an_unrelated_release(self):
        source = (ROOT / '.github/workflows/ci-deploy.yml').read_text()
        start = source.index("      - name: Publish only the owner's approved Services draft18 photo10")
        end = source.index('      - name:', start + 10)
        section = source[start:end]
        self.assertGreater(start, source.index('      - name: Verify deployed site health'))
        self.assertGreater(start, source.index('      - name: Set up authorised Avito services connection once'))
        self.assertIn("github.event_name == 'push'", section)
        self.assertIn("github.ref == 'refs/heads/main'", section)
        self.assertIn('[avito-services-approved-photo18-20261025]', section)
        self.assertIn('scripts/avito_services_approved_photo_receipt.py', section)
        self.assertNotIn('cat ', section)
        remote = (ROOT / '.github/scripts/avito_services_approved_photo_remote.sh').read_text()
        self.assertIn('SERVICE2_AVITO_APPROVED_PHOTO=1', remote)
        self.assertIn('flock -n 9', remote)
        self.assertIn('git ls-remote --exit-code origin refs/heads/main', remote)
        self.assertNotIn('force', remote)


if __name__ == '__main__':
    unittest.main()
