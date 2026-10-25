"""File-input regressions use fixture pixels only, never production photographs.

A passing test is not proof of a real browser, account or approved original.
"""
from copy import deepcopy
from io import BytesIO, StringIO
import hashlib
import json
import os
from unittest.mock import MagicMock, patch

from PIL import Image
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_services_media as media
from pool_service import avito_services_media_livecheck as live
from pool_service import avito_services_release_operation as release
from pool_service.avito_media_models import AvitoServicesImage
from pool_service.communication_models import ChannelConnection
from pool_service.models import OrganizationAccess
from . import test_avito_services_media_livecheck as live_fixtures


class StoredFileInputTests(TestCase):
    def setUp(self):
        # Reuse the fixture setup, not inheritance of its old external-link tests.
        live_fixtures.LiveMediaCheckTests.setUp(self)
        self.assets = []
        for color in ('red', 'blue'):
            raw = BytesIO()
            Image.new('RGB', (24, 16), color).save(raw, format='PNG')
            request = live._request('POST', self.owner, {
                'revision': str(self.feed.revision), 'avito_id': '111',
                'photos': SimpleUploadedFile('fixture.png', raw.getvalue(), content_type='image/png')})
            response = media.services_image_upload(request, 4)
            self.assertEqual(response.status_code, 200)
            asset = media._asset(self.connection, json.loads(response.content)['images'][0]['url'])
            self.assertIsNotNone(asset)
            self.assets.append(asset)
        AvitoServicesImage.objects.filter(pk__in=[asset.pk for asset in self.assets]).update(
            published_in_feed_at=timezone.now())
        self.feed.draft[0]['rows'][0]['ImageUrls'] = ' | '.join(asset.public_url for asset in self.assets)
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.save()
        self.old_external = {'phase': 'failed', 'operation_id': None, 'lease_until': None,
                             'private_fixture_metadata': 'private-external-429-fixture'}
        self.connection.settings = {
            **self.connection.settings, release.KEY: deepcopy(self.old_external),
            'unrelated_fixture_setting': {'preserved': True}}
        self.connection.save(update_fields=['settings'])
        self.file_url = reverse('avito_services_media_filecheck')

    def state(self):
        return live_fixtures.LiveMediaCheckTests.state(self)

    def post(self, token, **extra):
        return self.client.post(self.file_url, json.dumps({'token': token, **extra}),
                                content_type='application/json')

    def reserve(self):
        token, result = live._reserve_file_check(self.connection)
        self.assertIsNotNone(token)
        self.assertIsNone(result)
        return token

    def set_photos(self, values):
        self.feed.refresh_from_db()
        self.feed.draft[0]['rows'][0]['ImageUrls'] = ' | '.join(values)
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.save()

    def test_inventory_is_database_only_closed_and_readonly(self):
        before = self.state()
        self.connection.refresh_from_db()
        settings = deepcopy(self.connection.settings)
        with patch.object(media, '_download') as download, patch.object(media, '_read_asset') as read:
            result = live.local_file_inventory(self.connection)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['operation_state'], 'not_started')
        self.assertEqual((result['asset_count'], result['approved_shared_asset_count'],
                          result['cover_order_candidate_count']), (2, 2, 2))
        self.assertEqual(result['source_kind'], 'stored_normalized_jpeg')
        self.assertFalse(result['raw_original_retained'])
        self.assertFalse(result['external_http_checked'])
        self.assertTrue(result['external_generation_required'])
        download.assert_not_called()
        read.assert_not_called()
        self.assertEqual(self.state(), before)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, settings)
        encoded = json.dumps(result)
        for private in (self.assets[0].public_url, self.assets[0].sha256,
                        'private-external-429-fixture', 'unrelated_fixture_setting'):
            self.assertNotIn(private, encoded)

    def test_real_file_handlers_preview_changed_cover_order_reopen_and_restore(self):
        before, published, revision, _ = self.state()
        snapshots, save = [], live._save
        def capture(*args):
            version = save(*args)
            snapshots.append(deepcopy(args[2].draft))
            return version
        token = self.reserve()
        with patch.object(media, '_download') as download, \
                patch.object(media, 'services_image_upload', wraps=media.services_image_upload) as upload, \
                patch.object(media, 'services_image_preview', wraps=media.services_image_preview) as preview, \
                patch.object(live, '_save', side_effect=capture), \
                patch.object(live, '_reopen_file_editor', wraps=live._reopen_file_editor) as reopen:
            response = self.post(token)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['state'], 'completed')
        for key in ('file_input_processed', 'private_preview_verified', 'cover_order_saved',
                    'editor_reopened', 'original_draft_restored', 'published_unchanged'):
            self.assertIs(result[key], True)
        self.assertIs(type(result['asset_reused']), bool)
        self.assertEqual(result['new_storage_write'], not result['asset_reused'])
        self.assertFalse(result['browser_clicks_verified'])
        self.assertFalse(result['provider_content_verified'])
        self.assertFalse(result['external_http_checked'])
        self.assertTrue(result['external_generation_required'])
        self.assertEqual((upload.call_count, preview.call_count, reopen.call_count), (1, 1, 3))
        request = upload.call_args.args[0]
        self.assertEqual(len(request.FILES.getlist('photos')), 1)
        self.assertFalse(request.POST.get('image_url'))
        download.assert_not_called()
        self.assertEqual(len(snapshots), 3)
        self.assertNotEqual(snapshots[0], before)
        self.assertNotEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[2], before)
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.draft, self.feed.published, self.feed.revision),
                         (before, published, revision + 3))
        self.assertEqual((result['revision_before'], result['revision_after']), (revision, revision + 3))
        edits = list(self.feed.versions.filter(action='edit').order_by('pk'))
        self.assertEqual(len(edits), 3)
        self.assertEqual([item.revision for item in edits], [revision + 1, revision + 2, revision + 3])
        self.assertFalse(self.feed.versions.filter(action='publish').exists())
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[release.KEY], self.old_external)
        self.assertEqual(self.connection.settings['unrelated_fixture_setting'], {'preserved': True})

    def test_signed_replay_reconciles_exact_receipt_without_more_assets_or_history(self):
        token = self.reserve()
        self.assertEqual(self.post(token).status_code, 200)
        saved = self.state()
        count = AvitoServicesImage.objects.count()
        with patch.object(media, '_read_asset') as read, patch.object(media, '_download') as download:
            response = self.post(token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state(), saved)
        self.assertEqual(AvitoServicesImage.objects.count(), count)
        read.assert_not_called()
        download.assert_not_called()
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(live.file_check_receipt(self.connection, operation_id), response.json())
        self.assertIsNone(live.file_check_receipt(self.connection, '0' * 64))

    def test_unattached_or_unpublished_assets_are_not_inferred_to_be_approved(self):
        before = self.state()
        self.set_photos(['https://example.org/first.jpg', 'https://example.org/second.jpg'])
        with patch.object(media, '_read_asset') as read, patch.object(media, '_download') as download:
            result = live.local_file_inventory(self.connection)
            with self.assertRaises(live.FileCheckError):
                live._reserve_file_check(self.connection)
        self.assertEqual(result['code'], 'file_check_needs_owner_original')
        self.assertEqual(result['asset_count'], 2)
        self.assertEqual(result['approved_shared_asset_count'], 0)
        read.assert_not_called()
        download.assert_not_called()
        self.set_photos([asset.public_url for asset in self.assets])
        AvitoServicesImage.objects.filter(connection=self.connection).update(published_in_feed_at=None)
        result = live.local_file_inventory(self.connection)
        self.assertEqual(result['code'], 'file_check_needs_owner_original')
        self.assertEqual(self.feed.revision, before[2])

    def test_one_distinct_approved_photo_cannot_prove_order_changes(self):
        self.set_photos([self.assets[0].public_url])
        before = self.state()
        result = live.local_file_inventory(self.connection)
        self.assertEqual(result['code'], 'file_check_needs_second_photo')
        with self.assertRaises(live.FileCheckError):
            live._reserve_file_check(self.connection)
        self.assertEqual(self.state(), before)

    def test_external_reserved_or_unknown_generation_is_never_taken_over(self):
        for marker in (
            {'phase': 'pending', 'operation_id': 'a' * 64, 'lease_until': None},
            {'phase': 'unknown', 'operation_id': None, 'lease_until': None},
            {'phase': 'failed', 'lease_until': None},
            {'phase': 'failed', 'operation_id': None, 'lease_until': '2099-01-01T00:00:00+00:00'},
        ):
            with self.subTest(marker=marker):
                self.connection.refresh_from_db()
                self.connection.settings[release.KEY] = marker
                self.connection.save(update_fields=['settings'])
                before = self.state()
                with patch.object(media, '_download') as download:
                    result = live.local_file_inventory(self.connection)
                    with self.assertRaises(live.FileCheckError):
                        live._reserve_file_check(self.connection)
                self.assertEqual(result['code'], 'file_check_external_reserved')
                self.assertEqual(self.state(), before)
                self.connection.refresh_from_db()
                self.assertEqual(self.connection.settings[release.KEY], marker)
                self.assertNotIn(live.FILE_KEY, self.connection.settings)
                download.assert_not_called()

    def test_pending_file_generation_is_visible_and_never_mints_a_second_request(self):
        self.reserve()
        with patch.object(media, '_download') as download, patch.object(release, '_post_photo') as post:
            result = live.local_file_inventory(self.connection)
            with self.assertRaises(live.FileCheckError):
                live._reserve_file_check(self.connection)
        self.assertEqual((result['state'], result['operation_state']), ('blocked', 'pending'))
        self.assertEqual(result['code'], 'file_check_result_unknown')
        download.assert_not_called()
        post.assert_not_called()

    def test_timeout_does_not_mint_or_post_another_token(self):
        with patch.object(live, '_authorize_file_operation'), \
                patch.object(release, '_post_photo', side_effect=TimeoutError) as post:
            first = live.run_stored_file_check(self.connection, perform=True, expected_sha='a' * 40)
            second = live.run_stored_file_check(self.connection, perform=True, expected_sha='a' * 40)
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(second['code'], 'file_check_result_unknown')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs, {'endpoint': 'avito_services_media_filecheck'})
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.revision, 1)

    def test_unsigned_expired_or_arbitrary_link_payload_cannot_read_a_source(self):
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.reserve()
        before = self.state()
        with patch.object(media, '_read_asset') as read, patch.object(media, '_download') as download:
            for token in ('unsigned', expired, expired + 'changed'):
                self.assertEqual(self.post(token).status_code, 403)
            self.assertEqual(self.post(expired, image_url='http://127.0.0.1/private').status_code, 403)
            self.assertEqual(self.client.get(self.file_url).status_code, 405)
        read.assert_not_called()
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_revision_account_organization_and_current_actor_are_rechecked(self):
        token = self.reserve()
        before = self.state()
        changes = (
            (self.feed.__class__, self.feed.pk, {'revision': 2}, {'revision': before[2]}),
            (ChannelConnection, 4, {'external_id': 'other-account'}, {'external_id': '446312402'}),
            (self.feed.__class__, self.feed.pk, {'organization_id_at_import': self.org.pk + 1},
             {'organization_id_at_import': self.org.pk}),
        )
        for model, pk, update, reset in changes:
            with self.subTest(update=update):
                model.objects.filter(pk=pk).update(**update)
                with patch.object(media, '_read_asset') as read, patch.object(media, '_download') as download:
                    self.assertEqual(self.post(token).status_code, 409)
                read.assert_not_called()
                download.assert_not_called()
                model.objects.filter(pk=pk).update(**reset)
        access = OrganizationAccess.objects.get(user=self.owner, organization=self.org)
        access.role = 'manager'
        access.save()
        with patch.object(media, '_read_asset') as read:
            self.assertEqual(self.post(token).status_code, 409)
        read.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_source_path_and_digest_corruption_fail_without_fallback_or_draft_changes(self):
        token = self.reserve()
        payload = signing.loads(token, salt=live.FILE_SALT)
        source = AvitoServicesImage.objects.get(pk=payload['asset_id'])
        before = self.state()
        original_name = source.file.name
        AvitoServicesImage.objects.filter(pk=source.pk).update(file='../../outside.jpg')
        with patch.object(media, '_download') as download:
            self.assertEqual(self.post(token).status_code, 409)
        download.assert_not_called()
        AvitoServicesImage.objects.filter(pk=source.pk).update(file=original_name)
        with source.file.storage.open(original_name, 'wb') as target:
            target.write(b'corrupt-fixture')
        with patch.object(media, '_download') as download:
            self.assertEqual(self.post(token).status_code, 409)
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_invalid_image_bytes_are_rejected_even_with_matching_metadata_digest(self):
        raw = b'not-an-image-fixture'
        source = self.assets[1]
        with source.file.storage.open(source.file.name, 'wb') as target:
            target.write(raw)
        AvitoServicesImage.objects.filter(pk=source.pk).update(
            sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
        token = self.reserve()
        before = self.state()
        with patch.object(media, '_download') as download:
            self.assertEqual(self.post(token).status_code, 409)
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_ten_photo_draft_stays_within_limit_during_every_edit_and_is_restored(self):
        values = [asset.public_url for asset in self.assets] + [
            'https://example.org/photo-%s.jpg' % number for number in range(8)]
        self.set_photos(values)
        token = self.reserve()
        with patch.object(media, '_download') as download:
            response = self.post(token)
        self.assertEqual(response.status_code, 200)
        download.assert_not_called()
        for version in self.feed.versions.filter(action='edit'):
            self.assertLessEqual(len(version.snapshot[0]['rows'][0]['ImageUrls'].split(' | ')), 10)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft[0]['rows'][0]['ImageUrls'], ' | '.join(values))
        self.assertEqual(self.feed.published[0]['rows'][0]['ImageUrls'], ' | '.join(values))

    def test_eleven_photo_draft_blocks_before_any_local_or_remote_image_read(self):
        self.set_photos([asset.public_url for asset in self.assets] + [
            'https://example.org/photo-%s.jpg' % number for number in range(9)])
        before = self.state()
        with patch.object(media, '_read_asset') as read, patch.object(media, '_download') as download:
            result = live.local_file_inventory(self.connection)
        self.assertEqual(result['state'], 'blocked')
        read.assert_not_called()
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_refused_local_link_in_existing_photo_list_does_not_create_ssrf(self):
        self.set_photos([self.assets[0].public_url, 'http://127.0.0.1/private.jpg'])
        before = self.state()
        def invoke(token, *, endpoint):
            return self.post(token).json()
        with patch.object(live, '_authorize_file_operation'), \
                patch.object(release, '_post_photo', side_effect=invoke), \
                patch.object(media, '_download') as download:
            result = live.run_stored_file_check(self.connection, perform=True, expected_sha='a' * 40)
        self.assertNotEqual(result['state'], 'completed')
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_pending_unpublished_text_is_preserved_without_becoming_publication(self):
        self.feed.draft[0]['rows'][0]['Title'] = 'Pending fixture title'
        self.feed.save()
        before = self.state()
        token = self.reserve()
        with patch.object(media, '_download') as download:
            response = self.post(token)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['draft_matches_published'])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, before[0])
        self.assertEqual(self.feed.published, before[1])
        self.assertNotEqual(self.feed.draft, self.feed.published)
        self.assertEqual(self.feed.revision, before[2] + 3)
        download.assert_not_called()

    def test_editor_reopen_failure_rolls_back_edits_and_preserves_source_files(self):
        token = self.reserve()
        before = self.state()
        count = AvitoServicesImage.objects.count()
        with patch.object(live, '_reopen_file_editor', side_effect=live.FileCheckError('file_check_failed')):
            self.assertEqual(self.post(token).status_code, 409)
        self.assertEqual(self.state(), before)
        self.assertEqual(AvitoServicesImage.objects.count(), count)
        for asset in self.assets:
            self.assertTrue(media._read_asset(asset))

    def test_completed_receipt_rejects_private_fields_invalid_types_and_changed_history(self):
        token = self.reserve()
        self.assertEqual(self.post(token).status_code, 200)
        operation_id = hashlib.sha256(token.encode()).hexdigest()
        self.connection.refresh_from_db()
        original = deepcopy(self.connection.settings[live.FILE_KEY])
        for mutate in (
            lambda value: value.update(source_url='PRIVATE'),
            lambda value: value.update(code='PRIVATE'),
            lambda value: value.update(file_input_processed='PRIVATE'),
            lambda value: value.update(raw_original_retained=True),
            lambda value: value.update(restore_version_id=True),
        ):
            marker = deepcopy(original)
            mutate(marker['result'])
            self.connection.settings[live.FILE_KEY] = marker
            self.connection.save(update_fields=['settings'])
            self.assertIsNone(live.file_check_receipt(self.connection, operation_id))
        self.connection.settings[live.FILE_KEY] = original
        self.connection.save(update_fields=['settings'])
        self.feed.__class__.objects.filter(pk=self.feed.pk).update(revision=self.feed.revision + 1)
        self.assertIsNone(live.file_check_receipt(self.connection, operation_id))

    def test_operator_activation_requires_protected_release_and_cli_scope_is_explicit(self):
        before = self.state()
        with patch.dict(os.environ, {}, clear=True), patch.object(release, '_post_photo') as post:
            result = live.run_stored_file_check(self.connection, perform=True, expected_sha='a' * 40)
        self.assertEqual(result['code'], 'file_check_release_denied')
        post.assert_not_called()
        self.assertEqual(self.state(), before)
        with self.assertRaises(CommandError):
            call_command('run_avito_services_filecheck', connection_id=1,
                         account_id='446312402', stdout=StringIO())
        output = StringIO()
        with patch.object(media, '_read_asset') as read, patch.object(media, '_download') as download:
            call_command('run_avito_services_filecheck', connection_id=4,
                         account_id='446312402', stdout=output)
        self.assertEqual(json.loads(output.getvalue())['state'], 'ready')
        read.assert_not_called()
        download.assert_not_called()


    def test_shared_operator_transport_retains_default_route_and_only_allows_filecheck_alternative(self):
        client, response = MagicMock(), MagicMock()
        response.status = 200
        response.getheader.return_value = 'identity'
        response.read.return_value = b'{}'
        client.getresponse.return_value = response
        with patch.object(release, 'public_origin', return_value='https://service2.aqualine22.ru'), \
                patch.object(media, '_resolve', return_value='93.184.216.34'), \
                patch.object(media, '_public_ip', return_value=True), \
                patch.object(media, '_PinnedHTTPS', return_value=client), \
                patch.object(release.threading, 'Timer'):
            release._post_photo('fixture-capability')
            self.assertEqual(client.request.call_args.args[:2],
                ('POST', reverse('avito_services_media_livecheck')))
            client.request.reset_mock()
            release._post_photo('fixture-capability', endpoint='avito_services_media_filecheck')
            self.assertEqual(client.request.call_args.args[:2],
                ('POST', reverse('avito_services_media_filecheck')))
        with patch.object(media, '_resolve') as dns, patch.object(media, '_PinnedHTTPS') as socket:
            with self.assertRaises(ValueError):
                release._post_photo('fixture-capability', endpoint='unrelated_route')
        dns.assert_not_called()
        socket.assert_not_called()
