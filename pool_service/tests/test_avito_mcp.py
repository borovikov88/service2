"""Synthetic OAuth/resource/cache isolation tests. No live provider requests."""
import base64
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.avito_mcp_auth import (
    AVITO_READ_SCOPE, _binding_scope, _organization_scope, resource_url,
)
from pool_service.avito_mcp_policy import can_access_avito_mcp
from pool_service.avito_mcp_reader import CACHE_SCOPE_KEY, TOOL_NAMES, cache_binding, read_tool
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationAccess, CommunicationChannel
from pool_service.finance_mcp_auth import CHATGPT_CLIENT_ID_METADATA_URL
from pool_service.models import (
    FinanceMcpAccessToken, FinanceMcpAuditEvent, FinanceMcpAuthorizationCode,
    FinanceMcpClient, FinanceMcpGrant, FinanceMcpPrincipal,
    FinanceMcpPrincipalOrganization, Organization, OrganizationAccess,
)

ISSUER = "https://service2.example"
RESOURCE = ISSUER + "/mcp/avito"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


@override_settings(
    SITE_URL=ISSUER, AVITO_SERVICES_PUBLIC_ORIGIN=ISSUER,
    ADVISOR_AVITO_MCP_ENABLED=True, ADVISOR_AVITO_MCP_RESOURCE_URL=RESOURCE,
    ADVISOR_AVITO_MCP_ALLOWED_ORIGINS={"https://chatgpt.com"},
)
class AvitoMcpTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Synthetic services organization")
        self.other_org = Organization.objects.create(name="Other synthetic organization")
        self.owner = User.objects.create_user("avito-mcp-owner", password="test")
        self.manager = User.objects.create_user("avito-mcp-manager", password="test")
        OrganizationAccess.objects.create(user=self.owner, organization=self.organization, role="owner")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        self.channel = CommunicationChannel.objects.create(organization=self.organization, kind="avito", name="Synthetic services")
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            name="Synthetic services", external_id="446312402", is_active=True)
        self.credential = AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-client-key", client_secret_encrypted="synthetic-secret-key")
        self.principal = FinanceMcpPrincipal.objects.create(subject="chatgpt:avito:test", display_name="Synthetic principal")
        FinanceMcpPrincipalOrganization.objects.create(principal=self.principal,
            organization=self.organization, granted_by=self.owner)
        self.oauth_client = FinanceMcpClient.objects.create(
            client_id=CHATGPT_CLIENT_ID_METADATA_URL, display_name="Synthetic ChatGPT",
            client_type="public", redirect_uris=[REDIRECT], client_metadata_sha256="a"*64,
            client_metadata_verified_at=timezone.now(), principal=self.principal)

    def _token(self, *, resource=RESOURCE, scopes=None, actor=None, raw="synthetic-read-token"):
        scopes = [AVITO_READ_SCOPE] if scopes is None else scopes
        grant = FinanceMcpGrant.objects.create(client=self.oauth_client, principal=self.principal,
            authorized_by=actor or self.owner, resource=resource,
            scopes=list(scopes)+[_organization_scope(self.organization.pk), _binding_scope()])
        FinanceMcpAccessToken.objects.create(grant=grant,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(), audience=resource,
            scopes=scopes, expires_at=timezone.now()+timedelta(minutes=10))
        return raw

    def _post(self, payload, token=None, **headers):
        request_headers = {"HTTP_ACCEPT": "application/json", "HTTP_MCP_PROTOCOL_VERSION": "2025-06-18"}
        if token is not None:
            request_headers["HTTP_AUTHORIZATION"] = "Bearer " + token
        request_headers.update(headers)
        return self.client.post(reverse("avito_mcp"), json.dumps(payload),
            content_type="application/json", **request_headers)

    def _call(self, name="get_avito_funds", arguments=None, token=None):
        return self._post({"jsonrpc":"2.0", "id":1, "method":"tools/call",
            "params":{"name":name, "arguments":arguments or {"connection_id":4}}}, token)

    def _save_section(self, name, data, *, bound=True):
        self.connection.refresh_from_db()
        metadata = dict(self.connection.settings or {})
        workspace = dict(metadata.get("avito_workspace") or {"account_id":"446312402"})
        sections = dict(workspace.get("sections") or {})
        when = timezone.now().isoformat()
        row = {"status":"ok", "success_at":when, "checked_at":when, "data":data, "stale":False}
        if bound:
            row[CACHE_SCOPE_KEY] = cache_binding(self.connection)
        sections[name] = row
        workspace["sections"] = sections
        metadata["avito_workspace"] = workspace
        self.connection.settings = metadata
        self.connection.save(update_fields=["settings"])
        return row

    def _authorization(self):
        verifier = "v"*43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return verifier, {"response_type":"code", "client_id":CHATGPT_CLIENT_ID_METADATA_URL,
            "redirect_uri":REDIRECT, "resource":RESOURCE, "scope":AVITO_READ_SCOPE+" offline_access",
            "state":"synthetic-consent", "code_challenge_method":"S256", "code_challenge":challenge}

    def test_public_discovery_is_read_only_and_does_not_grant_access(self):
        metadata = self.client.get(reverse("avito_mcp_protected_resource_metadata"))
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()["resource"], RESOURCE)
        self.assertEqual(metadata.json()["scopes_supported"], [AVITO_READ_SCOPE])
        listed = self._post({"jsonrpc":"2.0", "id":1, "method":"tools/list"})
        tools = listed.json()["result"]["tools"]
        self.assertEqual({row["name"] for row in tools}, set(TOOL_NAMES))
        self.assertTrue(all(row["annotations"]["readOnlyHint"] for row in tools))
        self.assertTrue(all(row["inputSchema"]["properties"]["connection_id"]["enum"] == [4] for row in tools))
        self.assertEqual(FinanceMcpGrant.objects.count(), 0)
        self.assertTrue(self._call().json()["result"]["isError"])
        self.assertEqual(self._post({"jsonrpc":"2.0","method":"bad"}).status_code, 401)
        shared = self.client.get(reverse("finance_mcp_authorization_server_metadata"))
        self.assertIn(AVITO_READ_SCOPE, shared.json()["scopes_supported"])

    def test_only_exact_avito_transport_and_discovery_paths_reach_bearer_auth(self):
        metadata = self.client.get(reverse("avito_mcp_protected_resource_metadata"))
        self.assertEqual(metadata.status_code, 200)
        self.assertNotIn("Location", metadata)
        self.assertEqual(metadata.json()["resource"], RESOURCE)
        challenged = self._call()
        self.assertEqual(challenged.status_code, 200)
        self.assertNotIn("Location", challenged)
        result = challenged.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("mcp/www_authenticate", result["_meta"])
        for adjacent in ("/mcp/avito-evil", "/mcp/avito/child",
                         "/.well-known/oauth-protected-resource/mcp/avito-extra"):
            with self.subTest(path=adjacent):
                denied = self.client.get(adjacent)
                self.assertEqual(denied.status_code, 302)
                self.assertEqual(denied["Location"], "/accounts/login/")

    def test_finance_operations_and_wrong_scope_tokens_cannot_read_avito(self):
        for resource, scopes, raw in (
            (ISSUER+"/mcp/finance", ["finance.read"], "synthetic-finance"),
            (ISSUER+"/mcp/operations", ["service2.operations"], "synthetic-operations"),
            (RESOURCE, ["service2.operations"], "synthetic-wrong-scope"),
        ):
            with self.subTest(resource=resource, scopes=scopes):
                token = self._token(resource=resource, scopes=scopes, raw=raw)
                self.assertEqual(self._call(token=token).status_code, 401)

    def test_scope_does_not_accept_goods_connection_or_arbitrary_arguments(self):
        token = self._token()
        for arguments in ({"connection_id":1}, {"connection_id":True}, {"connection_id":"4"},
                          {"connection_id":4,"account_id":"other"}, {"connection_id":4,"url":"https://bad.invalid"}):
            self.assertTrue(self._call(arguments=arguments, token=token).json()["result"]["isError"])
        self.assertFalse(self._call(token=token).json()["result"]["isError"])

    def test_funds_keep_unknown_separate_from_zero_and_drop_private_fields(self):
        self._save_section("balance", {"real":"42.10", "bonus":"0.00", "currency":"RUB",
                                      "private":"SYNTHETIC_PRIVATE", "token":"SYNTHETIC_SECRET"})
        token = self._token()
        with patch("pool_service.communication_avito._json_request", side_effect=AssertionError("network forbidden")):
            result = self._call(token=token).json()["result"]
        self.assertFalse(result["isError"])
        funds = result["structuredContent"]["data"]
        self.assertEqual(funds["wallet"]["data"]["cash"], "42.10")
        self.assertEqual(funds["wallet"]["data"]["bonuses"], "0.00")
        self.assertIsNone(funds["advance"]["data"]["amount"])
        self.assertFalse(result["structuredContent"]["provider_request_performed"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        audit = FinanceMcpAuditEvent.objects.latest("pk")
        self.assertEqual(audit.tool_name, "avito.get_avito_funds")
        self.assertGreater(audit.response_bytes, 0)

    def test_legacy_unbound_wallet_is_not_adopted_after_new_consent(self):
        self._save_section("balance", {"real":"42.10", "bonus":"3.50", "currency":"RUB"}, bound=False)
        result = self._call(token=self._token()).json()["result"]["structuredContent"]["data"]
        self.assertEqual(result["wallet"]["state"], "cache_binding_unconfirmed")
        self.assertIsNone(result["wallet"]["data"]["cash"])
        self.assertIsNone(result["wallet"]["completeness"]["available"])

    def test_capability_loss_deactivation_and_principal_scope_revocation_fail_closed(self):
        token = self._token()
        CommunicationAccess.objects.filter(user=self.owner, organization=self.organization).update(can_manage_channels=False)
        self.assertEqual(self._call(token=token).status_code, 401)
        CommunicationAccess.objects.filter(user=self.owner, organization=self.organization).update(can_manage_channels=True)
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertEqual(self._call(token=token).status_code, 401)
        self.owner.is_active = True
        self.owner.save(update_fields=["is_active"])
        FinanceMcpPrincipalOrganization.objects.filter(principal=self.principal).delete()
        self.assertEqual(self._call(token=token).status_code, 401)

    def test_credential_and_organization_rebind_invalidates_old_grant(self):
        token = self._token()
        self.credential.client_secret_encrypted = "synthetic-rotated-key"
        self.credential.save(update_fields=["client_secret_encrypted"])
        self.assertEqual(self._call(token=token).status_code, 401)
        token = self._token(raw="synthetic-after-key-rotation")
        self.channel.organization = self.other_org
        self.channel.save(update_fields=["organization"])
        self.assertEqual(self._call(token=token).status_code, 401)

    def test_only_current_owner_or_admin_with_explicit_capability_can_consent(self):
        self.assertTrue(can_access_avito_mcp(self.owner, self.organization))
        self.assertFalse(can_access_avito_mcp(self.manager, self.organization))
        self.assertFalse(can_access_avito_mcp(self.owner, self.other_org))
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        self.assertFalse(can_access_avito_mcp(self.owner, self.organization))

    def test_consent_binding_rejects_credential_change_between_get_and_post(self):
        self.client.force_login(self.owner)
        _verifier, params = self._authorization()
        shown = self.client.get(reverse("finance_mcp_authorize"), params)
        self.assertEqual(shown.status_code, 200)
        self.assertContains(shown, "Разрешить чтение Услуг №4")
        self.credential.client_secret_encrypted = "synthetic-new-consent-generation"
        self.credential.save(update_fields=["client_secret_encrypted"])
        response = self.client.post(reverse("finance_mcp_authorize"),
            params | {"decision":"approve", "consent_binding":shown.context["consent_binding"]})
        self.assertIn("error=access_denied", response["Location"])
        self.assertEqual(FinanceMcpAuthorizationCode.objects.count(), 0)

    def test_pkce_token_rotation_and_resource_isolation(self):
        self.client.force_login(self.owner)
        verifier, params = self._authorization()
        shown = self.client.get(reverse("finance_mcp_authorize"), params)
        accepted = self.client.post(reverse("finance_mcp_authorize"),
            params | {"decision":"approve", "consent_binding":shown.context["consent_binding"]})
        code = parse_qs(urlsplit(accepted["Location"]).query)["code"][0]
        token_params = {"grant_type":"authorization_code", "code":code,
            "client_id":CHATGPT_CLIENT_ID_METADATA_URL, "redirect_uri":REDIRECT,
            "code_verifier":verifier, "resource":RESOURCE}
        issued = self.client.post(reverse("finance_mcp_token"), urlencode(token_params),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(issued.status_code, 200)
        token = issued.json()
        self.assertEqual(set(token["scope"].split()), {AVITO_READ_SCOPE, "offline_access"})
        self.assertFalse(self._call(token=token["access_token"]).json()["result"]["isError"])
        refresh_params = {"grant_type":"refresh_token", "refresh_token":token["refresh_token"],
            "client_id":CHATGPT_CLIENT_ID_METADATA_URL, "resource":RESOURCE}
        refreshed = self.client.post(reverse("finance_mcp_token"), urlencode(refresh_params),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(refreshed.status_code, 200)
        replay = self.client.post(reverse("finance_mcp_token"), urlencode(refresh_params),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual(self._call(token=refreshed.json()["access_token"]).status_code, 401)

    def test_consent_requires_signed_binding_and_csrf_not_just_approve(self):
        self.client.force_login(self.owner)
        _verifier, params = self._authorization()
        result = self.client.post(reverse("finance_mcp_authorize"), params | {"decision":"approve"})
        self.assertIn("error=access_denied", result["Location"])
        self.assertEqual(FinanceMcpGrant.objects.count(), 0)
        from django.test import Client as HttpClient
        csrf_client = HttpClient(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(reverse("finance_mcp_authorize"), params).status_code, 403)

    def test_bad_origin_unknown_tool_and_provider_error_text_are_not_echoed(self):
        token = self._token()
        blocked = self._post({"jsonrpc":"2.0","id":1,"method":"tools/list"}, token,
                             HTTP_ORIGIN="https://bad.invalid")
        self.assertEqual(blocked.status_code, 403)
        self.assertTrue(self._call(name="send_message", token=token).json()["error"])
        with patch("pool_service.avito_mcp_views.read_tool", side_effect=RuntimeError("SYNTHETIC_PRIVATE")):
            result = self._call(token=token)
        self.assertTrue(result.json()["result"]["isError"])
        self.assertNotIn("SYNTHETIC_PRIVATE", result.content.decode())

    def test_disabled_resource_and_wrong_resource_configuration_are_closed(self):
        with override_settings(ADVISOR_AVITO_MCP_ENABLED=False):
            self.assertEqual(self.client.get(reverse("avito_mcp_protected_resource_metadata")).status_code, 404)
            self.assertEqual(self._call().status_code, 404)
        with override_settings(ADVISOR_AVITO_MCP_RESOURCE_URL="https://other.invalid/mcp/avito"):
            self.assertEqual(self.client.get(reverse("avito_mcp_protected_resource_metadata")).status_code, 500)

    def test_regular_scoped_refresh_records_cache_provenance_only_for_services(self):
        goods = ChannelConnection.objects.create(pk=1, channel=self.channel,
            name="Synthetic goods", external_id="100001", is_active=True)
        AvitoCredential.objects.create(connection=goods, client_id_encrypted="synthetic-goods",
                                       client_secret_encrypted="synthetic-goods-secret")
        self.client.force_login(self.owner)
        with patch("pool_service.avito_management.access_token", side_effect=lambda connection: str(connection.external_id)), \
             patch("pool_service.avito_workspace._get", side_effect=lambda token, path: {"id":token}), \
             patch("pool_service.avito_workspace.fetch_section", return_value={"real":"42.10","bonus":"0.00","currency":"RUB"}):
            for connection in (self.connection, goods):
                response = self.client.post(reverse("avito_refresh_data", args=[connection.pk]), {"section":"balance"})
                self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        goods.refresh_from_db()
        self.assertEqual(self.connection.settings["avito_workspace"]["sections"]["balance"][CACHE_SCOPE_KEY],
                         cache_binding(self.connection))
        self.assertNotIn(CACHE_SCOPE_KEY, goods.settings["avito_workspace"]["sections"]["balance"])

    def test_statistics_returns_exact_saved_period_without_filling_daily_rows(self):
        from pool_service import avito_period_statistics as period
        from pool_service.avito_statistics_models import AvitoDailyStatistic
        known = {slug:None for slug in period.FIELDS}
        known.update(impressions="20", views="5", contacts="1", spending="42.10")
        snapshot = {"state":"available", "source":"own_ads_stats_v2_period", "reports":[{
            "date_from":"2026-01-01", "date_to":"2026-01-07", "grouping":"item",
            "received_at":timezone.now().isoformat(), "ids":["1001"], "rows":[{"id":"1001","metrics":known}],
            "metrics":[{"slug":"spending","value":"42.10","unit":"₽"},{"slug":"commission","value":None,"unit":"₽"}]}]}
        history = {"completed_dates":28, "expected_dates":30, "state":"observed", "cutoff":"2026-01-07"}
        with patch.object(period, "display", return_value=snapshot), \
             patch("pool_service.avito_daily_statistics.source_period_evidence", return_value=history):
            result = self._call(name="get_avito_statistics",
                arguments={"connection_id":4,"date_from":"2026-01-01","date_to":"2026-01-07"},
                token=self._token()).json()["result"]["structuredContent"]["data"]
        self.assertEqual(result["daily_history"]["completed_dates"], 28)
        self.assertEqual(len(result["reports"]), 1)
        self.assertEqual(result["reports"][0]["metrics"][1]["value"], None)
        self.assertEqual(result["provider_timezone"], "unknown")
        self.assertEqual(AvitoDailyStatistic.objects.count(), 0)

    def test_mid_read_revocation_discards_result_and_never_exports_it(self):
        token = self._token()
        def revoke(*_args):
            CommunicationAccess.objects.filter(user=self.owner, organization=self.organization).update(can_manage_channels=False)
            return {"private":"SYNTHETIC_NOT_RELEASED"}
        with patch("pool_service.avito_mcp_views.read_tool", side_effect=revoke):
            result = self._call(token=token)
        self.assertTrue(result.json()["result"]["isError"])
        self.assertNotIn("SYNTHETIC_NOT_RELEASED", result.content.decode())

    def test_unknown_tool_name_is_not_persisted_in_audit(self):
        token = self._token()
        self._call(name="SYNTHETIC_SECRET_IN_TOOL_NAME", token=token)
        self.assertEqual(FinanceMcpAuditEvent.objects.latest("pk").tool_name, "avito.invalid_tool")

    def test_grant_freezes_exact_consented_binding_even_if_next_read_rotates(self):
        from pool_service import avito_mcp_auth as auth
        self.client.force_login(self.owner)
        _verifier, params = self._authorization()
        shown = self.client.get(reverse("finance_mcp_authorize"), params)
        old = _binding_scope()
        fresh = old[:-1] + ("a" if old[-1] != "a" else "b")
        # validate request and grant check see the authorized generation. A third
        # mutable read must never replace it with a generation not consented to.
        with patch.object(auth, "_binding_scope", side_effect=[old, old, fresh]):
            result = self.client.post(reverse("finance_mcp_authorize"),
                params | {"decision":"approve", "consent_binding":shown.context["consent_binding"]})
        self.assertNotIn("error=", result["Location"])
        grant = FinanceMcpGrant.objects.latest("pk")
        self.assertIn(old, grant.scopes)
        self.assertNotIn(fresh, grant.scopes)

    def test_each_public_tool_runs_through_real_transport_without_provider_call(self):
        token = self._token()
        with patch("pool_service.communication_avito._json_request", side_effect=AssertionError("network forbidden")), \
             patch("pool_service.avito_workspace._get", side_effect=AssertionError("provider get forbidden")):
            for name in TOOL_NAMES:
                with self.subTest(name=name):
                    response = self._call(name=name, token=token)
                    self.assertEqual(response.status_code, 200)
                    result = response.json()["result"]
                    self.assertFalse(result["isError"])
                    self.assertEqual(result["structuredContent"]["connection_id"], 4)
                    self.assertEqual(result["structuredContent"]["avito_account_id"], "446312402")
                    self.assertFalse(result["structuredContent"]["provider_request_performed"])

    def test_expired_or_revoked_token_and_grant_are_rejected(self):
        token = self._token(raw="synthetic-expired")
        FinanceMcpAccessToken.objects.filter(token_hash=hashlib.sha256(token.encode()).hexdigest()).update(
            expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self._call(token=token).status_code, 401)
        token = self._token(raw="synthetic-revoked-grant")
        grant_id = FinanceMcpAccessToken.objects.get(token_hash=hashlib.sha256(token.encode()).hexdigest()).grant_id
        FinanceMcpGrant.objects.filter(pk=grant_id).update(revoked_at=timezone.now())
        self.assertEqual(self._call(token=token).status_code, 401)

    @override_settings(ADVISOR_FINANCE_MCP_ENABLED=False, ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED=False,
                       ADVISOR_OPERATIONS_MCP_ENABLED=False)
    def test_avito_only_resource_keeps_shared_issuer_discovery_without_enabling_finance(self):
        metadata = self.client.get(reverse("finance_mcp_authorization_server_metadata"))
        self.assertEqual(metadata.status_code, 200)
        self.assertIn(AVITO_READ_SCOPE, metadata.json()["scopes_supported"])
        self.assertEqual(self.client.get(reverse("finance_mcp_protected_resource_metadata")).status_code, 404)
        self.assertEqual(FinanceMcpGrant.objects.count(), 0)

    def test_archived_managed_draft_row_remains_visible_when_absent_from_published_feed(self):
        from pool_service.avito_feed_models import AvitoServicesFeed
        active = {"Id":"synthetic-active", "AvitoId":"1001"}
        archived = {"Id":"synthetic-archive", "AvitoId":"1002"}
        when = timezone.now()
        finish = (when+timedelta(days=3)).isoformat()
        AvitoServicesFeed.objects.create(connection=self.connection, account_id="446312402",
            organization_id_at_import=self.organization.pk,
            draft=[{"rows":[active, archived]}], published=[{"rows":[active]}],
            last_checked_at=when, latest_observations=[
                {"avito_id":"1001","id":"synthetic-active","status":"active",
                 "source":"avito_item_detail","checked_at":when.isoformat(),
                 "finish_time":finish,"finish_time_source":"avito_item_detail.finish_time"},
                {"avito_id":"1002","id":"synthetic-archive","status":"old",
                 "source":"avito_item_detail","checked_at":when.isoformat(),
                 "finish_time":finish,"finish_time_source":"avito_item_detail.finish_time"}])
        result = self._call(name="get_avito_listings", token=self._token()).json()["result"]["structuredContent"]["data"]
        self.assertEqual({row["avito_id"] for row in result["listings"]}, {"1001", "1002"})
        self.assertEqual(result["completeness"]["expected_published_ids"], ["1001"])
        self.assertTrue(result["completeness"]["all_listings"])

    def test_wrong_xml_identity_is_hidden_and_unproven_deadline_stays_unknown(self):
        from pool_service.avito_feed_models import AvitoServicesFeed
        when = timezone.now()
        AvitoServicesFeed.objects.create(connection=self.connection, account_id="446312402",
            organization_id_at_import=self.organization.pk,
            draft=[{"rows":[{"Id":"synthetic-one","AvitoId":"1001"},{"Id":"synthetic-two","AvitoId":"1002"}]}],
            published=[{"rows":[]}], last_checked_at=when, latest_observations=[
                {"avito_id":"1001","id":"WRONG_XML_ID","status":"active",
                 "source":"avito_item_detail","checked_at":when.isoformat()},
                {"avito_id":"1002","id":"synthetic-two","status":"old",
                 "source":"avito_item_detail","checked_at":when.isoformat(),
                 "finish_time":(when+timedelta(days=3)).isoformat(), "finish_time_source":"unconfirmed"}])
        result = self._call(name="get_avito_listings", token=self._token()).json()["result"]["structuredContent"]["data"]
        self.assertEqual(len(result["listings"]), 1)
        self.assertEqual(result["listings"][0]["avito_id"], "1002")
        self.assertIsNone(result["listings"][0]["finish_time"])
        self.assertFalse(result["listings"][0]["deadline_known"])
        self.assertFalse(result["completeness"]["all_listings"])


    def test_source_calendar_dates_are_returned_without_inventing_deadline_time(self):
        from pool_service.avito_feed_models import AvitoServicesFeed
        when = timezone.now()
        owned = [{"Id":"synthetic-one","AvitoId":"1001"}, {"Id":"synthetic-two","AvitoId":"1002"}]
        observations = [{"avito_id":row["AvitoId"], "id":row["Id"], "status":"active",
            "source":"avito_item_detail", "checked_at":when.isoformat(), "finish_time":None,
            "finish_time_source":"", "finish_evidence":{
                "source":"avito_item_detail.finish_time", "finish_state":state,
                "finish_time":None, "source_finish_date":"2030-01-02"}}
            for row, state in zip(owned, ("timezone_missing", "calendar_date_only"))]
        AvitoServicesFeed.objects.create(connection=self.connection, account_id="446312402",
            organization_id_at_import=self.organization.pk, draft=[{"rows":owned}],
            published=[{"rows":owned[:1]}], last_checked_at=when, latest_observations=observations)
        result = self._call(name="get_avito_listings", token=self._token()).json()["result"]["structuredContent"]["data"]
        self.assertEqual(len(result["listings"]), 2)
        self.assertTrue(result["completeness"]["all_listings"])
        self.assertEqual(result["completeness"]["known_deadlines"], 0)
        self.assertEqual(result["completeness"]["source_calendar_dates_without_timezone"], 2)
        for row in result["listings"]:
            self.assertEqual(row["source_finish_date"], "2030-01-02")
            self.assertEqual(row["source_timezone"], "unknown")
            self.assertIsNone(row["finish_time"])
            self.assertFalse(row["deadline_known"])

    def test_foreign_malformed_or_conflicting_calendar_evidence_remains_unknown(self):
        from pool_service.avito_feed_models import AvitoServicesFeed
        when = timezone.now()
        good = {"source":"avito_item_detail.finish_time", "finish_state":"timezone_missing",
                "finish_time":None, "source_finish_date":"2030-01-02"}
        bad = [
            good | {"source":"foreign.finish_time"},
            good | {"source_finish_date":"2030-02-30"},
            good | {"source_finish_date":"2030-01-02T00:00:00"},
            good | {"finish_state":"known"},
            good | {"finish_time":"2030-01-02T00:00:00Z"},
        ]
        owned = [{"Id":f"synthetic-{i}", "AvitoId":str(1001+i)} for i in range(len(bad)+2)]
        observations = [{"avito_id":row["AvitoId"], "id":row["Id"], "status":"active",
            "source":"avito_item_detail", "checked_at":when.isoformat(), "finish_time":None,
            "finish_time_source":"", "finish_evidence":evidence}
            for row, evidence in zip(owned, bad)]
        # A valid-looking calendar from another XML identity is never adopted.
        observations.append({"avito_id":owned[-2]["AvitoId"], "id":"wrong-identity",
            "status":"active", "source":"avito_item_detail", "checked_at":when.isoformat(),
            "finish_time":None, "finish_evidence":good})
        # RFC3339 -00:00 means an unknown offset, not a confirmed UTC instant.
        observations.append({"avito_id":owned[-1]["AvitoId"], "id":owned[-1]["Id"],
            "status":"active", "source":"avito_item_detail", "checked_at":when.isoformat(),
            "finish_time":"2030-01-02T00:00:00-00:00", "finish_time_source":"avito_item_detail.finish_time"})
        AvitoServicesFeed.objects.create(connection=self.connection, account_id="446312402",
            organization_id_at_import=self.organization.pk, draft=[{"rows":owned}],
            published=[{"rows":owned}], last_checked_at=when, latest_observations=observations)
        result = self._call(name="get_avito_listings", token=self._token()).json()["result"]["structuredContent"]["data"]
        self.assertEqual(len(result["listings"]), len(owned)-1)
        self.assertFalse(result["completeness"]["all_listings"])
        self.assertEqual(result["completeness"]["known_deadlines"], 0)
        self.assertEqual(result["completeness"]["source_calendar_dates_without_timezone"], 0)
        for row in result["listings"]:
            self.assertIsNone(row["source_finish_date"])
            self.assertIsNone(row["source_timezone"])
            self.assertIsNone(row["finish_time"])
            self.assertFalse(row["deadline_known"])


from django.test import TransactionTestCase


@override_settings(SITE_URL=ISSUER, AVITO_SERVICES_PUBLIC_ORIGIN=ISSUER,
    ADVISOR_AVITO_MCP_ENABLED=True, ADVISOR_AVITO_MCP_RESOURCE_URL=RESOURCE)
class AvitoMcpWalletSetupTests(TransactionTestCase):
    setUp = AvitoMcpTests.setUp

    def _run(self):
        from pool_service.avito_mcp_setup import prepare_wallet_once
        with patch.dict("os.environ", {"SERVICE2_AVITO_DEPLOY_SETUP":"1"}), \
             patch("pool_service.avito_services_workplace_setup._actor", return_value=self.owner):
            return prepare_wallet_once()

    def _provider(self, _token, path):
        from django.db import connection as database_connection
        self.assertFalse(database_connection.in_atomic_block)
        if path.endswith("/balance/"):
            return {"real":"42.10", "bonus":"0.00"}
        return {"id":446312402}

    def test_wallet_read_is_outside_transaction_bound_and_not_repeated(self):
        with patch("pool_service.avito_mcp_setup.access_token", return_value="synthetic-token"), \
             patch("pool_service.avito_workspace._get", side_effect=self._provider) as provider:
            first = self._run()
            second = self._run()
        self.assertEqual(first["state"], "read_confirmed")
        self.assertTrue(first["request_performed"])
        self.assertEqual(second["state"], "already_available")
        self.assertFalse(second["request_performed"])
        self.assertEqual(provider.call_count, 3)
        self.connection.refresh_from_db()
        row = self.connection.settings["avito_workspace"]["sections"]["balance"]
        self.assertEqual(row[CACHE_SCOPE_KEY], cache_binding(self.connection))
        self.assertEqual(row["data"]["real"], "42.10")
        self.assertNotIn("42.10", json.dumps(first))

    def test_failure_preserves_prior_payload_and_claim_prevents_repeated_http(self):
        self.connection.settings = {"avito_workspace":{"account_id":"446312402",
            "sections":{"balance":{"status":"ok","success_at":"2026-01-01T00:00:00Z",
                "data":{"real":"10.00","bonus":"1.00","currency":"RUB"}}}}}
        self.connection.save(update_fields=["settings"])
        before = json.loads(json.dumps(self.connection.settings["avito_workspace"]["sections"]["balance"]))
        from pool_service.communication_avito import AvitoError
        with patch("pool_service.avito_mcp_setup.access_token", side_effect=AvitoError("provider_unavailable")) as token:
            first = self._run()
            second = self._run()
        self.assertEqual(first["state"], "failed")
        self.assertEqual(first["code"], "provider_unavailable")
        self.assertIsNone(first["request_performed"])
        self.assertFalse(first["wallet_request_performed"])
        self.assertEqual(second["state"], "already_attempted")
        self.assertEqual(token.call_count, 1)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings["avito_workspace"]["sections"]["balance"], before)

    def test_busy_workspace_does_not_claim_or_call_provider(self):
        from pool_service.avito_mcp_setup import KEY
        self.connection.settings = {"avito_workspace_refresh_lease":"synthetic-other-lease",
            "avito_workspace_refresh_until":(timezone.now()+timedelta(minutes=1)).isoformat()}
        self.connection.save(update_fields=["settings"])
        with patch("pool_service.avito_mcp_setup.access_token") as token:
            result = self._run()
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["code"], "workspace_busy")
        token.assert_not_called()
        self.connection.refresh_from_db()
        self.assertNotIn(KEY, self.connection.settings)

    def test_key_rotation_during_read_does_not_publish_a_bound_wallet(self):
        def rotate_after_first_profile(token, path):
            result = self._provider(token, path)
            if path.endswith("/balance/"):
                AvitoCredential.objects.filter(pk=self.credential.pk).update(
                    client_secret_encrypted="synthetic-key-rotated-during-read")
            return result
        with patch("pool_service.avito_mcp_setup.access_token", return_value="synthetic-token"), \
             patch("pool_service.avito_workspace._get", side_effect=rotate_after_first_profile):
            result = self._run()
        self.assertEqual(result["code"], "wallet_scope_changed")
        self.connection.refresh_from_db()
        self.assertNotIn("avito_workspace", self.connection.settings)

    def test_ordinary_call_without_protected_deploy_flag_cannot_read_or_modify(self):
        from pool_service.avito_mcp_setup import prepare_wallet_once
        with patch.dict("os.environ", {"SERVICE2_AVITO_DEPLOY_SETUP":""}), \
             patch("pool_service.avito_mcp_setup.access_token") as token:
            result = prepare_wallet_once()
        self.assertEqual(result["code"], "protected_setup_required")
        self.assertFalse(result["request_performed"])
        token.assert_not_called()

    def test_expired_workspace_lease_never_commits_wallet_payload(self):
        def expire_lease(token, path):
            result = self._provider(token, path)
            if path.endswith("/balance/"):
                self.connection.refresh_from_db()
                metadata = dict(self.connection.settings)
                metadata["avito_workspace_refresh_until"] = (timezone.now()-timedelta(seconds=1)).isoformat()
                ChannelConnection.objects.filter(pk=4).update(settings=metadata)
            return result
        with patch("pool_service.avito_mcp_setup.access_token", return_value="synthetic-token"), \
             patch("pool_service.avito_workspace._get", side_effect=expire_lease):
            result = self._run()
        self.assertEqual(result["code"], "wallet_lease_expired")
        self.assertTrue(result["wallet_request_performed"])
        self.connection.refresh_from_db()
        self.assertNotIn("avito_workspace", self.connection.settings)

    def test_local_credential_failure_does_not_claim_a_wallet_request(self):
        from pool_service.communication_avito import AvitoError
        with patch("pool_service.avito_mcp_setup.access_token", side_effect=AvitoError("provider_credentials_invalid")):
            result = self._run()
        self.assertFalse(result["request_performed"])
        self.assertFalse(result["wallet_request_performed"])
