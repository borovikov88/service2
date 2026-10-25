"""Safe response fixtures: no production image, provider write or customer message."""
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import hashlib
import tempfile
import uuid
from unittest.mock import patch

from PIL import Image
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.signals import request_finished
from django.db import close_old_connections, connection as database
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_services_approved_photo as operation
from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_media_models import AvitoServicesImage
from pool_service.avito_media_storage import AvitoImageStorage
from pool_service.avito_services_feed import parse_export, entries
from pool_service.avito_services_xml import xml_bytes
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes


REAL_PREVIEW = operation._preview
REAL_PROFILE_CURRENT = operation._profile_current
REAL_CAPTURE_REPORT = operation._capture_report


@override_settings(ALLOWED_HOSTS=['testserver', 'service2.aqualine22.ru'],
    AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class ApprovedPhotoPublicationTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = override_settings(AVITO_SERVICES_MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        storage = AvitoImageStorage()
        # Like Django's test client, a synthetic view call must not close the
        # TestCase's outer transaction when FileResponse.close emits request_finished.
        # All real preview permission, storage, streaming and checksum code still runs.
        def preview_without_test_connection_close(*args):
            connected = request_finished.disconnect(close_old_connections)
            try:
                return REAL_PREVIEW(*args)
            finally:
                if connected:
                    request_finished.connect(close_old_connections)
        self.patch('pool_service.avito_services_approved_photo._preview',
                   side_effect=preview_without_test_connection_close)
        self.patch('pool_service.avito_services_approved_photo._actor', side_effect=lambda c: self.owner)
        field = patch.object(AvitoServicesImage._meta.get_field('file'), 'storage', storage)
        field.start()
        self.addCleanup(field.stop)
        self.patch('pool_service.avito_services_approved_photo.ASSET_KEY_SHA256',
            new=hashlib.sha256(b'00000000-0000-4000-8000-000000000018').hexdigest())
        self.org = Organization.objects.create(name='Approved publication test')
        self.owner = User.objects.create_user('approved-photo-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel,
            external_id=operation.ACCOUNT_ID, name='Services')
        self.goods = ChannelConnection.objects.create(pk=1, channel=channel, external_id='other',
            name='Goods sentinel', settings={'preserve': 'unchanged'})
        identifiers = (operation.TARGET_ID, operation.WATER_ID) + tuple(str(9000000000+i) for i in range(9))
        published = parse_export(export_bytes(identifiers))
        for row in entries(published):
            row['ImageUrls'] = ' | '.join('https://photos.invalid/%s/%s.jpg' % (row['AvitoId'], n)
                                        for n in range(10))
        self.before = deepcopy(published)
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection,
            account_id=operation.ACCOUNT_ID, organization_id_at_import=self.org.pk,
            revision=18, published_revision=15, published_photo_limit=10,
            enabled=True, published=published, draft=deepcopy(published))
        raw = BytesIO()
        Image.new('RGB', (1536, 1152), 'blue').save(raw, format='JPEG')
        self.raw = raw.getvalue()
        self.asset = AvitoServicesImage(connection=self.connection, account_id=operation.ACCOUNT_ID,
            organization_id_at_upload=self.org.pk, public_key=uuid.UUID('00000000-0000-4000-8000-000000000018'),
            sha256=hashlib.sha256(self.raw).hexdigest(), byte_size=len(self.raw),
            width=1536, height=1152, uploaded_by=self.owner)
        self.asset.public_url = 'https://service2.aqualine22.ru' + reverse(
            'avito_services_image_public', args=[self.asset.public_key])
        name = 'avito_services_images/%s/4/00000000000040008000000000000018.jpg' % self.org.pk
        self.asset.file.name = storage.save(name, ContentFile(self.raw))
        self.asset.save()
        target = entries(self.feed.draft)[0]
        target['ImageUrls'] = ' | '.join(target['ImageUrls'].split(' | ')[:9] + [self.asset.public_url])
        self.feed.save()
        AvitoServicesFeedVersion.objects.create(pk=6, feed=self.feed, revision=18,
            published_revision=15, action='edit', actor=self.owner, snapshot=self.feed.draft,
            changes=[{'avito_id': operation.TARGET_ID, 'field': 'ImageUrls'}])
        self.patch('pool_service.avito_services_approved_photo._capture_report', return_value={
            'state': 'not_checked', 'upload_id': None, 'started_at': None,
            'complete': None, 'upload_status': '', 'target': None})
        self.patch('pool_service.avito_services_approved_photo._profile_current', return_value={'matches': True, 'format': 'xml'})
        self.patch('pool_service.avito_services_media.diagnose_snapshot_media', return_value=[])
        self.patch('pool_service.avito_services_feed.validate_snapshot_media',
                   return_value={'retained': []})
        self.inventory = self.patch('pool_service.avito_services_feed.verify_inventory',
            side_effect=self.verified)
        self.patch('pool_service.avito_input_copy._download_url',
            side_effect=lambda *a: xml_bytes(self.feed.draft))
        self.network_image = self.patch('pool_service.avito_services_media._download', return_value=self.raw)

    def patch(self, name, **options):
        value = patch(name, **options)
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def verified(self, connection, sheets):
        return {'sheets': deepcopy(sheets), 'observations': [
            {'avito_id': row['AvitoId'], 'id': row['Id'], 'status': 'active', 'included': True,
             'source': 'avito_item_detail', 'checked_at': timezone.now().isoformat()}
            for row in entries(sheets)]}

    def assert_not_published(self):
        self.feed.refresh_from_db()
        self.asset.refresh_from_db()
        self.assertEqual(self.feed.published_revision, 15)
        self.assertEqual(self.feed.published, self.before)
        self.assertIsNone(self.asset.published_in_feed_at)
        self.assertEqual(self.feed.versions.filter(action='publish').count(), 0)

    def test_exact_change_uses_normal_publisher_atomic_history_and_public_readback_without_duplicate(self):
        public = reverse('avito_services_image_public', args=[self.asset.public_key])
        self.assertEqual(Client().get(public).status_code, 404)
        result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'verified', result)
        self.assertTrue(result['authorized_preview_verified'])
        self.assertTrue(result['asset_storage_verified'])
        self.assertTrue(result['xml_public_verified'])
        self.assertTrue(result['image_public_verified'])
        self.assertFalse(result['facts']['source_confirmed'])
        self.assertFalse(result['facts']['live_content_verified'])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.published, self.feed.draft)
        self.assertEqual(self.feed.published_revision, 18)
        version = self.feed.versions.get(action='publish')
        self.assertEqual(version.source_version_id, 6)
        self.assertEqual(len(version.changes), 1)
        self.assertEqual(Client().get(public).status_code, 200)
        # Exactly one public new-image GET; private prepublication preview was local.
        self.assertEqual(self.network_image.call_count, 1)
        with patch('pool_service.avito_services_feed.publish_services_snapshot') as publish:
            repeated = operation.run_approved_photo_publication('a' * 40)
        publish.assert_not_called()
        self.assertEqual(repeated['version_id'], version.pk)
        self.assertEqual(self.feed.versions.filter(action='publish').count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'unchanged'})

    def test_unapproved_text_price_other_ad_or_cover_change_is_blocked_before_publication(self):
        good = deepcopy(self.feed.draft)
        for index, field, value in ((0, 'Title', 'Unapproved'), (0, 'Price', '9'),
                                    (1, 'Description', 'Unapproved'), (0, 'Id', 'changed')):
            with self.subTest(field=field, index=index):
                candidate = deepcopy(good)
                entries(candidate)[index][field] = value
                self.feed.draft = candidate
                self.feed.save()
                with patch('pool_service.avito_services_feed.publish_services_snapshot') as publish:
                    result = operation.run_approved_photo_publication('a' * 40)
                publish.assert_not_called()
                self.assertEqual(result['state'], 'blocked')
                self.assert_not_published()
        self.feed.draft = deepcopy(good)
        values = entries(self.feed.draft)[0]['ImageUrls'].split(' | ')
        values[0], values[1] = values[1], values[0]
        entries(self.feed.draft)[0]['ImageUrls'] = ' | '.join(values)
        self.feed.save()
        self.assertEqual(operation.run_approved_photo_publication('a' * 40)['state'], 'blocked')
        self.assert_not_published()

    def test_source_history_or_foreign_asset_cannot_be_substituted(self):
        AvitoServicesFeedVersion.objects.filter(pk=6).update(revision=19)
        self.assertEqual(operation.run_approved_photo_publication('a' * 40)['state'], 'blocked')
        self.assert_not_published()
        AvitoServicesFeedVersion.objects.filter(pk=6).update(revision=18)
        AvitoServicesImage.objects.filter(pk=self.asset.pk).update(account_id='foreign')
        self.assertEqual(operation.run_approved_photo_publication('a' * 40)['state'], 'blocked')
        self.assert_not_published()

    def test_wrong_provider_source_blocks_without_changing_profile_or_public_file(self):
        with patch.object(operation, '_profile_current', return_value={'matches': False, 'format': 'unknown'}):
            result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['code'], 'approved_photo_source_not_current_stable')
        self.assert_not_published()

    def test_one_archived_listing_rolls_back_before_publication_even_if_inventory_excludes_it(self):
        def archived(connection, sheets):
            value = self.verified(connection, sheets)
            value['observations'][1].update(status='old', included=False)
            value['sheets'][0]['rows'].pop(1)
            return value
        self.inventory.side_effect = archived
        result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'blocked')
        self.assert_not_published()

    def test_incomplete_reserved_operation_is_not_retaken_or_sent_again(self):
        operation._reserve('a' * 40)
        with patch('pool_service.avito_services_feed.publish_services_snapshot') as publish:
            result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['code'], 'approved_photo_prior_result_unknown')
        publish.assert_not_called()
        self.assert_not_published()

    def test_public_readback_failure_keeps_committed_publication_and_never_republishes(self):
        self.network_image.side_effect = ValueError('SAFE TEST FAILURE')
        result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'published', result)
        self.assertFalse(result['image_public_verified'])
        self.assertTrue(result['facts']['file_published'])
        with patch('pool_service.avito_services_feed.publish_services_snapshot') as publish:
            operation.run_approved_photo_publication('a' * 40)
        publish.assert_not_called()
        self.assertEqual(self.feed.versions.filter(action='publish').count(), 1)

    def test_callback_failure_rolls_back_ledger_history_public_file_and_asset_mark(self):
        real = operation._published
        def broken(marker):
            save = real(marker)
            def callback(*args):
                save(*args)
                raise operation.Blocked('approved_photo_internal_error')
            return callback
        with patch.object(operation, '_published', side_effect=broken):
            result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'blocked')
        self.assert_not_published()

    def test_revoked_actor_or_changed_connection_prevents_the_operation(self):
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])
        self.assertEqual(operation.run_approved_photo_publication('a' * 40)['state'], 'blocked')
        self.assert_not_published()
        self.owner.is_active = True
        self.owner.save(update_fields=['is_active'])
        self.connection.external_id = 'foreign'
        self.connection.save(update_fields=['external_id'])
        self.assertEqual(operation.run_approved_photo_publication('a' * 40)['state'], 'blocked')
        self.assert_not_published()


    def test_exact_enabled_single_stable_provider_source_is_required(self):
        from pool_service.avito_profile_source import profile_data
        expected_url = 'https://service2.aqualine22.ru' + reverse(
            'avito_services_feed_public', args=[self.feed.public_key])
        right = profile_data({'autoload_enabled': True, 'feeds_data': [{'feed_url': expected_url}]})
        wrong = profile_data({'autoload_enabled': True, 'feeds_data': [
            {'feed_url': expected_url + '?different=1'}]})
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_profile_source.fetch_profile_source', return_value=right) as profile:
            self.assertEqual(REAL_PROFILE_CURRENT(self.connection, self.feed), {'matches': True, 'format': 'xml'})
            profile.return_value = wrong
            self.assertEqual(REAL_PROFILE_CURRENT(self.connection, self.feed), {'matches': False, 'format': 'unknown'})


    def test_current_xlsx_source_is_checked_for_same_snapshot_without_source_change(self):
        from pool_service.avito_services_feed import workbook_bytes
        with patch.object(operation, '_profile_current', return_value={'matches': True, 'format': 'xlsx'}), \
             patch('pool_service.avito_input_copy._download_url',
                 side_effect=lambda part, *a: workbook_bytes(self.feed.draft)
                    if part.path.endswith('.xlsx') else xml_bytes(self.feed.draft)):
            result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'verified', result)
        self.assertEqual(result['configured_source_format'], 'xlsx')
        self.assertTrue(result['configured_source_public_verified'])


    def report_for(self, version, delta):
        return {'kind': 'current', 'upload_id': '999', 'complete': True,
            'upload_finished': True, 'upload_status': 'success',
            'started_at': (version.created_at + delta).isoformat(),
            'total': 11, 'source_url_sha256': [],
            'rows': [{'ad_id': row['Id'], 'avito_id': int(row['AvitoId']),
                'avito_status': 'active', 'messages': [{'type': 'warning', 'code': 1218}]}
                for row in entries(version.snapshot)]}

    def test_old_provider_report_is_historical_and_not_attached_to_new_publication(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        report = self.report_for(version, -timedelta(hours=1))
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_autoload_report.fetch_report', return_value=report), \
             patch('pool_service.avito_services_publication.correlate_report') as correlate:
            result = REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
        self.assertEqual(result['state'], 'predates_publication')
        self.assertEqual(result['target']['messages'], [{'type': 'warning', 'code': 1218}])
        correlate.assert_not_called()
        self.assertEqual(version.publication.attempts.count(), 0)

    def test_only_later_complete_report_reaches_existing_correlator_without_source_invention(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        report = self.report_for(version, timedelta(seconds=1))
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_autoload_report.fetch_report', return_value=report):
            result = REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
        self.assertEqual(result['state'], 'later_completed')
        attempt = version.publication.attempts.get()
        self.assertEqual(attempt.upload_id, '999')
        self.assertEqual(len(attempt.rows), 11)
        self.assertFalse(attempt.source_confirmed)
        self.assertFalse(attempt.content_verified)


    def test_public_key_change_in_readback_gap_preserves_commit_without_stale_current_proof(self):
        original = operation._current_publication
        calls = [0]
        def change_after_outside_check(marker):
            value = original(marker)
            calls[0] += 1
            if calls[0] == 2:
                AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(public_key=uuid.uuid4())
            return value
        with patch.object(operation, '_current_publication', side_effect=change_after_outside_check):
            result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'published', result)
        self.assertEqual(result['code'], 'approved_photo_current_state_changed')
        self.assertTrue(result['local_publication_committed'])
        self.assertFalse(result['current_publication_verified'])
        self.assertTrue(result['facts']['file_published'])
        self.assertIsNone(result['xml_public_verified'])
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[operation.KEY]['state'], 'published')
        self.assertEqual(self.feed.versions.filter(action='publish').count(), 1)
        with patch('pool_service.avito_services_feed.publish_services_snapshot') as publish:
            again = operation.run_approved_photo_publication('a' * 40)
        publish.assert_not_called()
        self.assertTrue(again['local_publication_committed'])
        self.assertFalse(again['current_publication_verified'])

    def test_locked_report_revalidation_rejects_snapshot_or_key_change_without_correlation(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.feed.refresh_from_db()
        good_key = self.feed.public_key
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        report = self.report_for(version, timedelta(seconds=1))
        original = operation._base
        changed_snapshot = deepcopy(version.snapshot)
        entries(changed_snapshot)[1]['Title'] = 'Concurrent publication change'
        for update in ({'public_key': uuid.uuid4()}, {'published': changed_snapshot},
                       {'published_revision': 19}, {'enabled': False}):
            with self.subTest(update=tuple(update)):
                def race(*, locked=False):
                    if locked:
                        AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(**update)
                    return original(locked=locked)
                with patch.object(operation, '_base', side_effect=race), \
                     patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
                     patch('pool_service.communication_avito.access_token', return_value='TEST'), \
                     patch('pool_service.avito_autoload_report.fetch_report', return_value=report) as read, \
                     patch('pool_service.avito_services_publication.correlate_report') as correlate:
                    with self.assertRaises(operation.Blocked):
                        REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
                self.assertEqual(read.call_args.kwargs['kind'], 'current')
                correlate.assert_not_called()
                AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(
                    published=version.snapshot, public_key=good_key, published_revision=18, enabled=True)
        self.assertEqual(version.publication.attempts.count(), 0)

    def test_media_failure_keeps_the_specific_nonfirst_listing_photo_and_safe_cause(self):
        from pool_service import avito_services_media as media
        row = entries(self.feed.draft)[1]
        url = row['ImageUrls'].split(' | ')[4]
        error = media.SnapshotMediaError(row, 5, url, media._PhotoTransportError(
            'PRIVATE PROVIDER BODY', reason='dns_private_address', phase='dns'))
        with patch('pool_service.avito_services_feed.validate_snapshot_media', side_effect=error):
            result = operation.run_approved_photo_publication('a' * 40)
        self.assertEqual(result['state'], 'blocked', result)
        self.assertEqual(result['media_failure']['avito_id'], operation.WATER_ID)
        self.assertEqual(result['media_failure']['photo_number'], 5)
        self.assertIn('checked_at', result['media_failure'])
        self.assertNotIn('PRIVATE', str(result))
        self.assertNotIn(url, str(result))
        self.assert_not_published()

    def test_current_processing_report_does_not_confirm_or_correlate_application(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        report = self.report_for(version, timedelta(seconds=1))
        report.update(upload_status='processing', upload_finished=False)
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_autoload_report.fetch_report', return_value=report) as read, \
             patch('pool_service.avito_services_publication.correlate_report') as correlate:
            result = REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
        self.assertEqual(read.call_args.kwargs['kind'], 'current')
        self.assertEqual(result['state'], 'later_unfinished')
        correlate.assert_not_called()
        self.assertEqual(version.publication.attempts.count(), 0)

    def test_later_error_report_keeps_target_error_codes_and_uses_existing_failure_state(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        report = self.report_for(version, timedelta(seconds=1))
        report['upload_status'] = 'error'
        report['rows'][0].update(avito_status='rejected', messages=[{'type': 'error', 'code': 4001}])
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_autoload_report.fetch_report', return_value=report):
            result = REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
        self.assertEqual(result['state'], 'later_completed')
        self.assertEqual(result['target']['status'], 'rejected')
        self.assertEqual(result['target']['messages'], [{'type': 'error', 'code': 4001}])
        attempt = version.publication.attempts.get()
        # The provider reported an error, but its unverified source takes
        # precedence: it must not be labelled this version's failed upload.
        self.assertEqual(attempt.state, 'report_source_unknown')
        self.assertEqual(attempt.upload_status, 'error')

    def test_report_read_failure_has_safe_code_without_provider_body(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_autoload_report.fetch_report', side_effect=ValueError('PRIVATE BODY')):
            result = REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['error_code'], 'provider_report_unavailable')
        self.assertNotIn('PRIVATE', str(result))


    def test_current_unknown_upload_status_stays_null_and_does_not_confirm_application(self):
        operation.run_approved_photo_publication('a' * 40)
        version = self.feed.versions.get(action='publish')
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        report = self.report_for(version, timedelta(seconds=1))
        report.update(upload_status=None, upload_finished=False)
        with patch('pool_service.communication_avito.authorized_account_id', return_value=operation.ACCOUNT_ID), \
             patch('pool_service.communication_avito.access_token', return_value='TEST'), \
             patch('pool_service.avito_autoload_report.fetch_report', return_value=report), \
             patch('pool_service.avito_services_publication.correlate_report') as correlate:
            result = REAL_CAPTURE_REPORT(self.connection, self.feed, version, marker)
        self.assertEqual(result['state'], 'later_unfinished')
        self.assertIsNone(result['upload_status'])
        correlate.assert_not_called()


    def test_manual_private_preview_keeps_the_synthetic_test_transaction_usable(self):
        operation._preview(self.connection, self.owner, self.asset)
        self.assertFalse(database.closed_in_transaction)
        self.assertFalse(database.needs_rollback)
        self.assertEqual(ChannelConnection.objects.get(pk=4).external_id, operation.ACCOUNT_ID)
        self.assertIsNone(AvitoServicesImage.objects.get(pk=self.asset.pk).published_in_feed_at)
