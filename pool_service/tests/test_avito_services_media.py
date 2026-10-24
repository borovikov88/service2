from copy import deepcopy
from io import BytesIO
from pathlib import Path
import os
import tempfile
from unittest.mock import MagicMock, patch

from PIL import Image
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from pool_service import avito_services_media as media
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.avito_media_models import AvitoServicesImage
from pool_service.avito_media_storage import AvitoImageStorage
from pool_service.avito_services_feed import parse_export
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from .test_avito_services_feed import export_bytes


def picture(color='blue', format='PNG', size=(24, 16)):
    output = BytesIO()
    Image.new('RGB', size, color).save(output, format=format)
    return output.getvalue()


class MediaNetworkTests(SimpleTestCase):
    def test_formats_bytes_dimensions_and_metadata_are_validated_before_use(self):
        raw, size = media._image(picture())
        self.assertEqual(size, (24, 16))
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertNotIn('exif', image.info)
        for invalid in (b'<html>PRIVATE</html>', picture(format='GIF'), b'x' * (media.MAX_BYTES + 1),
                        picture(size=(10001, 1))):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                media._image(invalid)

    def test_private_mapped_multicast_site_local_and_mixed_dns_are_rejected(self):
        for host in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1',
                     '::ffff:127.0.0.1', 'ff02::1', 'fec0::1', '0.0.0.0',
                     '2002:7f00:1::', '2002:a00:1::', '64:ff9b::7f00:1', '::7f00:1'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                media._resolve(host, 443, media.time.monotonic() + 2)
        answers = [(2, 1, 6, '', ('93.184.216.34', 443)), (2, 1, 6, '', ('127.0.0.1', 443))]
        with patch.object(media.socket, 'getaddrinfo', return_value=answers), self.assertRaises(ValueError):
            media._resolve('photos.example.invalid', 443, media.time.monotonic() + 2)

    def test_url_credentials_fragments_and_nonstandard_ports_are_rejected(self):
        for url in ('file:///tmp/photo', 'https://user:pass@photos.invalid/a.jpg',
                    'https://photos.invalid:8443/a.jpg', 'https://photos.invalid:0/a.jpg', 'https://photos.invalid/a.jpg#x',
                    'https://photos.invalid/a\n.jpg'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                media._parse_url(url)

    def test_socket_is_pinned_and_tls_uses_original_hostname(self):
        raw = MagicMock()
        with patch.object(media, '_numeric_socket', return_value=raw) as create:
            connection = media._PinnedHTTPS('photos.example.invalid', 443, '93.184.216.34', 2)
            context = MagicMock()
            connection._context = context
            connection.connect()
        create.assert_called_once_with('93.184.216.34', 443, 2)
        context.wrap_socket.assert_called_once_with(raw, server_hostname='photos.example.invalid',
                                                   do_handshake_on_connect=False)
        context.wrap_socket.return_value.do_handshake.assert_called_once()

    def response(self, status=200, content_type='image/jpeg', raw=None):
        response = MagicMock()
        payload = raw or picture(format='JPEG')
        response.status = status
        response.getheader.side_effect = lambda key, default=None: {
            'Content-Type': content_type, 'Content-Length': str(len(payload))}.get(key, default)
        response.read1.side_effect = [payload, b'']
        return response

    def test_redirect_html_and_oversize_response_are_not_accepted(self):
        for response in (self.response(status=302), self.response(content_type='text/html')):
            connection = MagicMock()
            connection.getresponse.return_value = response
            with patch.object(media, '_resolve', return_value='93.184.216.34'), \
                 patch.object(media, '_PinnedHTTPS', return_value=connection), \
                 patch.object(media.threading, 'Timer'), self.assertRaises(ValueError):
                media._download('https://photos.invalid/photo.jpg', media.time.monotonic() + 5)
            connection.close.assert_called()
        with patch.object(media, '_PinnedHTTPS') as connect, self.assertRaises(ValueError):
            media._download('https://127.0.0.1/photo.jpg', media.time.monotonic() + 5)
        connect.assert_not_called()

    def test_a_valid_image_with_an_incomplete_http_body_is_rejected(self):
        payload = picture(format='JPEG')
        response = self.response(raw=payload)
        response.getheader.side_effect = lambda key, default=None: {
            'Content-Type': 'image/jpeg', 'Content-Length': str(len(payload) + 17)}.get(key, default)
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(media, '_resolve', return_value='93.184.216.34'), \
             patch.object(media, '_PinnedHTTPS', return_value=connection), \
             patch.object(media.threading, 'Timer'), self.assertRaises(ValueError):
            media._download('https://photos.invalid/photo.jpg', media.time.monotonic() + 5)

    def test_progressing_slow_reads_still_hit_the_total_deadline(self):
        clock = [0.]
        response = self.response()
        def slow_read(limit):
            clock[0] += 2
            return b'chunk'
        response.read1.side_effect = slow_read
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(media.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(media, '_resolve', return_value='93.184.216.34'), \
             patch.object(media, '_PinnedHTTPS', return_value=connection), \
             patch.object(media.threading, 'Timer'), self.assertRaises(ValueError):
            media._download('https://photos.invalid/photo.jpg', 3)
        self.assertEqual(response.read1.call_count, 2)
        response.read.assert_not_called()

    def test_abort_during_connect_cannot_start_a_request_or_reconnect_after_deadline(self):
        clock, callbacks = [0.], []
        connection = MagicMock()
        connection.sock = None
        connection._pinned_socket = None
        def timer(seconds, callback):
            callbacks.append(callback)
            return MagicMock()
        def connect():
            callbacks[0]()
            clock[0] = 6
        connection.connect.side_effect = connect
        with patch.object(media.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(media, '_resolve', return_value='93.184.216.34'), \
             patch.object(media, '_PinnedHTTPS', return_value=connection), \
             patch.object(media.threading, 'Timer', side_effect=timer), self.assertRaises(ValueError):
            media._download('https://photos.invalid/photo.jpg', 5)
        connection.connect.assert_called_once()
        connection.request.assert_not_called()
        connection.getresponse.assert_not_called()
        self.assertEqual(connection.auto_open, 0)

    def test_deadline_timer_shuts_the_actual_socket_during_header_wait(self):
        connection = MagicMock()
        callbacks = []
        def timer(seconds, callback):
            callbacks.append(callback)
            return MagicMock()
        def headers():
            callbacks[0]()
            raise OSError('PRIVATE')
        connection.getresponse.side_effect = headers
        with patch.object(media, '_resolve', return_value='93.184.216.34'), \
             patch.object(media, '_PinnedHTTPS', return_value=connection), \
             patch.object(media.threading, 'Timer', side_effect=timer), self.assertRaises(ValueError):
            media._download('https://photos.invalid/photo.jpg', media.time.monotonic() + 5)
        connection._pinned_socket.shutdown.assert_called_once_with(media.socket.SHUT_RDWR)

    def download_failure(self, connection):
        with patch.object(media, '_resolve', return_value='93.184.216.34'), \
                patch.object(media, '_PinnedHTTPS', return_value=connection), \
                patch.object(media.threading, 'Timer'), self.assertRaises(ValueError) as caught:
            media._download('https://photos.invalid/PRIVATE.jpg', media.time.monotonic() + 5)
        return caught.exception, media.photo_transport_failure(caught.exception)

    def test_transport_status_mime_and_length_reasons_preserve_status_without_remote_values(self):
        cases = [
            (self.response(status=302), 'http_status', 302),
            (self.response(status=403), 'http_status', 403),
            (self.response(content_type='text/html'), 'content_type', 200),
        ]
        for length, reason in (('PRIVATE', 'content_length_invalid'),
                               (str(media.MAX_BYTES + 1), 'content_length_limit')):
            response = self.response()
            response.getheader.side_effect = lambda key, default=None, length=length: {
                'Content-Type': 'image/jpeg', 'Content-Length': length}.get(key, default)
            cases.append((response, reason, 200))
        for response, reason, status in cases:
            with self.subTest(reason=reason, status=status):
                connection = MagicMock()
                connection.getresponse.return_value = response
                error, value = self.download_failure(connection)
                self.assertEqual(value['reason'], reason)
                self.assertEqual(value['phase'], 'headers')
                self.assertEqual(value['http_status'], status)
                self.assertEqual(value['source_scheme'], 'https')
                self.assertTrue(value['path_ascii'])
                self.assertEqual(value['selected_ip_family'], 'ipv4')
                self.assertNotIn('PRIVATE', str(value))
                self.assertNotIn('photos.invalid', str(value))
                response.read1.assert_not_called()
                connection.request.assert_called_once()
        self.assertEqual(value['declared_bytes'], media.MAX_BYTES + 1)

    def test_transport_incomplete_body_and_io_families_are_distinct(self):
        import ssl
        response = self.response()
        actual = len(picture(format='JPEG'))
        response.getheader.side_effect = lambda key, default=None: {
            'Content-Type': 'image/jpeg', 'Content-Length': str(actual + 1)}.get(key, default)
        connection = MagicMock()
        connection.getresponse.return_value = response
        error, value = self.download_failure(connection)
        self.assertEqual(value['reason'], 'incomplete_body')
        self.assertEqual(value['declared_bytes'], actual + 1)
        self.assertEqual(value['received_bytes'], actual)
        for failure, expected in (
                (TimeoutError('PRIVATE'), 'io_timeout'),
                (ssl.SSLCertVerificationError('PRIVATE'), 'tls_certificate'),
                (ssl.SSLError('PRIVATE'), 'tls_error'),
                (OSError(111, 'PRIVATE'), 'io_error'),
                (media.http.client.BadStatusLine('PRIVATE'), 'http_protocol'),
                (UnicodeEncodeError('ascii', 'я', 0, 1, 'PRIVATE'), 'request_encoding')):
            with self.subTest(reason=expected):
                connection = MagicMock()
                connection.connect.side_effect = failure
                error, value = self.download_failure(connection)
                self.assertEqual(value['reason'], expected)
                self.assertEqual(value['phase'], 'connect')
                self.assertIsNone(value['http_status'])
                self.assertNotIn('PRIVATE', str(value))
                connection.request.assert_not_called()

    def test_transport_dns_metadata_only_contains_public_families_and_count(self):
        diagnostics = {}
        answers = [(2, 1, 6, '', ('93.184.216.34', 443)),
                   (10, 1, 6, '', ('2606:4700:4700::1111', 443))]
        with patch.object(media.socket, 'getaddrinfo', return_value=answers):
            media._resolve('photos.invalid', 443, media.time.monotonic() + 3, diagnostics=diagnostics)
        self.assertEqual(diagnostics, {'dns_family': 'mixed', 'dns_answer_count': 2})
        with patch.object(media.socket, 'getaddrinfo', return_value=answers + [(2, 1, 6, '', ('127.0.0.1', 443))]), \
                self.assertRaises(ValueError) as caught:
            media._resolve('photos.invalid', 443, media.time.monotonic() + 3, diagnostics={})
        self.assertEqual(media.photo_transport_failure(caught.exception)['reason'], 'dns_private_address')
        semaphore = media.threading.BoundedSemaphore(1)
        with patch.object(media, '_DNS_SLOTS', semaphore), \
                patch.object(media.threading, 'Thread') as thread, self.assertRaises(ValueError) as caught:
            thread.return_value.start.side_effect = RuntimeError('PRIVATE')
            media._resolve('photos.invalid', 443, media.time.monotonic() + 3)
        self.assertEqual(media.photo_transport_failure(caught.exception)['reason'], 'dns_unavailable')
        self.assertTrue(semaphore.acquire(blocking=False))
        semaphore.release()

    def test_transport_untyped_error_does_not_export_message_or_attributes(self):
        failure = ValueError('PRIVATE https://secret.invalid')
        failure.reason = 'PRIVATE'
        failure.http_status = 403
        self.assertEqual(media.photo_transport_failure(failure),
            {'reason': None, 'phase': None, 'http_status': None})


class MediaAssetsTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(AVITO_SERVICES_MEDIA_ROOT=self.temp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.storage = AvitoImageStorage()
        self.storage_patch = patch.object(AvitoServicesImage._meta.get_field('file'), 'storage', self.storage)
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.org = Organization.objects.create(name='Synthetic media scope')
        self.owner = User.objects.create_user('media-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Synthetic')
        self.connection = ChannelConnection.objects.create(pk=101, channel=channel, external_id='123', name='Services')
        self.other = ChannelConnection.objects.create(pk=102, channel=channel, external_id='456', name='Other')
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id='123',
            organization_id_at_import=self.org.pk, draft=parse_export(export_bytes()))
        self.client.force_login(self.owner)
        self.upload_url = reverse('avito_services_image_upload', args=[self.connection.pk])

    def upload(self, content=None):
        return self.client.post(self.upload_url, {'revision': self.feed.revision, 'avito_id': '111',
            'photos': SimpleUploadedFile('photo.png', content or picture(), content_type='image/png')})

    def test_upload_is_private_scoped_immutable_and_idempotent(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        asset = AvitoServicesImage.objects.get()
        with self.storage.open(asset.file.name) as stream:
            self.assertEqual(asset.byte_size, len(stream.read()))
        self.assertEqual(os.stat(self.storage.path(asset.file.name)).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(Path(self.storage.path(asset.file.name)).parent).st_mode & 0o777, 0o700)
        self.assertEqual(Client().get(reverse('avito_services_image_public', args=[asset.public_key])).status_code, 404)
        self.assertEqual(self.client.get(response.json()['images'][0]['preview_url']).status_code, 200)
        self.assertEqual(self.client.get(reverse('avito_services_image_preview',
                                               args=[self.other.pk, asset.public_key])).status_code, 404)
        with self.assertRaises(ValueError):
            media.validate_photo_scope(self.other, asset.public_url)
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(AvitoServicesImage.objects.count(), 1)

    def test_public_availability_requires_actual_file_publication_and_survives_later_versions(self):
        self.upload()
        asset = AvitoServicesImage.objects.get()
        candidate = deepcopy(self.feed.draft)
        candidate[0]['rows'][0]['ImageUrls'] = asset.public_url
        with transaction.atomic(), self.assertRaises(ValueError):
            media.mark_snapshot_media_published(self.connection, candidate)
        self.feed.published = candidate
        self.feed.enabled = True
        with transaction.atomic():
            self.feed.save()
            media.mark_snapshot_media_published(self.connection, candidate)
        public = reverse('avito_services_image_public', args=[asset.public_key])
        self.assertEqual(Client().get(public).status_code, 200)
        self.feed.published = deepcopy(self.feed.draft)
        self.feed.save()
        self.assertEqual(Client().get(public).status_code, 200)
        self.connection.external_id = '999'
        self.connection.save(update_fields=['external_id'])
        self.assertEqual(Client().get(public).status_code, 404)

    def test_asset_query_encoding_and_alternate_origin_never_fall_back_to_network(self):
        self.upload()
        asset = AvitoServicesImage.objects.get()
        for url in (asset.public_url + '?x=1', asset.public_url.replace('/api/', '/%61pi/'),
                    asset.public_url.replace('service2.aqualine22.ru', 'photos.invalid'),
                    asset.public_url.replace('photo.jpg', 'photoXjpg')):
            with self.subTest(url=url), patch.object(media, '_download') as get, self.assertRaises(ValueError):
                media.validate_snapshot_media(self.connection, [{'rows': [{'ImageUrls': url}]}])
            get.assert_not_called()

    def test_csrf_revision_scope_and_invalid_uploads_preserve_draft(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.upload_url, {'image_url': 'https://photos.invalid/image.jpg'}).status_code, 403)
        response = self.upload(b'<html>PRIVATE</html>')
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, 'PRIVATE', status_code=400)
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        response = self.client.post(self.upload_url, {'revision': 999, 'avito_id': '111',
            'photos': SimpleUploadedFile('photo.png', picture())})
        self.assertEqual(response.status_code, 409)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.revision, 1)
        self.assertEqual(self.feed.versions.count(), 0)

    def test_ten_limit_before_network_and_uploaded_assets_do_not_self_fetch(self):
        urls = ' | '.join('https://photos.invalid/%s.jpg' % i for i in range(11))
        with patch.object(media, '_download') as get, self.assertRaises(ValueError):
            media.validate_snapshot_media(self.connection, [{'rows': [{'ImageUrls': urls}]}])
        get.assert_not_called()
        self.upload()
        asset = AvitoServicesImage.objects.get()
        with patch.object(media, '_download') as get:
            media.validate_snapshot_media(self.connection, [{'rows': [{'ImageUrls': asset.public_url}]}])
        get.assert_not_called()

    def test_multilisting_validation_checkpoints_progress_and_retries_without_duplicate_downloads(self):
        clock = [0.]
        payload = picture(format='JPEG')
        def download(url, deadline):
            clock[0] += 4
            return payload
        snapshot = [{'rows': [{'ImageUrls': ' | '.join('https://photos.invalid/%s.jpg' % i
                                                     for i in range(start, start + 3))}]
                     } for start in (0, 3)]
        with patch.object(media.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(media, '_download', side_effect=download) as get:
            with self.assertRaises(ValueError):
                media.validate_snapshot_media(self.connection, snapshot, seconds=8)
            with self.assertRaises(ValueError):
                media.validate_snapshot_media(self.connection, snapshot, seconds=8)
            media.validate_snapshot_media(self.connection, snapshot, seconds=8)
        self.assertEqual(get.call_count, 6)
        self.connection.refresh_from_db()
        state = self.connection.settings['avito_services_image_checks']
        self.assertEqual(len(state['checks']), 6)
        self.assertNotIn('https://', str(state))
        self.other.settings = deepcopy(self.connection.settings)
        self.other.save(update_fields=['settings'])
        self.assertEqual(media._read_checks(self.other), {})

    def test_storage_rejects_a_web_checkout_location(self):
        from django.conf import settings
        with override_settings(AVITO_SERVICES_MEDIA_ROOT=str(Path(settings.BASE_DIR) / 'media')):
            with self.assertRaises(ImproperlyConfigured):
                AvitoImageStorage()

    def test_storage_probe_uses_synthetic_bytes_and_cleans_its_file(self):
        result = media.storage_probe()
        self.assertEqual(result, {'outside_checkout': True, 'file_private': True,
            'directory_private': True, 'readable': True, 'synthetic_only': True})
        self.assertEqual(list(Path(self.temp.name).rglob('*.jpg')), [])
        self.assertEqual(AvitoServicesImage.objects.count(), 0)

    def _probe_fixture(self):
        connection = ChannelConnection.objects.create(pk=4, channel=self.connection.channel,
            external_id='446312402', name='Synthetic signed-probe target',
            settings={'avito_services_workplace_v1': {'automatic_reports_configured_once': True}})
        return connection

    def test_signed_probe_runs_in_web_context_and_only_returns_fixed_storage_flags(self):
        import json
        target = self._probe_fixture()
        from pool_service import avito_services_workplace_setup as setup
        binding = [target.channel_id, self.org.pk, '446312402', 'synthetic-fingerprint']
        with patch.object(setup, '_actor', return_value=self.owner), patch.object(setup, '_binding', return_value=binding):
            token = media.make_storage_probe_token(target, self.owner)
            client = Client(enforce_csrf_checks=True)
            response = client.post(reverse('avito_services_media_probe'), json.dumps({'token': token}),
                                   content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'outside_checkout': True, 'file_private': True,
            'directory_private': True, 'readable': True, 'synthetic_only': True})
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.assertEqual(list(Path(self.temp.name).rglob('*.jpg')), [])
        self.assertEqual(Client().get(reverse('avito_services_media_probe')).status_code, 405)

    def test_probe_rejects_unsigned_expired_and_changed_account_without_storage_writes(self):
        import json
        target = self._probe_fixture()
        from pool_service import avito_services_workplace_setup as setup
        binding = [target.channel_id, self.org.pk, '446312402', 'synthetic-fingerprint']
        client = Client(enforce_csrf_checks=True)
        with patch.object(setup, '_actor', return_value=self.owner), patch.object(setup, '_binding', return_value=binding):
            with patch('django.core.signing.time.time', return_value=1):
                expired = media.make_storage_probe_token(target, self.owner)
            current = media.make_storage_probe_token(target, self.owner)
            with patch.object(media, 'storage_probe') as probe:
                for token in ('unsigned', expired):
                    response = client.post(reverse('avito_services_media_probe'), json.dumps({'token': token}),
                                           content_type='application/json')
                    self.assertEqual(response.status_code, 403)
                target.external_id = '999'
                target.save(update_fields=['external_id'])
                response = client.post(reverse('avito_services_media_probe'), json.dumps({'token': current}),
                                       content_type='application/json')
                self.assertEqual(response.status_code, 403)
                probe.assert_not_called()

    def test_probe_requires_configured_setup_and_the_signed_specific_actor(self):
        import json
        target = self._probe_fixture()
        from pool_service import avito_services_workplace_setup as setup
        binding = [target.channel_id, self.org.pk, '446312402', 'synthetic-fingerprint']
        with patch.object(setup, '_actor', return_value=self.owner), patch.object(setup, '_binding', return_value=binding):
            token = media.make_storage_probe_token(target, self.owner)
            other = User.objects.create_user('synthetic-other-actor')
            with patch.object(media, 'storage_probe') as probe:
                with patch.object(setup, '_actor', return_value=other):
                    response = Client().post(reverse('avito_services_media_probe'), json.dumps({'token': token}),
                                             content_type='application/json')
                self.assertEqual(response.status_code, 403)
                target.settings = {'avito_services_workplace_v1': {'automatic_reports_configured_once': False}}
                target.save(update_fields=['settings'])
                response = Client().post(reverse('avito_services_media_probe'), json.dumps({'token': token}),
                                         content_type='application/json')
                self.assertEqual(response.status_code, 403)
                probe.assert_not_called()
