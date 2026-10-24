from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_publication_models import (
    AvitoServicesPublication, AvitoServicesPublicationAttempt, AvitoServicesListingIntent)
from pool_service.avito_services_history import record_version
from pool_service.avito_services_publication import (
    public_origin, version_url, bind_publication, correlate_report, publication_context, archive_context, annotate_reactivation)
from pool_service.avito_services_feed import parse_export
from pool_service.avito_services_xml import xml_bytes
from pool_service.avito_autoload_report import summary_data
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes
from pool_service.tests.test_avito_autoload_report import upload, item as provider_item, page as provider_page

class PublicationResultTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Publication test')
        self.owner = User.objects.create_user('publication-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Synthetic service')
        self.connection = ChannelConnection.objects.create(channel=channel, external_id='123', name='Synthetic')
        data = parse_export(export_bytes(('111', '222')))
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id='123',
            organization_id_at_import=self.org.pk, draft=deepcopy(data), published=data,
            enabled=True, revision=2, published_revision=2, published_photo_limit=10)
        self.client.force_login(self.owner)
        self.url = reverse('avito_services_feed', args=[self.connection.pk])
        self.report_started = timezone.now() - timedelta(hours=1)
        self.version, self.publication = self.publish_version(timezone.now() - timedelta(hours=2))

    def publish_version(self, at):
        version = record_version(self.feed, 'publish', [], self.feed.published,
            actor=self.owner, content=xml_bytes(self.feed.published))
        AvitoServicesFeedVersion.objects.filter(pk=version.pk).update(created_at=at)
        version.refresh_from_db()
        return version, bind_publication(self.feed, version)

    def report(self, *, source='immutable', when=None, **changes):
        sources = {'immutable': self.publication.immutable_source_sha256,
                   'stable': self.publication.stable_source_sha256}
        result = {'version': 1, 'complete': True, 'kind': 'last_successful', 'upload_id': '900',
            'upload_status': 'success', 'upload_finished': True, 'total': 2,
            'started_at': (when if when is not None else self.report_started).isoformat(),
            'source_url_sha256': [sources[source]],
            'rows': [{'ad_id': str(i), 'avito_id': i, 'avito_status': 'active',
                      'avito_date_end': None, 'messages': []} for i in (111, 222)]}
        result.update(changes)
        return result

    def test_exact_immutable_source_links_version_and_item_results_not_text(self):
        attempt = correlate_report(self.connection, self.report())
        self.assertEqual(attempt.state, 'source_version_confirmed')
        self.assertTrue(attempt.source_confirmed)
        self.assertFalse(attempt.content_verified)
        self.assertEqual({row['avito_id'] for row in attempt.rows}, {'111', '222'})
        self.assertEqual(attempt.publication.version_id, self.version.pk)

    def test_stable_url_only_produces_candidate(self):
        attempt = correlate_report(self.connection, self.report(source='stable'))
        self.assertEqual(attempt.state, 'candidate_finished')
        self.assertFalse(attempt.source_confirmed)
        self.assertFalse(attempt.content_verified)

    def test_old_and_equal_time_reports_never_confirm_new_publication(self):
        for offset in (timedelta(minutes=-1), timedelta()):
            report = self.report(when=self.version.created_at + offset, upload_id=str(901 + int(offset.total_seconds() == 0)))
            attempt = correlate_report(self.connection, report)
            self.assertEqual(attempt.state, 'report_predates_publication')
            self.assertFalse(attempt.source_confirmed)

    def test_unfinished_report_then_finished_is_history_and_identical_retry_is_idempotent(self):
        first = correlate_report(self.connection, self.report(upload_status='processing', upload_finished=False))
        self.assertEqual(first.state, 'report_unfinished')
        self.assertFalse(first.source_confirmed)
        second = correlate_report(self.connection, self.report())
        repeated = correlate_report(self.connection, self.report())
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(repeated.pk, second.pk)
        reordered = self.report()
        reordered['rows'].reverse()
        self.assertEqual(correlate_report(self.connection, reordered).pk, second.pk)
        self.assertEqual(AvitoServicesPublicationAttempt.objects.count(), 2)

    def test_missing_mixed_or_unrelated_sources_never_confirm(self):
        cases = [([], 'report_source_unknown'),
                 ([self.publication.immutable_source_sha256, '0' * 64], 'report_source_mismatch'),
                 (['0' * 64], 'report_source_mismatch')]
        for sources, state in cases:
            with self.subTest(sources=sources):
                attempt = correlate_report(self.connection, self.report(source_url_sha256=sources))
                self.assertEqual(attempt.state, state)
                self.assertFalse(attempt.source_confirmed)

    def test_changed_ids_and_duplicate_rows_fail_closed(self):
        report = self.report()
        report['rows'][0]['avito_id'] = 333
        attempt = correlate_report(self.connection, report)
        self.assertEqual(attempt.state, 'report_identity_mismatch')
        self.assertFalse(attempt.source_confirmed)
        report['rows'][1] = deepcopy(report['rows'][0])
        self.assertIsNone(correlate_report(self.connection, report))

    def test_errors_and_rejections_are_not_successful_application(self):
        report = self.report(upload_status='success_warning')
        report['rows'][0].update(avito_status='rejected', messages=[{'type': 'error', 'code': 999}])
        attempt = correlate_report(self.connection, report)
        self.assertEqual(attempt.state, 'item_errors')
        self.assertTrue(attempt.source_confirmed)
        self.assertFalse(attempt.content_verified)
        self.assertTrue(attempt.rows[0]['has_error'])
        self.assertEqual(attempt.rows[0]['status'], 'rejected')
        report['upload_status'] = 'error'
        failed = correlate_report(self.connection, report)
        self.assertEqual(failed.state, 'upload_failed')
        self.assertFalse(failed.source_confirmed)

    def test_incomplete_future_and_wrong_total_reports_are_not_saved(self):
        cases = [self.report(complete=False), self.report(total=3),
                 self.report(when=timezone.now() + timedelta(hours=1))]
        for report in cases:
            self.assertIsNone(correlate_report(self.connection, report))
        self.assertEqual(AvitoServicesPublicationAttempt.objects.count(), 0)

    def test_old_upload_can_link_old_version_but_never_newer_version(self):
        self.feed.revision += 1
        self.feed.published_revision = self.feed.revision
        self.feed.published[0]['rows'][0]['Title'] = 'Second synthetic content'
        self.feed.save()
        newer, binding = self.publish_version(timezone.now() - timedelta(minutes=10))
        attempt = correlate_report(self.connection, self.report())
        self.assertEqual(attempt.publication_id, self.publication.pk)
        context = publication_context(self.feed)
        self.assertEqual(context['version'].pk, newer.pk)
        self.assertEqual(context['state'], 'waiting')
        self.assertFalse(context['source_confirmed'])

    def test_immutable_url_serves_original_bytes_after_later_publication(self):
        original = xml_bytes(self.version.snapshot)
        self.feed.published[0]['rows'][0]['Title'] = 'Later content'
        self.feed.save()
        response = Client().get(reverse('avito_services_feed_version_public', args=[self.feed.public_key, self.version.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, original)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), self.publication.file_sha256)
        self.assertEqual(AvitoServicesPublicationAttempt.objects.count(), 0)
        self.feed.enabled = False
        self.feed.save()
        self.assertEqual(Client().get(reverse('avito_services_feed_version_public',
            args=[self.feed.public_key, self.version.pk])).status_code, 404)

    def test_tampered_snapshot_account_drift_and_wrong_feed_capability_fail_closed(self):
        url = reverse('avito_services_feed_version_public', args=[self.feed.public_key, self.version.pk])
        corrupt = deepcopy(self.version.snapshot)
        corrupt[0]['rows'][0]['Title'] = 'Corrupted'
        AvitoServicesFeedVersion.objects.filter(pk=self.version.pk).update(snapshot=corrupt)
        self.assertEqual(Client().get(url).status_code, 503)
        self.assertIsNone(correlate_report(self.connection, self.report()))
        self.connection.external_id = '999'
        self.connection.save()
        self.assertIsNone(correlate_report(self.connection, self.report()))
        self.assertEqual(Client().get(url).status_code, 404)
        import uuid
        self.assertEqual(Client().get(reverse('avito_services_feed_version_public',
            args=[uuid.uuid4(), self.version.pk])).status_code, 404)

    @override_settings(AVITO_SERVICES_PUBLIC_ORIGIN='https://trusted.invalid')
    def test_canonical_origin_uses_server_setting_not_request_host(self):
        self.assertEqual(public_origin(), 'https://trusted.invalid')
        self.assertTrue(version_url(self.version).startswith('https://trusted.invalid/'))
        with override_settings(AVITO_SERVICES_PUBLIC_ORIGIN='http://localhost/private?token=value'):
            with self.assertRaises(ValueError):
                public_origin()

    def test_report_keeps_source_hash_without_private_url_or_query(self):
        header = upload()
        normalized = summary_data(header)
        self.assertEqual(len(normalized['source_url_sha256']), 1)
        self.assertNotIn('private', json.dumps(normalized))
        self.assertNotIn('token=secret', json.dumps(normalized))

    def test_owner_policy_is_scoped_logged_and_does_not_change_provider_or_file(self):
        before = deepcopy(self.feed.published)
        payload = {'action': 'policy', 'revision': self.feed.revision, 'avito_id': '111',
                   'owner_withdrawn': '1', 'reason': 'Synthetic owner decision'}
        with patch('pool_service.communication_avito.access_token') as token:
            self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        token.assert_not_called()
        self.feed.refresh_from_db()
        intent = AvitoServicesListingIntent.objects.get(feed=self.feed, avito_id='111')
        self.assertTrue(intent.owner_withdrawn)
        self.assertEqual(intent.marked_by, self.owner)
        self.assertEqual(self.feed.published, before)
        version = self.feed.versions.get(action='policy')
        self.assertEqual(version.changes[0]['before'], False)
        self.assertEqual(version.changes[0]['after'], True)
        self.assertNotContains(self.client.get(self.url, {'history_version': version.pk}),
                               'Восстановить содержание в новый черновик')
        self.assertEqual(self.client.post(self.url, payload).status_code, 200)
        self.feed.refresh_from_db()
        response = self.client.post(self.url, dict(payload, revision=self.feed.revision, avito_id='999'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AvitoServicesListingIntent.objects.count(), 1)

    def test_policy_requires_csrf_and_foreign_organization_is_denied(self):
        payload = {'action': 'policy', 'revision': self.feed.revision, 'avito_id': '111', 'owner_withdrawn': '1'}
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.url, payload).status_code, 403)
        other = Organization.objects.create(name='Other publication org')
        channel = CommunicationChannel.objects.create(organization=other, kind='avito', name='Other')
        connection = ChannelConnection.objects.create(channel=channel, external_id='999', name='Other')
        self.assertEqual(self.client.post(reverse('avito_services_feed', args=[connection.pk]), payload).status_code, 404)

    def test_archive_reason_and_free_eligibility_are_not_invented(self):
        row = self.feed.draft[0]['rows'][0]
        unknown = archive_context(row, {'status': 'old'}, None)
        self.assertEqual(unknown['code'], 'archive_unknown')
        self.assertIsNone(unknown['cost'])
        self.assertFalse(unknown['can_reactivate'])
        old = archive_context(row, {'status': 'old'}, None,
            {'status': 'old', 'finish_time': (timezone.now() - timedelta(days=1)).isoformat()})
        self.assertEqual(old['code'], 'provider_expired')
        removed = archive_context(row, {'status': 'removed'}, None)
        self.assertEqual(removed['code'], 'removed')
        self.assertIn('инициатор', removed['label'])
        intent = AvitoServicesListingIntent.objects.create(feed=self.feed, avito_id='111',
            account_id='123', organization_id=self.org.pk, owner_withdrawn=True, marked_by=self.owner)
        controlled = archive_context(row, {'status': 'old'}, intent)
        self.assertEqual(controlled['code'], 'owner_withdrawn')
        self.assertFalse(controlled['can_reactivate'])

    def test_rotating_provider_copy_url_withholds_exact_source_proof_without_breaking_report(self):
        from pool_service import avito_autoload_report, avito_workspace
        first = upload()
        last = deepcopy(first)
        last['feed_urls'][0]['url'] = 'https://example.com/private?token=rotated'
        responses = [first, provider_page([provider_item('111')], total=1), last]
        with patch.object(avito_workspace, '_get', side_effect=responses), patch.object(avito_workspace, 'enforce_rate'):
            report = avito_autoload_report.fetch_report('synthetic-token', '123', kind='current')
        self.assertTrue(report['complete'])
        self.assertEqual(report['source_url_sha256'], [])

    def test_external_reactivation_requires_same_ids_active_status_and_later_known_term(self):
        now = timezone.now()
        previous = [{'id': '111', 'avito_id': '111', 'status': 'old',
            'checked_at': (now - timedelta(hours=1)).isoformat(),
            'finish_time': (now - timedelta(days=1)).isoformat(),
            'finish_time_source': 'avito_item_detail.finish_time'}]
        current = [{'id': '111', 'avito_id': '111', 'status': 'active',
            'checked_at': now.isoformat(), 'finish_time': (now + timedelta(days=20)).isoformat(),
            'finish_time_source': 'avito_item_detail.finish_time'}]
        intent = AvitoServicesListingIntent.objects.create(feed=self.feed, avito_id='111',
            account_id='123', organization_id=self.org.pk, owner_withdrawn=True, marked_by=self.owner)
        result = annotate_reactivation(previous, current)
        intent.refresh_from_db()
        self.assertTrue(intent.owner_withdrawn)
        self.assertTrue(result[0]['reactivation']['confirmed'])
        self.assertIsNone(result[0]['reactivation']['actor'])
        self.assertIsNone(result[0]['reactivation']['additional_cost'])
        self.assertFalse(result[0]['reactivation']['service2_executed'])
        for change in ({'finish_time': None}, {'id': 'different'}, {'status': 'removed'},
                       {'finish_time': previous[0]['finish_time']},
                       {'finish_time': (now - timedelta(minutes=1)).isoformat()}):
            self.assertNotIn('reactivation', annotate_reactivation(previous, [dict(current[0], **change)])[0])

    def test_publication_binding_and_report_observation_are_append_only(self):
        self.publication.stable_source_sha256 = '0' * 64
        with self.assertRaises(ValidationError):
            self.publication.save()
        attempt = correlate_report(self.connection, self.report())
        attempt.state = 'candidate_finished'
        with self.assertRaises(ValidationError):
            attempt.save()

    def test_later_upload_of_older_immutable_source_links_old_version_not_current_version(self):
        self.feed.revision += 1
        self.feed.published_revision = self.feed.revision
        self.feed.published[0]['rows'][0]['Title'] = 'Newer local version'
        self.feed.save()
        newer, binding = self.publish_version(timezone.now() - timedelta(minutes=20))
        attempt = correlate_report(self.connection, self.report(when=timezone.now() - timedelta(minutes=5)))
        self.assertEqual(attempt.publication_id, self.publication.pk)
        self.assertTrue(attempt.source_confirmed)
        self.assertEqual(publication_context(self.feed)['state'], 'waiting')
        self.assertFalse(publication_context(self.feed)['source_confirmed'])

    def test_rejected_without_message_is_still_an_item_problem(self):
        report = self.report(upload_status='success_warning')
        report['rows'][0]['avito_status'] = 'rejected'
        report['rows'][1]['avito_status'] = 'blocked'
        attempt = correlate_report(self.connection, report)
        self.assertTrue(attempt.source_confirmed)
        self.assertEqual(attempt.state, 'item_errors')
        self.assertTrue(all(row['has_error'] for row in attempt.rows))
        self.assertFalse(attempt.content_verified)

    def test_xml_hash_survives_database_object_key_reordering(self):
        before = xml_bytes(self.feed.published)
        self.feed.refresh_from_db()
        persisted = xml_bytes(self.feed.published)
        # This exercises the real JSONField adapter on SQLite and MySQL CI.
        self.assertEqual(before, persisted)
        reordered = json.loads(json.dumps(self.feed.published, sort_keys=True))
        self.assertEqual(xml_bytes(reordered), before)
        without_headers = [{'rows': deepcopy(self.feed.published[0]['rows'])}]
        reordered_without_headers = json.loads(json.dumps(without_headers, sort_keys=True))
        self.assertEqual(xml_bytes(without_headers), xml_bytes(reordered_without_headers))
        invalid_headers = deepcopy(without_headers)
        invalid_headers[0]['headers'] = {'invalid': 'shape'}
        self.assertEqual(xml_bytes(without_headers), xml_bytes(invalid_headers))
        self.assertEqual(correlate_report(self.connection, self.report()).state, 'source_version_confirmed')

    def test_immutable_source_uses_anonymous_api_route_and_retains_capability_scope(self):
        url = reverse('avito_services_feed_version_public', args=[self.feed.public_key, self.version.pk])
        self.assertTrue(url.startswith('/api/communications/avito/services-feed/'))
        self.assertEqual(Client().get(url).status_code, 200)
        self.assertNotIn('/accounts/login/', Client().get(url).get('Location', ''))
