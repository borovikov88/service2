from copy import deepcopy
from io import BytesIO
from datetime import timedelta
import hashlib
import json
import time
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from pool_service import avito_input_copy as copy_api
from pool_service import avito_autoload_auto as auto, avito_autoload_report, avito_workspace
from pool_service.avito_services_publication import correlate_report, publication_context
from pool_service.communication_models import AvitoCredential
from pool_service.tests import test_avito_services_publication as publication_tests
from pool_service.tests.test_avito_autoload_report import upload, page

def copy_summary(url='https://copy.avito.st/private.xml?token=PRIVATE'):
    return {'feed_urls': [{'url': url}]}

class Response:
    def __init__(self, raw=b'<Ads/>', status=200, **headers):
        self.raw, self.status, self.offset, self.reads = raw, status, 0, 0
        self.headers = dict(headers)
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, size):
        self.reads += 1
        value = self.raw[self.offset:self.offset + size]
        self.offset += len(value)
        return value
    def close(self):
        pass

class InputCopyTransportTests(SimpleTestCase):
    def test_only_first_party_https_without_userinfo_or_ports_is_eligible(self):
        urls = ['http://copy.avito.st/file.xml', 'https://avito.st.evil.invalid/file.xml',
            'https://avito.st@127.0.0.1/file.xml', 'https://copy.avito.st:444/file.xml',
            'https://127.0.0.1/file.xml', 'https://copy.avito.st/file.xml#fragment']
        for url in urls:
            with self.subTest(url=url), patch('pool_service.avito_services_media._resolve') as resolve:
                with self.assertRaises(copy_api.CopyUnavailable):
                    copy_api._download(copy_summary(url), time.monotonic() + 10)
                resolve.assert_not_called()

    def test_dns_private_or_mixed_addresses_are_rejected_before_connect(self):
        import socket
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('pool_service.avito_services_media.socket.getaddrinfo', return_value=answers), \
             patch('pool_service.avito_services_media._PinnedHTTPS') as transport:
            with self.assertRaises(copy_api.CopyUnavailable) as error:
                copy_api._download(copy_summary(), time.monotonic() + 10)
        self.assertEqual(str(error.exception), 'input_copy_address_unavailable')
        transport.assert_not_called()

    def test_numeric_pinned_tls_uses_original_host_and_no_provider_headers(self):
        response = Response(b'XML bytes', **{'Content-Length': '9'})
        with patch('pool_service.avito_services_media._resolve', return_value='93.184.216.34'), \
             patch('pool_service.avito_services_media._PinnedHTTPS') as transport:
            transport.return_value.getresponse.return_value = response
            self.assertEqual(copy_api._download(copy_summary(), time.monotonic() + 10), b'XML bytes')
            args = transport.call_args.args
            self.assertEqual(args[:3], ('copy.avito.st', 443, '93.184.216.34'))
            headers = transport.return_value.request.call_args.kwargs['headers']
            self.assertEqual(headers['Host'], 'copy.avito.st')
            self.assertEqual(set(headers), {'Host', 'Accept', 'Accept-Encoding', 'User-Agent'})
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('Cookie', headers)
            self.assertEqual(transport.return_value.auto_open, 0)

    def test_redirect_error_and_oversized_response_are_fixed_failures_without_following(self):
        for response, code in [
            (Response(status=302, Location='http://127.0.0.1/PRIVATE'), 'input_copy_redirect_denied'),
            (Response(status=403), 'input_copy_http_403'),
            (Response(**{'Content-Length': str(copy_api.MAX_BYTES + 1)}), 'input_copy_size_invalid'),
            (Response(**{'Content-Encoding': 'gzip'}), 'input_copy_encoding_unsupported'),
        ]:
            with self.subTest(code=code), patch('pool_service.avito_services_media._resolve', return_value='93.184.216.34'), \
                 patch('pool_service.avito_services_media._PinnedHTTPS') as transport:
                transport.return_value.getresponse.return_value = response
                with self.assertRaises(copy_api.CopyUnavailable) as error:
                    copy_api._download(copy_summary(), time.monotonic() + 10)
                self.assertEqual(str(error.exception), code)
                self.assertEqual(transport.return_value.request.call_count, 1)
                self.assertEqual(response.reads, 0)

    def test_expired_budget_does_not_resolve_or_download(self):
        with patch('pool_service.avito_services_media._resolve') as resolve:
            with self.assertRaises(copy_api.CopyUnavailable) as error:
                copy_api._download(copy_summary(), time.monotonic() - 1)
        self.assertEqual(str(error.exception), 'input_copy_budget_exhausted')
        resolve.assert_not_called()

    def test_xml_entities_and_excessive_nesting_are_rejected(self):
        entity = '<!DOCTYPE Ads [<!ENTITY x "PRIVATE">]><Ads>&x;</Ads>'
        for raw in [entity.encode(), entity.encode('utf-16')]:
            with self.assertRaises(copy_api.CopyUnavailable) as error:
                copy_api._xml_pairs(raw, time.monotonic() + 10)
            self.assertEqual(str(error.exception), 'input_copy_xml_forbidden')
        nested = ('<Ads formatVersion="3" target="Avito.ru">' + '<X>' * 25 + '</X>' * 25 + '</Ads>').encode()
        with self.assertRaises(copy_api.CopyUnavailable) as error:
            copy_api._xml_pairs(nested, time.monotonic() + 10)
        self.assertEqual(str(error.exception), 'input_copy_xml_limit')

    def test_non_xml_and_xlsx_are_explicitly_unsupported(self):
        data = BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('xl/workbook.xml', '<workbook/>')
        for raw, kind in [(data.getvalue(), 'xlsx'), (b'PRIVATE not XML', 'other')]:
            with self.assertRaises(copy_api.CopyUnavailable) as error:
                copy_api._xml_pairs(raw, time.monotonic() + 10)
            self.assertTrue(error.exception.unsupported)
            self.assertEqual(error.exception.content_kind, kind)

