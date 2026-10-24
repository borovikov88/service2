from copy import deepcopy
from unittest.mock import patch
from xml.etree import ElementTree as ET

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.avito_services_feed import images, parse_export, verify_inventory, workbook_bytes
from pool_service.avito_services_history import require_identities
from pool_service.avito_services_xml import xml_bytes
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes


class FeedVersionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Version test')
        self.owner = User.objects.create_user('version-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='avito', name='Version Avito')
        self.connection = ChannelConnection.objects.create(channel=channel, name='Services', external_id='123')
        self.url = reverse('avito_services_feed', args=[self.connection.pk])
        self.publish_url = reverse('avito_services_feed_publish', args=[self.connection.pk])
        self.client.force_login(self.owner)

    def import_rows(self, identifiers=('111',)):
        response = self.client.post(self.url, {'action': 'import',
            'revision': getattr(AvitoServicesFeed.objects.filter(connection=self.connection).first(), 'revision', ''),
            'file': SimpleUploadedFile('123_export.xlsx', export_bytes(identifiers))})
        self.assertEqual(response.status_code, 302)
        return AvitoServicesFeed.objects.get(connection=self.connection)

    def feed(self, identifiers=('111',)):
        data = parse_export(export_bytes(identifiers))
        return AvitoServicesFeed.objects.create(connection=self.connection, account_id='123',
            organization_id_at_import=self.org.pk, draft=data)

    def edit(self, feed, title='Новый текст', photos='https://example.org/image.jpg'):
        return self.client.post(reverse('avito_services_feed_edit', args=[self.connection.pk, '111']),
            {'revision': feed.revision, 'title': title, 'description': 'Описание после изменения',
             'image_urls': photos, 'reason': 'Проверка текста услуги'})

    @patch('pool_service.avito_workspace.fetch_item_detail')
    @patch('pool_service.avito_workspace._get')
    @patch('pool_service.communication_avito.access_token', return_value='test')
    @patch('pool_service.communication_avito.authorized_account_id', return_value='123')
    def test_one_of_eleven_archived_keeps_ten_active_and_all_draft_identities(self, account, token, get, detail):
        identifiers = tuple(str(i) for i in range(111, 122))
        feed = self.feed(identifiers)
        feed.published = deepcopy(feed.draft); feed.enabled = True; feed.save()
        get.return_value = {'resources': [{'id': i} for i in range(111, 121)]}
        detail.side_effect = lambda access, owner, item: {
            'status': 'old' if item == '121' else 'active', 'autoload_item_id': item,
            'finish_time': '2026-01-01T00:00:00+07:00'}
        self.assertEqual(self.edit(feed).status_code, 302)
        feed.refresh_from_db()
        response = self.client.post(self.publish_url, {'revision': feed.revision})
        self.assertEqual(response.status_code, 302)
        feed.refresh_from_db()
        self.assertEqual(len(feed.draft[0]['rows']), 11)
        self.assertEqual({r['AvitoId'] for r in feed.published[0]['rows']}, set(identifiers[:-1]))
        self.assertEqual(feed.draft[0]['rows'][0]['Title'], 'Новый текст')
        archived = feed.latest_observations[-1]
        self.assertEqual(archived['status'], 'old'); self.assertFalse(archived['included'])
        self.assertIn('не подтверждена', archived['status_label'])
        publication = feed.versions.get(action='publish')
        self.assertEqual(publication.source_version.action, 'edit')
        self.assertEqual(len(publication.file_digest), 64)
        public = reverse('avito_services_feed_public', args=[feed.public_key])
        before = (feed.versions.count(), feed.last_checked_at)
        xml = Client().get(public)
        self.assertEqual(xml.status_code, 200)
        self.assertEqual({a.findtext('AvitoId') for a in ET.fromstring(xml.content)}, set(identifiers[:-1]))
        feed.refresh_from_db()
        self.assertEqual((feed.versions.count(), feed.last_checked_at), before)
        page = self.client.get(self.url)
        self.assertContains(page, 'применение изменений пока не подтверждены')
        self.assertContains(page, 'Неактивное объявление не отправляется повторно')
        self.assertEqual(get.call_count, 1)

    @patch('pool_service.avito_workspace.fetch_item_detail')
    @patch('pool_service.avito_workspace._get', return_value={'resources': [{'id': 111}]})
    @patch('pool_service.communication_avito.access_token', return_value='test')
    @patch('pool_service.communication_avito.authorized_account_id', return_value='123')
    def test_removed_rejected_blocked_excluded_unknown_or_unowned_blocks(self, account, token, get, detail):
        data = parse_export(export_bytes(('111', '222')))
        for status in ('removed', 'rejected', 'blocked'):
            detail.side_effect = lambda access, owner, item: {
                'status': 'active' if item == '111' else status, 'autoload_item_id': item}
            checked = verify_inventory(self.connection, data)
            self.assertEqual([r['AvitoId'] for r in checked['sheets'][0]['rows']], ['111'])
            self.assertEqual(checked['observations'][1]['status'], status)
        detail.side_effect = None
        detail.return_value = {'status': 'unrecognised', 'autoload_item_id': '111'}
        with self.assertRaises(AvitoError): verify_inventory(self.connection, data)
        detail.side_effect = AvitoError('provider_item_unavailable')
        with self.assertRaises(AvitoError): verify_inventory(self.connection, data)

    @patch('pool_service.avito_workspace.fetch_item_detail')
    @patch('pool_service.avito_workspace._get')
    @patch('pool_service.communication_avito.access_token', return_value='test')
    @patch('pool_service.communication_avito.authorized_account_id', return_value='123')
    def test_missing_active_duplicate_page_and_status_race_fail_closed(self, account, token, get, detail):
        data = parse_export(export_bytes())
        get.return_value = {'resources': [{'id': 111}, {'id': 222}]}
        with self.assertRaises(ValueError): verify_inventory(self.connection, data)
        get.return_value = {'resources': [{'id': 111}, {'id': 111}]}
        with self.assertRaises(AvitoError): verify_inventory(self.connection, data)
        get.return_value = {'resources': [{'id': True}]}
        with self.assertRaises(AvitoError): verify_inventory(self.connection, data)
        get.return_value = {'resources': [{'id': 111}]}
        detail.return_value = {'status': 'old', 'autoload_item_id': '111'}
        with self.assertRaises(ValueError): verify_inventory(self.connection, data)

    def test_import_cannot_drop_known_rows_or_reassign_either_side_of_identity(self):
        feed = self.import_rows(('111', '222'))
        dropped = self.client.post(self.url, {'action': 'import', 'revision': feed.revision,
            'file': SimpleUploadedFile('123_export.xlsx', export_bytes(('111',)))})
        self.assertContains(dropped, 'Нельзя исключать известные объявления')
        for change in ({'Id': '222'}, {'AvitoId': '333'}):
            candidate = deepcopy(feed.draft)
            candidate[0]['rows'][0].update(change)
            with self.assertRaises(ValueError): require_identities(feed, candidate)
        feed.refresh_from_db()
        self.assertEqual(len(feed.draft[0]['rows']), 2)
        self.assertEqual(feed.versions.count(), 1)

    def test_photo_limit_rejects_import_edit_and_new_xml_but_keeps_legacy_public_file(self):
        ten = ' | '.join(f'https://example.org/{i}.jpg' for i in range(10))
        eleven = ten + ' | https://example.org/extra.jpg'
        self.assertEqual(images(ten), ten)
        with self.assertRaises(ValueError): images(eleven)
        data = parse_export(export_bytes()); data[0]['rows'][0]['ImageUrls'] = eleven
        response = self.client.post(self.url, {'action': 'import',
            'file': SimpleUploadedFile('123_export.xlsx', workbook_bytes(data))})
        self.assertContains(response, 'от 1 до 10')
        self.assertFalse(AvitoServicesFeed.objects.exists())
        feed = self.feed()
        feed.published = data; feed.enabled = True; feed.save()
        response = self.edit(feed, photos=eleven)
        self.assertContains(response, 'от 1 до 10')
        self.assertEqual(feed.versions.count(), 0)
        with self.assertRaises(ValueError): xml_bytes(data)
        public = reverse('avito_services_feed_public', args=[feed.public_key])
        response = Client().get(public)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ET.fromstring(response.content).findall('Ad/Images/Image')), 11)
        with patch('pool_service.avito_services_feed.verify_inventory',
                   return_value={'sheets': data, 'observations': []}):
            response = self.client.post(self.publish_url, {'revision': feed.revision})
        self.assertContains(response, 'от 1 до 10', status_code=400)
        feed.refresh_from_db()
        self.assertEqual(feed.published_photo_limit, 30)
        self.assertEqual(feed.published, data)

    def test_history_before_after_author_reason_and_restore_is_new_draft(self):
        feed = self.import_rows()
        imported = feed.versions.get(action='import')
        feed.published = deepcopy(feed.draft); feed.enabled = True; feed.save()
        self.assertEqual(self.edit(feed).status_code, 302)
        feed.refresh_from_db()
        change = feed.versions.get(action='edit')
        title = next(c for c in change.changes if c['field'] == 'Title')
        self.assertEqual((title['before'], title['after']), ('Тест услуги', 'Новый текст'))
        self.assertEqual(change.actor, self.owner)
        self.assertEqual(change.reason, 'Проверка текста услуги')
        old_revision = feed.revision
        response = self.client.post(self.url, {'action': 'restore', 'revision': feed.revision,
                                               'version_id': imported.pk, 'reason': 'Вернуть исходный текст'})
        self.assertEqual(response.status_code, 302)
        feed.refresh_from_db(); imported.refresh_from_db()
        self.assertEqual(feed.revision, old_revision + 1)
        self.assertEqual(feed.draft, imported.snapshot)
        self.assertEqual(feed.published[0]['rows'][0]['Title'], 'Тест услуги')
        restored = feed.versions.get(action='restore')
        self.assertEqual(restored.source_version, imported)
        self.assertEqual(restored.reason, 'Вернуть исходный текст')
        imported.reason = 'tamper'
        with self.assertRaises(ValidationError): imported.save()
        selected = self.client.get(self.url, {'history_version': change.pk})
        self.assertContains(selected, 'Проверка текста услуги')
        self.assertContains(selected, 'Новый текст')

    def test_restore_preserves_later_added_listing_and_rejects_stale_version(self):
        feed = self.import_rows()
        first = feed.versions.get(action='import')
        stale = feed.revision
        feed = self.import_rows(('111', '222'))
        response = self.client.post(self.url, {'action': 'restore', 'revision': stale, 'version_id': first.pk})
        self.assertContains(response, 'Данные изменились')
        response = self.client.post(self.url, {'action': 'restore', 'revision': feed.revision, 'version_id': first.pk})
        self.assertEqual(response.status_code, 302)
        feed.refresh_from_db()
        self.assertEqual({r['AvitoId'] for r in feed.draft[0]['rows']}, {'111', '222'})

    def test_foreign_history_is_inaccessible_and_restore_has_csrf(self):
        feed = self.import_rows()
        other_org = Organization.objects.create(name='Other version test')
        channel = CommunicationChannel.objects.create(organization=other_org, kind='avito', name='Other')
        connection = ChannelConnection.objects.create(channel=channel, name='Other', external_id='789')
        foreign = AvitoServicesFeed.objects.create(connection=connection, account_id='789',
                                                   organization_id_at_import=other_org.pk)
        version = AvitoServicesFeedVersion.objects.create(feed=foreign, revision=1, action='baseline')
        self.assertEqual(self.client.get(self.url, {'history_version': version.pk}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {'history_version': 'bad'}).status_code, 404)
        payload = {'action': 'restore', 'revision': feed.revision, 'version_id': version.pk}
        self.assertEqual(self.client.post(self.url, payload).status_code, 404)
        self.assertEqual(feed.versions.count(), 1)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.url, payload).status_code, 403)

    @patch('pool_service.avito_workspace.fetch_item_detail')
    @patch('pool_service.avito_workspace._get', return_value={'resources': [{'id': 111}]})
    @patch('pool_service.communication_avito.access_token', return_value='test')
    @patch('pool_service.communication_avito.authorized_account_id', return_value='123')
    def test_observation_saves_real_deadline_or_missing_without_claiming_application(self, account, token, get, detail):
        feed = self.feed()
        for value, expected in (('2026-01-01T00:00:00+07:00', '2026-01-01T00:00:00+07:00'),
                                (None, None), ('0', None), ('not a date', None)):
            detail.return_value = {'status': 'active', 'autoload_item_id': '111', 'finish_time': value}
            response = self.client.post(self.url, {'action': 'verify', 'revision': feed.revision})
            self.assertEqual(response.status_code, 302)
            feed.refresh_from_db()
            row = feed.latest_observations[0]
            self.assertEqual(row['finish_time'], expected)
            self.assertEqual(row['finish_time_source'], 'avito_item_detail.finish_time' if expected else '')
            self.assertEqual(row['status'], 'active')
            self.assertEqual(feed.published, [])
            self.assertFalse(feed.enabled)
        verification = feed.versions.filter(action='verify').first()
        self.assertEqual(verification.changes, [])
        self.assertIn('не подтверждены', verification.reason)
        previous = feed.latest_observations
        detail.side_effect = AvitoError('provider_http_404')
        count = feed.versions.count()
        self.assertEqual(self.client.post(self.url, {'action': 'verify', 'revision': feed.revision}).status_code, 400)
        feed.refresh_from_db()
        self.assertEqual(feed.latest_observations, previous)
        self.assertEqual(feed.versions.count(), count)

    @patch('pool_service.avito_workspace.fetch_item_detail', return_value={'status': 'old', 'autoload_item_id': '111'})
    @patch('pool_service.avito_workspace._get', return_value={'resources': []})
    @patch('pool_service.communication_avito.access_token', return_value='test')
    @patch('pool_service.communication_avito.authorized_account_id', return_value='123')
    def test_all_inactive_cannot_publish_empty_file(self, account, token, get, detail):
        feed = self.feed()
        feed.published = deepcopy(feed.draft); feed.enabled = True; feed.save()
        result = self.client.post(self.publish_url, {'revision': feed.revision})
        self.assertContains(result, 'Нет активных объявлений', status_code=400)
        feed.refresh_from_db()
        self.assertEqual(feed.published, feed.draft)
        self.assertEqual(feed.versions.count(), 0)

    @patch('pool_service.avito_services_feed.verify_inventory')
    def test_post_provider_revision_and_account_races_do_not_publish(self, verify):
        feed = self.import_rows()
        def concurrent_edit(connection, sheets):
            AvitoServicesFeed.objects.filter(pk=feed.pk).update(revision=feed.revision + 1)
            return {'sheets': sheets, 'observations': []}
        verify.side_effect = concurrent_edit
        self.assertEqual(self.client.post(self.publish_url, {'revision': feed.revision}).status_code, 409)
        feed.refresh_from_db()
        def concurrent_account(connection, sheets):
            ChannelConnection.objects.filter(pk=connection.pk).update(external_id='456')
            return {'sheets': sheets, 'observations': []}
        verify.side_effect = concurrent_account
        self.assertEqual(self.client.post(self.publish_url, {'revision': feed.revision}).status_code, 409)
        feed.refresh_from_db()
        self.assertEqual(feed.published, [])
        self.assertEqual(feed.versions.count(), 1)

    def test_history_pagination_and_legacy_baseline_are_explicit(self):
        feed = self.feed()
        feed.published = deepcopy(feed.draft); feed.enabled = True; feed.save()
        self.assertEqual(self.edit(feed).status_code, 302)
        feed.refresh_from_db()
        baseline = feed.versions.get(action='baseline')
        self.assertIsNone(baseline.actor)
        self.assertIn('не восстановлены', baseline.reason)
        self.assertEqual(feed.versions.filter(action='legacy_published').count(), 1)
        for _ in range(21):
            AvitoServicesFeedVersion.objects.create(feed=feed, revision=feed.revision, action='verify')
        response = self.client.get(self.url)
        self.assertEqual(len(response.context['history']), 20)
        self.assertTrue(response.context['history'].has_next())
        self.assertEqual(feed.versions.count(), 24)


    def test_legacy_identity_conflict_returns_form_error_without_changing_snapshots(self):
        feed = self.feed()
        feed.published = deepcopy(feed.draft)
        feed.published[0]['rows'][0]['Id'] = 'legacy-different-id'
        feed.enabled = True
        feed.save()
        before = (deepcopy(feed.draft), deepcopy(feed.published), feed.revision)
        response = self.edit(feed)
        self.assertContains(response, 'Нельзя менять соответствие Id и AvitoId', status_code=400)
        self.assertTrue(response.context['form'].non_field_errors())
        feed.refresh_from_db()
        self.assertEqual((feed.draft, feed.published, feed.revision), before)
        self.assertEqual(feed.versions.count(), 0)

    def test_invalid_legacy_xml_rolls_back_partial_baseline_and_returns_form_error(self):
        feed = self.feed()
        feed.published = deepcopy(feed.draft)
        feed.published[0]['rows'][0]['PriceList'] = 'broken'
        feed.enabled = True
        feed.save()
        before = (deepcopy(feed.draft), deepcopy(feed.published), feed.revision)
        response = self.edit(feed)
        self.assertContains(response, 'Некорректная строка прайс-листа XML', status_code=400)
        self.assertTrue(response.context['form'].non_field_errors())
        feed.refresh_from_db()
        self.assertEqual((feed.draft, feed.published, feed.revision), before)
        self.assertTrue(feed.enabled)
        self.assertEqual(feed.versions.count(), 0)
