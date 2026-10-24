"""Real HTTP entry-point proof with safe substituted existing-photo transport.

These fixtures are not evidence that the live account or browser was exercised.
"""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import signing
from django.core.signals import request_finished
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from pool_service import avito_services_media as media
from pool_service import avito_services_media_livecheck as live
from pool_service import avito_services_workplace_setup as setup
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.avito_media_models import AvitoServicesImage
from pool_service.avito_media_storage import AvitoImageStorage
from pool_service.avito_services_feed import parse_export
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from .test_avito_services_feed import export_bytes
from .test_avito_services_media import picture


class LiveMediaCheckTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        override = override_settings(AVITO_SERVICES_MEDIA_ROOT=self.temp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.storage = AvitoImageStorage()
        storage_patch = patch.object(AvitoServicesImage._meta.get_field('file'), 'storage', self.storage)
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.org = Organization.objects.create(name='Live media fixture organization')
        self.owner = User.objects.create_user('live-media-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel,
            external_id='446312402', name='Fixture services',
            settings={setup.KEY: {'automatic_reports_configured_once': True}})
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection,
            account_id='446312402', organization_id_at_import=self.org.pk,
            draft=parse_export(export_bytes()), enabled=True)
        self.feed.draft[0]['rows'][0]['ImageUrls'] = 'https://example.org/first.jpg | https://example.org/second.jpg'
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.published_revision = self.feed.revision
        self.feed.save()
        self.actor_patch = patch.object(setup, '_actor', return_value=self.owner)
        self.actor_patch.start()
        self.addCleanup(self.actor_patch.stop)
        self.binding_patch = patch.object(setup, '_binding', return_value={
            'account_id': '446312402', 'channel_id': channel.pk,
            'organization_id': self.org.pk, 'credential_stamp': 'fixed-test-credential'})
        self.binding_patch.start()
        self.addCleanup(self.binding_patch.stop)
        self.url = reverse('avito_services_media_livecheck')
        self.client = Client(enforce_csrf_checks=True)

    def token(self):
        return live.make_media_livecheck_token(self.connection, self.owner, '111')

    def post(self, token):
        return self.client.post(self.url, json.dumps({'token': token}), content_type='application/json')

    def state(self):
        self.feed.refresh_from_db()
        return (deepcopy(self.feed.draft), deepcopy(self.feed.published), self.feed.revision,
                list(self.feed.versions.values_list('pk', flat=True)))

    def test_existing_real_photo_path_saves_cover_order_and_restores_original_without_publication(self):
        before, published = deepcopy(self.feed.draft), deepcopy(self.feed.published)
        snapshots, save = [], live._save
        def capture(*args):
            version = save(*args)
            snapshots.append(deepcopy(args[2].draft))
            return version
        with patch.object(media, '_download', return_value=picture()) as download, \
                patch.object(media, 'services_image_upload', wraps=media.services_image_upload) as upload, \
                patch.object(media, 'services_image_preview', wraps=media.services_image_preview) as preview, \
                patch.object(live, '_save', side_effect=capture):
            response = self.post(self.token())
        self.assertEqual(response.status_code, 200)
        evidence = response.json()
        self.assertTrue(evidence['upload_saved'])
        self.assertTrue(evidence['private_preview_verified'])
        self.assertTrue(evidence['cover_order_saved'])
        self.assertTrue(evidence['original_draft_restored'])
        self.assertTrue(evidence['published_unchanged'])
        self.assertFalse(evidence['image_publicly_available'])
        self.assertFalse(evidence['browser_clicks_verified'])
        self.assertFalse(evidence['provider_content_verified'])
        self.assertEqual(evidence['before_draft_sha256'], evidence['after_draft_sha256'])
        self.assertTrue(evidence['source']['source_in_published'])
        self.assertEqual(download.call_args.args[0], 'https://example.org/first.jpg')
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(preview.call_count, 1)
        asset = AvitoServicesImage.objects.get()
        expected = deepcopy(before)
        expected[0]['rows'][0]['ImageUrls'] = asset.public_url + ' | ' + before[0]['rows'][0]['ImageUrls']
        self.assertEqual(snapshots, [expected, before])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, before)
        self.assertEqual(self.feed.published, published)
        self.assertEqual(self.feed.revision, 3)
        edits = list(self.feed.versions.filter(action='edit').order_by('pk'))
        self.assertEqual(len(edits), 2)
        for version in edits:
            self.assertEqual({change['field'] for change in version.changes}, {'ImageUrls'})
        self.assertFalse(self.feed.versions.filter(action='publish').exists())
        self.assertIsNone(asset.published_in_feed_at)
        self.assertEqual(self.client.get(reverse('avito_services_image_public', args=[asset.public_key])).status_code, 404)

    def test_internal_preview_closes_only_file_without_finishing_the_outer_request(self):
        token = self.token()
        payload = signing.loads(token, salt=live.SALT, max_age=120)
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        finished = []
        def listener(sender, **kwargs):
            finished.append(sender)
        request_finished.connect(listener, weak=False)
        self.addCleanup(request_finished.disconnect, listener)
        streams, original_preview = [], media.services_image_preview
        def capture(*args):
            response = original_preview(*args)
            streams.append(response.file_to_stream)
            return response
        with patch.object(media, '_download', return_value=picture()), \
                patch.object(media, 'services_image_preview', side_effect=capture) as preview:
            result = live._run(payload, [], operation_id)
        self.assertTrue(result['original_draft_restored'])
        self.assertEqual(finished, [])
        self.assertTrue(streams[0].closed)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.revision, 3)
        self.assertEqual(preview.call_count, 1)

    def test_readonly_release_source_has_fixed_allowlist_and_no_writes(self):
        before = self.state()
        with patch.object(media, '_download', return_value=picture()) as download:
            result = live.media_release_source(self.connection)
        self.assertEqual(set(result), {'scope_valid', 'source', 'source_in_published',
            'photo_count', 'readable', 'format', 'width', 'height', 'bytes', 'checked_at',
            'https_upgrade_followed'})
        self.assertTrue(result['source_in_published'])
        self.assertEqual(result['format'], 'PNG')
        self.assertEqual((result['width'], result['height']), (24, 16))
        self.assertEqual(self.state(), before)
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.assertEqual(download.call_count, 1)
        self.feed.published = []
        self.feed.save()
        with patch.object(media, '_download') as download, self.assertRaises(live.LiveMediaCheckError):
            live.media_release_source(self.connection)
        download.assert_not_called()

    def test_unsigned_expired_and_tampered_scope_do_not_download_or_write(self):
        before = self.state()
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.token()
        current = self.token()
        with patch.object(media, '_download') as download:
            for token in ('unsigned', expired, current + 'changed'):
                self.assertEqual(self.post(token).status_code, 403)
            self.assertEqual(self.client.get(self.url).status_code, 405)
            extra = self.client.post(self.url, json.dumps({'token': current,
                'image_url': 'http://127.0.0.1/private'}), content_type='application/json')
            self.assertEqual(extra.status_code, 403)
        download.assert_not_called()
        self.assertEqual(self.state(), before)
        self.assertEqual(AvitoServicesImage.objects.count(), 0)

    def test_binding_revision_account_organization_and_current_permissions_are_rechecked(self):
        token = self.token()
        before = self.state()
        cases = (
            lambda: self.feed.__class__.objects.filter(pk=self.feed.pk).update(revision=2),
            lambda: self.connection.__class__.objects.filter(pk=4).update(external_id='other'),
            lambda: self.feed.__class__.objects.filter(pk=self.feed.pk).update(organization_id_at_import=self.org.pk + 1),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                mutate()
                with patch.object(media, '_download') as download:
                    self.assertEqual(self.post(token).status_code, 409)
                download.assert_not_called()
                self.feed.__class__.objects.filter(pk=self.feed.pk).update(
                    revision=before[2], organization_id_at_import=self.org.pk)
                self.connection.__class__.objects.filter(pk=4).update(external_id='446312402')
        with patch.object(setup, '_binding', return_value={'credential_stamp': 'changed'}), \
                patch.object(media, '_download') as download:
            self.assertEqual(self.post(token).status_code, 409)
            download.assert_not_called()
        access = OrganizationAccess.objects.get(user=self.owner)
        access.role = 'manager'
        access.save()
        with patch.object(media, '_download') as download:
            self.assertEqual(self.post(token).status_code, 409)
            download.assert_not_called()
        self.assertEqual(AvitoServicesImage.objects.count(), 0)

    def test_successful_token_replay_returns_durable_receipt_without_adding_assets_or_history(self):
        token = self.token()
        with patch.object(media, '_download', return_value=picture()):
            self.assertEqual(self.post(token).status_code, 200)
        saved = self.state()
        with patch.object(media, '_download') as download:
            self.assertEqual(self.post(token).status_code, 200)
            download.assert_not_called()
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        receipt = live.media_livecheck_receipt(self.connection, operation_id)
        self.assertEqual(receipt['operation_id'], operation_id)
        self.assertTrue(receipt['original_draft_restored'])
        self.assertEqual(receipt['before_draft_sha256'], receipt['after_draft_sha256'])
        self.assertIsNone(live.media_livecheck_receipt(self.connection, '0' * 64))
        self.assertEqual(self.state(), saved)
        self.assertEqual(AvitoServicesImage.objects.count(), 1)

    def test_timeout_reconciliation_is_exact_current_bound_and_preserves_other_settings(self):
        metadata = dict(self.connection.settings)
        metadata['unrelated_fixture_setting'] = {'preserved': True}
        self.connection.settings = metadata
        self.connection.save(update_fields=['settings'])
        token = self.token()
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        with patch.object(media, '_download', return_value=picture()):
            response = self.post(token)
        self.assertEqual(response.status_code, 200)
        expected = response.json()
        with patch.object(media, '_download') as download:
            # A command whose HTTP response was lost can read this after token expiry.
            with patch('django.core.signing.time.time', return_value=9999999999):
                receipt = live.media_livecheck_receipt(self.connection, operation_id)
            self.assertEqual(receipt, expected)
            download.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings['unrelated_fixture_setting'], {'preserved': True})
        with patch.object(setup, '_binding', return_value={'credential_stamp': 'changed'}):
            self.assertIsNone(live.media_livecheck_receipt(self.connection, operation_id))

    def test_restore_failure_rolls_back_draft_history_and_only_new_files(self):
        before, save = self.state(), live._save
        calls = []
        def fail_restore(*args):
            if calls:
                raise live.LiveMediaCheckError('fixed-failure')
            calls.append(True)
            return save(*args)
        with patch.object(media, '_download', return_value=picture()), \
                patch.object(live, '_save', side_effect=fail_restore):
            self.assertEqual(self.post(self.token()).status_code, 409)
        self.assertEqual(self.state(), before)
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.connection.refresh_from_db()
        self.assertNotIn(live.RECEIPT_KEY, self.connection.settings)
        self.assertEqual(list(Path(self.temp.name).rglob('*.jpg')), [])
        # A later failed proof reusing an existing immutable asset must not delete it.
        with patch.object(media, '_download', return_value=picture()):
            self.assertEqual(self.post(self.token()).status_code, 200)
        saved, asset = self.state(), AvitoServicesImage.objects.get()
        calls.clear()
        with patch.object(media, '_download', return_value=picture()), \
                patch.object(live, '_save', side_effect=fail_restore):
            self.assertEqual(self.post(self.token()).status_code, 409)
        self.assertEqual(self.state(), saved)
        self.assertTrue(self.storage.exists(asset.file.name))
        self.assertEqual(AvitoServicesImage.objects.count(), 1)

    def test_ten_original_photos_never_exceed_ten_and_restore_all_ten(self):
        original = ' | '.join('https://example.org/%s.jpg' % i for i in range(10))
        self.feed.draft[0]['rows'][0]['ImageUrls'] = original
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.save()
        with patch.object(media, '_download', return_value=picture()):
            response = self.post(self.token())
        self.assertEqual(response.status_code, 200)
        for version in self.feed.versions.filter(action='edit'):
            self.assertEqual(len(version.snapshot[0]['rows'][0]['ImageUrls'].split(' | ')), 10)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft[0]['rows'][0]['ImageUrls'], original)

    def test_invalid_format_size_and_private_existing_source_leave_everything_unchanged(self):
        for raw in (b'<html>PRIVATE</html>', picture(format='GIF'), b'x' * (media.MAX_BYTES + 1)):
            with self.subTest(length=len(raw)), patch.object(media, '_download', return_value=raw):
                before = self.state()
                self.assertEqual(self.post(self.token()).status_code, 409)
                self.assertEqual(self.state(), before)
                self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.feed.draft[0]['rows'][0]['ImageUrls'] = 'http://127.0.0.1/private.jpg'
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.save()
        with patch.object(media.socket, 'getaddrinfo') as dns, self.assertRaises(ValueError):
            self.token()
        dns.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).rglob('*.jpg')), [])

    def test_durable_source_metadata_observes_upgrade_from_same_get_without_editing_source(self):
        from . import test_avito_services_media as network
        source = 'http://photos.invalid/photo.jpg?a=1'
        self.feed.draft[0]['rows'][0]['ImageUrls'] = source
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.save()
        before = self.state()
        helper = network.MediaNetworkTests()
        plain, secure = network.MagicMock(), network.MagicMock()
        plain.getresponse.return_value = helper.upgrade_response()
        secure.getresponse.return_value = helper.response()
        with patch.object(media, '_resolve', return_value='93.184.216.34'), \
                patch.object(media, '_PinnedHTTP', return_value=plain), \
                patch.object(media, '_PinnedHTTPS', return_value=secure), \
                patch.object(media.threading, 'Timer'):
            result = live.media_livecheck_source(self.connection, self.owner, '111')
        self.assertTrue(result['readable'])
        self.assertTrue(result['source_in_published'])
        self.assertTrue(result['https_upgrade_followed'])
        self.assertEqual(self.state(), before)
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.assertEqual(plain.request.call_count, 1)
        self.assertEqual(secure.request.call_count, 1)
        self.assertNotIn(source, json.dumps(result))
