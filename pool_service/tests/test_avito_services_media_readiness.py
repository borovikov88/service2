"""Synthetic source fixtures only; no export or live account content."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.test import TestCase

from pool_service import avito_services_media as media
from pool_service import avito_services_media_readiness as readiness
from pool_service import avito_services_workplace_setup as setup
from pool_service.communication_models import CommunicationAccess
from pool_service.models import Organization, OrganizationAccess
from . import test_avito_services_media_livecheck as fixtures
from .test_avito_services_media import picture


class MediaReadinessTests(TestCase):
    def setUp(self):
        fixtures.LiveMediaCheckTests.setUp(self)
        self.connection.settings = {setup.KEY: {'actor_id': self.owner.pk,
            'automatic_reports_configured_once': True}}
        self.connection.save(update_fields=['settings'])
        self.feed.published_revision = 15
        self.feed.save(update_fields=['published_revision'])

    def state(self):
        self.feed.refresh_from_db()
        self.connection.refresh_from_db()
        return (deepcopy(self.feed.draft), deepcopy(self.feed.published),
                self.feed.revision, self.feed.versions.count(),
                deepcopy(self.connection.settings), self.feed.published_revision)

    def test_success_is_fixed_shape_readonly_and_never_uploads_or_mints(self):
        before = self.state()
        with patch.object(media, '_download', return_value=picture()) as download, \
                patch.object(media, 'services_image_upload') as upload, \
                patch('pool_service.avito_services_media_livecheck.make_media_livecheck_token') as mint:
            value = readiness.media_release_readiness(self.connection)
        self.assertEqual(set(value), {'state', 'stage', 'reason', 'exception_class', 'checks'})
        self.assertEqual(value['state'], 'ready')
        self.assertEqual(value['stage'], 'complete')
        self.assertEqual(value['reason'], 'media_ready')
        self.assertIsNone(value['exception_class'])
        self.assertEqual(value['checks'], dict.fromkeys(readiness.CHECKS, True))
        self.assertEqual(download.call_count, 1)
        upload.assert_not_called()
        mint.assert_not_called()
        self.assertEqual(self.state(), before)
        encoded = json.dumps(value)
        for private in ('https://', self.owner.username, 'first.jpg', 'sha256', 'avito_id'):
            self.assertNotIn(private, encoded)

    def test_legacy_whitespace_is_diagnosed_separately_without_relaxing_guard(self):
        original = deepcopy(self.feed.draft)
        for field, stage in (('Title', 'title_roundtrip'), ('Description', 'description_roundtrip')):
            with self.subTest(field=field):
                self.feed.draft = deepcopy(original)
                self.feed.draft[0]['rows'][0][field] = '\n ' + self.feed.draft[0]['rows'][0][field] + ' \n'
                self.feed.save(update_fields=['draft'])
                before = self.state()
                with patch.object(media, '_download') as download:
                    value = readiness.media_release_readiness(self.connection)
                self.assertEqual(value['state'], 'blocked')
                self.assertEqual(value['stage'], stage)
                self.assertEqual(value['reason'], readiness.REASONS[stage])
                self.assertTrue(value['checks']['form_valid'])
                self.assertFalse(value['checks'][stage])
                self.assertIsNone(value['checks']['source_read'])
                self.assertEqual(value['exception_class'], 'LiveMediaCheckError')
                download.assert_not_called()
                self.assertEqual(self.state(), before)

    def test_revoked_capability_is_a_permission_stage_before_network(self):
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(
            can_manage_channels=False)
        before = self.state()
        with patch.object(media, '_download') as download:
            value = readiness.media_release_readiness(self.connection)
        self.assertEqual(value['stage'], 'manage_channels')
        self.assertEqual(value['reason'], 'media_capability_denied')
        self.assertTrue(value['checks']['editor_organization'])
        self.assertFalse(value['checks']['manage_channels'])
        self.assertIsNone(value['checks']['source_read'])
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_other_first_organization_is_distinct_from_target_channel_capability(self):
        foreign = Organization.objects.create(name='Other synthetic editor organization')
        # Retain the first membership's identity/order, moving it to another org.
        # Then restore a second valid owner membership for the target connection.
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(organization=foreign)
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role='owner')
        CommunicationAccess.objects.update_or_create(user=self.owner, organization=self.org,
            defaults={'can_manage_channels': True})
        before = self.state()
        with patch.object(media, '_download') as download:
            value = readiness.media_release_readiness(self.connection)
        self.assertEqual(value['stage'], 'editor_organization')
        self.assertEqual(value['reason'], 'media_editor_organization_mismatch')
        self.assertFalse(value['checks']['editor_organization'])
        self.assertTrue(value['checks']['manage_channels'])
        download.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_download_and_decode_failures_have_distinct_fixed_stage_without_body(self):
        before = self.state()
        with patch.object(media, '_download', side_effect=ValueError('PRIVATE https://secret.invalid')):
            transport = readiness.media_release_readiness(self.connection)
        self.assertEqual(transport['stage'], 'source_read')
        self.assertEqual(transport['exception_class'], 'ValueError')
        self.assertFalse(transport['checks']['source_read'])
        self.assertIsNone(transport['checks']['image_decode'])
        with patch.object(media, '_download', return_value=b'PRIVATE INVALID BODY'):
            decode = readiness.media_release_readiness(self.connection)
        self.assertEqual(decode['stage'], 'image_decode')
        self.assertEqual(decode['exception_class'], 'ValueError')
        self.assertTrue(decode['checks']['source_read'])
        self.assertFalse(decode['checks']['image_decode'])
        self.assertNotIn('PRIVATE', json.dumps([transport, decode]))
        self.assertNotIn('secret.invalid', json.dumps([transport, decode]))
        self.assertEqual(self.state(), before)
