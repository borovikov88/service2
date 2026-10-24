import io
import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.core.management import call_command, CommandError
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from pool_service import avito_services_release_operation as operation
from pool_service import avito_services_workplace_setup as setup
from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_services_feed import parse_export
from pool_service.communication_models import CommunicationChannel, ChannelConnection, AvitoCredential, CommunicationAccess
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes


@override_settings(ALLOWED_HOSTS=['service2.aqualine22.ru'], AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class ServicesReleaseOperationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic release operation')
        self.owner = User.objects.create_user('release-operator-owner')
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role='owner')
        CommunicationAccess.objects.update_or_create(user=self.owner, organization=self.org,
            defaults={'can_manage_channels': True})
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Synthetic services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel, external_id='446312402')
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted='synthetic-id', client_secret_encrypted='synthetic-secret')
        self.connection.settings = {setup.KEY: {**setup._binding(self.connection), 'actor_id': self.owner.pk,
            'automatic_reports_configured_once': True}}
        self.connection.save(update_fields=['settings'])
        data = parse_export(export_bytes(('111', '222')))
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id='446312402',
            organization_id_at_import=self.org.pk, draft=deepcopy(data), published=deepcopy(data),
            revision=17, published_revision=15, enabled=True, published_photo_limit=10)
        goods = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Synthetic goods')
        self.goods = ChannelConnection.objects.create(pk=1, channel=goods, external_id='555', settings={'keep': 'goods'})
        self.original = deepcopy(data)
        self.proof = None
        self.source = {'scope_valid': True, 'source': 'existing_services_draft_image',
            'source_in_published': True, 'photo_count': 1, 'readable': True,
            'format': 'JPEG', 'width': 2, 'height': 2, 'bytes': 100,
            'checked_at': timezone.now().isoformat()}
        self.patches = [
            patch.object(operation, 'authorized_account_id', return_value='446312402'),
            patch('pool_service.avito_services_media_livecheck.media_release_source', return_value=self.source),
            patch('pool_service.avito_services_media_livecheck.make_media_livecheck_token', return_value='synthetic-capability'),
            patch('pool_service.avito_services_media_livecheck.media_livecheck_receipt', side_effect=lambda *args: self.proof),
        ]
        self.account, self.source_read, self.token, self.receipt = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def run_operation(self):
        return operation.run_release_operation('a' * 40, operation.GENERATION)

    def commit_photo(self, token):
        self.connection.refresh_from_db()
        marker = self.connection.settings[operation.KEY]
        self.assertIn(marker['phase'], {'pending', 'unknown'})
        self.assertRegex(marker['operation_id'], r'^[0-9a-f]{64}$')
        self.assertNotIn(token, json.dumps(self.connection.settings))
        self.feed.refresh_from_db()
        before = self.feed.revision
        staged = deepcopy(self.feed.draft)
        staged[0]['rows'][0]['ImageUrls'] = 'https://example.com/another.jpg'
        edit = AvitoServicesFeedVersion.objects.create(feed=self.feed, action='edit',
            revision=before + 1, snapshot=staged)
        restore = AvitoServicesFeedVersion.objects.create(feed=self.feed, action='edit',
            revision=before + 2, snapshot=deepcopy(self.feed.draft))
        self.feed.revision = before + 2
        self.feed.save(update_fields=['revision'])
        self.proof = {'connection_id': 4, 'account_id': '446312402', 'avito_id': marker['avito_id'],
            'operation_id': marker['operation_id'], 'checked_at': timezone.now().isoformat(),
            'source': self.source, 'revision_before': before, 'revision_after': before + 2,
            'edit_version_id': edit.pk, 'restore_version_id': restore.pk,
            'before_draft_sha256': operation._digest(self.feed.draft),
            'after_draft_sha256': operation._digest(self.feed.draft),
            'published_sha256': operation._digest(self.feed.published),
            'source_content_sha256': 'f' * 64,
            **dict.fromkeys(('upload_saved', 'private_preview_verified', 'cover_order_saved',
                'original_draft_restored', 'published_unchanged'), True),
            **dict.fromkeys(('image_publicly_available', 'browser_clicks_verified', 'provider_content_verified'), False)}
        self.connection.refresh_from_db()
        metadata = dict(self.connection.settings)
        metadata['avito_services_media_livecheck_v1'] = {'must_survive': True}
        self.connection.settings = metadata
        self.connection.save(update_fields=['settings'])
        return self.proof

    def test_missing_immutable_binding_does_not_block_real_draft_photo_proof(self):
        with patch.object(operation, '_post_photo', side_effect=self.commit_photo) as post:
            result = self.run_operation()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['code'], 'release_completed')
        self.assertFalse(result['publication_preflight']['binding_present'])
        self.assertEqual(result['publication_preflight']['http_code'], 'publication_missing')
        self.assertTrue(result['photo_proof']['original_draft_restored'])
        self.assertFalse(result['photo_proof']['provider_content_verified'])
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft, self.original)
        self.assertEqual(self.feed.published, self.original)
        self.assertEqual(self.feed.published_revision, 15)
        self.assertEqual(self.feed.revision, 19)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings['avito_services_media_livecheck_v1'], {'must_survive': True})
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'keep': 'goods'})
        encoded = json.dumps(result)
        for private in ('operation_id', 'sha256', 'synthetic-capability', 'https://', 'avito_id'):
            self.assertNotIn(private, encoded)
        post.assert_called_once()

    def test_timeout_after_commit_reconciles_without_another_post(self):
        def timeout(token):
            self.commit_photo(token)
            raise TimeoutError('private body')
        with patch.object(operation, '_post_photo', side_effect=timeout) as post:
            first = self.run_operation()
            second = self.run_operation()
        self.assertEqual(first['state'], 'completed')
        self.assertEqual(second['state'], 'completed')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.token.call_count, 1)
        self.assertEqual(AvitoServicesFeedVersion.objects.count(), 2)

    def test_timeout_without_receipt_remains_unknown_and_never_reposts(self):
        with patch.object(operation, '_post_photo', side_effect=TimeoutError('private URL')) as post:
            first = self.run_operation()
            self.connection.refresh_from_db()
            operation_id = self.connection.settings[operation.KEY]['operation_id']
            second = self.run_operation()
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(second['state'], 'unknown')
        self.assertEqual(first['code'], 'release_photo_unknown')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.token.call_count, 1)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[operation.KEY]['operation_id'], operation_id)
        self.assertEqual(AvitoServicesFeedVersion.objects.count(), 0)
        self.assertNotIn('private URL', json.dumps(first))

    def test_same_generation_busy_lease_never_mints_or_posts(self):
        operation._reserve('a' * 40)
        with patch.object(operation, '_post_photo') as post:
            result = self.run_operation()
        self.assertEqual(result['state'], 'busy')
        self.assertEqual(result['code'], 'release_busy')
        self.token.assert_not_called()
        post.assert_not_called()

    def test_account_drift_and_pending_owner_edit_are_not_overwritten(self):
        def changed_source(*args):
            changed = deepcopy(self.feed.draft)
            changed[0]['rows'][0]['Title'] = 'New pending owner edit'
            AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(draft=changed, revision=18)
            return self.source
        self.source_read.side_effect = changed_source
        with patch.object(operation, '_post_photo') as post:
            result = self.run_operation()
        self.assertEqual(result['code'], 'release_binding_changed')
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.draft[0]['rows'][0]['Title'], 'New pending owner edit')
        self.assertEqual(self.feed.published, self.original)
        self.token.assert_not_called()
        post.assert_not_called()

    def test_incorrect_restored_revision_or_history_is_not_accepted(self):
        def wrong_revision(token):
            proof = self.commit_photo(token)
            self.proof = dict(proof, revision_after=proof['revision_after'] + 1)
        with patch.object(operation, '_post_photo', side_effect=wrong_revision):
            result = self.run_operation()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['code'], 'release_media_receipt_invalid')
        self.assertIsNone(result['photo_proof'])

    def test_scope_and_generation_gates_do_not_touch_goods_or_mint(self):
        bad = operation.run_release_operation('a' * 40, 'other')
        self.assertEqual(bad['code'], 'release_generation_invalid')
        self.connection.external_id = '555'
        self.connection.save(update_fields=['external_id'])
        with patch.object(operation, '_post_photo') as post:
            result = self.run_operation()
        self.assertEqual(result['code'], 'release_scope_invalid')
        self.token.assert_not_called()
        post.assert_not_called()
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'keep': 'goods'})

    def test_command_requires_operator_env_exact_generation_and_checkout(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(CommandError):
                call_command('run_avito_services_release_operation', expected_sha='a' * 40,
                    release_generation=operation.GENERATION, stdout=io.StringIO())
        command = 'pool_service.management.commands.run_avito_services_release_operation'
        with patch.dict('os.environ', {'SERVICE2_AVITO_RELEASE_OPERATOR': '1'}):
            with patch(command + '.verify_checkout', side_effect=CommandError('mismatch')):
                with self.assertRaises(CommandError):
                    call_command('run_avito_services_release_operation', expected_sha='a' * 40,
                        release_generation=operation.GENERATION, stdout=io.StringIO())
            output = io.StringIO()
            with patch(command + '.verify_checkout') as verify, patch(command + '.run_release_operation',
                    return_value=operation._result('unknown', 'release_photo_unknown')) as run:
                call_command('run_avito_services_release_operation', expected_sha='a' * 40,
                    release_generation=operation.GENERATION, stdout=output)
            verify.assert_called_once_with('a' * 40)
            run.assert_called_once_with('a' * 40, operation.GENERATION)
            self.assertTrue(output.getvalue().startswith('AVITO_SERVICES_RELEASE_OPERATION_RECEIPT '))


    def test_late_durable_receipt_resolves_unknown_without_new_token_or_http(self):
        with patch.object(operation, '_post_photo', side_effect=TimeoutError) as post:
            first = self.run_operation()
            self.assertEqual(first['state'], 'unknown')
            self.commit_photo('synthetic-capability')
            second = self.run_operation()
        self.assertEqual(second['state'], 'completed')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.token.call_count, 1)

    def test_revoked_channel_capability_remains_actor_denial(self):
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(
            can_manage_channels=False)
        with patch.object(operation, '_post_photo') as post:
            result = self.run_operation()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['code'], 'release_actor_invalid')
        self.token.assert_not_called()
        self.account.assert_not_called()
        post.assert_not_called()

    def test_credential_rotation_during_preflight_blocks_before_post(self):
        def rotate(*args):
            AvitoCredential.objects.filter(connection=self.connection).update(
                client_secret_encrypted='changed-synthetic-secret')
            return self.source
        self.source_read.side_effect = rotate
        with patch.object(operation, '_post_photo') as post:
            result = self.run_operation()
        self.assertEqual(result['code'], 'release_binding_changed')
        self.token.assert_not_called()
        post.assert_not_called()