class InputCopyIntegrationTests(TestCase):
    def setUp(self):
        publication_tests.PublicationResultTests.setUp(self)
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted='synthetic-id', client_secret_encrypted='synthetic-secret')
        with patch.object(auto, 'scheduler_state', return_value={'ready': True}):
            auto.configure(self.connection, self.owner, 'enable')
        from pool_service.avito_services_workplace_setup import _binding
        self.connection.refresh_from_db()
        self.connection.settings['avito_services_workplace_v1'] = {
            **_binding(self.connection), 'actor_id': self.owner.pk,
            'automatic_reports_configured_once': True}
        self.connection.save(update_fields=['settings'])
        from pool_service.avito_services_xml import xml_bytes
        self.raw = xml_bytes(self.version.snapshot)

    def publish_version(self, at):
        return publication_tests.PublicationResultTests.publish_version(self, at)

    def report(self, **options):
        return publication_tests.PublicationResultTests.report(self, **options)

    def proof_report(self, **options):
        report = self.report(**options)
        with patch.object(copy_api, '_download', return_value=self.raw):
            report['input_copy_proof'] = copy_api.fetch_input_proof(copy_summary(), report,
                self.connection, '123', deadline=time.monotonic() + 10)
        report['source_url_sha256'] = ['0' * 64]  # API returned a provider-hosted copy URL.
        return report

    def test_existing_enrollment_is_required_and_optout_is_not_overridden(self):
        self.assertTrue(copy_api.services_input_scope(self.connection, '123'))
        self.connection.settings[auto.KEY]['enabled'] = False
        self.connection.save(update_fields=['settings'])
        with patch.object(copy_api, '_download') as download:
            proof = copy_api.fetch_input_proof(copy_summary(), self.report(), self.connection,
                '123', deadline=time.monotonic() + 10)
        self.assertEqual(proof['code'], 'input_copy_scope_unavailable')
        download.assert_not_called()
        self.assertFalse(copy_api.services_input_scope(self.connection, '999'))

    def test_private_xml_proof_is_bound_and_has_no_raw_url_or_xml_text(self):
        report = self.proof_report()
        proof = report['input_copy_proof']
        self.assertEqual(proof['state'], 'verified_xml')
        self.assertEqual(proof['file_sha256'], self.publication.file_sha256)
        self.assertEqual(proof['identity_count'], 2)
        self.assertNotIn('PRIVATE', json.dumps(proof))
        self.assertNotIn('<Ads', json.dumps(proof))
        attempt = correlate_report(self.connection, report)
        self.assertEqual(attempt.state, 'source_copy_confirmed')
        self.assertTrue(attempt.source_confirmed)
        self.assertFalse(attempt.content_verified)

    def test_copy_must_match_the_whole_report_and_reject_duplicate_identities(self):
        mismatched = self.raw.replace(b'<AvitoId>222</AvitoId>', b'<AvitoId>333</AvitoId>')
        duplicate = self.raw.replace(b'<Id>222</Id>', b'<Id>111</Id>')
        for raw in (mismatched, duplicate):
            with patch.object(copy_api, '_download', return_value=raw):
                proof = copy_api.fetch_input_proof(copy_summary(), self.report(), self.connection,
                    '123', deadline=time.monotonic() + 10)
            self.assertEqual(proof['code'], 'input_copy_identity_mismatch')
            self.assertNotIn('file_sha256', proof)

    def test_copy_scope_is_rechecked_after_download(self):
        def moved(*args):
            type(self.connection).objects.filter(pk=self.connection.pk).update(external_id='999')
            return self.raw
        with patch.object(copy_api, '_download', side_effect=moved):
            proof = copy_api.fetch_input_proof(copy_summary(), self.report(), self.connection,
                '123', deadline=time.monotonic() + 10)
        self.assertEqual(proof['code'], 'input_copy_scope_unavailable')

    def test_same_bytes_in_two_eligible_versions_are_ambiguous(self):
        self.feed.revision += 1
        self.feed.published_revision = self.feed.revision
        self.feed.save()
        self.publish_version(timezone.now() - timedelta(minutes=70))
        report = self.proof_report()
        attempt = correlate_report(self.connection, report)
        self.assertEqual(attempt.state, 'source_copy_ambiguous')
        self.assertFalse(attempt.source_confirmed)
        self.assertFalse(publication_context(self.feed)['source_confirmed'])

    def test_old_copy_cannot_confirm_later_publication_and_foreign_proof_is_ignored(self):
        report = self.proof_report(when=self.version.created_at)
        attempt = correlate_report(self.connection, report)
        self.assertEqual(attempt.state, 'report_predates_publication')
        self.assertFalse(attempt.source_confirmed)
        report = self.proof_report(upload_id='902')
        report['input_copy_proof']['account_id'] = '999'
        attempt = correlate_report(self.connection, report)
        self.assertFalse(attempt.source_confirmed)

    def test_sdk_defaults_do_not_download_and_optional_failure_preserves_report(self):
        header = upload(status='success')
        rows = [{'ad_id': str(i), 'avito_id': i, 'avito_status': 'active',
            'avito_date_end': None, 'section': {'slug': 'active', 'title': 'Active'},
            'messages': []} for i in (111, 222)]
        responses = [header, page(rows, total=2), header]
        with patch.object(avito_workspace, '_get', side_effect=responses), \
             patch.object(avito_workspace, 'enforce_rate'), patch.object(copy_api, 'fetch_input_proof') as fetch:
            data = avito_autoload_report.fetch_report('synthetic-token', '123', kind='current')
        self.assertTrue(data['complete'])
        fetch.assert_not_called()
        with patch.object(avito_workspace, '_get', side_effect=responses), \
             patch.object(avito_workspace, 'enforce_rate'), \
             patch.object(copy_api, 'fetch_input_proof', side_effect=RuntimeError('PRIVATE')):
            data = avito_autoload_report.fetch_report('synthetic-token', '123', kind='current',
                include_input_proof=True, input_proof_connection=self.connection)
        self.assertTrue(data['complete'])
        self.assertEqual(data['input_copy_proof']['code'], 'input_copy_check_failed')
        self.assertNotIn('PRIVATE', json.dumps(data))

    def test_sdk_rechecks_alias_after_copy_without_replacing_good_report(self):
        header = upload(status='success')
        rows = [{'ad_id': str(i), 'avito_id': i, 'avito_status': 'active',
            'avito_date_end': None, 'section': {'slug': 'active', 'title': 'Active'},
            'messages': []} for i in (111, 222)]
        changed = deepcopy(header)
        changed['upload_id'] += 1
        with patch.object(avito_workspace, '_get', side_effect=[header, page(rows, total=2), header, changed]), \
             patch.object(avito_workspace, 'enforce_rate'), patch.object(copy_api, 'fetch_input_proof',
                 return_value={'state': 'verified_xml', 'code': 'input_copy_xml_verified'}):
            data = avito_autoload_report.fetch_report('synthetic-token', '123', kind='current',
                include_input_proof=True, input_proof_connection=self.connection)
        self.assertTrue(data['complete'])
        self.assertEqual(data['input_copy_proof']['code'], 'input_copy_report_changed')

    def test_services_audit_skips_foreign_legacy_goods_ids_and_physical_stock(self):
        from pool_service import avito_readonly_audit as audit
        with patch.object(audit, 'binding', return_value=('stable',)), \
             patch.object(audit, 'authorized_account_id', return_value='123'), \
             patch.object(audit, 'access_token', return_value='synthetic-token'), \
             patch.object(audit, '_provider_root', return_value='https://api.avito.ru'), \
             patch.object(audit, 'fetch_profile_source', return_value=({'complete': True}, 'same')), \
             patch.object(audit.avito_advance, 'fetch'), \
             patch.object(audit.avito_autoload_report, 'fetch_report', return_value=self.report()), \
             patch.object(audit, 'report_summary', return_value={'complete': True}), \
             patch.object(audit.avito_status_monitor, 'full_scan', return_value=(
                 {'111': {'status': 'active', 'title': 'Synthetic one'},
                  '222': {'status': 'active', 'title': 'Synthetic two'}}, 1)), \
             patch.object(audit.avito_inventory, 'build_inventory') as goods_inventory, \
             patch.object(audit, 'fetch_detail') as detail, \
             patch.object(audit, 'monitoring_state', return_value={}), \
             patch.object(audit, 'cached_configuration_state', return_value={}), \
             patch('pool_service.avito_runtime.checks', return_value=[]), \
             patch('pool_service.avito_services_workplace_setup.webhook_evidence', return_value={}), \
             patch('pool_service.avito_services_workplace_setup.storage_probe_evidence', return_value={}):
            result = audit.collect_connection(self.connection, ['800', '900'], {'goods': []})
        self.assertNotIn('error', result)
        detail.assert_not_called()
        goods_inventory.assert_not_called()
        self.assertEqual(result['items'], [])
        self.assertEqual(result['active_source_audit'], {'scope': 'services',
            'state': 'not_applicable', 'reason': 'services_have_no_physical_stock'})
        self.assertEqual(result['services_coverage']['active_covered'], 2)
        self.assertEqual(result['services_coverage']['active_missing'], 0)
        self.assertEqual(result['services_coverage']['skipped_out_of_scope'], 2)

    def test_services_item_link_uses_own_mapping_and_unknown_provider_scan_stays_unknown(self):
        from pool_service.avito_readonly_audit import services_item_link, services_coverage
        detail = {'status': 'active', 'autoload_item_id': '111'}
        linked = services_item_link('111', detail, self.feed)
        self.assertEqual(linked['link_state'], 'services_feed_match')
        self.assertEqual(linked['xml_matches'], [])
        unknown = services_coverage(self.feed, None, requested=1, checked=0, skipped=0)
        self.assertFalse(unknown['complete'])
        self.assertIsNone(unknown['active_count'])
        self.assertIsNone(unknown['active_missing'])
