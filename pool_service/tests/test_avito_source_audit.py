from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from pool_service import avito_source_audit as source
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import ChannelConnection, CommunicationChannel
from pool_service.models import Notification, Organization, OrganizationAccess


def feed(rows):
    return ('<Ads formatVersion="3" target="Avito.ru">' + rows + '</Ads>').encode()


def ad(identifier="1_2", extra=""):
    return f'<Ad><Id>{identifier}</Id><Title>Насос</Title><Description>&lt;p&gt;Описание&lt;/p&gt;</Description><Images><Image url="https://example.com/a.jpg"/></Images>{extra}</Ad>'


class AvitoSourceReportTests(SimpleTestCase):
    def test_full_feed_counts_duplicates_and_missing_fields_without_raw_payload(self):
        report, ids = source.build_report(feed(ad() + ad() + '<Ad><Description>&lt;p&gt; &lt;/p&gt;</Description><ContactPhone>private</ContactPhone></Ad>'))
        self.assertEqual((report["total"], report["unique_ids"], report["duplicate_id_count"], report["duplicate_rows"]), (3, 1, 1, 1))
        self.assertEqual((report["missing_title"], report["missing_description"], report["missing_images"], report["missing_tnved"]), (1, 1, 1, 3))
        self.assertEqual(ids, {"1_2"})
        self.assertNotIn("private", str(report))
        self.assertTrue(report["complete"])

    def test_tnved_is_field_presence_check_and_samples_are_bounded(self):
        report, _ = source.build_report(feed("".join(ad(str(i), "<TNVEDCode>1234567890</TNVEDCode>") for i in range(40))))
        self.assertEqual((report["total"], report["missing_tnved"], len(report["samples"])), (40, 0, 0))
        report, _ = source.build_report(feed("".join(ad(str(i)) for i in range(40))))
        self.assertEqual((report["missing_tnved"], len(report["samples"])), (40, 25))

    def test_empty_feed_is_valid_but_wrong_structure_entities_encoding_and_oversize_fail(self):
        self.assertEqual(source.build_report(feed(""))[0]["total"], 0)
        invalid = [
            b'<html/>', b'<Ads><Unexpected/></Ads>', b'<Ads>',
            b'<!DOCTYPE Ads [<!ENTITY secret SYSTEM "file:///etc/passwd">]><Ads/>',
            '<?xml version="1.0" encoding="UTF-16"?><Ads/>'.encode("utf-16"),
        ]
        for payload in invalid:
            with self.subTest(payload=payload[:20]), self.assertRaises(AvitoError):
                source.build_report(payload)
        with patch.object(source, "MAX_BYTES", 5), self.assertRaises(AvitoError):
            source.build_report(feed(""))
        with patch.object(source, "MAX_ITEMS", 1), self.assertRaises(AvitoError):
            source.build_report(feed(ad() + ad("2_3")))

    def test_ambiguous_and_missing_stock_is_unknown_and_unrelated_zero_is_excluded(self):
        payload = b'<items><item><id>1_2</id><stock>0</stock></item><item><id>1_2</id><stock>5</stock></item><item><id>2_3</id><stock>-1</stock></item><item><id>9_9</id><stock>0</stock></item></items>'
        report = source.stock_report(payload, {"1_2", "2_3", "3_4"})
        self.assertEqual((report["matched"], report["unknown"], report["nonpositive_count"]), (1, 2, 1))
        self.assertEqual(report["nonpositive_ids"], ["2_3"])
        self.assertEqual(report["invalid_rows"], 1)

    def test_stock_failure_keeps_complete_feed_but_no_fabricated_stock_totals(self):
        with patch.object(source, "_read_xml", side_effect=[feed(ad()), AvitoError("secret-payload")]) as read:
            report = source.fetch_report()
        self.assertEqual([call.args[0] for call in read.call_args_list], [source.FEED_PATH, source.STOCK_PATH])
        self.assertTrue(report["complete"])
        self.assertFalse(report["stock"]["complete"])
        self.assertNotIn("nonpositive_count", report["stock"])
        self.assertNotIn("secret", str(report))


