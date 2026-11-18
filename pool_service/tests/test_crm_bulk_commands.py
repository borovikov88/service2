"""Synthetic HTTP regressions for bounded, independently committed stage writes."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.test import Client as HttpClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import crm_bulk_views
from pool_service.crm_commercial_models import CrmCommand, CrmDeal, CrmEvent, CrmLead
from pool_service.models import Organization, ServiceTask
from pool_service.services.crm_commercial_commands import create_commercial_deal
from pool_service.services.task_feedback import version_for
from pool_service.tests.test_crm_lead_commands import LeadFixture


class BulkStageCommandTests(LeadFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.crm_close = True
        self.org.save()
        self.grant.view_team_deals = self.grant.transition_own_deals = True
        self.grant.save()
        self.http = HttpClient()
        self.http.force_login(self.user)

    def payload(self, card, kind="lead", changes=None):
        return dict(operation_key=str(uuid4()), subject_id=card.pk, expected_revision=card.revision,
            command_type=kind + (".disposition" if kind == "lead" else ".transition"),
            changes=changes if changes is not None else ({"column": "unparsed", "reason": "Synthetic review"} if kind == "lead" else
                {"stage_id": self.pipeline.stages.get(code="technical").pk, "reason": ""}))

    def post(self, commands, kind="lead", http=None, org=None):
        url = reverse("crm_live_lead_bulk_command" if kind == "lead" else "crm_live_bulk_command")
        return (http or self.http).post(url + "?org_id=" + str((org or self.org).pk),
            {"commands": commands}, content_type="application/json")

    def new_deal(self):
        result = create_commercial_deal(actor=self.user, organization=self.org, operation_key=str(uuid4()),
            client_id=self.client.pk, pipeline_id=self.pipeline.pk, title="Synthetic batch deal", next_action=self.action())
        return CrmDeal.objects.get(pk=result["deal_id"])

    def test_lead_batch_skips_full_details_and_replays_without_duplicate_events(self):
        leads = [self.create() for _ in range(3)]
        payloads = [self.payload(lead) for lead in leads]
        with patch("pool_service.crm_lead_views._detail", side_effect=AssertionError("Unneeded full card read")):
            response = self.post(payloads)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual([row["status"] for row in response.json()["results"]], [200] * 3)
            for row, lead in zip(response.json()["results"], leads):
                self.assertEqual(row["lead"], {"id": lead.pk, "revision": lead.revision + 1})
                self.assertEqual(set(row), {"operation_key", "status", "result", "lead"})
            count = CrmEvent.objects.count()
            self.assertEqual(self.post(payloads).json(), response.json())
            self.assertEqual(CrmEvent.objects.count(), count)
            self.assertIn("no-store", response.headers["Cache-Control"])

    def test_deal_batch_uses_commands_and_never_serializes_card_finance_or_history(self):
        deals = [self.new_deal() for _ in range(2)]
        payloads = [self.payload(deal, "deal") for deal in deals]
        with patch("pool_service.crm_live_views._serialize_deals", side_effect=AssertionError("Unneeded card projection")):
            response = self.post(payloads, "deal")
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual([row["status"] for row in response.json()["results"]], [200, 200])
            before = CrmEvent.objects.count()
            self.assertEqual(self.post(payloads, "deal").json(), response.json())
            self.assertEqual(CrmEvent.objects.count(), before)
        for deal in deals:
            deal.refresh_from_db()
            self.assertEqual(deal.stage.code, "technical")

    def test_stale_card_is_individual_conflict_and_does_not_block_others(self):
        leads = [self.create() for _ in range(3)]
        payloads = [self.payload(lead) for lead in leads]
        payloads[1]["expected_revision"] += 1
        response = self.post(payloads)
        self.assertEqual([row["status"] for row in response.json()["results"]], [200, 409, 200])
        leads[1].refresh_from_db()
        self.assertEqual(leads[1].revision, payloads[1]["expected_revision"] - 1)
        changed = dict(payloads[0], changes={"column": "working", "reason": ""})
        self.assertEqual(self.post([changed]).json()["results"][0]["status"], 409)

    def test_rights_are_rechecked_between_cards_instead_of_trusting_board_flags(self):
        payloads = [self.payload(self.create()) for _ in range(2)]
        original = crm_bulk_views.execute_lead_command

        def revoke_after_first(**kwargs):
            result = original(**kwargs)
            self.grant.transition_own_leads = False
            self.grant.save(update_fields=["transition_own_leads"])
            return result

        with patch.object(crm_bulk_views, "execute_lead_command", side_effect=revoke_after_first):
            response = self.post(payloads)
        self.assertEqual([row["status"] for row in response.json()["results"]], [200, 403])
        self.assertFalse(CrmCommand.objects.filter(operation_key=payloads[1]["operation_key"]).exists())

    def test_foreign_organization_card_cannot_be_changed_in_batch(self):
        own = self.create()
        other = Organization.objects.create(name="Synthetic other tenant")
        foreign = CrmLead.objects.create(organization=other, title="Synthetic hidden lead")
        response = self.post([self.payload(own), self.payload(foreign)])
        self.assertEqual([row["status"] for row in response.json()["results"]], [200, 403])
        foreign.refresh_from_db()
        self.assertEqual(foreign.revision, 0)
        self.assertNotIn(foreign.title, response.content.decode())

    def test_lost_read_access_after_commit_returns_uncertain_and_same_key_recovers(self):
        lead = self.create()
        payload = self.payload(lead)
        with patch("pool_service.crm_lead_views._organization", side_effect=[self.org, PermissionDenied]):
            row = self.post([payload]).json()["results"][0]
        self.assertEqual((row["status"], row["code"]), (503, "verify_required"))
        self.assertNotIn("result", row)
        self.assertTrue(CrmCommand.objects.filter(operation_key=payload["operation_key"]).exists())
        before = CrmEvent.objects.count()
        self.assertEqual(self.post([payload]).json()["results"][0]["status"], 200)
        self.assertEqual(CrmEvent.objects.count(), before)

    def test_failure_preserves_task_version_check_and_cancellation(self):
        first, second = self.create(), self.create()
        payloads = [self.payload(lead, changes={"column": "failed", "reason": "Synthetic lost enquiry",
            "expected_task_version": version_for(lead.next_task)}) for lead in (first, second)]
        second.next_task.title = "Synthetic concurrent task edit"
        second.next_task.save(update_fields=["title"])
        rows = self.post(payloads).json()["results"]
        self.assertEqual([row["status"] for row in rows], [200, 409])
        first.next_task.refresh_from_db()
        second.next_task.refresh_from_db()
        self.assertEqual(first.next_task.status, ServiceTask.STATUS_CANCELLED)
        self.assertNotEqual(second.next_task.status, ServiceTask.STATUS_CANCELLED)

    def test_deal_failure_preserves_task_and_stage_validation(self):
        deals = [self.new_deal() for _ in range(2)]
        payloads = [dict(self.payload(deal, "deal"), command_type="deal.close_negative",
            changes={"outcome": "lost", "reason": "Synthetic lost deal",
                "expected_task_version": version_for(deal.next_action.task_link.task)}) for deal in deals]
        payloads[1]["changes"]["expected_task_version"] = "0" * 64
        rows = self.post(payloads, "deal").json()["results"]
        self.assertEqual([row["status"] for row in rows], [200, 409])
        deals[0].refresh_from_db()
        deals[1].refresh_from_db()
        self.assertEqual((deals[0].outcome, deals[1].outcome), ("lost", "open"))

    def test_envelope_limit_duplicates_types_and_command_scope_reject_before_writes(self):
        first, second = self.payload(self.create()), self.payload(self.create())
        before = CrmCommand.objects.count()
        invalid = [[], [first] * 26, [first, first], [first, dict(second, operation_key=first["operation_key"])],
            [first, dict(second, subject_id=True)], [first, dict(second, expected_revision=True)],
            [first, dict(second, command_type="lead.assign")], [first, dict(second, extra=True)],
            [first, dict(second, changes=[])], [first, dict(second, subject_id=2**64)]]
        for commands in invalid:
            with self.subTest(commands=commands):
                self.assertEqual(self.post(commands).status_code, 400)
                self.assertEqual(CrmCommand.objects.count(), before)

    def test_auth_csrf_and_disabled_write_remain_enforced(self):
        payload = self.payload(self.create())
        self.assertEqual(self.post([payload], http=HttpClient()).status_code, 401)
        csrf_http = HttpClient(enforce_csrf_checks=True)
        csrf_http.force_login(self.user)
        self.assertEqual(self.post([payload], http=csrf_http).status_code, 403)
        self.org.crm_v2_write = False
        self.org.save(update_fields=["crm_v2_write"])
        self.assertEqual(self.post([payload]).json()["results"][0]["status"], 403)

    def test_prefetched_task_version_matches_fresh_version_without_extra_queries(self):
        task = self.create().next_task
        expected = version_for(task)
        prefetched = ServiceTask.objects.prefetch_related("responsibles").get(pk=task.pk)
        with self.assertNumQueries(0):
            self.assertEqual(version_for(prefetched), expected)
