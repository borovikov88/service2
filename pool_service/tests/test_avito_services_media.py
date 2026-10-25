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

    def upgrade_response(self, locations=None, status=301):
        response = self.response(status=status, content_type='text/html')
        response.getheaders.return_value = [('Location', value) for value in
            (locations if locations is not None else ['https://photos.invalid/photo.jpg?a=1'])]
        return response

    def test_single_image_upgrade_rechecks_dns_tls_without_changing_source_or_forwarding_headers(self):
        original = 'http://photos.invalid/photo.jpg?a=1'
        for status in (301, 308):
            with self.subTest(status=status):
                redirect, final = self.upgrade_response(status=status), self.response()
                plain, secure = MagicMock(), MagicMock()
                plain.getresponse.return_value = redirect
                secure.getresponse.return_value = final
                observed = {}
                with patch.object(media, '_resolve', side_effect=['93.184.216.34', '93.184.216.35']) as dns, \
                        patch.object(media, '_PinnedHTTP', return_value=plain) as http, \
                        patch.object(media, '_PinnedHTTPS', return_value=secure) as https, \
                        patch.object(media.threading, 'Timer'):
                    raw = media._download(original, media.time.monotonic() + 10, diagnostics=observed)
                media._image(raw, normalize=False)
                self.assertEqual(original, 'http://photos.invalid/photo.jpg?a=1')
                self.assertEqual(dns.call_count, 2)
                self.assertEqual([call.args[:2] for call in dns.call_args_list],
                                 [('photos.invalid', 80), ('photos.invalid', 443)])
                self.assertEqual(dns.call_args_list[0].args[2], dns.call_args_list[1].args[2])
                self.assertEqual(http.call_args.args[:3], ('photos.invalid', 80, '93.184.216.34'))
                self.assertEqual(https.call_args.args[:3], ('photos.invalid', 443, '93.184.216.35'))
                self.assertTrue(observed['https_upgrade_followed'])
                self.assertEqual(observed['source_scheme'], 'http')
                self.assertEqual(observed['host_relation'], 'same')
                redirect.read1.assert_not_called()
                redirect.close.assert_called_once()
                self.assertEqual(plain.auto_open, 0)
                self.assertEqual(secure.auto_open, 0)
                for client in (plain, secure):
                    args, kwargs = client.request.call_args
                    self.assertEqual(args, ('GET', '/photo.jpg?a=1'))
                    self.assertEqual(set(kwargs['headers']), {'Host', 'Accept', 'User-Agent'})
                    self.assertNotIn('Authorization', kwargs['headers'])
                    self.assertNotIn('Cookie', kwargs['headers'])
                    self.assertNotIn('Referer', kwargs['headers'])

    def test_image_upgrade_rejects_every_location_change_without_second_dns_or_get(self):
        cases = (
            ([], 'upgrade_location_missing'),
            (['https://photos.invalid/photo.jpg?a=1'] * 2, 'upgrade_location_multiple'),
            ([''], 'upgrade_location_invalid'),
            (['https://photos.invalid/photo.jpg?a=1\r\nPRIVATE'], 'upgrade_location_controls'),
            (['https://photos.invalid/photo.jpg?a=1#'], 'upgrade_location_fragment'),
            (['https://user@photos.invalid/photo.jpg?a=1'], 'upgrade_location_userinfo'),
            (['/photo.jpg?a=1'], 'upgrade_target_scheme'),
            (['//photos.invalid/photo.jpg?a=1'], 'upgrade_target_scheme'),
            (['http://photos.invalid/photo.jpg?a=1'], 'upgrade_target_scheme'),
            (['https://photos.invalid:8443/photo.jpg?a=1'], 'upgrade_target_port'),
            (['https://other.invalid/photo.jpg?a=1'], 'upgrade_target_host'),
            (['https://www.evil.invalid/photo.jpg?a=1'], 'upgrade_target_host'),
            (['https://evil-www.photos.invalid/photo.jpg?a=1'], 'upgrade_target_host'),
            (['https://www.photos.invalid.evil.invalid/photo.jpg?a=1'], 'upgrade_target_host'),
            (['https://www.www.photos.invalid/photo.jpg?a=1'], 'upgrade_target_host'),
            (['https://photos.invalid./photo.jpg?a=1'], 'upgrade_target_host'),
            (['https://photos.invalid/another.jpg?a=1'], 'upgrade_target_path'),
            (['https://photos.invalid/photo.jpg?a=2'], 'upgrade_target_query'),
        )
        for locations, reason in cases:
            with self.subTest(reason=reason, locations=locations):
                plain, redirect = MagicMock(), self.upgrade_response(locations)
                plain.getresponse.return_value = redirect
                with patch.object(media, '_resolve', return_value='93.184.216.34') as dns, \
                        patch.object(media, '_PinnedHTTP', return_value=plain), \
                        patch.object(media, '_PinnedHTTPS') as https, \
                        patch.object(media.threading, 'Timer'), self.assertRaises(ValueError) as caught:
                    media._download('http://photos.invalid/photo.jpg?a=1', media.time.monotonic() + 10)
                detail = media.photo_transport_failure(caught.exception)
                self.assertEqual(detail['reason'], reason)
                self.assertEqual(detail['http_status'], 301)
                self.assertFalse(detail['https_upgrade_followed'])
                self.assertNotIn('PRIVATE', str(detail))
                self.assertNotIn('invalid', str({key: value for key, value in detail.items()
                                                if key != 'reason'}))
                self.assertEqual(dns.call_count, 1)
                https.assert_not_called()
                redirect.read1.assert_not_called()

    def test_image_upgrade_rejects_second_hop_and_initial_https_redirect(self):
        plain, secure = MagicMock(), MagicMock()
        plain.getresponse.return_value = self.upgrade_response(['https://www.photos.invalid/photo.jpg?a=1'])
        secure.getresponse.return_value = self.upgrade_response(['https://photos.invalid/photo.jpg?a=1'])
        with patch.object(media, '_resolve', return_value='93.184.216.34') as dns, \
                patch.object(media, '_PinnedHTTP', return_value=plain), \
                patch.object(media, '_PinnedHTTPS', return_value=secure), \
                patch.object(media.threading, 'Timer'), self.assertRaises(ValueError) as caught:
            media._download('http://photos.invalid/photo.jpg?a=1', media.time.monotonic() + 10)
        detail = media.photo_transport_failure(caught.exception)
        self.assertEqual(detail['reason'], 'upgrade_second_hop')
        self.assertTrue(detail['https_upgrade_followed'])
        self.assertEqual(detail['source_scheme'], 'http')
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(plain.request.call_count, 1)
        self.assertEqual(secure.request.call_count, 1)
        secure.getresponse.return_value.read1.assert_not_called()
        error, detail = self.download_failure(secure)
        self.assertEqual(detail['reason'], 'upgrade_source_scheme')
        self.assertFalse(detail['https_upgrade_followed'])

    def test_image_upgrade_rechecks_all_target_dns_answers_and_blocks_private_or_mixed(self):
        public = [(2, 1, 6, '', ('93.184.216.34', 80))]
        private = [(2, 1, 6, '', ('127.0.0.1', 443))]
        for target in (private, public + private):
            with self.subTest(answers=len(target)):
                plain = MagicMock()
                plain.getresponse.return_value = self.upgrade_response()
                with patch.object(media.socket, 'getaddrinfo', side_effect=[public, target]) as dns, \
                        patch.object(media, '_PinnedHTTP', return_value=plain), \
                        patch.object(media, '_PinnedHTTPS') as https, \
                        patch.object(media.threading, 'Timer'), self.assertRaises(ValueError) as caught:
                    media._download('http://photos.invalid/photo.jpg?a=1', media.time.monotonic() + 10)
                detail = media.photo_transport_failure(caught.exception)
                self.assertEqual(detail['reason'], 'dns_private_address')
                self.assertFalse(detail['https_upgrade_followed'])
                self.assertEqual(dns.call_count, 2)
                https.assert_not_called()

    def test_image_upgrade_keeps_original_deadline_and_cannot_send_late_https_get(self):
        clock = [0.]
        plain, secure = MagicMock(), MagicMock()
        redirect = self.upgrade_response()
        def headers():
            clock[0] = 9
            return redirect
        plain.getresponse.side_effect = headers
        secure.connect.side_effect = lambda: clock.__setitem__(0, 11)
        with patch.object(media.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(media, '_resolve', return_value='93.184.216.34') as dns, \
                patch.object(media, '_PinnedHTTP', return_value=plain), \
                patch.object(media, '_PinnedHTTPS', return_value=secure) as https, \
                patch.object(media.threading, 'Timer') as timer, self.assertRaises(ValueError) as caught:
            media._download('http://photos.invalid/photo.jpg?a=1', 10)
        self.assertEqual(media.photo_transport_failure(caught.exception)['reason'], 'deadline')
        self.assertEqual([call.args[2] for call in dns.call_args_list], [10, 10])
        self.assertEqual(https.call_args.args[3], 1)
        self.assertEqual([call.args[0] for call in timer.call_args_list], [10, 1])
        secure.request.assert_not_called()
        redirect.read1.assert_not_called()

    def test_image_upgrade_allows_only_single_www_dns_pair_and_uses_target_host_and_tls_name(self):
        for original_host, target_host in (
                ('photos.invalid', 'www.photos.invalid'),
                ('www.photos.invalid', 'photos.invalid')):
            with self.subTest(original_host=original_host):
                plain, secure = MagicMock(), MagicMock()
                plain.getresponse.return_value = self.upgrade_response([
                    'https://' + target_host + '/photo.jpg?a=1'])
                secure.getresponse.return_value = self.response()
                observed = {}
                with patch.object(media, '_resolve', side_effect=['93.184.216.34', '93.184.216.35']) as dns, \
                        patch.object(media, '_PinnedHTTP', return_value=plain), \
                        patch.object(media, '_PinnedHTTPS', return_value=secure) as tls, \
                        patch.object(media.threading, 'Timer'):
                    raw = media._download('http://' + original_host + '/photo.jpg?a=1',
                        media.time.monotonic() + 10, diagnostics=observed)
                media._image(raw, normalize=False)
                self.assertEqual([call.args[:2] for call in dns.call_args_list],
                    [(original_host, 80), (target_host, 443)])
                self.assertEqual(tls.call_args.args[:3], (target_host, 443, '93.184.216.35'))
                self.assertEqual(secure.request.call_args.kwargs['headers']['Host'], target_host)
                self.assertTrue(observed['https_upgrade_followed'])
                self.assertEqual(observed['host_relation'], 'www_alias')
                self.assertEqual(observed['source_scheme'], 'http')
                plain.getresponse.return_value.read1.assert_not_called()
                self.assertEqual(set(secure.request.call_args.kwargs['headers']), {'Host', 'Accept', 'User-Agent'})
                # The pinned TLS connection validates this exact target name.
                socket = MagicMock()
                with patch.object(media, '_numeric_socket', return_value=socket):
                    connection = media._PinnedHTTPS(target_host, 443, '93.184.216.35', 2)
                    connection._context = MagicMock()
                    connection.connect()
                connection._context.wrap_socket.assert_called_once_with(
                    socket, server_hostname=target_host, do_handshake_on_connect=False)

    def test_www_relation_never_treats_ips_double_www_or_registry_neighbors_as_aliases(self):
        for source, target in (
                ('93.184.216.34', 'www.93.184.216.34'),
                ('0x5db8d822', 'www.0x5db8d822'),
                ('0x5d.0xb8.0xd8.0x22', 'www.0x5d.0xb8.0xd8.0x22'),
                ('2001:4860:4860::8888', 'www.2001:4860:4860::8888'),
                ('www', 'www.www'),
                ('www.photos.invalid', 'www.www.photos.invalid'),
                ('www.www.photos.invalid', 'www.photos.invalid'),
                ('photos.invalid', 'www.photos.invalid.evil.invalid'),
                ('photos.invalid', 'evil-www.photos.invalid'),
                ('photos.invalid.', 'www.photos.invalid.')):
            with self.subTest(source=source, target=target):
                self.assertEqual(media._image_host_relation(source, target), 'other')

    def test_www_upgrade_retains_path_query_port_and_private_target_dns_guards(self):
        cases = (
            ('https://www.photos.invalid/another.jpg?a=1', 'upgrade_target_path'),
            ('https://www.photos.invalid/photo.jpg?a=2', 'upgrade_target_query'),
            ('https://www.photos.invalid:8443/photo.jpg?a=1', 'upgrade_target_port'),
        )
        for target, reason in cases:
            with self.subTest(reason=reason):
                plain = MagicMock()
                plain.getresponse.return_value = self.upgrade_response([target])
                with patch.object(media, '_resolve', return_value='93.184.216.34') as dns, \
                        patch.object(media, '_PinnedHTTP', return_value=plain), \
                        patch.object(media, '_PinnedHTTPS') as tls, \
                        patch.object(media.threading, 'Timer'), self.assertRaises(ValueError) as caught:
                    media._download('http://photos.invalid/photo.jpg?a=1', media.time.monotonic() + 10)
                self.assertEqual(media.photo_transport_failure(caught.exception)['reason'], reason)
                self.assertEqual(dns.call_count, 1)
                tls.assert_not_called()
        plain = MagicMock()
        plain.getresponse.return_value = self.upgrade_response(['https://www.photos.invalid/photo.jpg?a=1'])
        public = [(2, 1, 6, '', ('93.184.216.34', 80))]
        private = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with patch.object(media.socket, 'getaddrinfo', side_effect=[public, private]) as dns, \
                patch.object(media, '_PinnedHTTP', return_value=plain), \
                patch.object(media, '_PinnedHTTPS') as tls, \
                patch.object(media.threading, 'Timer'), self.assertRaises(ValueError) as caught:
            media._download('http://photos.invalid/photo.jpg?a=1', media.time.monotonic() + 10)
        self.assertEqual(media.photo_transport_failure(caught.exception)['reason'], 'dns_private_address')
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(dns.call_args.args[0], 'www.photos.invalid')
        tls.assert_not_called()


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


class PublishedReferenceRetentionTests(TestCase):
    # Reuse only fixture helpers, not inherited tests.
    setUp = MediaAssetsTests.setUp
    upload = MediaAssetsTests.upload

    def services_feed(self):
        self.feed.delete()
        channel = self.connection.channel
        self.connection.delete()
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel,
            external_id='446312402', name='Services scoped retention')
        data = parse_export(export_bytes(('111', '222')))
        for row in data[0]['rows']:
            row['ImageUrls'] = ' | '.join(f'http://photos.example.org/{row["AvitoId"]}/{number}.jpg'
                                         for number in range(1, 11))
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id='446312402',
            organization_id_at_import=self.org.pk, draft=deepcopy(data), published=deepcopy(data),
            revision=18, published_revision=15, enabled=True)
        self.upload_url = reverse('avito_services_image_upload', args=[4])
        return deepcopy(data)

    def observations(self, data):
        from django.utils import timezone
        return [{'id': row['Id'], 'avito_id': row['AvitoId'], 'source': 'avito_item_detail',
                 'status': 'active', 'included': True, 'checked_at': timezone.now().isoformat()}
                for sheet in data for row in sheet['rows']]

    def test_exact_published_positions_preserved_without_external_get_but_not_claimed_available(self):
        data = self.services_feed()
        with patch.object(media, '_resolve', return_value='8.8.8.8') as resolve, patch.object(media, '_download') as get:
            result = media.validate_snapshot_media(self.connection, data,
                                                   verified_observations=self.observations(data))
        get.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(len(result['retained']), 20)
        self.assertEqual(result['local_count'], 0)
        self.assertEqual(result['checked_count'], 0)
        self.assertEqual(result['retained'][0]['code'], 'published_reference_retained')
        self.assertNotIn('http:', str(result))
        self.assertNotIn('available', str(result))

    def test_new_local_tenth_photo_remains_private_and_uses_storage_not_public_http(self):
        data = self.services_feed()
        response = self.upload(picture(format='JPEG'))
        self.assertEqual(response.status_code, 200)
        asset = AvitoServicesImage.objects.get()
        urls = data[0]['rows'][0]['ImageUrls'].split(' | ')
        data[0]['rows'][0]['ImageUrls'] = ' | '.join(urls[:9] + [asset.public_url])
        with patch.object(media, '_resolve', return_value='8.8.8.8'), patch.object(media, '_download') as get:
            result = media.validate_snapshot_media(self.connection, data,
                                                   verified_observations=self.observations(data))
        get.assert_not_called()
        self.assertEqual(result['local_count'], 1)
        self.assertEqual(len(result['retained']), 19)
        self.assertEqual(Client().get(reverse('avito_services_image_public', args=[asset.public_key])).status_code, 404)
        self.assertEqual(self.client.get(response.json()['images'][0]['preview_url']).status_code, 200)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.published_revision, 15)
        self.assertEqual(self.feed.draft[0]['rows'][0]['ImageUrls'], ' | '.join(urls))

    def test_new_cross_ad_reordered_or_query_tampered_reference_requires_strict_get(self):
        original = self.services_feed()
        for kind in ('new', 'other_ad', 'reordered', 'query'):
            data = deepcopy(original)
            urls = data[0]['rows'][0]['ImageUrls'].split(' | ')
            if kind == 'new':
                urls[0] = 'https://photos.example.org/new.jpg'
            elif kind == 'other_ad':
                urls[0] = data[0]['rows'][1]['ImageUrls'].split(' | ')[0]
            elif kind == 'reordered':
                urls[0], urls[1] = urls[1], urls[0]
            else:
                urls[0] += '?changed=1'
            data[0]['rows'][0]['ImageUrls'] = ' | '.join(urls)
            with self.subTest(kind=kind), patch.object(media, '_resolve', return_value='8.8.8.8'), \
                 patch.object(media, '_download', side_effect=media._PhotoTransportError(
                     'fixed', reason='http_status', phase='headers', http_status=403)) as get:
                with self.assertRaises(media.SnapshotMediaError) as caught:
                    media.validate_snapshot_media(self.connection, data,
                                                   verified_observations=self.observations(data))
            self.assertEqual(caught.exception.diagnostic['photo_number'], 1)
            self.assertEqual(caught.exception.diagnostic['avito_id'], '111')
            self.assertEqual(get.call_count, 1)

    def test_no_retention_without_current_complete_owned_active_inventory(self):
        from datetime import timedelta
        from django.utils import timezone
        data = self.services_feed()
        for kind in ('missing', 'stale', 'wrong_id', 'inactive', 'missing_row', 'duplicate'):
            observations = self.observations(data)
            if kind == 'missing':
                observations = None
            elif kind == 'stale':
                observations[0]['checked_at'] = (timezone.now() - timedelta(minutes=3)).isoformat()
            elif kind == 'wrong_id':
                observations[0]['id'] = 'foreign'
            elif kind == 'inactive':
                observations[0].update(status='removed', included=False)
            elif kind == 'missing_row':
                observations.pop()
            else:
                observations.append(deepcopy(observations[0]))
            with self.subTest(kind=kind), patch.object(media, '_resolve') as resolve, \
                 patch.object(media, '_download', side_effect=ValueError('fixed')) as get:
                with self.assertRaises(media.SnapshotMediaError):
                    media.validate_snapshot_media(self.connection, data, verified_observations=observations)
            get.assert_called_once()
            resolve.assert_not_called()

    def test_changed_account_org_disabled_feed_or_other_connection_cannot_reuse_provenance(self):
        data = self.services_feed()
        for kind in ('account', 'organization', 'disabled', 'other'):
            target = self.connection
            self.feed.account_id = '446312402'
            self.feed.organization_id_at_import = self.org.pk
            self.feed.enabled = True
            if kind == 'account':
                self.feed.account_id = 'foreign'
            elif kind == 'organization':
                self.feed.organization_id_at_import = self.org.pk + 1000
            elif kind == 'disabled':
                self.feed.enabled = False
            else:
                target = self.other
            self.feed.save()
            with self.subTest(kind=kind), patch.object(media, '_download', side_effect=ValueError('fixed')) as get:
                with self.assertRaises(media.SnapshotMediaError):
                    media.validate_snapshot_media(target, data, verified_observations=self.observations(data))
            get.assert_called_once()

    def test_retained_url_still_rejects_credentials_private_dns_and_foreign_local_assets(self):
        data = self.services_feed()
        for url in ('http://user:secret@photos.example.org/a.jpg', 'http://127.0.0.1/a.jpg'):
            altered = deepcopy(data)
            altered[0]['rows'][0]['ImageUrls'] = url
            self.feed.published = altered
            self.feed.save()
            with self.subTest(url=url), patch.object(media, '_download') as get, self.assertRaises(ValueError):
                media.validate_snapshot_media(self.connection, altered,
                                               verified_observations=self.observations(altered))
            get.assert_not_called()
        self.feed.published = data
        self.feed.save()
        with patch.object(media.socket, 'getaddrinfo', return_value=[
                (media.socket.AF_INET, media.socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80)),
                (media.socket.AF_INET, media.socket.SOCK_STREAM, 6, '', ('10.0.0.1', 80))]), \
             patch.object(media, '_download') as get, self.assertRaises(media.SnapshotMediaError) as caught:
            media.validate_snapshot_media(self.connection, data, verified_observations=self.observations(data))
        get.assert_not_called()
        self.assertEqual(caught.exception.diagnostic['reason'], 'dns_private_address')
        self.upload()
        asset = AvitoServicesImage.objects.get()
        altered = deepcopy(data)
        altered[0]['rows'][0]['ImageUrls'] = asset.public_url.replace(str(asset.public_key),
                                                                    '00000000-0000-4000-8000-000000000000')
        self.feed.published = altered
        self.feed.save()
        with patch.object(media, '_download') as get, self.assertRaises(ValueError):
            media.validate_snapshot_media(self.connection, altered,
                                           verified_observations=self.observations(altered))
        get.assert_not_called()

    def test_failure_location_distinguishes_http_auth_rate_redirect_and_decode_without_url(self):
        data = self.services_feed()
        for status in (401, 403, 429, 302):
            with self.subTest(status=status), patch.object(media, '_download', side_effect=media._PhotoTransportError(
                    'SECRET REMOTE BODY', reason='http_status', phase='headers', http_status=status)):
                with self.assertRaises(media.SnapshotMediaError) as caught:
                    media.validate_snapshot_media(self.connection, data)
            detail = caught.exception.diagnostic
            self.assertEqual(detail['http_status'], status)
            self.assertEqual(detail['avito_id'], '111')
            self.assertEqual(detail['photo_number'], 1)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertNotIn('photos.example.org', str(caught.exception))
            self.connection.settings = {}
            self.connection.save(update_fields=['settings'])
        with patch.object(media, '_download', return_value=b'<html>SECRET</html>'):
            with self.assertRaises(media.SnapshotMediaError) as caught:
                media.validate_snapshot_media(self.connection, data)
        self.assertEqual(caught.exception.diagnostic['reason'], 'image_invalid')

    def test_negative_403_429_cache_is_scoped_and_probe_does_not_retry_same_hostname(self):
        data = self.services_feed()
        for status in (403, 429):
            with self.subTest(status=status), patch.object(media, '_download', side_effect=media._PhotoTransportError(
                    'fixed', reason='http_status', phase='headers', http_status=status)) as get:
                first = media.diagnose_snapshot_media(self.connection, data, target_ids=['111', '222'])
                second = media.diagnose_snapshot_media(self.connection, data, target_ids=['111', '222'])
            self.assertEqual(get.call_count, 1)
            self.assertEqual(first[0]['http_status'], status)
            self.assertEqual(second[0]['http_status'], status)
            self.assertTrue(second[0]['cached'])
            self.assertEqual(first[1]['reason'], 'not_probed_same_host')
            self.assertIsNone(first[1]['http_status'])
            self.assertNotIn('http:', str(first))
            self.other.settings = deepcopy(ChannelConnection.objects.get(pk=4).settings)
            self.other.save(update_fields=['settings'])
            self.assertEqual(media._read_checks(self.other, failures=True), {})
            self.connection.settings = {}
            self.connection.save(update_fields=['settings'])

    def test_inactive_row_does_not_block_retention_of_independent_active_rows(self):
        data = self.services_feed()
        observations = self.observations(data)
        observations[1].update(status='old', included=False)
        active = deepcopy(data)
        active[0]['rows'] = active[0]['rows'][:1]
        with patch.object(media, '_resolve', return_value='8.8.8.8'), patch.object(media, '_download') as get:
            result = media.validate_snapshot_media(self.connection, active, verified_observations=observations)
        get.assert_not_called()
        self.assertEqual(len(result['retained']), 10)


    def test_diagnostic_duplicate_rows_cannot_expand_two_ad_probe_bound(self):
        data = self.services_feed()
        duplicate = deepcopy(data)
        duplicate[0]['rows'] = [deepcopy(data[0]['rows'][0]) for _ in range(12)]
        with patch.object(media, '_download', return_value=picture(format='JPEG')) as get:
            result = media.diagnose_snapshot_media(self.connection, duplicate, target_ids=['111'])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(result), 1)