@override_settings(ALLOWED_HOSTS=['service2.aqualine22.ru'],
                   AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class ReleaseOperationTransportTests(SimpleTestCase):
    def test_photo_handler_uses_remaining_deadline_after_bounded_tls_connect(self):
        response = MagicMock()
        response.status = 200
        response.getheader.return_value = 'identity'
        response.read.return_value = b'{}'
        client = MagicMock()
        client.getresponse.return_value = response
        with patch('pool_service.avito_services_media._resolve', return_value='93.184.216.34'), \
                patch('pool_service.avito_services_media._PinnedHTTPS', return_value=client) as create, \
                patch.object(operation.time, 'monotonic', return_value=100), \
                patch.object(operation.threading, 'Timer') as timer:
            self.assertEqual(operation._post_photo('synthetic-capability'), {})
        create.assert_called_once_with('service2.aqualine22.ru', 443, '93.184.216.34', 5)
        client.connect.assert_called_once_with()
        client.sock.settimeout.assert_called_once_with(45)
        self.assertEqual(timer.call_args.args[0], 45)
        timer.return_value.start.assert_called_once_with()
        timer.return_value.cancel.assert_called_once_with()
        args, kwargs = client.request.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], '/api/communications/avito/services-media/livecheck/')
        self.assertEqual(json.loads(kwargs['body']), {'token': 'synthetic-capability'})
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertNotIn('Cookie', kwargs['headers'])
        self.assertEqual(kwargs['headers']['Accept-Encoding'], 'identity')
