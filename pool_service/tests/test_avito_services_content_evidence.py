from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from io import StringIO
import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_services_content_evidence as evidence
from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_media_models import AvitoServicesImage
from pool_service.avito_services_feed import parse_export
from pool_service.avito_services_publication import bind_publication, publication_facts
from pool_service.avito_services_xml import xml_bytes
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes


@override_settings(ALLOWED_HOSTS=['testserver', 'service2.aqualine22.ru'],
                   AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class PartialPhotoEvidenceTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 25, 14, 30, tzinfo=evidence.BARNAUL)
        timer = patch.object(evidence.timezone, 'now', return_value=self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.org = Organization.objects.create(name='Partial evidence fixture')
        self.owner = User.objects.create_user('partial-photo-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            external_id=evidence.ACCOUNT_ID, name='Services')
        self.goods = ChannelConnection.objects.create(pk=1, channel=self.channel,
            external_id='other', name='Goods sentinel', settings={'keep': 'unchanged'})
        self.asset_key = uuid.UUID('00000000-0000-4000-8000-000000000018')
        key = patch.object(evidence, 'OWNER_ASSET_KEY_SHA256',
            hashlib.sha256(str(self.asset_key).encode()).hexdigest())
        key.start()
        self.addCleanup(key.stop)
        self.published_at = datetime(2026, 10, 25, 13, 19, 37, tzinfo=evidence.BARNAUL)
        self.asset = AvitoServicesImage(connection=self.connection, account_id=evidence.ACCOUNT_ID,
            organization_id_at_upload=self.org.pk, public_key=self.asset_key,
            sha256='a' * 64, byte_size=1, width=1536, height=1152, uploaded_by=self.owner,
            published_in_feed_at=self.published_at)
        self.asset.public_url = 'https://service2.aqualine22.ru' + reverse(
            'avito_services_image_public', args=[self.asset_key])
        self.asset.file.name = f'avito_services_images/{self.org.pk}/4/{self.asset_key.hex}.jpg'
        self.asset.save()
        sheets = parse_export(export_bytes(('8343962135', '111')))
        for row in sheets[0]['rows']:
            row['ImageUrls'] = ' | '.join(f'https://photos.invalid/{row["AvitoId"]}/{n}.jpg' for n in range(10))
        sheets[0]['rows'][0]['ImageUrls'] = ' | '.join(
            sheets[0]['rows'][0]['ImageUrls'].split(' | ')[:9] + [self.asset.public_url])
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection,
            account_id=evidence.ACCOUNT_ID, organization_id_at_import=self.org.pk,
            enabled=True, revision=18, published_revision=18, published_photo_limit=10,
            draft=deepcopy(sheets), published=sheets,
            latest_observations=[{'avito_id': '8343962135', 'status': 'active'}],
            last_checked_at=self.published_at)
        self.version = AvitoServicesFeedVersion.objects.create(pk=7, feed=self.feed, revision=18,
            published_revision=18, action='publish', actor=self.owner, snapshot=sheets,
            file_digest=hashlib.sha256(xml_bytes(sheets)).hexdigest())
        AvitoServicesFeedVersion.objects.filter(pk=7).update(created_at=self.published_at)
        self.version.refresh_from_db()
        self.publication = bind_publication(self.feed, self.version)
        actor = patch('pool_service.avito_services_workplace_setup._actor', return_value=self.owner)
        actor.start()
        self.addCleanup(actor.stop)
        self.client.force_login(self.owner)
        self.url = reverse('avito_services_feed', args=[4])
        self.args = dict(publication_version_id=7, avito_id='8343962135', photo_number=10,
            source_kind='owner_reported_screenshot', source_filename='1000025527.jpg',
            observed_at=datetime(2026, 10, 25, 14, 13, tzinfo=evidence.BARNAUL))

    def report(self, *, started=None, status='success', finished=True):
        return {'version': 1, 'complete': True, 'kind': 'current', 'upload_id': '900',
            'started_at': (started or self.now - timedelta(minutes=10)).isoformat(),
            'upload_status': status, 'upload_finished': finished, 'total': 2,
            'source_url_sha256': [self.publication.immutable_source_sha256],
            'rows': [{'ad_id': row['Id'], 'avito_id': int(row['AvitoId']), 'avito_status': 'active',
                      'avito_date_end': None, 'messages': [{'type': 'info', 'code': 3002}]}
                     for row in self.feed.published[0]['rows']],
            'input_copy_proof': {'state': 'unavailable', 'code': 'input_copy_http_401'}}

    def provider(self, report=None, error=None):
        context = patch('pool_service.communication_avito.authorized_account_id', return_value=evidence.ACCOUNT_ID)
        token = patch('pool_service.communication_avito.access_token', return_value='synthetic-token')
        fetch = patch('pool_service.avito_autoload_report.fetch_report',
            return_value=report or self.report(), side_effect=error)
        context.start()
        token.start()
        mocked = fetch.start()
        self.addCleanup(context.stop)
        self.addCleanup(token.stop)
        self.addCleanup(fetch.stop)
        return mocked

    def test_one_photo_proof_is_append_only_and_never_global_provider_or_content_proof(self):
        before = deepcopy((self.feed.draft, self.feed.published, self.feed.revision,
                           self.feed.published_revision, self.feed.latest_observations, self.feed.last_checked_at))
        event_id, created = evidence.record_photo_evidence(self.connection, self.owner, **self.args)
        self.assertTrue(created)
        event = self.feed.versions.get(pk=event_id)
        self.assertEqual(event.action, 'verify')
        self.assertEqual(event.source_version_id, 7)
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.changes[0]['evidence']['observed_precision'], 'minute')
        self.assertFalse(event.changes[0]['evidence']['evidence_file_retained'])
        self.assertNotIn('screenshot_sha256', event.changes[0]['evidence'])
        self.feed.refresh_from_db()
        self.assertEqual(before, (self.feed.draft, self.feed.published, self.feed.revision,
            self.feed.published_revision, self.feed.latest_observations, self.feed.last_checked_at))
        self.assertEqual(self.publication.attempts.count(), 0)
        self.assertFalse(publication_facts(self.publication)['live_content_verified'])
        context = evidence.manual_photo_context(self.connection, self.feed, self.version)
        self.assertEqual(len(context['records']), 1)
        self.assertEqual((context['records'][0]['avito_id'], context['records'][0]['photo_number']),
                         ('8343962135', 10))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'keep': 'unchanged'})

    def test_duplicate_owner_declaration_across_recording_methods_keeps_one_event(self):
        first, created = evidence.record_photo_evidence(self.connection, self.owner, **self.args)
        second, repeated = evidence.record_photo_evidence(self.connection, self.owner,
            **self.args, recording_method='protected_operator_owner_report')
        self.assertEqual(first, second)
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(self.feed.versions.filter(action='verify').count(), 1)

    def test_bad_times_paths_photo_numbers_and_foreign_rows_are_rejected(self):
        for changes in ({'photo_number': 0}, {'photo_number': 11}, {'avito_id': 'foreign'},
                {'source_filename': '../secret.jpg'}, {'source_kind': 'api'},
                {'observed_at': self.published_at.replace(second=0)},
                {'observed_at': self.now + timedelta(minutes=1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evidence.record_photo_evidence(self.connection, self.owner, **{**self.args, **changes})
        self.assertEqual(self.feed.versions.filter(action='verify').count(), 0)

    def test_revoked_permissions_and_other_account_cannot_write(self):
        viewer = User.objects.create_user('partial-photo-viewer')
        OrganizationAccess.objects.create(organization=self.org, user=viewer, role='employee')
        with self.assertRaises(PermissionDenied):
            evidence.record_photo_evidence(self.connection, viewer, **self.args)
        with self.assertRaises(ValueError):
            evidence.record_photo_evidence(self.goods, self.owner, **self.args)
        self.assertEqual(self.feed.versions.filter(action='verify').count(), 0)

    def test_stale_ui_version_is_rejected_and_old_evidence_never_propagates(self):
        evidence.record_photo_evidence(self.connection, self.owner, **self.args)
        newer = deepcopy(self.feed.published)
        newer[0]['rows'][0]['ImageUrls'] = newer[0]['rows'][0]['ImageUrls'].replace('0.jpg', 'new.jpg')
        self.feed.published, self.feed.draft = newer, deepcopy(newer)
        self.feed.revision = self.feed.published_revision = 19
        self.feed.save()
        version = AvitoServicesFeedVersion.objects.create(feed=self.feed, action='publish', revision=19,
            published_revision=19, actor=self.owner, snapshot=newer,
            file_digest=hashlib.sha256(xml_bytes(newer)).hexdigest())
        bind_publication(self.feed, version)
        with self.assertRaises(ValueError):
            evidence.record_photo_evidence(self.connection, self.owner, **self.args)
        self.assertEqual(evidence.manual_photo_context(self.connection, self.feed, version)['records'], [])
        self.assertFalse(publication_facts(version.publication)['live_content_verified'])

    def test_tampered_evidence_or_binding_is_not_displayed_as_a_match(self):
        event_id, unused = evidence.record_photo_evidence(self.connection, self.owner, **self.args)
        event = self.feed.versions.get(pk=event_id)
        changes = event.changes
        changes[0]['evidence']['photo_url_sha256'] = 'f' * 64
        changes[0]['fingerprint'] = evidence._fingerprint(changes[0]['evidence'])
        self.feed.versions.filter(pk=event_id).update(changes=changes)
        self.assertEqual(evidence.manual_photo_context(self.connection, self.feed, self.version)['records'], [])
        self.connection.external_id = 'foreign'
        self.connection.save(update_fields=['external_id'])
        self.assertFalse(evidence.manual_photo_context(self.connection, self.feed, self.version)['available'])

    def test_form_saves_only_selected_photo_and_explicit_barnaul_time(self):
        with timezone.override('UTC'):
            response = self.client.post(self.url, {'action': 'confirm_photo', 'publication_version_id': '7',
                'avito_id': '8343962135', 'photo_number': '10', 'source_kind': 'owner_reported_screenshot',
                'source_filename': '1000025527.jpg', 'observed_at': '2026-10-25T14:13',
                'content_verified': 'true', 'source_confirmed': 'true'})
            self.assertEqual(response.status_code, 302)
            response = self.client.get(self.url)
        self.assertContains(response, 'фото №10')
        self.assertContains(response, '25.10.2026 14:13')
        self.assertContains(response, 'Сообщение владельца о скриншоте')
        self.assertContains(response, 'Полная сверка карточек Авито не выполнена')
        self.assertContains(response, 'Ручная сверка фото №10')
        self.assertFalse(publication_facts(self.publication)['live_content_verified'])
        self.assertEqual(self.publication.attempts.count(), 0)

    def test_fixed_owner_setup_records_once_and_reads_current_report_once(self):
        fetch = self.provider()
        first = evidence.record_owner_photo_report_once()
        second = evidence.record_owner_photo_report_once()
        self.assertEqual(first['state'], 'recorded')
        self.assertEqual(first['manual_photo']['source_kind'], 'owner_reported_screenshot')
        self.assertEqual(first['provider_report']['state'], 'later_completed')
        self.assertEqual(second['provider_report']['state'], 'already_checked')
        self.assertFalse(first['facts']['live_content_verified'])
        self.assertEqual(first['manual_photo']['history_event_id'], second['manual_photo']['history_event_id'])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs['kind'], 'current')
        self.assertFalse(fetch.call_args.kwargs['allow_shop_recovery'])

    def test_old_and_unfinished_reports_never_get_attached_to_publication(self):
        for report, expected in (
                (self.report(started=self.published_at - timedelta(minutes=1)), 'predates_publication'),
                (self.report(status=None, finished=False), 'later_unfinished')):
            # Fresh isolated once-key for each controlled response; manual event remains idempotent.
            self.connection.refresh_from_db()
            self.connection.settings = {}
            self.connection.save(update_fields=['settings'])
            fetch = self.provider(report)
            result = evidence.record_owner_photo_report_once()
            self.assertEqual(result['provider_report']['state'], expected)
            self.assertEqual(self.publication.attempts.count(), 0)
            self.assertFalse(result['facts']['source_confirmed'])

    def test_provider_failure_keeps_committed_manual_evidence_and_does_not_retry(self):
        from pool_service.communication_avito import AvitoError
        fetch = self.provider(error=AvitoError('provider_http_403'))
        result = evidence.record_owner_photo_report_once()
        repeated = evidence.record_owner_photo_report_once()
        self.assertEqual(result['state'], 'recorded')
        self.assertEqual(result['provider_report']['error_code'], 'provider_http_403')
        self.assertEqual(repeated['provider_report']['state'], 'already_checked')
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(self.feed.versions.filter(action='verify').exists())

    def test_mid_read_account_change_withholds_provider_facts_without_erasing_manual_event(self):
        fetch = self.provider()
        report = self.report()
        def changed(*args, **kwargs):
            ChannelConnection.objects.filter(pk=4).update(external_id='foreign')
            return report
        fetch.side_effect = changed
        result = evidence.record_owner_photo_report_once()
        self.assertEqual(result['state'], 'recorded')
        self.assertEqual(result['provider_report']['state'], 'unavailable')
        self.assertFalse(any(result['facts'].values()))
        self.assertEqual(self.feed.versions.filter(action='verify').count(), 1)
        self.assertEqual(self.publication.attempts.count(), 0)

    def test_ineligible_snapshot_does_not_fabricate_manual_evidence_or_call_provider(self):
        fetch = self.provider()
        self.version.delete()
        result = evidence.record_owner_photo_report_once()
        self.assertEqual(result['state'], 'ineligible')
        fetch.assert_not_called()
        self.assertFalse(self.feed.versions.filter(action='verify').exists())
