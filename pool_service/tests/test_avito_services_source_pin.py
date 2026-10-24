import io
import json
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace

from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service import avito_services_source_pin as pin
from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_publication_models import AvitoServicesPublication
from pool_service.avito_services_publication import bind_publication
from pool_service.avito_services_xml import xml_bytes
from pool_service.communication_models import AvitoCredential
from pool_service.tests.test_avito_services_release_operation import ServicesReleaseOperationTests


@override_settings(ALLOWED_HOSTS=['service2.aqualine22.ru'], AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class SourcePinTests(TestCase):
    setUp = ServicesReleaseOperationTests.setUp

    def photo_event(self):
        self.feed.refresh_from_db()
        before = self.feed.revision
        staged = deepcopy(self.feed.draft)
        staged[0]['rows'][0]['ImageUrls'] = 'https://photos.example.invalid/temporary.jpg'
        edit = AvitoServicesFeedVersion.objects.create(feed=self.feed, revision=before + 1,
            action='edit', snapshot=staged)
        restore = AvitoServicesFeedVersion.objects.create(feed=self.feed, revision=before + 2,
            action='edit', snapshot=deepcopy(self.feed.draft))
        self.feed.revision = before + 2
        self.feed.save(update_fields=['revision'])
        return {'connection_id': 4, 'account_id': '446312402',
            'checked_at': timezone.now().isoformat(), 'source': self.source,
            'revision_before': before, 'revision_after': before + 2,
            'edit_version_id': edit.pk, 'restore_version_id': restore.pk,
            **dict.fromkeys(('upload_saved', 'private_preview_verified', 'cover_order_saved',
                            'original_draft_restored', 'published_unchanged'), True),
            **dict.fromkeys(('image_publicly_available', 'browser_clicks_verified',
                            'provider_content_verified'), False)}

    def inventory(self, connection, sheets):
        from pool_service.avito_services_feed import entries
        return {'sheets': deepcopy(sheets), 'observations': [
            {'id': row['Id'], 'avito_id': row['AvitoId'], 'status': 'active', 'included': True,
             'checked_at': timezone.now().isoformat(), 'finish_time': None,
             'finish_time_source': '', 'source': 'avito_item_detail'}
            for row in entries(sheets)]}

    def execute(self):
        return pin.run_source_pin('a' * 40, pin.GENERATION)

    def prepare(self):
        self.photo = self.photo_event()
        self.photo_patch = patch.object(pin, '_photo_before_publication', return_value=self.photo)
        self.photo_check = self.photo_patch.start()
        self.addCleanup(self.photo_patch.stop)
        self.inventory_patch = patch('pool_service.avito_services_feed.verify_inventory', side_effect=self.inventory)
        self.verify = self.inventory_patch.start()
        self.addCleanup(self.inventory_patch.stop)
        self.media_patch = patch('pool_service.avito_services_feed.validate_snapshot_media')
        self.media = self.media_patch.start()
        self.addCleanup(self.media_patch.stop)
        self.http_patch = patch('pool_service.avito_input_copy._download_url', side_effect=lambda *args: xml_bytes(self.feed.published))
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    def test_ordinary_same_content_publication_gets_now_binding_and_one_durable_result(self):
        self.prepare()
        original = deepcopy(self.feed.published)
        started = timezone.now()
        first = self.execute()
        second = self.execute()
        self.assertEqual(first['state'], 'published')
        self.assertEqual(second['state'], 'already_published')
        self.assertEqual(first['publication']['revision'], self.photo['revision_after'])
        self.assertTrue(first['same_content'])
        self.assertTrue(first['all_ids_active'])
        self.assertEqual(first['excluded_count'], 0)
        self.assertTrue(first['publication']['http_matches'])
        self.assertFalse(first['publication']['facts']['source_confirmed'])
        self.assertFalse(first['publication']['facts']['live_content_verified'])
        self.assertFalse(first['photo_before_publication']['provider_content_verified'])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, original)
        self.assertEqual(self.feed.published, original)
        self.assertEqual(self.feed.published_revision, self.photo['revision_after'])
        publication = AvitoServicesPublication.objects.get(version__feed=self.feed)
        self.assertGreaterEqual(publication.version.created_at, started)
        self.assertEqual(self.feed.versions.filter(action='publish').count(), 1)
        self.assertEqual(self.feed.versions.filter(action='legacy_published', published_revision=15).count(), 1)
        self.assertEqual(self.photo_check.call_count, 1)
        self.assertEqual(self.verify.call_count, 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'keep': 'goods'})
        encoded = json.dumps(first)
        for private in ('https://', 'sha256', 'operation_id', 'synthetic-capability'):
            self.assertNotIn(private, encoded)

    def test_missing_real_photo_receipt_blocks_without_inventory_or_publication(self):
        with patch('pool_service.avito_services_release_operation.run_release_operation',
                   return_value={'state': 'failed'}), \
                patch('pool_service.avito_services_feed.verify_inventory') as verify:
            result = self.execute()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['code'], 'source_pin_photo_required')
        self.assertIsNone(result['photo_before_publication'])
        self.assertFalse(AvitoServicesPublication.objects.exists())
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.published_revision, 15)
        verify.assert_not_called()

    def test_completed_r1_response_without_durable_receipt_is_insufficient(self):
        with patch('pool_service.avito_services_release_operation.run_release_operation',
                   return_value={'state': 'completed'}), \
                patch('pool_service.avito_services_feed.verify_inventory') as verify:
            result = self.execute()
        self.assertEqual(result['code'], 'source_pin_photo_required')
        self.assertFalse(AvitoServicesPublication.objects.exists())
        verify.assert_not_called()

    def test_pending_owner_draft_is_preserved_and_no_photo_or_file_action_runs(self):
        pending = deepcopy(self.feed.draft)
        pending[0]['rows'][0]['Title'] = 'Pending owner change'
        AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(draft=pending, revision=18)
        with patch.object(pin, '_photo_before_publication') as photo, \
                patch('pool_service.avito_services_feed.verify_inventory') as verify:
            result = self.execute()
        self.assertEqual(result['code'], 'source_pin_pending_draft')
        self.assertFalse(result['same_content'])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, pending)
        self.assertEqual(self.feed.published_revision, 15)
        photo.assert_not_called()
        verify.assert_not_called()

    def test_archived_original_row_is_never_excluded_to_make_pin_succeed(self):
        self.prepare()
        def archived(connection, sheets):
            verified = self.inventory(connection, sheets)
            verified['sheets'][0]['rows'].pop()
            verified['observations'][-1].update(status='old', included=False)
            return verified
        self.verify.side_effect = archived
        result = self.execute()
        self.assertEqual(result['code'], 'source_pin_inventory_changed')
        self.assertFalse(result['all_ids_active'])
        self.assertEqual(result['excluded_count'], 1)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.published_revision, 15)
        self.assertEqual(self.feed.published, self.original)
        self.assertFalse(AvitoServicesPublication.objects.exists())
        self.media.assert_not_called()

    def test_owner_draft_changes_during_prepare_abort_before_mutation(self):
        self.prepare()
        pending = deepcopy(self.feed.draft)
        pending[0]['rows'][0]['Description'] = 'New owner edit while media check ran'
        def changed(*args, **kwargs):
            AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(
                draft=pending, revision=self.feed.revision + 1)
        self.media.side_effect = changed
        result = self.execute()
        self.assertEqual(result['code'], 'source_pin_binding_changed')
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, pending)
        self.assertEqual(self.feed.published_revision, 15)
        self.assertFalse(AvitoServicesPublication.objects.exists())

    def test_credential_rotation_after_provider_scan_aborts_the_shared_publish(self):
        self.prepare()
        def rotated(connection, sheets):
            value = self.inventory(connection, sheets)
            AvitoCredential.objects.filter(connection=self.connection).update(
                client_secret_encrypted='new-synthetic-secret')
            return value
        self.verify.side_effect = rotated
        result = self.execute()
        self.assertEqual(result['code'], 'source_pin_binding_changed')
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.published_revision, 15)
        self.assertFalse(AvitoServicesPublication.objects.exists())

    def test_ledger_callback_failure_rolls_back_file_history_and_binding_together(self):
        self.prepare()
        original = pin._Approval.record_locked
        def failure(approval, *args):
            original(approval, *args)
            raise pin.SourcePinError('source_pin_ledger_invalid')
        with patch.object(pin._Approval, 'record_locked', new=failure):
            result = self.execute()
        self.assertEqual(result['code'], 'source_pin_ledger_invalid')
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.published_revision, 15)
        self.assertEqual(self.feed.published, self.original)
        self.assertFalse(self.feed.versions.filter(action='publish').exists())
        self.assertFalse(self.feed.versions.filter(action='legacy_published').exists())
        self.assertFalse(AvitoServicesPublication.objects.exists())
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.settings[pin.KEY]['version_id'])

    def test_http_failure_does_not_erase_known_commit_or_cause_another_publication(self):
        self.prepare()
        self.http.side_effect = OSError('PRIVATE HTTP URL')
        first = self.execute()
        second = self.execute()
        self.assertEqual(first['state'], 'published')
        self.assertEqual(second['state'], 'already_published')
        self.assertEqual(first['code'], 'source_pin_http_unconfirmed')
        self.assertFalse(first['publication']['http_matches'])
        self.assertTrue(first['publication']['facts']['file_published'])
        self.assertEqual(self.verify.call_count, 1)
        self.assertEqual(AvitoServicesPublication.objects.count(), 1)
        self.assertNotIn('PRIVATE', json.dumps(first))

    def test_existing_valid_binding_needs_no_new_publication_or_photo_operation(self):
        version = AvitoServicesFeedVersion.objects.create(feed=self.feed, revision=15, published_revision=15,
            action='publish', snapshot=self.feed.published, file_digest=pin._hash(xml_bytes(self.feed.published)))
        bind_publication(self.feed, version)
        with patch.object(pin, '_photo_before_publication') as photo, \
                patch('pool_service.avito_services_feed.verify_inventory') as verify, \
                patch('pool_service.avito_input_copy._download_url', return_value=xml_bytes(self.feed.published)):
            result = self.execute()
        self.assertEqual(result['state'], 'already_bound')
        self.assertIsNone(result['photo_before_publication'])
        self.assertIsNone(result['all_ids_active'])
        self.assertEqual(AvitoServicesPublication.objects.count(), 1)
        photo.assert_not_called()
        verify.assert_not_called()

    def test_completed_r2_reconciles_before_r1_published15_context_even_with_new_pending_draft(self):
        self.prepare()
        self.assertEqual(self.execute()['state'], 'published')
        pending = deepcopy(self.feed.draft)
        pending[0]['rows'][0]['Title'] = 'New pending edit after source publication'
        AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(
            draft=pending, revision=self.photo['revision_after'] + 1)
        with patch('pool_service.avito_services_release_operation._context',
                   side_effect=AssertionError('r1 published15 scope must not run')):
            result = self.execute()
        self.assertEqual(result['state'], 'already_published')
        self.assertEqual(result['photo_before_publication']['revision_after'], self.photo['revision_after'])
        self.assertFalse(result['publication']['draft_matches_published'])
        self.assertEqual(self.verify.call_count, 1)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, pending)

    def test_same_generation_busy_lease_never_starts_another_provider_scan(self):
        self.prepare()
        pin._reserve('a' * 40, self.photo)
        result = self.execute()
        self.assertEqual(result['state'], 'busy')
        self.photo_check.assert_not_called()
        self.verify.assert_not_called()
        self.assertFalse(AvitoServicesPublication.objects.exists())

    def test_protected_command_requires_env_checkout_and_both_actual_markers(self):
        command = 'pool_service.management.commands.run_avito_services_source_pin'
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(CommandError):
                call_command('run_avito_services_source_pin', expected_sha='a' * 40,
                    release_generation=pin.GENERATION, stdout=io.StringIO())
        with patch.dict('os.environ', {'SERVICE2_AVITO_RELEASE_OPERATOR': '1'}), \
                patch(command + '.verify_checkout'), \
                patch(command + '.run_source_pin', return_value=pin._result()) as run:
            with patch(command + '.subprocess.run', return_value=SimpleNamespace(stdout=pin.R1_MARKER)):
                with self.assertRaises(CommandError):
                    call_command('run_avito_services_source_pin', expected_sha='a' * 40,
                        release_generation=pin.GENERATION, stdout=io.StringIO())
            run.assert_not_called()
            output = io.StringIO()
            with patch(command + '.subprocess.run',
                       return_value=SimpleNamespace(stdout=pin.R1_MARKER + '\n' + pin.R2_MARKER)):
                call_command('run_avito_services_source_pin', expected_sha='a' * 40,
                    release_generation=pin.GENERATION, stdout=output)
            run.assert_called_once_with('a' * 40, pin.GENERATION)
            self.assertTrue(output.getvalue().startswith('AVITO_SERVICES_SOURCE_PIN_RECEIPT '))
