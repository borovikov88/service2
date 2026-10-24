"""Producer/consumer integration: real source, signed handler and editor logic.

Only external API/account reads and the network/socket boundary are substituted;
no photo-source helper, image decoder, token signer, handler or editor is faked.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from pool_service import avito_services_release_operation as operation
from pool_service import avito_services_source_pin as source_pin
from pool_service import avito_services_media as media
from pool_service import avito_services_media_livecheck as live
from pool_service import avito_services_workplace_setup as setup
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.avito_media_models import AvitoServicesImage
from pool_service.avito_media_storage import AvitoImageStorage
from pool_service.avito_services_feed import parse_export
from pool_service.communication_models import (
    CommunicationChannel, ChannelConnection, AvitoCredential, CommunicationAccess)
from pool_service.models import Organization, OrganizationAccess
from .test_avito_services_feed import export_bytes
from .test_avito_services_media import picture


class NetworkResponse:
    def __init__(self, raw=b'', status=200, headers=None):
        self.raw, self.status, self.offset = raw, status, 0
        self.headers = dict(headers or {})
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def getheaders(self):
        return list(self.headers.items())
    def read1(self, size):
        chunk = self.raw[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk
    def read(self, size):
        return self.read1(size)
    def close(self):
        pass


class NetworkConnection:
    def __init__(self, test, host, port, address, timeout):
        self.test, self.host, self.port = test, host, port
        self.sock = MagicMock()
        self.response = None
        self.auto_open = 0
        test.connections.append((host, port, address, timeout))
    def connect(self):
        pass
    def request(self, method, path, body=None, headers=None):
        self.test.events.append((method, self.host))
        if self.host == 'service2.aqualine22.ru':
            if method == 'GET':
                response = self.test.web.get(path)
                self.response = NetworkResponse(response.content, response.status_code, headers={
                    'Content-Type': 'application/xml', 'Content-Length': str(len(response.content))})
                return
            self.test.operator_attempts += 1
            # The first actual operator POST must follow a decoded real source
            # GET through the exact same-host HTTPS upgrade.
            self.test.assertEqual(self.test.image_gets, 2)
            self.test.assertEqual(AvitoServicesImage.objects.count(), 0)
            if self.test.timeout_before_dispatch:
                raise TimeoutError('private substituted network')
            self.test.handler_posts += 1
            response = self.test.web.post(path, body, content_type='application/json')
            self.response = NetworkResponse(response.content, response.status_code)
            return
        self.test.image_gets += 1
        if self.port == 80:
            self.response = NetworkResponse(status=301, headers={
                'Content-Type': 'text/html', 'Location': self.test.location})
        else:
            raw = self.test.image_bytes
            self.response = NetworkResponse(raw, headers={
                'Content-Type': 'image/jpeg', 'Content-Length': str(len(raw))})
    def getresponse(self):
        if self.host == 'service2.aqualine22.ru' and self.test.timeout_after_dispatch:
            raise TimeoutError('private lost response after durable commit')
        return self.response
    def close(self):
        pass


@override_settings(ALLOWED_HOSTS=['testserver', 'service2.aqualine22.ru'],
                   AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class RealReleaseProducerIntegrationTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = override_settings(AVITO_SERVICES_MEDIA_ROOT=temporary.name)
        override.enable()
        self.addCleanup(override.disable)
        self.root = Path(temporary.name)
        storage = patch.object(AvitoServicesImage._meta.get_field('file'), 'storage', AvitoImageStorage())
        storage.start()
        self.addCleanup(storage.stop)
        org = Organization.objects.create(name='Real producer integration')
        owner = User.objects.create_user('real-release-integration')
        OrganizationAccess.objects.create(organization=org, user=owner, role='owner')
        CommunicationAccess.objects.update_or_create(organization=org, user=owner,
            defaults={'can_manage_channels': True})
        channel = CommunicationChannel.objects.create(organization=org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel, external_id='446312402')
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted='synthetic-id', client_secret_encrypted='synthetic-secret')
        self.connection.settings = {setup.KEY: {
            **setup._binding(self.connection), 'actor_id': owner.pk,
            'automatic_reports_configured_once': True}}
        self.connection.save(update_fields=['settings'])
        data = parse_export(export_bytes())
        self.original_url = 'http://photos.invalid/photo.jpg?a=1'
        data[0]['rows'][0]['ImageUrls'] = self.original_url
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection,
            account_id='446312402', organization_id_at_import=org.pk, draft=deepcopy(data),
            published=deepcopy(data), revision=15, published_revision=15,
            published_photo_limit=10, enabled=True)
        self.original = deepcopy(data)
        self.location = 'https://photos.invalid/photo.jpg?a=1'
        self.image_bytes = picture(format='JPEG')
        self.web = Client(enforce_csrf_checks=True)
        self.events, self.connections = [], []
        self.image_gets = self.operator_attempts = self.handler_posts = 0
        self.timeout_before_dispatch = self.timeout_after_dispatch = False
        # Observe actual preflight + signed handler with only the socket boundary
        # substituted. Real media_release_source is never patched.
        for target, kwargs in (
            ('pool_service.avito_services_release_operation.authorized_account_id',
             {'return_value': '446312402'}),
            ('pool_service.avito_services_media._resolve', {'return_value': '93.184.216.34'}),
            ('pool_service.avito_services_media._PinnedHTTP',
             {'side_effect': lambda *args: NetworkConnection(self, *args)}),
            ('pool_service.avito_services_media._PinnedHTTPS',
             {'side_effect': lambda *args: NetworkConnection(self, *args)}),
            ('pool_service.avito_services_media.threading.Timer', {}),
        ):
            substitution = patch(target, **kwargs)
            substitution.start()
            self.addCleanup(substitution.stop)

    def seed_prior_failed_before_token(self):
        current, actor, binding, stamp, lease, marker = operation._reserve('a' * 40)
        current.refresh_from_db()
        metadata = dict(current.settings)
        metadata[operation.KEY] = dict(metadata[operation.KEY],
            phase='failed', operation_id=None, lease_until=None,
            public_receipt=operation._result('failed', 'release_internal_error'))
        current.settings = metadata
        current.save(update_fields=['settings'])
        self.assertFalse(operation.release_operation_diagnostics(current)['has_operation_id'])

    def run_operation(self):
        return operation.run_release_operation('b' * 40, operation.GENERATION)

    def test_real_upgraded_source_then_signed_handler_saves_and_restores_once(self):
        self.seed_prior_failed_before_token()
        signer = live.make_media_livecheck_token
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token:
            first = self.run_operation()
            second = self.run_operation()
        self.assertEqual(first['state'], 'completed')
        self.assertEqual(second['state'], 'completed')
        self.assertTrue(first['photo_source']['https_upgrade_followed'])
        self.assertTrue(first['photo_proof']['source']['https_upgrade_followed'])
        self.assertEqual(token.call_count, 1)
        self.assertEqual(self.operator_attempts, 1)
        self.assertEqual(self.handler_posts, 1)
        self.assertEqual(self.image_gets, 4)  # preflight and handler, each HTTP→HTTPS
        self.assertEqual(self.events[:3], [('GET', 'photos.invalid'), ('GET', 'photos.invalid'),
                                          ('POST', 'service2.aqualine22.ru')])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, self.original)
        self.assertEqual(self.feed.published, self.original)
        self.assertEqual(self.feed.revision, 17)
        self.assertEqual(self.feed.published_revision, 15)
        self.assertEqual(self.feed.draft[0]['rows'][0]['ImageUrls'], self.original_url)
        self.assertEqual(self.feed.versions.filter(action='edit').count(), 2)
        self.assertFalse(self.feed.versions.filter(action='publish').exists())
        self.assertEqual(AvitoServicesImage.objects.count(), 1)
        asset = AvitoServicesImage.objects.get()
        self.assertIsNone(asset.published_in_feed_at)
        self.assertFalse(first['photo_proof']['image_publicly_available'])
        self.assertFalse(first['photo_proof']['browser_clicks_verified'])
        self.assertFalse(first['photo_proof']['provider_content_verified'])
        self.assertNotIn(self.original_url, json.dumps(first))

    def test_unsupported_actual_location_blocks_before_token_or_post(self):
        self.seed_prior_failed_before_token()
        self.location = 'https://other.invalid/photo.jpg?a=1'
        signer = live.make_media_livecheck_token
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token:
            result = self.run_operation()
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['photo_source'])
        token.assert_not_called()
        self.assertEqual(self.operator_attempts, 0)
        self.assertEqual(self.handler_posts, 0)
        self.assertEqual(self.image_gets, 1)
        self.assertFalse(operation.release_operation_diagnostics(self.connection)['has_operation_id'])
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, self.original)
        self.assertEqual(self.feed.published, self.original)
        self.assertFalse(self.feed.versions.exists())

    def test_decode_failure_after_allowed_upgrade_still_blocks_before_token(self):
        self.image_bytes = b'<html>not an actual photo</html>'
        signer = live.make_media_livecheck_token
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token:
            result = self.run_operation()
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['photo_source'])
        self.assertEqual(self.image_gets, 2)
        token.assert_not_called()
        self.assertEqual(self.operator_attempts, 0)
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
        self.assertFalse(operation.release_operation_diagnostics(self.connection)['pending_http'])

    def test_pending_unknown_transport_is_reconciled_without_new_source_token_or_post(self):
        self.timeout_before_dispatch = True
        signer = live.make_media_livecheck_token
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token:
            first = self.run_operation()
            second = self.run_operation()
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(second['state'], 'unknown')
        self.assertTrue(first['photo_source']['https_upgrade_followed'])
        self.assertEqual(token.call_count, 1)
        self.assertEqual(self.operator_attempts, 1)
        self.assertEqual(self.handler_posts, 0)
        self.assertEqual(self.image_gets, 2)
        self.assertTrue(operation.release_operation_diagnostics(self.connection)['pending_http'])
        self.assertFalse(self.feed.versions.exists())
        self.assertEqual(AvitoServicesImage.objects.count(), 0)

    def test_lost_response_after_real_handler_commit_uses_exact_receipt_without_repost(self):
        self.timeout_after_dispatch = True
        signer = live.make_media_livecheck_token
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token:
            first = self.run_operation()
            second = self.run_operation()
        self.assertEqual(first['state'], 'completed')
        self.assertEqual(second['state'], 'completed')
        self.assertEqual(token.call_count, 1)
        self.assertEqual(self.operator_attempts, 1)
        self.assertEqual(self.handler_posts, 1)
        self.assertTrue(operation.release_operation_diagnostics(self.connection)['has_exact_receipt'])
        self.assertEqual(self.feed.versions.filter(action='edit').count(), 2)


    def test_r2_resumes_failed_before_token_then_uses_real_r1_receipt_and_normal_publish(self):
        self.seed_prior_failed_before_token()
        def provider_inventory(connection, sheets):
            from pool_service.avito_services_feed import entries
            from django.utils import timezone
            return {'sheets': deepcopy(sheets), 'observations': [
                {'id': row['Id'], 'avito_id': row['AvitoId'], 'status': 'active', 'included': True,
                 'checked_at': timezone.now().isoformat(), 'finish_time': None,
                 'finish_time_source': '', 'source': 'avito_item_detail'}
                for row in entries(sheets)]}
        signer = live.make_media_livecheck_token
        # Only this provider response is substituted; source readiness, media
        # availability, signed handler and ordinary publication remain real.
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token, \
                patch('pool_service.avito_services_feed.verify_inventory',
                      side_effect=provider_inventory) as inventory:
            first = source_pin.run_source_pin('b' * 40, source_pin.GENERATION)
            second = source_pin.run_source_pin('b' * 40, source_pin.GENERATION)
        self.assertEqual(first['state'], 'published')
        self.assertEqual(second['state'], 'already_published')
        self.assertTrue(first['photo_before_publication']['source']['https_upgrade_followed'])
        self.assertTrue(first['publication']['http_matches'])
        self.assertFalse(first['publication']['facts']['source_confirmed'])
        self.assertFalse(first['publication']['facts']['live_content_verified'])
        self.assertEqual(token.call_count, 1)
        self.assertEqual(inventory.call_count, 1)
        self.assertEqual(self.operator_attempts, 1)
        self.assertEqual(self.handler_posts, 1)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, self.original)
        self.assertEqual(self.feed.published, self.original)
        self.assertEqual(self.feed.published_revision, 17)
        self.assertEqual(self.feed.versions.filter(action='publish').count(), 1)
        self.assertIsNone(AvitoServicesImage.objects.get().published_in_feed_at)

    def test_r2_pending_unknown_r1_never_mints_another_token_or_reposts(self):
        self.timeout_before_dispatch = True
        signer = live.make_media_livecheck_token
        with patch.object(live, 'make_media_livecheck_token', wraps=signer) as token:
            first = self.run_operation()
            result = source_pin.run_source_pin('b' * 40, source_pin.GENERATION)
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['code'], 'source_pin_photo_required')
        self.assertEqual(token.call_count, 1)
        self.assertEqual(self.operator_attempts, 1)
        self.assertEqual(self.handler_posts, 0)
        self.assertEqual(self.image_gets, 2)
        self.assertFalse(self.feed.versions.filter(action='publish').exists())
        self.assertEqual(AvitoServicesImage.objects.count(), 0)
