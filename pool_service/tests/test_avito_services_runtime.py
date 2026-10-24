import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone as dtz
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service import avito_runtime as runtime, avito_advance, avito_autoload_auto as auto
from pool_service import avito_services_workplace_setup as setup, avito_deferred_messages as quiet
from pool_service.avito_report_messages import classify, groups
from pool_service import avito_webhook_chain as chain
from pool_service.communication_api import _mark_avito_webhook_event
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import (CommunicationChannel, ChannelConnection, AvitoCredential,
    AvitoSchedulerHeartbeat, Conversation, ConversationMessage)
from pool_service.models import Organization, OrganizationAccess, Notification, Profile
from scripts import avito_readonly_receipt as receipt


@override_settings(SITE_URL="", ALLOWED_HOSTS=["service2.aqualine22.ru"])
class ServicesRuntimeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic services runtime")
        self.owner = User.objects.create_user("synthetic-runtime-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel, external_id="446312402")
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.run_id = uuid.uuid4()
        self.heartbeat = AvitoSchedulerHeartbeat.objects.create(key="avito-status-monitor", run_id=self.run_id,
            state="finished", started_at=timezone.now() - timedelta(minutes=1),
            finished_at=timezone.now(), exit_code=2)
        avito_advance.configure(self.connection, self.owner, "enable")

    def fresh(self):
        self.connection.refresh_from_db()
        return self.connection

    def begin(self):
        self.heartbeat.state = "running"
        self.heartbeat.started_at = timezone.now() - timedelta(seconds=1)
        self.heartbeat.finished_at = None
        self.heartbeat.checks = []
        self.heartbeat.save()

    def finish(self, code=2):
        self.heartbeat.refresh_from_db()
        self.heartbeat.state = "finished"
        self.heartbeat.finished_at = timezone.now()
        self.heartbeat.exit_code = code
        self.heartbeat.save()

    def test_legacy_partial_has_no_invented_current_failure(self):
        connection = self.fresh()
        data = dict(connection.settings)
        data[avito_advance.KEY]["last_error_code"] = "provider_http_403"
        data[avito_advance.KEY]["last_checked_at"] = (timezone.now() - timedelta(days=2)).isoformat()
        connection.settings = data
        connection.save(update_fields=["settings"])
        check = next(row for row in runtime.checks(connection) if row["check"] == "advance")
        self.assertEqual("provider_http_403", check["code"])
        self.assertFalse(check["last_worker_verified"])
        self.assertIsNone(check["last_worker_outcome"])

    def test_run_bound_failure_and_success_are_distinct_and_stale_writer_rejected(self):
        self.begin()
        connection = self.fresh()
        connection.settings[avito_advance.KEY]["last_error_code"] = "provider_http_403"
        connection.save(update_fields=["settings"])
        runtime.record(connection.pk, "advance", "failed", run_id=str(self.run_id))
        runtime.record(connection.pk, "autoload_report", "success", run_id=str(self.run_id))
        runtime.record(connection.pk, "advance", "success", run_id=str(uuid.uuid4()))
        self.finish()
        rows = {row["check"]: row for row in runtime.checks(self.fresh())}
        self.assertEqual("failed", rows["advance"]["last_worker_outcome"])
        self.assertEqual("provider_http_403", rows["advance"]["last_worker_code"])
        self.assertEqual("success", rows["autoload_report"]["last_worker_outcome"])
        self.connection.external_id = "999"
        self.connection.save(update_fields=["external_id"])
        self.assertFalse(any(row["last_worker_verified"] for row in runtime.checks(self.fresh())))

    def test_consistency_branch_is_run_bound_projected_and_never_inferred_from_old_code(self):
        from pool_service.avito_autoload_report import CONSISTENCY_GUARDS
        self.assertEqual(CONSISTENCY_GUARDS, set(runtime.CONSISTENCY_GUARDS))
        auto.configure(self.connection, self.owner, "enable")
        connection = self.fresh()
        state = connection.settings[auto.KEY]
        state.update(last_error_code="autoload_report_changed", last_consistency_guard="queried_rows_changed")
        connection.save(update_fields=["settings"])
        self.begin()
        runtime.record(connection.pk, "autoload_report", "failed", run_id=str(self.run_id))
        self.finish()
        connection = self.fresh()
        connection.settings[auto.KEY].update(last_error_code="provider_unavailable", last_consistency_guard=None)
        connection.save(update_fields=["settings"])
        rows = runtime.checks(connection)
        row = next(row for row in rows if row["check"] == "autoload_report")
        self.assertIsNone(row["consistency_guard"])
        self.assertEqual("queried_rows_changed", row["last_worker_consistency_guard"])
        self.assertEqual("autoload_report_changed", row["last_worker_code"])
        projected = next(row for row in receipt.runtime_checks(rows) if row["check"] == "autoload_report")
        self.assertEqual("queried_rows_changed", projected["last_worker_consistency_guard"])
        self.assertNotIn("label", projected)
        self.assertNotIn("http", str(projected))
        invalid = dict(row, last_worker_consistency_guard="PRIVATE")
        with self.assertRaises(ValueError):
            receipt.runtime_checks([invalid])
        with self.assertRaises(ValueError):
            receipt.runtime_checks([dict(row, check="advance")])
        self.heartbeat.checks = []
        self.heartbeat.save(update_fields=["checks"])
        connection.settings[auto.KEY].update(last_error_code="autoload_report_changed",
                                             last_consistency_guard="PRIVATE")
        connection.save(update_fields=["settings"])
        unknown = next(row for row in runtime.checks(connection) if row["check"] == "autoload_report")
        self.assertIsNone(unknown["consistency_guard"])
        self.assertIsNone(unknown["last_worker_consistency_guard"])

    def test_observed_positive_link_phrase_requires_completed_unconditional_noncorrective_text(self):
        positive = {"type": "warning", "code": 9999,
                    "title": "Объявление [значение] связано с объявлением на Авито по [поле] в [файл]",
                    "description": ""}
        result = classify(positive)
        self.assertEqual("existing_listing_link", result["category"])
        self.assertTrue(result["binding_evidence"])
        for prefix in ("не связано", "не было связано", "небыло связано", "невозможно связать",
                       "будет связано", "будетсвязано", "может быть связано",
                       "если объявление было связано", "чтобы объявление было связано"):
            title = prefix + " с объявлением на Авито по [поле]"
            with self.subTest(prefix=prefix):
                result = classify(dict(positive, title=title))
                self.assertNotEqual("existing_listing_link", result["category"])
                self.assertFalse(result["binding_evidence"])
        unconfirmed = (
            "Объявление должно быть связано с объявлением на Авито",
            "Параметр должен быть связан с существующим объявлением",
            "Карточка должна быть связана с существующим объявлением",
            "Карточки должны быть связаны с существующим объявлением",
            "Следует проверить, что объявление связано с объявлением на Авито",
            "Проверьте, что объявление связано с объявлением на Авито",
            "Убедитесь, что объявление связано с объявлением на Авито",
        )
        for title in unconfirmed:
            with self.subTest(title=title):
                result = classify(dict(positive, title=title))
                self.assertNotEqual("existing_listing_link", result["category"])
                self.assertFalse(result["binding_evidence"])
        for message in (dict(positive, description="Укажите AvitoId."),
                        dict(positive, type="error"), dict(positive, type="alarm")):
            self.assertEqual("action_required", classify(message)["category"])
            self.assertFalse(classify(message)["binding_evidence"])
        self.assertEqual("existing_listing_link", classify(dict(positive, code=1218))["category"])
        self.assertEqual("review_required", classify(dict(positive, code=1218, title="Неизвестное предупреждение"))["category"])

    def test_begin_resets_previous_check_evidence_and_status_stdout_stays_compatible(self):
        self.begin()
        runtime.record(4, "advance", "success", run_id=str(self.run_id))
        with patch.dict(os.environ, {"SERVICE2_AVITO_CRON_SUPERVISED": "1"}):
            call_command("avito_scheduler_heartbeat", "begin", run_id=uuid.uuid4(), stdout=io.StringIO())
        self.heartbeat.refresh_from_db()
        self.assertEqual([], self.heartbeat.checks)
        out, diagnostic = io.StringIO(), io.StringIO()
        call_command("monitor_avito_statuses", status=True, stdout=out, stderr=diagnostic)
        self.assertRegex(out.getvalue(), r"\AAVITO_STATUS_MONITOR_READY configured=\d+ enabled=\d+ baselined=\d+ due=\d+ running=\d+ failed=\d+\n\Z")
        self.assertIn("AVITO_CHECK connection=4", diagnostic.getvalue())

    def install(self, fail_readback=False):
        urls, writes = [], []
        def subscribed(connection, url):
            writes.append(url)
            urls.append(url)
            return {"ok": True}
        reads = [0]
        def subscriptions(connection):
            reads[0] += 1
            if fail_readback and reads[0] > 1:
                raise AvitoError("provider_unavailable")
            return list(urls)
        with patch.object(setup, "authorized_account_id", return_value="446312402"), \
             patch.object(setup, "verify_messenger_access", return_value=True), \
             patch.object(setup, "webhook_subscriptions", side_effect=subscriptions), \
             patch.object(setup, "subscribe_webhook", side_effect=subscribed), \
             patch.object(setup, "passenger_storage_probe", return_value={"source": "passenger_http", "checked": False, "error": "storage_probe_not_run"}):
            result = setup.setup(4, "446312402", "https://service2.aqualine22.ru")
        return result, writes

    @override_settings(SITE_URL="https://legacy.example.invalid",
                       AVITO_SERVICES_PUBLIC_ORIGIN="https://service2.aqualine22.ru")
    def test_setup_uses_dedicated_avito_origin_despite_unrelated_generic_site_url(self):
        result, writes = self.install()
        self.assertEqual("applied", result["setup"])
        self.assertTrue(result["automatic_reports_enabled"])
        self.assertTrue(result["webhook"]["registration_confirmed"])
        self.assertEqual(1, len(writes))
        self.assertTrue(writes[0].startswith("https://service2.aqualine22.ru/api/communications/avito/"))
        self.assertNotIn("legacy.example.invalid", writes[0])
        self.assertEqual({"source": "avito_services_public_origin", "cli_origin_verified": True,
                          "generic_site_url_differs": True}, result["origin"])
        self.assertNotIn("legacy.example.invalid", str(result))

    @override_settings(SITE_URL="https://legacy.example.invalid",
                       AVITO_SERVICES_PUBLIC_ORIGIN="https://service2.aqualine22.ru")
    def test_wrong_cli_origin_is_rejected_before_any_configuration_or_provider_write(self):
        from copy import deepcopy
        before = deepcopy(self.fresh().settings)
        with patch.object(setup, "authorized_account_id") as account, \
             patch.object(auto, "configure") as configure, \
             patch.object(setup, "subscribe_webhook") as write:
            with self.assertRaisesMessage(AvitoError, "services_setup_origin_mismatch"):
                setup.setup(4, "446312402", "https://legacy.example.invalid")
        account.assert_not_called()
        configure.assert_not_called()
        write.assert_not_called()
        self.assertEqual(before, self.fresh().settings)

    @override_settings(SITE_URL="https://legacy.example.invalid",
                       AVITO_SERVICES_PUBLIC_ORIGIN="https://service2.aqualine22.ru")
    def test_wrong_constructed_callback_is_rejected_before_token_hash_or_post(self):
        from copy import deepcopy
        original = self.fresh()
        before, token_hash = deepcopy(original.settings), original.api_token_hash
        for callback in ("https://other.example.invalid/path",
                         "http://service2.aqualine22.ru/path",
                         "https://service2.aqualine22.ru/path?token=synthetic",
                         "https://service2.aqualine22.ru/wrong-path/"):
            with self.subTest(callback=callback), \
                 patch.object(setup, "authorized_account_id", return_value="446312402"), \
                 patch.object(setup, "verify_messenger_access", return_value=True), \
                 patch.object(setup, "webhook_subscriptions", return_value=[]), \
                 patch("pool_service.communication_views._avito_callback_url", return_value=callback), \
                 patch.object(setup, "subscribe_webhook") as write:
                with self.assertRaisesMessage(AvitoError, "services_setup_callback_origin_mismatch"):
                    setup.ensure_webhook(4, setup._binding(self.fresh()), "https://service2.aqualine22.ru")
            write.assert_not_called()
            self.assertEqual(token_hash, self.fresh().api_token_hash)
            self.assertEqual(before, self.fresh().settings)

    @override_settings(SITE_URL="https://legacy.example.invalid",
                       AVITO_SERVICES_PUBLIC_ORIGIN="https://service2.aqualine22.ru")
    def test_legacy_generic_origin_subscription_cannot_confirm_avito_registration(self):
        result, writes = self.install()
        self.assertTrue(result["webhook"]["registration_confirmed"])
        current = self.fresh()
        legacy_url = writes[0].replace("https://service2.aqualine22.ru", "https://legacy.example.invalid")
        self.assertFalse(setup._registered(current, [legacy_url], "https://legacy.example.invalid"))
        self.assertFalse(setup._registered(current, [legacy_url], "https://service2.aqualine22.ru"))
        current.settings[setup.WEBHOOK_KEY]["origin"] = "https://legacy.example.invalid"
        current.save(update_fields=["settings"])
        self.assertFalse(setup.webhook_evidence(self.fresh())["registration_confirmed"])

    @override_settings(AVITO_SERVICES_PUBLIC_ORIGIN="http://service2.aqualine22.ru")
    def test_invalid_trusted_origin_is_not_replaced_with_a_cli_fallback(self):
        with patch.object(setup, "authorized_account_id") as account:
            with self.assertRaisesMessage(AvitoError, "services_setup_origin_invalid"):
                setup.setup(4, "446312402", "https://service2.aqualine22.ru")
        account.assert_not_called()

    def test_setup_uses_valid_existing_actor_and_partial_completed_worker(self):
        result, writes = self.install()
        self.assertEqual("applied", result["setup"])
        self.assertTrue(result["automatic_reports_enabled"])
        self.assertTrue(result["webhook"]["registration_confirmed"])
        self.assertFalse(result["webhook"]["delivery_observed"])
        self.assertFalse(result["background_report_success"])
        self.assertEqual(1, len(writes))
        self.assertTrue(auto._allowed(self.fresh(), auto._state(self.connection)))
        self.assertNotIn("synthetic-secret", str(result))
        self.assertNotIn("webhook/", str(result))

    def test_one_time_component_checkpoints_do_not_override_later_owner_optout(self):
        self.install()
        auto.configure(self.fresh(), self.owner, "disable")
        with patch.object(setup, "subscribe_webhook") as write, \
             patch.object(setup, "passenger_storage_probe", return_value={"source": "passenger_http", "checked": False, "error": "storage_probe_not_run"}):
            result = setup.setup(4, "446312402", "https://service2.aqualine22.ru")
        self.assertEqual("already_applied", result["setup"])
        self.assertFalse(result["automatic_reports_enabled"])
        write.assert_not_called()

    def test_ambiguous_registration_reconciles_without_second_write_or_reenable(self):
        first, writes = self.install(fail_readback=True)
        self.assertFalse(first["webhook"]["registration_confirmed"])
        auto.configure(self.fresh(), self.owner, "disable")
        with patch.object(setup, "authorized_account_id", return_value="446312402"), \
             patch.object(setup, "verify_messenger_access", return_value=True), \
             patch.object(setup, "webhook_subscriptions", return_value=[]), \
             patch.object(setup, "subscribe_webhook") as write, \
             patch.object(setup, "passenger_storage_probe", return_value={"source": "passenger_http", "checked": False, "error": "storage_probe_not_run"}):
            setup.setup(4, "446312402", "https://service2.aqualine22.ru")
        write.assert_not_called()
        self.assertFalse(auto._state(self.fresh())["enabled"])

    def test_setup_fails_closed_on_account_role_and_key_changes(self):
        with patch.object(setup, "authorized_account_id", return_value="999"), \
             patch.object(setup, "subscribe_webhook") as write:
            with self.assertRaises(AvitoError):
                setup.setup(4, "446312402", "https://service2.aqualine22.ru")
        write.assert_not_called()
        self.assertNotIn(setup.KEY, self.fresh().settings)
        OrganizationAccess.objects.filter(user=self.owner).update(role="manager")
        with self.assertRaises(AvitoError):
            setup.setup(4, "446312402", "https://service2.aqualine22.ru")

    def test_delivery_requires_current_ingested_message_and_cannot_use_legacy_timestamp(self):
        connection = self.fresh()
        connection.settings["avito_webhook_last_received_at"] = timezone.now().isoformat()
        connection.save(update_fields=["settings"])
        self.install()
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])
        _mark_avito_webhook_event(self.connection, "ignored")
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])
        _mark_avito_webhook_event(self.connection, "created", message_present=True)
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])
        message = self.message()
        _mark_avito_webhook_event(self.fresh(), "created", message_present=True, message=message)
        self.assertTrue(setup.webhook_evidence(self.fresh())["delivery_observed"])
        AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="synthetic-new-key")
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])

    def post_webhook(self, path, *, user_id="446312402", message_id="PRIVATE_SYNTHETIC_MESSAGE"):
        payload = {"payload": {"type": "message", "value": {
            "user_id": user_id, "id": message_id, "chat_id": "PRIVATE_SYNTHETIC_CHAT",
            "author_id": "42", "author_name": "PRIVATE_SYNTHETIC_NAME", "type": "text",
            "content": {"text": "PRIVATE_SYNTHETIC_BODY"}}}}
        return self.client.post(path, data=json.dumps(payload), content_type="application/json")

    def test_authenticated_webhook_chain_proves_persistence_owner_record_and_dedupe(self):
        _, writes = self.install()
        path = urlsplit(writes[0]).path
        with patch.object(quiet, "daylight", return_value=True), \
             patch("pool_service.services.notifications.send_push_to_users"):
            response = self.post_webhook(path)
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.json()["created"])
            result = chain.summary(self.fresh())
            self.assertEqual("accepted_persisted", result["state"])
            self.assertTrue(result["generation_bound"])
            self.assertTrue(result["authenticated_event"])
            self.assertTrue(result["conversation_bound"])
            self.assertTrue(result["message_persisted"])
            self.assertEqual("direct_recorded", result["owner_notification_state"])
            self.assertTrue(result["owner_notification_recorded"])
            self.assertFalse(result["push_delivery_confirmed"])
            self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(result))
            self.assertNotIn("message_id", result)
            self.assertNotIn("conversation_id", result)
            duplicate = self.post_webhook(path)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(1, ConversationMessage.objects.count())
        self.assertEqual(1, Notification.objects.filter(user=self.owner, kind="communication").count())
        self.assertFalse(ConversationMessage.objects.filter(direction=ConversationMessage.DIRECTION_OUT).exists())
        self.assertEqual("duplicate", chain.summary(self.fresh())["event_result"])
        current = self.fresh()
        setup._save_webhook_evidence(4, setup._binding(current), "https://service2.aqualine22.ru",
                                     current.api_token_hash, verified=True)
        self.assertEqual("accepted_persisted", chain.summary(self.fresh())["state"])

    def test_wrong_token_wrong_provider_account_and_ignored_events_never_prove_delivery(self):
        from django.urls import reverse
        _, writes = self.install()
        denied = self.post_webhook(reverse("avito_webhook", args=[self.fresh().public_id, "synthetic-wrong-token"]))
        self.assertEqual(401, denied.status_code)
        response = self.post_webhook(urlsplit(writes[0]).path, user_id="999999")
        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()["created"])
        self.assertFalse(ConversationMessage.objects.exists())
        self.assertFalse(Notification.objects.exists())
        result = chain.summary(self.fresh())
        self.assertEqual("awaiting_first", result["state"])
        self.assertIsNone(result["received_at"])
        self.assertFalse(result["authenticated_event"])
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])

    def test_webhook_owner_chain_tracks_quiet_queue_and_preference_retry_without_night_send(self):
        _, writes = self.install()
        with patch.object(quiet, "daylight", return_value=False), \
             patch("pool_service.services.notifications.send_push_to_users") as push:
            self.assertTrue(self.post_webhook(urlsplit(writes[0]).path).json()["created"])
            self.assertEqual("queued", chain.summary(self.fresh())["owner_notification_state"])
            self.assertEqual("skipped", quiet.flush(4))
            push.assert_not_called()
        self.assertFalse(Notification.objects.exists())
        profile, _ = Profile.objects.get_or_create(user=self.owner)
        profile.in_app_notifications_enabled = False
        profile.save(update_fields=["in_app_notifications_enabled"])
        with patch.object(quiet, "daylight", return_value=True):
            self.assertEqual("failed", quiet.flush(4))
        result = chain.summary(self.fresh())
        self.assertEqual("queued", result["owner_notification_state"])
        self.assertEqual("notification_preferences_disabled", result["owner_notification_error"])
        self.assertFalse(result["owner_notification_recorded"])
        profile.in_app_notifications_enabled = True
        profile.save(update_fields=["in_app_notifications_enabled"])
        with patch.object(quiet, "daylight", return_value=False):
            self.assertEqual("skipped", quiet.flush(4))
        self.assertFalse(Notification.objects.exists())
        with patch.object(quiet, "daylight", return_value=True):
            self.assertEqual("success", quiet.flush(4))
            self.assertEqual("skipped", quiet.flush(4))
        result = chain.summary(self.fresh())
        self.assertEqual("quiet_digest_recorded", result["owner_notification_state"])
        self.assertTrue(result["owner_notification_recorded"])
        self.assertEqual(1, Notification.objects.filter(user=self.owner, kind="communication").count())

    def test_chain_generation_change_deleted_or_foreign_message_cannot_reuse_proof(self):
        _, writes = self.install()
        with patch.object(quiet, "daylight", return_value=True), \
             patch("pool_service.services.notifications.send_push_to_users"):
            self.post_webhook(urlsplit(writes[0]).path)
        current = self.fresh()
        evidence = current.settings[setup.WEBHOOK_KEY]
        original = dict(evidence["chain"])
        original_verified = evidence["verified_at"]
        evidence["verified_at"] = (timezone.now() - timedelta(microseconds=1)).isoformat()
        current.save(update_fields=["settings"])
        self.assertEqual("awaiting_first", chain.summary(self.fresh())["state"])
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])
        # Restore the original window, then show that a deleted DB row is not proof.
        current = self.fresh()
        current.settings[setup.WEBHOOK_KEY]["verified_at"] = original_verified
        current.settings[setup.WEBHOOK_KEY]["chain"] = original
        current.save(update_fields=["settings"])
        other = Organization.objects.create(name="Synthetic foreign proof")
        channel = CommunicationChannel.objects.create(organization=other, kind="avito", name="Foreign")
        foreign_connection = ChannelConnection.objects.create(channel=channel, external_id="999999")
        foreign_conversation = Conversation.objects.create(connection=foreign_connection, organization=other,
                                                             external_id="synthetic-foreign", participant_name="Synthetic")
        foreign_message = ConversationMessage.objects.create(conversation=foreign_conversation,
            external_id="synthetic-foreign", direction=ConversationMessage.DIRECTION_IN, body="Synthetic")
        current.settings[setup.WEBHOOK_KEY]["chain"] = dict(original, message_id=foreign_message.pk,
                                                           conversation_id=foreign_conversation.pk)
        current.save(update_fields=["settings"])
        self.assertEqual("persisted_row_missing", chain.summary(self.fresh())["state"])
        current = self.fresh()
        current.settings[setup.WEBHOOK_KEY]["chain"] = original
        current.save(update_fields=["settings"])
        ConversationMessage.objects.filter(pk=original["message_id"]).delete()
        result = chain.summary(self.fresh())
        self.assertEqual("persisted_row_missing", result["state"])
        self.assertFalse(result["message_persisted"])
        self.assertFalse(result["owner_notification_recorded"])

    def test_fresh_connection_move_cannot_reuse_foreign_organization_conversation(self):
        from pool_service.communication_services import receive_message
        self.install()
        Conversation.objects.create(connection=self.connection, organization=self.org,
                                    external_id="synthetic-existing-chat", participant_name="Synthetic")
        other = Organization.objects.create(name="Other synthetic scope")
        channel = CommunicationChannel.objects.create(organization=other, kind="avito", name="Foreign")
        ChannelConnection.objects.filter(pk=4).update(channel=channel)
        with self.assertRaisesMessage(ValueError, "avito_conversation_binding_changed"):
            receive_message(connection=self.fresh(), external_conversation_id="synthetic-existing-chat",
                            participant_name="Synthetic", body="Synthetic", external_message_id="synthetic-new")
        self.assertFalse(ConversationMessage.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_manual_recovery_is_deduped_bounded_and_never_claims_webhook_delivery(self):
        from pool_service import communication_avito as provider
        self.install()
        responses = {"chats": [{"id": "synthetic-recovery-chat"}]}
        message = {"id": "synthetic-recovery-message", "author_id": "42", "direction": "in",
                   "created": int(timezone.now().timestamp()), "type": "text", "content": {"text": "Synthetic recovery"}}
        with patch.object(provider, "authorized_account_id", return_value="446312402"), \
             patch.object(provider, "access_token", return_value="synthetic-token"), \
             patch.object(provider, "_json_request", return_value=responses) as chats, \
             patch.object(provider, "_json_list_request", return_value=[message]) as messages, \
             patch.object(quiet, "daylight", return_value=True), \
             patch("pool_service.services.notifications.send_push_to_users"):
            first = provider.sync_recent_messages(self.fresh(), chat_limit=1, messages_per_chat=1)
            second = provider.sync_recent_messages(self.fresh(), chat_limit=1, messages_per_chat=1)
        self.assertEqual((1, 0), (first.messages_created, first.messages_existing))
        self.assertEqual((0, 1), (second.messages_created, second.messages_existing))
        self.assertEqual(2, chats.call_count)
        self.assertEqual(2, messages.call_count)
        self.assertEqual(1, ConversationMessage.objects.count())
        self.assertEqual(1, Notification.objects.filter(user=self.owner, kind="communication").count())
        self.assertEqual("awaiting_first", chain.summary(self.fresh())["state"])
        self.assertFalse(setup.webhook_evidence(self.fresh())["delivery_observed"])

    def test_manual_services_recovery_rejects_other_provider_account_before_chat_read(self):
        from pool_service import communication_avito as provider
        self.install()
        with patch.object(provider, "authorized_account_id", return_value="999999"), \
             patch.object(provider, "access_token") as token, \
             patch.object(provider, "_json_request") as request:
            with self.assertRaisesMessage(AvitoError, "provider_account_mismatch"):
                provider.sync_recent_messages(self.fresh())
        token.assert_not_called()
        request.assert_not_called()
        self.assertFalse(ConversationMessage.objects.exists())

    def message(self):
        conversation = Conversation.objects.create(connection=self.connection, organization=self.org, external_id="synthetic-chat")
        return ConversationMessage.objects.create(conversation=conversation, external_id="synthetic-message",
            direction=ConversationMessage.DIRECTION_IN, body="Synthetic text")

    def test_owner_quiet_hours_preserve_queue_and_retry_only_after_eight(self):
        self.install()
        message = self.message()
        nighttime = datetime(2026, 10, 24, 14, 0, tzinfo=dtz.utc)  # 21:00 Barnaul
        with patch("pool_service.avito_deferred_messages.timezone.now", return_value=nighttime):
            self.assertEqual([], quiet.partition_users(self.fresh(), message, [self.owner]))
            self.assertEqual("skipped", quiet.flush(4))
        self.assertFalse(Notification.objects.exists())
        self.assertIn(quiet.KEY, self.fresh().settings)
        morning = datetime(2026, 10, 25, 1, 0, tzinfo=dtz.utc)  # 08:00 Barnaul
        with patch("pool_service.avito_deferred_messages.timezone.now", return_value=morning):
            self.assertEqual("success", quiet.flush(4))
            self.assertEqual("skipped", quiet.flush(4))
        self.assertEqual(1, Notification.objects.filter(user=self.owner, kind="communication").count())
        self.assertNotIn(quiet.KEY, self.fresh().settings)

    def test_quiet_digest_keeps_pending_when_preferences_or_scope_reject_delivery(self):
        self.install()
        message = self.message()
        with patch.object(quiet, "daylight", return_value=False):
            quiet.partition_users(self.fresh(), message, [self.owner])
        profile, _ = Profile.objects.get_or_create(user=self.owner)
        profile.in_app_notifications_enabled = False
        profile.save(update_fields=["in_app_notifications_enabled"])
        with patch.object(quiet, "daylight", return_value=True):
            self.assertEqual("failed", quiet.flush(4))
        self.assertIn(quiet.KEY, self.fresh().settings)
        self.assertFalse(Notification.objects.exists())
        OrganizationAccess.objects.filter(user=self.owner).update(role="manager")
        with patch.object(quiet, "daylight", return_value=True):
            self.assertEqual("failed", quiet.flush(4))
        self.assertIn(quiet.KEY, self.fresh().settings)

    def test_correlation_failure_preserves_good_report_and_has_its_own_worker_cause(self):
        auto.configure(self.fresh(), self.owner, "enable")
        self.begin()
        data = {"kind": "last_successful", "complete": True, "upload_finished": True,
                "upload_id": "123", "upload_status": "success", "rows": [], "counts": {}}
        zero = {"success": 0, "failed": 0, "skipped": 0}
        with patch.dict(os.environ, {"SERVICE2_AVITO_CRON_SUPERVISED": "1"}), \
             patch.object(auto, "access_token", return_value="synthetic-token"), \
             patch("pool_service.avito_workspace._get", return_value={"id": 446312402}), \
             patch("pool_service.avito_autoload_report.fetch_report", return_value=data), \
             patch("pool_service.avito_services_publication.correlate_report", side_effect=RuntimeError("PRIVATE")), \
             patch("pool_service.avito_advance.scan_due", return_value=zero), \
             patch("pool_service.avito_tariff_reminder.scan_due", return_value=zero):
            from django.core.management import CommandError
            with self.assertRaises(CommandError) as raised:
                call_command("monitor_avito_statuses", budget_seconds=450, stdout=io.StringIO())
        self.assertEqual(2, raised.exception.returncode)
        saved = self.fresh().settings["avito_workspace"]["sections"]["autoload_report"]
        self.assertEqual("ok", saved["status"])
        self.assertEqual(data, saved["data"])
        self.finish()
        rows = {row["check"]: row for row in runtime.checks(self.fresh())}
        self.assertEqual("success", rows["autoload_report"]["last_worker_outcome"])
        self.assertEqual("failed", rows["publication_correlation"]["last_worker_outcome"])
        self.assertEqual("publication_correlation_failed", rows["publication_correlation"]["last_worker_code"])
        self.assertNotIn("PRIVATE", str(rows))

    def test_quiet_day_boundaries_are_barnaul_and_include_twenty_oclock_slot(self):
        for hour, minute, expected in ((13, 59, True), (14, 0, False), (0, 59, False), (1, 0, True)):
            stamp = datetime(2026, 10, 24, hour, minute, tzinfo=dtz.utc)
            self.assertEqual(expected, quiet.daylight(stamp))

    def test_setup_command_requires_protected_exact_commit_before_any_mutation(self):
        from django.core.management import CommandError
        from types import SimpleNamespace
        with patch("pool_service.management.commands.configure_avito_services_workplace.setup") as configure:
            with patch.dict(os.environ, {"SERVICE2_AVITO_DEPLOY_SETUP": "0"}):
                with self.assertRaises(CommandError):
                    call_command("configure_avito_services_workplace", expected_sha="0"*40,
                                 base_url="https://service2.aqualine22.ru", stdout=io.StringIO())
            with patch.dict(os.environ, {"SERVICE2_AVITO_DEPLOY_SETUP": "1"}), \
                 patch("pool_service.management.commands.configure_avito_services_workplace.subprocess.run",
                       return_value=SimpleNamespace(stdout="1"*40)):
                with self.assertRaises(CommandError):
                    call_command("configure_avito_services_workplace", expected_sha="0"*40,
                                 base_url="https://service2.aqualine22.ru", stdout=io.StringIO())
        configure.assert_not_called()

    def test_stale_authenticated_connection_cannot_rebind_message_or_diagnostics(self):
        from copy import deepcopy
        from pool_service.communication_services import receive_message
        self.install()
        original = self.fresh()
        channel_id, token_hash = original.channel_id, original.api_token_hash
        other = Organization.objects.create(name="Other synthetic org")
        channel = CommunicationChannel.objects.create(organization=other, kind="avito", name="Foreign")
        for change in ({"external_id": "999"}, {"api_token_hash": "synthetic-other-hash"},
                       {"channel_id": channel.pk}, {"is_active": False}):
            with self.subTest(change=change):
                authenticated = setup._connection(4)
                before = deepcopy(authenticated.settings)
                ChannelConnection.objects.filter(pk=4).update(**change)
                with self.assertRaisesMessage(ValueError, "avito_connection_binding_changed"):
                    receive_message(connection=authenticated, external_conversation_id="stale-chat",
                        participant_name="Synthetic", body="Synthetic", external_message_id="stale-message")
                _mark_avito_webhook_event(authenticated, "created", message_present=True)
                self.assertEqual(before, setup._connection(4).settings)
                self.assertFalse(Conversation.objects.exists())
                self.assertFalse(ConversationMessage.objects.exists())
                self.assertFalse(Notification.objects.exists())
                ChannelConnection.objects.filter(pk=4).update(external_id="446312402",
                    channel_id=channel_id, api_token_hash=token_hash, is_active=True)

    def test_quiet_worker_reason_and_view_revocation_are_explicit(self):
        from pool_service.communication_models import CommunicationAccess
        self.install()
        message = self.message()
        self.begin()
        def observed(pk, check, outcome, **kwargs):
            runtime.record(pk, check, outcome, run_id=str(self.run_id), **kwargs)
        with patch.object(quiet, "daylight", return_value=False):
            quiet.partition_users(self.fresh(), message, [self.owner])
            result = quiet.scan_due(observer=observed)
        self.finish(0)
        row = next(row for row in runtime.checks(self.fresh()) if row["check"] == "messages_digest")
        self.assertEqual(0, result["failed"])
        self.assertEqual("quiet_hours", row["last_worker_code"])
        self.assertEqual("skipped", row["last_worker_outcome"])
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(
            can_manage_channels=True, can_view_conversations=False)
        with patch.object(quiet, "daylight", return_value=True):
            self.assertEqual("failed", quiet.flush(4))
        self.assertEqual("conversation_access_denied", self.fresh().settings[quiet.KEY]["last_error_code"])
        self.assertFalse(Notification.objects.exists())

    def test_passenger_probe_success_is_safe_and_redirect_failure_keeps_components_working(self):
        import json
        from types import SimpleNamespace
        from urllib.error import HTTPError
        self.install()
        captured = []
        flags = {key: True for key in setup.PROBE_FLAGS}
        class Response:
            status = 200
            def __enter__(inner): return inner
            def __exit__(inner, *args): return False
            def geturl(inner): return captured[-1].full_url
            def read(inner, limit): return json.dumps(dict(flags, private_path="PRIVATE")).encode()
        def opened(request, timeout):
            self.assertEqual(10, timeout)
            self.assertEqual("POST", request.method)
            self.assertEqual({"token": "SYNTHETIC_PRIVATE_TOKEN"}, json.loads(request.data))
            captured.append(request)
            return Response()
        with patch.object(setup, "authorized_account_id", return_value="446312402"), \
             patch("pool_service.avito_services_media.make_storage_probe_token", return_value="SYNTHETIC_PRIVATE_TOKEN"), \
             patch("urllib.request.build_opener", return_value=SimpleNamespace(open=opened)) as built:
            result = setup.passenger_storage_probe(4, setup._binding(self.fresh()), "https://service2.aqualine22.ru")
        self.assertTrue(result["checked"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", str(self.fresh().settings))
        self.assertIsNone(built.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://other.invalid"))
        self.connection = self.fresh()
        self.connection.settings[setup.KEY].pop("storage_probe")
        self.connection.save(update_fields=["settings"])
        def denied(request, timeout):
            raise HTTPError(request.full_url, 302, "PRIVATE", {}, None)
        with patch.object(setup, "authorized_account_id", return_value="446312402"), \
             patch("pool_service.avito_services_media.make_storage_probe_token", return_value="SYNTHETIC_PRIVATE_TOKEN"), \
             patch("urllib.request.build_opener", return_value=SimpleNamespace(open=denied)):
            result = setup.passenger_storage_probe(4, setup._binding(self.fresh()), "https://service2.aqualine22.ru")
        self.assertFalse(result["checked"])
        self.assertEqual("storage_probe_http_302", result["error"])
        self.assertNotIn("storage_probe", self.fresh().settings[setup.KEY])
        self.assertTrue(auto._state(self.fresh())["enabled"])
        self.assertTrue(setup.webhook_evidence(self.fresh())["registration_confirmed"])

    def test_runtime_projection_drops_private_fields_and_rejects_false_webhook_proof(self):
        rows = runtime.checks(self.fresh())
        rows[0]["actor_id"] = "PRIVATE"
        rows[0]["last_error_detail"] = "PRIVATE"
        projected = receipt.runtime_checks(rows)
        self.assertNotIn("PRIVATE", str(projected))
        evidence = {"binding_valid": False, "registration_confirmed": True, "delivery_observed": True,
                    "live_provider_checked": False, "live_registered": None}
        with self.assertRaises(ValueError):
            receipt.webhook_evidence(evidence)


class ProviderMessageMeaningTests(TestCase):
    def test_no_action_binding_is_benign_but_a_real_mixed_problem_wins(self):
        positive = {"type": "warning", "code": 1218, "title": "Существующее объявление привязано",
                    "description": "Ничего исправлять не нужно."}
        self.assertEqual("existing_listing_link", classify(positive)["category"])
        mixed = dict(positive, description="Ничего исправлять не нужно. Но укажите ContactPhone.")
        self.assertEqual("action_required", classify(mixed)["category"])
        self.assertEqual("action_required", classify(dict(positive, type="error"))["category"])

    def test_message_meaning_comes_from_text_not_numeric_code_or_count(self):
        positive = {"type": "warning", "code": 1218, "title": "Существующее объявление привязано",
                    "description": "Сопоставлено по AvitoId. https://private.invalid +79991234567"}
        self.assertEqual("existing_listing_link", classify(positive)["category"])
        negative = dict(positive, title="Не удалось привязать существующее объявление",
                        description="Укажите AvitoId")
        self.assertEqual("action_required", classify(negative)["category"])
        negated = dict(positive, title="Существующее объявление не привязано", description="")
        self.assertNotEqual("existing_listing_link", classify(negated)["category"])
        unknown = dict(positive, title="Неизвестное техническое предупреждение", description="")
        self.assertEqual("review_required", classify(unknown)["category"])
        informational = dict(positive, type="info", code=3002, title="Сведения Авито", description="")
        result = groups([{"messages": [positive, informational]}])
        self.assertEqual({"existing_listing_link", "information"}, {row["category"] for row in result})
        self.assertNotIn("private.invalid", str(result))
        self.assertNotIn("79991234567", str(result))