class AvitoSourceTransportTests(SimpleTestCase):
    def public_dns(self):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    def response(self, status=200, headers=None, chunks=None):
        response = MagicMock(status=status)
        headers = headers or {}
        response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
        response.read1.side_effect = chunks or [feed(""), b""]
        connection = MagicMock()
        connection.getresponse.return_value = response
        return connection, response

    def test_only_fixed_paths_and_public_pinned_addresses_are_used(self):
        connection, _ = self.response()
        with patch.object(source.socket, "getaddrinfo", return_value=self.public_dns()), patch.object(source, "_PinnedHTTPS", return_value=connection) as https:
            self.assertEqual(source._read_xml(source.FEED_PATH), feed(""))
        self.assertEqual(https.call_args.args[0], "93.184.216.34")
        self.assertEqual(connection.request.call_args.args, ("GET", source.FEED_PATH))
        self.assertNotIn("Authorization", connection.request.call_args.kwargs["headers"])
        connection.close.assert_called_once()
        with patch.object(source.socket, "getaddrinfo") as dns, self.assertRaises(AvitoError):
            source._read_xml("/index.php?route=extension/module/avitofeed")
        dns.assert_not_called()

    def test_private_dns_rejected_before_connect(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(address=address), patch.object(source.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]), patch.object(source, "_PinnedHTTPS") as https, self.assertRaises(AvitoError):
                source._read_xml(source.FEED_PATH)
            https.assert_not_called()

    def test_redirect_error_and_compressed_payloads_are_not_read(self):
        for status, headers in ((302, {"Location": "http://127.0.0.1"}), (404, {}), (200, {"Content-Encoding": "gzip"})):
            connection, response = self.response(status, headers)
            with self.subTest(status=status, headers=headers), patch.object(source.socket, "getaddrinfo", return_value=self.public_dns()), patch.object(source, "_PinnedHTTPS", return_value=connection), self.assertRaises(AvitoError):
                source._read_xml(source.FEED_PATH)
            response.read1.assert_not_called()
            connection.close.assert_called_once()

    def test_read_limits_truncation_and_deadline_are_failures(self):
        for headers, chunks in (({"Content-Length": "100"}, None), ({}, [b"x" * 51]), ({"Content-Length": "10"}, [b"x", b""])):
            connection, _ = self.response(headers=headers, chunks=chunks)
            with self.subTest(headers=headers), patch.object(source, "MAX_BYTES", 50), patch.object(source.socket, "getaddrinfo", return_value=self.public_dns()), patch.object(source, "_PinnedHTTPS", return_value=connection), self.assertRaises(AvitoError):
                source._read_xml(source.FEED_PATH)
        connection, _ = self.response()
        with patch.object(source.time, "monotonic", side_effect=[0, 0, 0, 21]), patch.object(source.socket, "getaddrinfo", return_value=self.public_dns()), patch.object(source, "_PinnedHTTPS", return_value=connection), self.assertRaises(AvitoError):
            source._read_xml(source.FEED_PATH)

    def test_tls_checks_original_host_and_socket_is_closed_on_tls_error(self):
        transport = MagicMock()
        with patch.object(source.socket, "create_connection", return_value=transport) as connect:
            connection = source._PinnedHTTPS("93.184.216.34", 5)
            context = MagicMock()
            connection._context = context
            connection.connect()
            context.wrap_socket.assert_called_once_with(transport, server_hostname=source.HOST)
            connect.assert_called_once_with(("93.184.216.34", 443), 5)
            context.wrap_socket.side_effect = OSError("tls failure")
            with self.assertRaises(OSError):
                connection.connect()
            transport.close.assert_called_once()


class AvitoSourceViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Source audit test")
        self.owner = User.objects.create_user("source-owner")
        self.manager = User.objects.create_user("source-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(channel=channel, external_id="123", name="Goods")
        self.refresh = reverse("avito_refresh_data", args=[self.connection.pk])
        self.client.force_login(self.owner)

    def state(self):
        self.connection.refresh_from_db()
        return self.connection.settings["avito_workspace"]["sections"]["source_audit"]

    def test_explicit_source_refresh_without_credentials_and_get_has_no_network(self):
        report, _ = source.build_report(feed(ad()))
        report["stock"] = {"complete": False, "detail": "Не проверено"}
        with patch.object(source, "fetch_report", return_value=report) as read, patch("pool_service.avito_management.access_token") as token:
            self.assertEqual(self.client.post(self.refresh, {"section": "source_audit"}).status_code, 302)
            response = self.client.get(reverse("avito_dashboard"))
        read.assert_called_once_with()
        token.assert_not_called()
        self.assertContains(response, "Проверить XML магазина")
        self.assertContains(response, "Связь XML с выбранным аккаунтом Авито ещё не подтверждена")
        self.assertContains(response, "Наличие не проверено")
        self.assertEqual(self.state()["data"]["total"], 1)
        self.assertFalse(Notification.objects.exists())

    def test_failure_keeps_prior_data_time_and_sanitizes_errors(self):
        old = {"status": "ok", "success_at": "2026-10-01T10:00:00+00:00", "data": {"complete": True, "total": 3}}
        self.connection.settings = {"avito_workspace": {"account_id": "123", "sections": {"source_audit": old}}}
        self.connection.save(update_fields=["settings"])
        with patch.object(source, "fetch_report", side_effect=AvitoError("PRIVATE XML BODY")):
            self.client.post(self.refresh, {"section": "source_audit"})
        state = self.state()
        self.assertEqual(state["data"], old["data"])
        self.assertEqual(state["success_at"], old["success_at"])
        self.assertTrue(state["stale"])
        self.assertNotIn("PRIVATE", str(state))

    def test_source_refresh_is_post_csrf_and_owner_scoped(self):
        with patch.object(source, "fetch_report") as read:
            self.assertEqual(self.client.get(self.refresh).status_code, 405)
            csrf = Client(enforce_csrf_checks=True)
            csrf.force_login(self.owner)
            self.assertEqual(csrf.post(self.refresh, {"section": "source_audit"}).status_code, 403)
            self.client.force_login(self.manager)
            self.assertEqual(self.client.post(self.refresh, {"section": "source_audit"}).status_code, 403)
            foreign = Organization.objects.create(name="Foreign source")
            channel = CommunicationChannel.objects.create(organization=foreign, kind="avito", name="Foreign")
            connection = ChannelConnection.objects.create(channel=channel, external_id="999", name="Foreign")
            self.client.force_login(self.owner)
            self.assertEqual(self.client.post(reverse("avito_refresh_data", args=[connection.pk]), {"section": "source_audit"}).status_code, 404)
        read.assert_not_called()

    def test_concurrent_account_change_and_active_lease_cannot_save(self):
        def changed():
            ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="999")
            return {"complete": True, "total": 7}
        with patch.object(source, "fetch_report", side_effect=changed):
            self.client.post(self.refresh, {"section": "source_audit"})
        self.connection.refresh_from_db()
        self.assertNotIn("source_audit", self.connection.settings.get("avito_workspace", {}).get("sections", {}))
        self.assertNotIn("avito_workspace_refresh_lease", self.connection.settings)

    def test_general_refresh_does_not_fetch_source(self):
        with patch.object(source, "fetch_report") as read, patch("pool_service.avito_management.access_token", return_value="test"), patch("pool_service.avito_workspace._get", return_value={"id": 123}), patch("pool_service.avito_workspace.fetch_section", return_value={}):
            self.client.post(self.refresh, {"section": "all"})
        read.assert_not_called()
