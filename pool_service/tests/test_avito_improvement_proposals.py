from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import os
from threading import Barrier
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse

from pool_service import avito_improvement_proposals as projects
from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_publication_models import AvitoServicesPublicationAttempt, AvitoServicesListingIntent
from pool_service.avito_services_content_evidence import is_manual_photo_event
from pool_service.avito_services_feed import parse_export
from pool_service.avito_services_publication import bind_publication
from pool_service.avito_services_xml import xml_bytes
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes


class ProposalFixture:
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 25, 18, 30, tzinfo=ZoneInfo('Asia/Barnaul'))
        timer = patch.object(projects.timezone, 'now', return_value=self.now)
        timer.start(); self.addCleanup(timer.stop)
        self.org = Organization.objects.create(name='Improvement project fixture')
        self.foreign_org = Organization.objects.create(name='Foreign project fixture')
        self.owner = User.objects.create_user('project-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Services')
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            external_id=projects.ACCOUNT, name='Services')
        self.goods = ChannelConnection.objects.create(pk=1, channel=self.channel, external_id='fixture-goods',
            name='Goods sentinel', settings={'preserve': 'goods'})
        data = parse_export(export_bytes(tuple(projects.TARGETS)))
        data[0]['headers'].append('Address')
        for row in data[0]['rows']:
            identifier = row['AvitoId']
            row['Title'] = 'Тестовая услуга водоочистки' if identifier == '8346503274' else 'Тестовая услуга строительства бассейнов'
            row['Description'] = ('Тестовое описание водоочистки.' if identifier == '8346503274'
                else 'Тестовое описание строительства бассейнов и хаммамов.')
            row['Address'] = 'Алтайский край, г. ' + projects.TARGETS[identifier][0] + ', Тестовая улица'
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id=projects.ACCOUNT,
            organization_id_at_import=self.org.pk, enabled=True, revision=18, published_revision=18,
            published_photo_limit=10, draft=deepcopy(data), published=deepcopy(data))
        self.source = AvitoServicesFeedVersion.objects.create(pk=7, feed=self.feed, revision=18,
            published_revision=18, action='publish', actor=self.owner, snapshot=deepcopy(data),
            file_digest=hashlib.sha256(xml_bytes(data)).hexdigest())
        AvitoServicesFeedVersion.objects.filter(pk=self.source.pk).update(created_at=self.now-timedelta(hours=3))
        self.source.refresh_from_db()
        self.publication = bind_publication(self.feed, self.source)
        self.attempt = AvitoServicesPublicationAttempt.objects.create(publication=self.publication,
            upload_id='project-report', report_sha256='d'*64, report_started_at=self.now-timedelta(hours=1),
            state='input_copy_unavailable', upload_status='success', source_confirmed=False,
            rows=[{'xml_id': row['Id'], 'avito_id': row['AvitoId'], 'status': 'active',
                   'has_error': False, 'messages': [], 'finish_time': None}
                  for row in data[0]['rows']])
        self.analysis = {'schema_version': 1, 'source': 'saved_avito_stats_v2_items',
            'connection_id': 4, 'account_id': projects.ACCOUNT, 'cutoff': '2026-10-23',
            'generated_at': self.now.isoformat(), 'ads': {}}
        for identifier in projects.TARGETS:
            self.analysis['ads'][identifier] = {'available': True, 'current_published': True,
                'publication_revision': 18, 'from': '2026-10-17', 'to': '2026-10-23',
                'expected_days': 7, 'reported_days': 3, 'pages_complete': False, 'ad_rows_complete': False,
                'metrics_known': {'views': True, 'contacts': False}, 'low_or_unknown_volume': True,
                'received_at': (self.now-timedelta(minutes=10)).isoformat(), 'basis_digest': 'a'*64,
                'private_raw_metric': 'PRIVATE'}
        binding = patch.object(projects, '_binding', side_effect=lambda connection: {
            'account_id': connection.external_id, 'channel_id': connection.channel_id,
            'organization_id': connection.channel.organization_id,
            'credential_stamp': 'fixture-' + str((connection.settings or {}).get('generation', 0))})
        binding.start(); self.addCleanup(binding.stop)
        evidence = patch.object(projects, 'decision_evidence', side_effect=lambda *args, **kwargs: deepcopy(self.analysis))
        self.evidence = evidence.start(); self.addCleanup(evidence.stop)
        provider = patch('pool_service.communication_avito._json_request')
        self.provider = provider.start(); self.addCleanup(provider.stop)
        self.url = reverse('avito_service_improvement_proposals', args=[4])
        self.client.force_login(self.owner)

    def prepare(self, **kwargs):
        return projects.prepare(self.connection, self.owner, expected_revision=18, expected_source=7, **kwargs)

    def events(self):
        return self.feed.versions.filter(action='verify', changes__0__kind=projects.KIND)


