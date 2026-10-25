import io
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase
from django.utils import timezone

from pool_service import avito_outbox as outbox
from pool_service.communication_avito import (
    AvitoAmbiguousDeliveryError, AvitoError, AvitoRetryableError,
    _json_request, _NoMessageRedirect, send_message,
)
from pool_service.communication_models import (
    ChannelConnection, CommunicationChannel, Conversation, ConversationMessage,
)
from pool_service.communication_views import conversation_reply
from pool_service.models import Organization, OrganizationAccess


class MessagePostTransportTests(SimpleTestCase):
    def response(self, raw):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = raw
        return response

    def test_ambiguous_post_response_never_becomes_safe_failure(self):
        for raw in (b"invalid", b"[]", b"x" * (1024 * 1024 + 1)):
            with self.subTest(size=len(raw)):
                with patch("pool_service.communication_avito._no_redirect_urlopen", return_value=self.response(raw)):
                    with self.assertRaisesMessage(AvitoAmbiguousDeliveryError, "provider_delivery_unknown"):
                        _json_request("https://api.avito.ru/test", method="POST", ambiguous_transport=True)

    def test_post_timeout_server_error_or_redirect_is_unknown(self):
        for error in (TimeoutError(), HTTPError("https://api.avito.ru/test", 503, "error", {}, None),
                      HTTPError("https://api.avito.ru/test", 302, "redirect", {}, None)):
            with self.subTest(error=type(error).__name__):
                with patch("pool_service.communication_avito._no_redirect_urlopen", side_effect=error):
                    with self.assertRaises(AvitoAmbiguousDeliveryError):
                        _json_request("https://api.avito.ru/test", method="POST", ambiguous_transport=True)

    def test_explicit_payment_and_access_rejections_preserve_different_codes(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                error = HTTPError("https://api.avito.ru/test", status, "error", {}, None)
                with patch("pool_service.communication_avito._no_redirect_urlopen", side_effect=error):
                    with self.assertRaisesMessage(AvitoError, "provider_http_" + str(status)):
                        _json_request("https://api.avito.ru/test", method="POST", ambiguous_transport=True)

    def test_message_post_redirect_does_not_forward_authorization(self):
        self.assertIsNone(_NoMessageRedirect().redirect_request(
            MagicMock(), None, 302, "redirect", {}, "https://other.invalid/message"))


class ScopedOutboxTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.org = Organization.objects.create(name="Outbox test organization")
        self.owner = User.objects.create_superuser("outbox-owner", "owner@example.invalid", "test")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(
            pk=4, channel=self.channel, name="Services", external_id="446312402")
        self.conversation = Conversation.objects.create(
            organization=self.org, connection=self.connection, external_id="test-chat", participant_name="Client")
        self.enable()

    def enable(self):
        self.connection.settings = {outbox.KEY: {
            "enabled": True, "account_id": self.connection.external_id,
            "organization_id": self.org.pk,
            "enabled_at": (timezone.now() - timedelta(hours=1)).isoformat(),
        }}
        self.connection.save(update_fields=["settings"])

    def message(self, **changes):
        values = dict(conversation=self.conversation, body="An explicitly approved test reply",
                      direction="out", sent_by=self.owner, delivery_status="pending")
        values.update(changes)
        return ConversationMessage.objects.create(**values)

    def accepted_sender(self, message, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(kwargs["token"], "verified-token")
        message.external_id = "test-provider-id"
        message.delivery_status = "delivered"
        message.delivered_at = timezone.now()
        message.save(update_fields=["external_id", "delivery_status", "delivered_at"])

    def run_worker(self, message, *, reconcile=None, sender=None, explicit=False, **kwargs):
        reconcile = reconcile or {"state": "absent", "error_code": "", "provider_id": None}
        with patch.object(outbox, "_provider_context", return_value="verified-token"), \
                patch.object(outbox, "_reconcile", return_value=reconcile):
            return outbox.run_outbox(4, message_id=message.pk, explicit=explicit,
                sender=sender or self.accepted_sender, **kwargs)

    def test_one_original_legacy_row_is_sent_once_without_creating_a_copy(self):
        message = self.message()
        ConversationMessage.objects.filter(pk=message.pk).update(created_at=timezone.now() - timedelta(days=1))
        self.run_worker(message, explicit=True)
        self.run_worker(message, explicit=True, sender=MagicMock(side_effect=AssertionError("duplicate")))
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, "delivered")
        self.assertEqual(message.external_id, "test-provider-id")
        self.assertEqual(message.delivery_attempts, 1)
        self.assertEqual(ConversationMessage.objects.count(), 1)

    def test_existing_provider_match_updates_the_original_without_post(self):
        message = self.message()
        sender = MagicMock()
        self.run_worker(message, reconcile={"state": "accepted", "provider_id": "already-there", "error_code": ""},
                        sender=sender, explicit=True)
        sender.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.external_id, "already-there")
        self.assertEqual(message.delivery_status, "delivered")

    def test_unknown_after_post_is_never_requeued_or_sent_again(self):
        message = self.message()
        sender = MagicMock(side_effect=AvitoAmbiguousDeliveryError("provider_delivery_unknown"))
        self.run_worker(message, sender=sender)
        self.run_worker(message, sender=sender, explicit=True)
        sender.assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, "sending")
        self.assertEqual(message.delivery_error, "provider_delivery_unknown")

    def test_incomplete_history_blocks_post_and_explains_unknown(self):
        message = self.message()
        sender = MagicMock()
        self.run_worker(message, reconcile={"state": "unknown", "error_code": "provider_reconcile_incomplete"},
                        sender=sender, explicit=True)
        sender.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, "sending")
        self.assertEqual(message.delivery_error, "provider_reconcile_incomplete")

    def test_regular_worker_holds_all_old_backlog(self):
        message = self.message()
        ConversationMessage.objects.filter(pk=message.pk).update(created_at=timezone.now() - timedelta(days=1))
        with patch.object(outbox, "_provider_context") as provider:
            result = outbox.run_outbox(4)
        provider.assert_not_called()
        self.assertEqual(result["accepted"], 0)
        self.connection.refresh_from_db()
        self.assertEqual(outbox.outbox_diagnostics(self.connection)["legacy_held_count"], 1)

    def test_scope_and_organization_mismatch_never_modify_another_message(self):
        other_org = Organization.objects.create(name="Other organization")
        foreign = Conversation.objects.create(organization=other_org, connection=self.connection,
                                              external_id="foreign-chat", participant_name="Foreign")
        message = self.message(conversation=foreign)
        with patch.object(outbox, "_provider_context") as provider:
            outbox.run_outbox(4, message_id=message.pk, explicit=True)
        provider.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, "pending")
        self.assertEqual(message.delivery_attempts, 0)

    def test_concurrent_claim_only_wins_once_and_rechecks_active_binding(self):
        message = self.message()
        self.assertIsNotNone(outbox.claim_message(message.pk, connection_id=4))
        self.assertIsNone(outbox.claim_message(message.pk, connection_id=4))
        message.refresh_from_db()
        self.assertEqual(message.delivery_attempts, 1)
        second = self.message()
        self.connection.external_id = "another-account"
        self.connection.save(update_fields=["external_id"])
        self.assertIsNone(outbox.claim_message(second.pk, connection_id=4))

    def test_bounded_prepost_transient_retries(self):
        message = self.message()
        with patch.object(outbox, "_provider_context", side_effect=AvitoRetryableError("provider_http_503")):
            for _ in range(3):
                outbox.run_outbox(4, message_id=message.pk)
        message.refresh_from_db()
        self.assertEqual(message.delivery_attempts, 3)
        self.assertEqual(message.delivery_status, "failed")
        self.assertEqual(message.delivery_error, "provider_http_503")

    def test_guard_change_or_actor_revocation_before_post_blocks_sending(self):
        for mutate in ("body", "actor"):
            with self.subTest(mutate=mutate):
                message = self.message()
                def reconcile(*args):
                    if mutate == "body":
                        ConversationMessage.objects.filter(pk=message.pk).update(body="Changed after approval")
                    else:
                        User.objects.filter(pk=self.owner.pk).update(is_active=False)
                    return {"state": "absent", "error_code": ""}
                sender = MagicMock()
                with patch.object(outbox, "_provider_context", return_value="verified-token"), \
                        patch.object(outbox, "_reconcile", side_effect=reconcile):
                    outbox.run_outbox(4, message_id=message.pk, sender=sender, explicit=True,
                                     expected_guard=lambda row: row.body == "An explicitly approved test reply")
                sender.assert_not_called()
                User.objects.filter(pk=self.owner.pk).update(is_active=True)

    def test_reconcile_requires_complete_valid_provider_history_and_never_writes(self):
        message = self.message()
        now = int(message.created_at.timestamp()) + 1
        match = {"id": "existing-id", "author_id": 446312402, "direction": "out",
                 "created": now, "type": "text", "content": {"text": message.body}}
        with patch.object(outbox, "_provider_context", return_value="verified-token"), \
                patch.object(outbox, "_json_list_request", return_value=[match]):
            result = outbox.reconcile_message(4, message.pk)
        self.assertEqual(result["state"], "accepted")
        self.assertTrue(result["complete"])
        message.refresh_from_db()
        self.assertIsNone(message.external_id)
        self.assertEqual(message.delivery_attempts, 0)
        with patch.object(outbox, "_provider_context", return_value="verified-token"), \
                patch.object(outbox, "_json_list_request", return_value=[match, dict(match, id="another-id")]):
            self.assertEqual(outbox.reconcile_message(4, message.pk)["error_code"], "provider_reconcile_ambiguous")

    def test_provider_send_without_id_is_unknown(self):
        message = self.message()
        with patch("pool_service.communication_avito._json_request", return_value={}):
            with self.assertRaises(AvitoAmbiguousDeliveryError):
                send_message(message, token="verified-token")

    def test_network_processing_is_rejected_inside_an_open_transaction(self):
        with transaction.atomic(), patch.object(outbox, "_provider_context") as provider:
            with self.assertRaisesMessage(AvitoError, "outbox_transaction_open"):
                outbox.run_outbox(4)
        provider.assert_not_called()

    def test_reply_callback_runs_after_commit_and_only_for_services(self):
        self.connection.settings = {}
        self.connection.save(update_fields=["settings"])
        request = RequestFactory().post("/communications/reply/", {"body": "New manager reply"})
        request.user = self.owner
        request.session = {}
        calls = []
        def dispatched(connection_id, message_id):
            self.assertFalse(connection.in_atomic_block)
            row = ConversationMessage.objects.get(pk=message_id)
            self.assertEqual(row.sent_by_id, self.owner.pk)
            calls.append((connection_id, message_id))
        with patch("pool_service.communication_views.dispatch_committed_reply", side_effect=dispatched):
            self.assertEqual(conversation_reply(request, self.conversation.uuid).status_code, 302)
        self.assertEqual(len(calls), 1)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.settings[outbox.KEY]["enabled"])
        goods = ChannelConnection.objects.create(pk=1, channel=self.channel, name="Goods", external_id="other")
        goods_chat = Conversation.objects.create(organization=self.org, connection=goods,
                                                external_id="goods-chat", participant_name="Goods client")
        with patch("pool_service.communication_views.dispatch_committed_reply") as dispatch:
            conversation_reply(request, goods_chat.uuid)
        dispatch.assert_not_called()
        goods.refresh_from_db()
        self.assertEqual(goods.settings, {})

    def test_periodic_evidence_is_distinct_from_immediate_dispatch(self):
        message = self.message()
        self.run_worker(message)
        self.connection.refresh_from_db()
        self.assertIsNone(outbox.outbox_diagnostics(self.connection)["last_periodic_success_at"])
        with patch.object(outbox, "_provider_context"):
            outbox.run_outbox(4, periodic=True)
        self.connection.refresh_from_db()
        self.assertIsNotNone(outbox.outbox_diagnostics(self.connection)["last_periodic_success_at"])

    def test_scope_required_for_cli_and_private_errors_are_sanitized(self):
        with self.assertRaises(CommandError):
            call_command("send_avito_outbox")
        self.assertEqual(outbox.safe_error("private provider payload or access token"), "outbox_internal_error")
        output = io.StringIO()
        call_command("send_avito_outbox", "--connection-id", "4", "--status", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["connection_id"], 4)

    def test_configure_preserves_an_existing_owner_opt_out(self):
        self.connection.settings[outbox.KEY]["enabled"] = False
        self.connection.save(update_fields=["settings"])
        with patch.object(outbox, "_provider_context", return_value="verified-token"):
            state = outbox.configure_outbox(4, "446312402")
        self.assertFalse(state["enabled"])
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.settings[outbox.KEY]["enabled"])

    def test_unknown_previous_post_stays_unknown_if_reconciliation_access_fails(self):
        message = self.message(delivery_status="sending", delivery_attempts=1,
                               delivery_error="provider_delivery_unknown")
        with patch.object(outbox, "_provider_context", side_effect=AvitoError("provider_http_403")), \
                patch.object(outbox, "send_message") as sender:
            outbox.run_outbox(4, message_id=message.pk, explicit=True)
        sender.assert_not_called()
        message.refresh_from_db()
        self.assertEqual(message.delivery_status, "sending")
        self.assertEqual(message.delivery_error, "provider_http_403")

    def test_two_fresh_identical_manager_intents_are_not_deduplicated_by_body(self):
        first, second = self.message(), self.message()
        sent = []
        def sender(message, **kwargs):
            sent.append(message.pk)
            message.external_id = "provider-" + str(message.pk)
            message.delivery_status = "delivered"
            message.save(update_fields=["external_id", "delivery_status"])
        with patch.object(outbox, "_provider_context", return_value="verified-token"), \
                patch.object(outbox, "_reconcile") as reconcile:
            outbox.run_outbox(4, message_id=first.pk, sender=sender)
            outbox.run_outbox(4, message_id=second.pk, sender=sender)
        reconcile.assert_not_called()
        self.assertEqual(sent, [first.pk, second.pk])

    def test_concurrent_new_callback_can_claim_a_different_message_while_first_is_sending(self):
        first, second = self.message(), self.message()
        sent = []
        def sender(message, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            sent.append(message.pk)
            if message.pk == first.pk:
                result = outbox.run_outbox(4, message_id=second.pk, sender=sender)
                self.assertEqual(result["accepted"], 1)
                # A replay of the still-running first callback cannot POST again.
                self.assertEqual(outbox.run_outbox(4, message_id=first.pk, sender=sender)["held"], 1)
            message.external_id = "concurrent-" + str(message.pk)
            message.delivery_status = "delivered"
            message.save(update_fields=["external_id", "delivery_status"])
        with patch.object(outbox, "_provider_context", return_value="verified-token"):
            outbox.run_outbox(4, message_id=first.pk, sender=sender)
        self.assertEqual(sent, [first.pk, second.pk])
        self.assertEqual(ConversationMessage.objects.filter(delivery_status="pending").count(), 0)

    def test_earlier_identical_provider_reply_is_ambiguous_for_legacy_recovery(self):
        message = self.message()
        match = {"id": "earlier-id", "author_id": 446312402, "direction": "out",
                 "created": int(message.created_at.timestamp()) - 60,
                 "type": "text", "content": {"text": message.body}}
        with patch.object(outbox, "_provider_context", return_value="verified-token"), \
                patch.object(outbox, "_json_list_request", return_value=[match]):
            result = outbox.reconcile_message(4, message.pk)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["error_code"], "provider_reconcile_ambiguous")

    def test_reconciled_record_revalidates_scope_snapshot_guard_atomically(self):
        message = self.message()
        snapshot = outbox.message_snapshot(outbox._message(4, message.pk))
        self.assertFalse(outbox.record_reconciled_message(4, message.pk, "provider-proof",
            expected_snapshot=snapshot, expected_guard=lambda row: False))
        message.refresh_from_db()
        self.assertIsNone(message.external_id)
        self.assertTrue(outbox.record_reconciled_message(4, message.pk, "provider-proof",
            expected_snapshot=snapshot, expected_guard=lambda row: row.body == message.body))
        self.assertFalse(outbox.record_reconciled_message(4, message.pk, "different-proof",
            expected_snapshot=snapshot, expected_guard=lambda row: True))
        message.refresh_from_db()
        self.assertEqual(message.external_id, "provider-proof")

    def test_two_empty_periodic_runs_record_start_finish_and_zero_counts_without_provider_io(self):
        base = timezone.now()
        with patch.object(outbox, "send_message") as sender, patch.object(outbox, "_provider_context") as context:
            for offset in (0, 60):
                with patch.object(outbox.timezone, "now", return_value=base + timedelta(seconds=offset)):
                    outbox.run_outbox(4, periodic=True)
        sender.assert_not_called()
        context.assert_not_called()
        self.connection.refresh_from_db()
        state = outbox.outbox_diagnostics(self.connection, now=base + timedelta(seconds=61))
        self.assertEqual(state["periodic_state"], "healthy")
        self.assertEqual(state["immediate_state"], "never_run")
        self.assertEqual(len(state["periodic_runs"]), 2)
        for row in state["periodic_runs"]:
            self.assertEqual(row["state"], "success")
            self.assertIsNotNone(row["completed_at"])
            self.assertEqual(row["queue_before"], {"pending": 0, "unknown": 0, "failed": 0})
            self.assertEqual(row["queue_after"], {"pending": 0, "unknown": 0, "failed": 0})
            self.assertEqual(sum(row["result"].values()), 0)
            self.assertNotIn("token", row)
        self.assertNotEqual(state["periodic_runs"][0]["started_at"], state["periodic_runs"][1]["started_at"])

    def test_manual_success_cannot_be_attributed_to_immediate_and_immediate_idle_is_not_stale(self):
        outbox.run_outbox(4)
        self.connection.refresh_from_db()
        self.assertIsNone(outbox.outbox_diagnostics(self.connection)["last_immediate_success_at"])
        with patch.object(outbox, "send_message") as sender:
            # An empty invocation marks the mechanism only; no customer text is needed.
            outbox.run_outbox(4, immediate=True)
        sender.assert_not_called()
        self.connection.refresh_from_db()
        state = outbox.outbox_diagnostics(self.connection, now=timezone.now() + timedelta(hours=2))
        self.assertEqual(state["immediate_state"], "healthy")
        self.assertEqual(state["periodic_state"], "never_run")
        self.assertIsNone(state["last_periodic_success_at"])

    def test_late_periodic_finish_cannot_erase_newer_error_or_running_state(self):
        first = outbox._start(4, periodic=True)
        second = outbox._start(4, periodic=True)
        result = dict(accepted=0, pending=0, failed=0, unknown=0, held=0)
        outbox._finish(4, second, "provider_http_403", periodic=True, result=result)
        outbox._finish(4, first, periodic=True, result=result)
        self.connection.refresh_from_db()
        state = outbox.outbox_diagnostics(self.connection)
        self.assertEqual(state["periodic_state"], "error")
        self.assertEqual(state["last_periodic_error_code"], "provider_http_403")
        self.assertEqual([row["state"] for row in state["periodic_runs"]], ["success", "error"])

    def test_periodic_history_is_bounded_and_does_not_release_old_or_unknown_rows(self):
        old = self.message()
        ConversationMessage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.message(delivery_status="sending", delivery_attempts=1, delivery_error="provider_delivery_unknown")
        with patch.object(outbox, "send_message") as sender:
            for _ in range(6):
                outbox.run_outbox(4, periodic=True)
        sender.assert_not_called()
        self.connection.refresh_from_db()
        state = outbox.outbox_diagnostics(self.connection)
        self.assertEqual(len(state["periodic_runs"]), 4)
        self.assertEqual(state["legacy_held_count"], 1)
        self.assertEqual(state["unknown_count"], 1)
        self.assertEqual(state["periodic_runs"][-1]["queue_before"], {"pending": 1, "unknown": 1, "failed": 0})
        self.assertEqual(state["periodic_runs"][-1]["queue_after"], {"pending": 1, "unknown": 1, "failed": 0})

    def test_committed_dispatch_is_the_only_automatic_immediate_attribution(self):
        with patch.object(outbox, "run_outbox", return_value={"accepted": 1}) as run:
            outbox.dispatch_committed_reply(4, 123)
        run.assert_called_once_with(4, message_id=123, immediate=True)

    def test_interrupted_new_periodic_run_is_unknown_even_after_recent_success(self):
        outbox.run_outbox(4, periodic=True)
        base = timezone.now() + timedelta(seconds=1)
        with patch.object(outbox.timezone, "now", return_value=base):
            outbox._start(4, periodic=True)
        self.connection.refresh_from_db()
        state = outbox.outbox_diagnostics(self.connection, now=base + timedelta(seconds=181))
        self.assertEqual(state["periodic_state"], "unknown")
        self.assertEqual(state["periodic_runs"][-1]["state"], "unknown")

    def test_prior_error_is_not_the_result_of_a_new_interrupted_run(self):
        for mode in ("periodic", "immediate"):
            with self.subTest(mode=mode):
                options = {mode: True}
                first = outbox._start(4, **options)
                result = dict(accepted=0, pending=0, failed=0, unknown=0, held=0)
                outbox._finish(4, first, "provider_http_403", result=result, **options)
                base = timezone.now() + timedelta(seconds=1)
                with patch.object(outbox.timezone, "now", return_value=base):
                    outbox._start(4, **options)
                self.connection.refresh_from_db()
                state = outbox.outbox_diagnostics(self.connection, now=base + timedelta(seconds=181))
                self.assertEqual(state[mode + "_state"], "unknown")
                self.assertEqual(state["last_" + mode + "_error_code"], "")

    def test_changed_account_or_organization_cannot_inherit_mechanism_proof(self):
        outbox.run_outbox(4, periodic=True)
        outbox.run_outbox(4, immediate=True)
        self.connection.refresh_from_db()
        bound_settings = self.connection.settings
        for field, value in (("account_id", "foreign-account"), ("organization_id", self.org.pk + 1000)):
            with self.subTest(field=field):
                self.connection.settings = {
                    **bound_settings, outbox.KEY: {**bound_settings[outbox.KEY], field: value},
                }
                state = outbox.outbox_diagnostics(self.connection)
                self.assertFalse(state["binding_valid"])
                self.assertEqual(state["periodic_state"], "disabled")
                self.assertEqual(state["immediate_state"], "disabled")
                self.assertEqual(state["periodic_runs"], [])
                for mode in ("immediate", "periodic"):
                    for suffix in ("started_at", "completed_at", "success_at"):
                        self.assertIsNone(state["last_" + mode + "_" + suffix])
                    self.assertEqual(state["last_" + mode + "_error_code"], "")
        self.connection.settings = {
            **bound_settings, outbox.KEY: {**bound_settings[outbox.KEY], "enabled": False},
        }
        state = outbox.outbox_diagnostics(self.connection)
        self.assertTrue(state["binding_valid"])
        self.assertFalse(state["enabled"])
        self.assertEqual(len(state["periodic_runs"]), 1)
        self.assertIsNotNone(state["last_immediate_success_at"])