@override_settings(ALLOWED_HOSTS=['testserver'], AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class ImprovementProjectTests(ProposalFixture, TestCase):
    def test_local_projects_keep_all_contents_and_proofs_unchanged_and_are_idempotent(self):
        baseline = (deepcopy(self.feed.draft), deepcopy(self.feed.published), self.feed.revision,
                    self.feed.published_revision, self.feed.updated_at, deepcopy(self.feed.latest_observations))
        first, second = self.prepare(), self.prepare()
        self.assertEqual([row['state'] for row in first['results']], ['created']*3)
        self.assertEqual([row['state'] for row in second['results']], ['existing']*3)
        self.assertEqual(self.events().count(), 3)
        self.feed.refresh_from_db()
        self.assertEqual(baseline, (self.feed.draft, self.feed.published, self.feed.revision,
            self.feed.published_revision, self.feed.updated_at, self.feed.latest_observations))
        self.assertEqual(self.publication.attempts.count(), 1)
        for event in self.events():
            item = event.changes[0]
            self.assertEqual(set(item['before']), {'Title'})
            self.assertEqual(set(item['after']), {'Title'})
            self.assertEqual(event.actor_id, self.owner.pk)
            self.assertEqual(event.source_version_id, 7)
            self.assertEqual(event.snapshot, self.feed.draft)
            self.assertFalse(is_manual_photo_event(event))
            self.assertFalse(item['source']['actual_avito_fields_verified'])
        self.provider.assert_not_called()

    def test_partial_analytics_stays_hypothesis_and_unknown_wordstat_is_not_a_number(self):
        self.prepare()
        text = str(list(self.events().values_list('changes', flat=True)))
        self.assertNotIn('PRIVATE', text)
        for event in self.events():
            project = event.changes[0]['proposal']
            self.assertEqual(project['wordstat']['state'], 'unavailable')
            self.assertIsNone(project['wordstat']['frequency'])
            self.assertFalse(project['analysis']['performance_ranked'])
            self.assertFalse(project['analysis']['ad_rows_complete'])
            self.assertEqual(project['evaluation']['state'], 'not_started')
            self.assertEqual(project['evaluation']['source_dates'], 14)
            self.assertNotIn('starts_at', project['evaluation'])
        self.provider.assert_not_called()

    def test_city_service_conflict_identical_and_already_clear_titles_are_not_changes(self):
        row = deepcopy(self.feed.published[0]['rows'][0])
        args = projects.TARGETS[row['AvitoId']]
        self.assertIsNotNone(projects._candidate(row, *args))
        row['Address'] = 'Алтайский край, г. Бийск, Тестовая улица'
        self.assertIsNone(projects._candidate(row, *args))
        row['Address'] = 'Алтайский край, г. Барнаул, Тестовая улица'
        row['Title'], row['Description'] = 'Тест другой услуги', 'Не содержит нужную услугу.'
        self.assertIsNone(projects._candidate(row, *args))
        row['Title'] = 'Тестовая услуга водоочистки · Барнаул'
        self.assertIs(projects._candidate(row, *args), False)

    def test_newer_inactive_status_owner_intent_and_missing_analytics_skip_independently(self):
        identifiers = list(projects.TARGETS)
        self.feed.latest_observations = [{'avito_id': identifiers[0], 'id': identifiers[0],
            'status': 'removed', 'source': 'avito_item_detail', 'checked_at': self.now.isoformat()}]
        self.feed.save(update_fields=['latest_observations'])
        AvitoServicesListingIntent.objects.create(feed=self.feed, avito_id=identifiers[1],
            account_id=projects.ACCOUNT, organization_id=self.org.pk, owner_withdrawn=True)
        self.analysis['ads'][identifiers[2]]['received_at'] = None
        result = self.prepare()
        self.assertEqual([row['code'] for row in result['results']],
                         ['inactive_or_unknown', 'owner_withdrawn', 'analytics_unknown'])
        self.assertFalse(self.events().exists())

    def test_rereading_an_old_report_does_not_make_old_active_status_fresh(self):
        AvitoServicesFeedVersion.objects.filter(pk=7).update(created_at=self.now-timedelta(hours=12))
        AvitoServicesPublicationAttempt.objects.filter(pk=self.attempt.pk).update(
            report_started_at=self.now-timedelta(hours=10), checked_at=self.now)
        self.assertTrue(all(row['code'] == 'inactive_or_unknown' for row in self.prepare()['results']))
        self.assertFalse(self.events().exists())

    def test_expired_known_term_and_unknown_status_never_create_a_project(self):
        rows = deepcopy(self.attempt.rows)
        rows[0]['finish_time'] = (self.now-timedelta(minutes=1)).isoformat()
        rows[1]['status'] = 'unknown'
        AvitoServicesPublicationAttempt.objects.filter(pk=self.attempt.pk).update(rows=rows)
        result = self.prepare()
        self.assertEqual(result['results'][0]['code'], 'inactive_or_unknown')
        self.assertEqual(result['results'][1]['code'], 'inactive_or_unknown')
        self.assertEqual(self.events().count(), 1)

    def test_revision_or_source_conflict_refuses_before_analysis_or_history(self):
        for kwargs in ({'expected_revision': 17}, {'expected_revision': 18, 'expected_source': 999}):
            with self.assertRaises(projects.ProposalError):
                projects.prepare(self.connection, self.owner, **kwargs)
        self.evidence.assert_not_called()
        self.assertFalse(self.events().exists())

    def test_binding_or_actor_change_in_report_helper_rolls_back_without_projects(self):
        def changed_binding(*args, **kwargs):
            ChannelConnection.objects.filter(pk=4).update(settings={'generation': 1})
            return deepcopy(self.analysis)
        self.evidence.side_effect = changed_binding
        with self.assertRaises(projects.ProposalError):
            self.prepare()
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings, {})
        self.assertFalse(self.events().exists())
        def revoked(*args, **kwargs):
            User.objects.filter(pk=self.owner.pk).update(is_active=False)
            return deepcopy(self.analysis)
        self.evidence.side_effect = revoked
        with self.assertRaises(PermissionDenied):
            self.prepare()
        self.assertFalse(self.events().exists())

    def test_foreign_account_or_organization_metadata_does_not_create_projects(self):
        for key, value in (('connection_id', 1), ('account_id', 'foreign')):
            original = self.analysis[key]
            self.analysis[key] = value
            self.assertTrue(all(row['code'] == 'analytics_unknown' for row in self.prepare()['results']))
            self.analysis[key] = original
        self.assertFalse(self.events().exists())
        with self.assertRaises(PermissionDenied):
            projects.prepare(self.goods, self.owner)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods'})

    def test_admin_route_permissions_csrf_and_single_project_label(self):
        manager = User.objects.create_user('project-manager')
        OrganizationAccess.objects.create(organization=self.org, user=manager, role='manager')
        foreign = User.objects.create_user('project-foreign-owner')
        OrganizationAccess.objects.create(organization=self.foreign_org, user=foreign, role='owner')
        for actor, status in ((manager, 403), (foreign, 404)):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, status)
            self.assertEqual(self.client.post(self.url, {'action': 'prepare'}).status_code, status)
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.owner)
        secured.get(self.url)
        self.assertEqual(secured.post(self.url, {'action': 'prepare'}).status_code, 403)
        self.assertEqual(secured.post(self.url, {'action': 'prepare'},
            HTTP_X_CSRFTOKEN=secured.cookies['csrftoken'].value).status_code, 409)
        response = secured.post(self.url, {'action': 'prepare', 'revision': 18, 'source_version': 7},
            HTTP_X_CSRFTOKEN=secured.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Проект, не применён', count=3)
        self.assertContains(response, 'Проверка ещё не началась', count=3)
        self.assertNotContains(response, 'Опубликовать')
        self.provider.assert_not_called()

    def test_history_labels_proposal_as_unapplied_and_unknown_raw_shapes_are_skipped(self):
        self.prepare()
        event = self.events().first()
        response = self.client.get(reverse('avito_services_feed', args=[4]), {'history_version': event.pk})
        self.assertContains(response, 'Проект, не применён')
        self.assertContains(response, 'Это предложение: поля черновика, XML и Авито не менялись')
        AvitoServicesFeedVersion.objects.create(feed=self.feed, action='verify', revision=18,
            source_version=self.source, snapshot=self.feed.draft,
            changes=[{'kind': projects.KIND, 'schema_version': 1, 'source': None}])
        self.assertEqual(len(projects.page_context(self.connection, self.owner)['projects']), 3)

    def test_unrelated_verify_events_do_not_evict_project_selection(self):
        self.prepare()
        AvitoServicesFeedVersion.objects.bulk_create([
            AvitoServicesFeedVersion(feed=self.feed, action='verify', revision=18,
                snapshot=[], changes=[{'kind': 'unrelated_observation'}]) for _ in range(205)])
        self.assertEqual(len(projects.page_context(self.connection, self.owner)['projects']), 3)

    @patch.dict(os.environ, {'SERVICE2_AVITO_DEPLOY_SETUP': '1'})
    @patch('pool_service.avito_services_workplace_setup._actor')
    def test_protected_cycle_is_once_and_closed_but_changes_no_feed_content(self, actor):
        actor.return_value = self.owner
        first = projects.setup_owner_cycle_once()
        second = projects.setup_owner_cycle_once()
        self.assertEqual([row['state'] for row in first['results']], ['created']*3)
        self.assertEqual(second['state'], 'already_attempted')
        self.assertEqual(self.events().count(), 3)
        self.assertNotIn('Title', str(first))
        self.assertNotIn('Description', str(first))
        self.assertNotIn('PRIVATE', str(first))
        self.connection.refresh_from_db()
        self.assertIn(projects.SETUP_KEY, self.connection.settings)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.revision, 18)
        self.assertEqual(self.feed.published_revision, 18)
        self.assertEqual(self.feed.draft, self.feed.published)
        self.provider.assert_not_called()


    @patch.dict(os.environ, {'SERVICE2_AVITO_DEPLOY_SETUP': '1'})
    @patch('pool_service.avito_services_workplace_setup._actor')
    def test_blocked_first_attempt_is_recorded_and_later_deploy_does_not_retry(self, actor):
        actor.return_value = self.owner
        with patch.object(projects, 'prepare', side_effect=projects.ProposalError('source_unavailable')) as attempt:
            first = projects.setup_owner_cycle_once()
            second = projects.setup_owner_cycle_once()
        self.assertEqual(first['state'], 'blocked')
        self.assertEqual(first['code'], 'source_unavailable')
        self.assertEqual(second['state'], 'already_attempted')
        self.assertEqual(second['attempt_state'], 'blocked')
        self.assertEqual(second['results'], [])
        self.assertEqual(attempt.call_count, 1)
        self.assertFalse(self.events().exists())

    @patch.dict(os.environ, {'SERVICE2_AVITO_DEPLOY_SETUP': '1'})
    @patch('pool_service.avito_services_workplace_setup._actor')
    def test_marker_does_not_transfer_old_results_to_changed_account_or_credentials(self, actor):
        actor.return_value = self.owner
        projects.setup_owner_cycle_once()
        self.connection.refresh_from_db()
        metadata = dict(self.connection.settings)
        ChannelConnection.objects.filter(pk=4).update(external_id='foreign-account')
        foreign = projects.setup_owner_cycle_once()
        self.assertEqual(foreign['state'], 'blocked')
        self.assertNotIn('results', foreign)
        ChannelConnection.objects.filter(pk=4).update(external_id=projects.ACCOUNT,
            settings={**metadata, 'generation': 1})
        rotated = projects.setup_owner_cycle_once()
        self.assertEqual(rotated['state'], 'blocked')
        self.assertEqual(rotated['code'], 'scope_changed')
        self.assertNotIn('results', rotated)
        self.assertEqual(self.events().count(), 3)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {'preserve': 'goods'})

    @patch.dict(os.environ, {'SERVICE2_AVITO_DEPLOY_SETUP': '1'})
    @patch('pool_service.avito_services_workplace_setup._actor')
    def test_future_publication_does_not_create_a_second_automatic_cycle(self, actor):
        actor.return_value = self.owner
        projects.setup_owner_cycle_once()
        self.feed.revision = self.feed.published_revision = 19
        self.feed.save(update_fields=['revision', 'published_revision'])
        with patch.object(projects, 'prepare', side_effect=AssertionError('must not prepare again')):
            result = projects.setup_owner_cycle_once()
        self.assertEqual(result['state'], 'already_attempted')
        self.assertEqual(result['source_version_id'], 7)
        self.assertEqual(result['publication_revision'], 18)
        self.assertEqual(self.events().count(), 3)


@override_settings(ALLOWED_HOSTS=['testserver'], AVITO_SERVICES_PUBLIC_ORIGIN='https://service2.aqualine22.ru')
class ImprovementProjectLockTests(ProposalFixture, TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_prepare_requests_append_one_project_per_target(self):
        barrier = Barrier(2)
        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                result = projects.prepare(self.connection, self.owner, expected_revision=18, expected_source=7)
                return sum(row['state'] == 'created' for row in result['results'])
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for _ in range(2)]
            self.assertEqual(sorted(future.result(timeout=20) for future in futures), [0, 3])
        self.assertEqual(self.events().count(), 3)
