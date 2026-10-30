"""Synthetic lead command regression/security tests, including real DB races."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import (
    CrmAccess, CrmCommand, CrmDeal, CrmDealNextAction, CrmEvent, CrmIntakeActivation,
    CrmLead, CrmLeadDealLink, CrmLeadTaskLink, CrmLeadThread, CrmPipeline,
)
from pool_service.models import Client, Employee, Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_commercial_commands import CommercialConflict, execute_commercial_command
from pool_service.services.crm_lead_commands import execute_lead_command
from pool_service.services.task_feedback import version_for


class LeadFixture:
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic lead organization", crm_v2_read=True, crm_v2_write=True)
        self.user = get_user_model().objects.create_user("lead-owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic lead manager")
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.user,
            view_leads=True, create_own_leads=True, take_leads=True, edit_own_leads=True,
            transition_own_leads=True, qualify_own_leads=True, create_own_deals=True, manage_own_next_action=True)
        self.client = Client.objects.create(organization=self.org, name="Synthetic preliminary contact")
        bootstrap_commercial_settings(self.org.pk)
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.due = timezone.now().replace(microsecond=0) + timedelta(days=1)

    def command(self, kind, changes, lead=None, key=None, **kwargs):
        args = dict(actor=self.user, organization=self.org, operation_key=key or uuid4().hex,
            command_type=kind, changes=changes)
        if lead is not None:
            args.update(subject_id=lead.pk, expected_revision=lead.revision)
        args.update(kwargs)
        return execute_lead_command(**args)

    def action(self, title="Clarify synthetic need"):
        return {"title": title, "due_at": self.due.isoformat()}

    def create(self, **changes):
        values = {"title": "Synthetic request", "need": "Pool construction", "next_action": self.action()}
        values.update(changes)
        result = self.command("lead.create", values)
        return CrmLead.objects.get(pk=result["lead_id"])

    def deal(self, **changes):
        values = {"title": "Synthetic independent project", "pipeline_id": self.pipeline.pk, "next_action": self.action("Prepare calculation")}
        values.update(changes)
        return values

    def state(self):
        return [list(model.objects.order_by("pk").values()) for model in
            (CrmLead, CrmDeal, CrmLeadTaskLink, CrmLeadDealLink, ServiceTask, CrmCommand, CrmEvent)]


class LeadCommandTests(LeadFixture, TestCase):
    def test_manual_no_client_no_phone_unknown_budget_real_private_next_action(self):
        lead = self.create()
        self.assertEqual((lead.assignee_id, lead.status, lead.result, lead.revision), (self.user.pk, "working", "", 1))
        self.assertIsNone(lead.client_id)
        self.assertEqual(lead.budget_kind, "unknown")
        self.assertIsNone(lead.budget_min)
        task = lead.next_task
        self.assertEqual((task.task_type, task.visibility, task.primary_responsible_id),
                         (ServiceTask.TYPE_CRM_FOLLOWUP, ServiceTask.VISIBILITY_PRIVATE, self.user.pk))
        self.assertEqual(task.payload_json, {"crm_lead": {"lead_id": lead.pk}})
        self.assertIsNone(task.crm_item_id)
        self.assertEqual(CrmLeadTaskLink.objects.get(lead=lead).task_id, task.pk)
        self.assertEqual(task.due_at, self.due)
        event = CrmEvent.objects.get(lead=lead)
        self.assertNotIn(lead.title, str(event.safe_diff))
        self.assertNotIn(lead.need, str(event.safe_diff))
        self.assertEqual((CrmDeal.objects.count(), CrmIntakeActivation.objects.count()), (0, 0))

    def test_create_replay_normalizes_and_rejects_changed_key_or_revoked_cap(self):
        changes = {"title": " Synthetic ", "need": " Need ", "next_action": self.action()}
        result = self.command("lead.create", changes, key="stable-create")
        self.assertEqual(result, self.command("lead.create", {**changes, "title": "Synthetic"}, key="stable-create"))
        self.assertEqual((CrmLead.objects.count(), ServiceTask.objects.count()), (1, 1))
        with self.assertRaises(CommercialConflict):
            self.command("lead.create", {**changes, "need": "Other"}, key="stable-create")
        self.grant.create_own_leads = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.command("lead.create", changes, key="stable-create")

    def test_validation_failure_is_atomic_and_budget_exact_range_unknown(self):
        invalid = [
            {"next_action": {"title": "Call", "due_at": "2026-11-01T10:00:00"}},
            {"budget_kind": "exact", "budget_min": "10.00", "budget_max": "20.00", "currency": "RUB"},
            {"budget_kind": "range", "budget_min": "20.00", "budget_max": "10.00", "currency": "RUB"},
            {"budget_min": "NaN"}, {"budget_min": True}, {"budget_max": "1.001"},
            {"direction": "anything"}, {"assignee_id": self.user.pk},
        ]
        before = self.state()
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.create(**values)
            self.assertEqual(before, self.state())
        lead = self.create(budget_kind="range", budget_min="10", budget_max="20", currency="RUB")
        self.assertEqual(str(lead.budget_min), "10.00")
        result = self.command("lead.edit", {"budget_kind": "exact", "budget_min": "15", "budget_max": "15"}, lead)
        lead.refresh_from_db()
        self.assertEqual(lead.budget_min, lead.budget_max)
        self.command("lead.edit", {"budget_kind": "unknown", "budget_min": None, "budget_max": None}, lead)
        lead.refresh_from_db()
        self.assertIsNone(lead.budget_min)

    def test_take_atomic_scope_current_authority_and_real_task(self):
        lead = CrmLead.objects.create(organization=self.org, title="Unassigned synthetic", needs_review=True)
        result = self.command("lead.take", {"next_action": self.action()}, lead, key="take-one")
        lead.refresh_from_db()
        self.assertEqual((lead.assignee_id, lead.status, lead.needs_review), (self.user.pk, "working", False))
        self.assertEqual(result, self.command("lead.take", {"next_action": self.action()}, lead,
            key="take-one", expected_revision=0))
        with self.assertRaises(CommercialConflict):
            self.command("lead.take", {"next_action": self.action()}, lead)
        with self.assertRaises(PermissionDenied):
            self.command("lead.edit", {"title": "Denied"}, lead, actor=get_user_model().objects.create_user("unrelated"))

    def test_explicit_lead_grant_does_not_need_team_deal_grant(self):
        membership = OrganizationAccess.objects.get(organization=self.org, user=self.user)
        membership.role = "admin"
        membership.save()
        lead = self.create()
        self.assertEqual(lead.assignee_id, self.user.pk)
        with self.assertRaises(PermissionDenied):
            self.command("lead.qualify", {"deals": [self.deal(client_id=self.client.pk)]}, lead)

    def test_dismissed_flags_and_explicit_off_defaults_deny(self):
        self.assertFalse(CrmAccess().view_leads)
        self.assertFalse(CrmAccess().take_leads)
        self.org.crm_v2_write = False
        self.org.save()
        with self.assertRaises(PermissionDenied):
            self.create()
        self.org.crm_v2_write = True
        self.org.save()
        self.employee.is_active = False
        self.employee.save()
        with self.assertRaises(PermissionDenied):
            self.create()

    def test_edit_and_wait_require_current_task_version_and_matching_date(self):
        lead = self.create()
        task = lead.next_task
        before = self.state()
        with self.assertRaises(CommercialConflict):
            self.command("lead.edit", {"next_action": self.action("New step")}, lead)
        self.assertEqual(before, self.state())
        self.command("lead.edit", {"next_action": self.action("New step"), "expected_task_version": version_for(task)}, lead)
        lead.refresh_from_db()
        task.refresh_from_db()
        changes = {"status": "waiting", "reason": "Waiting for data", "follow_up_at": self.due.isoformat(), "expected_task_version": version_for(task)}
        result = self.command("lead.transition", changes, lead, key="wait-result")
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.follow_up_at), ("waiting", self.due))
        self.assertEqual(result, self.command("lead.transition", changes, lead, key="wait-result", expected_revision=result["revision"] - 1))
        with self.assertRaises(ValidationError):
            self.command("lead.transition", {"status": "deferred"}, lead)

    def test_close_results_and_explicit_duplicate_target(self):
        original = self.create()
        duplicate = self.create()
        self.command("lead.transition", {"status": "closed", "result": "duplicate", "target_lead_id": original.pk}, duplicate)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.duplicate_of_id, original.pk)
        self.assertEqual(duplicate.next_task.status, ServiceTask.STATUS_CANCELLED)
        self.assertTrue(duplicate.next_task.is_archived)
        self.assertIsNone(duplicate.next_task.completed_at)
        for outcome in ("spam", "non_target", "no_deal"):
            lead = self.create()
            changes = {"status": "closed", "result": outcome}
            if outcome == "no_deal":
                with self.assertRaises(ValidationError):
                    self.command("lead.transition", changes, lead)
                changes["reason"] = "No commercial decision"
            self.command("lead.transition", changes, lead)
            lead.refresh_from_db()
            self.assertEqual((lead.status, lead.result), ("closed", outcome))
            with self.assertRaises(ValidationError):
                self.command("lead.transition", {"status": "working"}, lead)

    def test_multideal_qualification_replay_additional_independent_tasks(self):
        lead = self.create(client_id=self.client.pk)
        original_task = lead.next_task
        rows = [self.deal(title="Construction"), self.deal(title="Additional need")]
        before_revision = lead.revision
        result = self.command("lead.qualify", {"deals": rows}, lead, key="qualify-multiple")
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.result), ("closed", "qualified"))
        self.assertEqual(len(result["deal_ids"]), 2)
        self.assertEqual(CrmLeadDealLink.objects.filter(lead=lead, kind="qualification").count(), 2)
        tasks = list(CrmDealNextAction.objects.filter(deal_id__in=result["deal_ids"]).values_list("task_link__task_id", flat=True))
        self.assertEqual(len(set(tasks)), 2)
        self.assertNotIn(original_task.pk, tasks)
        self.assertTrue(all(task.crm_item_id is None for task in ServiceTask.objects.filter(pk__in=tasks)))
        replay = self.command("lead.qualify", {"deals": rows}, lead, key="qualify-multiple", expected_revision=before_revision)
        self.assertEqual(result, replay)
        self.assertEqual(CrmDeal.objects.count(), 2)
        extra = self.command("lead.add_deal", {"deals": [self.deal(title="Later decision")]}, lead)
        self.assertEqual(len(extra["deal_ids"]), 1)
        self.assertEqual(CrmLeadDealLink.objects.filter(lead=lead, kind="additional").count(), 1)
        self.assertEqual(CrmDeal.objects.count(), 3)

    def test_qualification_rolls_back_all_deals_if_one_invalid(self):
        lead = self.create(client_id=self.client.pk)
        before = self.state()
        for rows in ([self.deal(), self.deal(pipeline_id=999999)], [self.deal(client_id=999999)]):
            with self.assertRaises((ValidationError, PermissionDenied)):
                self.command("lead.qualify", {"deals": rows}, lead)
            self.assertEqual(before, self.state())
        self.assertEqual(lead.next_task.status, ServiceTask.STATUS_NEW)

    def test_replay_rechecks_source_privacy_and_deal_authority(self):
        lead = self.create(client_id=self.client.pk)
        rows = [self.deal()]
        result = self.command("lead.qualify", {"deals": rows}, lead, key="private-replay")
        with patch("pool_service.services.crm_lead_access.can_read_lead", return_value=False):
            with self.assertRaises(PermissionDenied):
                self.command("lead.qualify", {"deals": rows}, lead, key="private-replay")
        self.grant.qualify_own_leads = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.command("lead.qualify", {"deals": rows}, lead, key="private-replay")
        self.assertEqual(CrmDeal.objects.count(), 1)

    def test_existing_deal_link_and_cross_org_targets(self):
        qualified = self.create(client_id=self.client.pk)
        deal = self.command("lead.qualify", {"deals": [self.deal()]}, qualified)["deal_ids"][0]
        lead = self.create()
        self.command("lead.transition", {"status": "closed", "result": "linked", "target_deal_id": deal}, lead)
        lead.refresh_from_db()
        self.assertEqual(lead.linked_deal_id, deal)
        self.assertEqual(CrmLeadDealLink.objects.get(lead=lead).kind, "existing")
        other = Organization.objects.create(name="Other synthetic")
        foreign_client = Client.objects.create(organization=other, name="Other client")
        foreign_lead = CrmLead.objects.create(organization=other, title="Foreign")
        lead = self.create()
        before = self.state()
        with self.assertRaises(PermissionDenied):
            self.command("lead.edit", {"client_id": foreign_client.pk}, lead)
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.command("lead.transition", {"status": "closed", "result": "duplicate", "target_lead_id": foreign_lead.pk}, lead)
        self.assertEqual(before, self.state())

    def test_new_need_moves_only_thread_preserves_old_result_and_replay(self):
        old = self.create()
        thread = CrmLeadThread.objects.create(organization=self.org, context_key="a" * 64, lead=old)
        self.command("lead.transition", {"status": "closed", "result": "non_target"}, old)
        old.refresh_from_db()
        before_revision = old.revision
        changes = {"title": "Explicit new need", "need": "Different request", "next_action": self.action()}
        result = self.command("lead.new_need", changes, old, key="split-thread")
        thread.refresh_from_db()
        old.refresh_from_db()
        self.assertNotEqual(result["lead_id"], old.pk)
        self.assertEqual(thread.lead_id, result["lead_id"])
        self.assertEqual((old.status, old.result, old.revision), ("closed", "non_target", before_revision + 1))
        self.assertEqual(result, self.command("lead.new_need", changes, old, key="split-thread", expected_revision=before_revision))
        self.assertEqual(CrmLead.objects.count(), 2)
        self.assertEqual(old.events.filter(event_type="lead.new_need").count(), 1)

    def test_old_foundation_entrypoint_cannot_mutate_intake_lead(self):
        lead = self.create()
        with self.assertRaises(ValidationError):
            execute_commercial_command(actor=self.user, organization=self.org, operation_key="old-bypass",
                command_type="lead.edit", subject_id=lead.pk, expected_revision=lead.revision, changes={"title": "Bypass"})

    def test_typed_result_deal_list_is_strict(self):
        for bad in ([], [True], [1, 1], "1", list(range(1, 12))):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                CrmCommand.objects.create(organization=self.org, actor=self.user, operation_key=uuid4().hex,
                    command_type="test", payload_hash="b" * 64, result={"deal_ids": bad})


    def test_acknowledge_closed_continuation_does_not_create_or_change_work(self):
        lead = self.create()
        self.command("lead.transition", {"status": "closed", "result": "non_target"}, lead)
        lead.refresh_from_db()
        lead.needs_review = True
        lead.save()
        task_before = ServiceTask.objects.filter(pk=lead.next_task_id).values().get()
        before_count = CrmLead.objects.count(), CrmDeal.objects.count(), ServiceTask.objects.count()
        revision = lead.revision
        result = self.command("lead.acknowledge", {}, lead, key="ack-current-question")
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead.needs_review, lead.revision),
                         ("closed", "non_target", False, revision + 1))
        self.assertEqual(before_count, (CrmLead.objects.count(), CrmDeal.objects.count(), ServiceTask.objects.count()))
        self.assertEqual(task_before, ServiceTask.objects.filter(pk=lead.next_task_id).values().get())
        self.assertEqual(result, self.command("lead.acknowledge", {}, lead, key="ack-current-question", expected_revision=revision))
        with self.assertRaises(ValidationError):
            self.command("lead.acknowledge", {}, lead)
        with self.assertRaises(ValidationError):
            self.command("lead.acknowledge", {"status": "working"}, lead)
        self.grant.edit_own_leads = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.command("lead.acknowledge", {}, lead, key="ack-current-question", expected_revision=revision)

    def test_model_origin_and_task_provenance_cannot_be_rebound_or_deleted(self):
        lead = self.create()
        link = CrmLeadTaskLink.objects.get(lead=lead)
        with self.assertRaises(ValidationError):
            link.delete()
        with self.assertRaises(ValidationError):
            CrmLeadTaskLink.objects.filter(pk=link.pk).delete()
        with self.assertRaises(ValidationError):
            CrmLeadTaskLink._base_manager.filter(pk=link.pk).delete()
        with self.assertRaises(ValidationError):
            CrmLeadTaskLink._base_manager.filter(pk=link.pk).update(lead_id=lead.pk)
        other = self.create()
        lead.origin_lead = other
        with self.assertRaises(ValidationError):
            lead.save()
        lead.refresh_from_db()
        lead.next_task = other.next_task
        with self.assertRaises(ValidationError):
            lead.save()


@skipUnlessDBFeature("has_select_for_update")
class LeadCommandConcurrencyTests(LeadFixture, TransactionTestCase):
    def parallel(self, functions):
        barrier = Barrier(len(functions))
        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            except (CommercialConflict, PermissionDenied) as exc:
                return type(exc).__name__
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(functions)) as executor:
            return list(executor.map(run, functions))

    def test_double_take_one_owner_one_task(self):
        lead = CrmLead.objects.create(organization=self.org, title="Unassigned")
        other = get_user_model().objects.create_user("other-taker")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        CrmAccess.objects.create(organization=self.org, user=other, view_leads=True, take_leads=True)
        results = self.parallel([
            lambda: self.command("lead.take", {"next_action": self.action()}, lead, actor=self.user),
            lambda: self.command("lead.take", {"next_action": self.action()}, lead, actor=other),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(ServiceTask.objects.count(), 1)
        self.assertEqual(CrmLeadTaskLink.objects.count(), 1)

    def test_same_key_multiconversion_creates_once(self):
        lead = self.create(client_id=self.client.pk)
        rows = [self.deal(), self.deal(title="Second")]
        results = self.parallel([
            lambda: self.command("lead.qualify", {"deals": rows}, lead, key="race-qualify"),
            lambda: self.command("lead.qualify", {"deals": rows}, lead, key="race-qualify"),
        ])
        self.assertEqual(results[0], results[1])
        self.assertEqual(CrmDeal.objects.count(), 2)
        self.assertEqual(CrmLeadDealLink.objects.count(), 2)

    def test_different_key_same_revision_conflicts_without_extra_deals(self):
        lead = self.create(client_id=self.client.pk)
        results = self.parallel([
            lambda: self.command("lead.qualify", {"deals": [self.deal()]}, lead, key="qualify-race-one"),
            lambda: self.command("lead.qualify", {"deals": [self.deal()]}, lead, key="qualify-race-two"),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(CrmDeal.objects.count(), 1)


    def test_free_claim_into_work_competes_with_spam_without_theft_or_orphan_task(self):
        lead = CrmLead.objects.create(organization=self.org, title="Concurrent synthetic disposition")
        other = get_user_model().objects.create_user("disposition-competitor")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        CrmAccess.objects.create(organization=self.org, user=other, view_leads=True, take_leads=True, transition_own_leads=True)
        results = self.parallel([
            lambda: self.command("lead.disposition", {"column": "working", "reason": "Claim real work", "next_action": self.action()}, lead),
            lambda: self.command("lead.disposition", {"column": "spam", "reason": "Unwanted inquiry"}, lead, actor=other),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        lead.refresh_from_db()
        self.assertEqual(ServiceTask.objects.count(), 1 if lead.status == "working" else 0)
        self.assertEqual(CrmEvent.objects.filter(lead=lead).count(), 1)

    def test_same_disposition_key_claims_once_without_extra_history(self):
        lead = CrmLead.objects.create(organization=self.org, title="Concurrent unwanted inquiry")
        changes = {"column": "spam", "reason": "Explicit unwanted inquiry"}
        results = self.parallel([
            lambda: self.command("lead.disposition", changes, lead, key="concurrent-spam-key"),
            lambda: self.command("lead.disposition", changes, lead, key="concurrent-spam-key"),
        ])
        self.assertEqual(results[0], results[1])
        self.assertEqual(CrmEvent.objects.filter(lead=lead).count(), 1)
        self.assertEqual(ServiceTask.objects.count(), 0)


    def test_parallel_new_contact_same_phone_creates_one_identity(self):
        first=self.create();second=self.create()
        contact=dict(type='private',person_name='Synthetic race person',company_name='',phone='+79990004455',inn='')
        def convert(lead):
            try:
                return self.command('lead.qualify',{'deals':[self.deal(new_contact={**contact,'person_name':'Synthetic race '+str(lead.pk),'phone':'+7 (999) 000-44-55' if lead.pk==first.pk else '89990004455'})]},lead)
            except ValidationError:
                return 'duplicate_contact'
        results=self.parallel([lambda:convert(first),lambda:convert(second)])
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual(Client.objects.filter(name__startswith='Synthetic race ').count(),1)
        self.assertEqual(CrmDeal.objects.count(),1)

    def test_parallel_transfer_same_key_preserves_single_task(self):
        lead=self.create(client_id=self.client.pk);task=lead.next_task
        changes={'expected_task_version':version_for(task),'deals':[self.deal(client_id=self.client.pk,reuse_next_action=True,next_action=self.action())]}
        results=self.parallel([lambda:self.command('lead.qualify',changes,lead,key='transfer-race'),lambda:self.command('lead.qualify',changes,lead,key='transfer-race')])
        self.assertEqual(results[0],results[1]);self.assertEqual(CrmDeal.objects.count(),1)
        self.assertEqual(ServiceTask.objects.count(),1)
        self.assertEqual(CrmDeal.objects.get().next_action.task_link.task_id,task.pk)


from pool_service.tests.test_crm_inbox import Fixture as InboxFixture
from pool_service.communication_models import CommunicationAccess
from pool_service.services.crm_inbox import claim_one, deliver_claim
from pool_service.services.crm_lead_intake import consume_incoming
from pool_service.services.crm_lead_access import can_read_lead, leads_for_user



class SplitLeadSourcePrivacyTests(InboxFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.crm_v2_read = self.org.crm_v2_write = self.org.crm_intake_process = True
        self.org.paid_until = self.now + timedelta(days=30)
        self.org.save()
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now - timedelta(minutes=30))
        self.user = get_user_model().objects.create_user("split-synthetic-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        CrmAccess.objects.create(organization=self.org, user=self.user, view_leads=True,
            create_own_leads=True, take_leads=True, edit_own_leads=True, transition_own_leads=True)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_conversations=True)
        self.next_action = {"title": "Clarify explicit need", "due_at": (self.now + timedelta(days=1)).isoformat()}

    def command(self, kind, changes, lead, key=None):
        return execute_lead_command(actor=self.user, organization=self.org, operation_key=key or uuid4().hex,
            command_type=kind, subject_id=lead.pk, expected_revision=lead.revision, changes=changes)

    def test_new_need_keeps_root_source_privacy_after_revocation(self):
        self.incoming()
        claim = claim_one(**self.scope())
        self.assertEqual(deliver_claim(*claim, consumer=consume_incoming), "done")
        old = CrmLead.objects.get()
        self.command("lead.take", {"next_action": self.next_action}, old)
        old.refresh_from_db()
        self.command("lead.transition", {"status": "closed", "result": "non_target"}, old)
        old.refresh_from_db()
        result = self.command("lead.new_need", {"title": "New explicit need", "need": "New request", "next_action": self.next_action}, old)
        new = CrmLead.objects.get(pk=result["lead_id"])
        self.assertEqual(new.origin_lead_id, old.pk)
        self.assertFalse(new.sources.exists())
        self.assertTrue(old.sources.exists())
        self.assertTrue(can_read_lead(self.user, self.org, new))
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_conversations=False)
        self.assertFalse(can_read_lead(self.user, self.org, new))
        self.assertFalse(leads_for_user(self.user, self.org).filter(pk=new.pk).exists())
        with self.assertRaises(PermissionDenied):
            self.command("lead.edit", {"title": "Cannot mutate hidden derived lead"}, new)


    def test_qualified_deal_task_inherits_call_privacy_without_copying_source(self):
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.communication_models import PhoneCall, TelephonyEmployeeIdentity
        from pool_service.services.task_read_permissions import readable_tasks
        from pool_service.services.call_privacy import private_source_task_ids
        self.megafon()
        employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic mapper")
        TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension="101", external_user="synthetic-provider", employee=employee,
            status=TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED, match_method="manual",
            confirmed_by=self.user, confirmed_at=self.now)
        grant = CrmAccess.objects.get(organization=self.org, user=self.user)
        grant.qualify_own_leads = grant.create_own_deals = grant.manage_own_next_action = True
        grant.save()
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_all_calls=True)
        self.assertEqual(self.history(user="synthetic-provider", ext="101").status_code, 200)
        source = PhoneCall.objects.get(connection=self.telephony)
        self.assertEqual(deliver_claim(*claim_one(**self.scope("call")), consumer=consume_incoming), "done")
        lead = CrmLead.objects.get()
        customer = Client.objects.create(organization=self.org, name="Synthetic customer")
        self.command("lead.edit", {"need": "Confirmed pool need", "client_id": customer.pk,
            "next_action": self.next_action}, lead)
        lead.refresh_from_db()
        self.assertEqual(lead.next_task.payload_json["source_call_id"], source.pk)
        bootstrap_commercial_settings(self.org.pk)
        pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        result = self.command("lead.qualify", {"deals": [{"pipeline_id": pipeline.pk,
            "title": "Explicit manager business title", "next_action": self.next_action}]}, lead)
        pointer = CrmDealNextAction.objects.get(deal_id=result["deal_ids"][0])
        task = pointer.task_link.task
        self.assertEqual(task.payload_json["source_call_id"], source.pk)
        self.assertEqual(task.description, "")
        self.assertTrue(readable_tasks(ServiceTask.objects.filter(pk=task.pk), self.user))
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, phone_key="9001234567")
        self.assertEqual(private_source_task_ids([task]), {task.pk})
        self.assertFalse(readable_tasks(ServiceTask.objects.filter(pk=task.pk), self.user))
        self.assertFalse(can_read_lead(self.user, self.org, lead))


class LeadDispositionTests(LeadFixture, TestCase):
    def move(self, lead, column, **changes):
        values = {"column": column, "reason": "Synthetic explicit classification"}
        if lead.next_task_id:
            values["expected_task_version"] = version_for(ServiceTask.objects.get(pk=lead.next_task_id))
        values.update(changes)
        result = self.command("lead.disposition", values, lead)
        lead.refresh_from_db()
        return result

    def test_spam_restore_creates_new_task_retains_source_history_and_links(self):
        lead = self.create()
        old_id = lead.next_task_id
        self.move(lead, "spam")
        self.assertEqual((lead.status, lead.result), ("closed", "spam"))
        old = ServiceTask.objects.get(pk=old_id)
        self.assertTrue(old.is_archived)
        self.move(lead, "unparsed")
        self.assertEqual((lead.status, lead.result, lead.needs_review), ("new", "", True))
        self.move(lead, "working", next_action=self.action("A new real step"))
        self.assertNotEqual(lead.next_task_id, old_id)
        old.refresh_from_db()
        self.assertTrue(old.is_archived)
        self.assertEqual(CrmLeadTaskLink.objects.filter(lead=lead).count(), 2)
        self.assertEqual(CrmEvent.objects.filter(lead=lead, event_type="lead.disposition").count(), 3)

    def test_work_to_unparsed_preserves_stage_owner_and_task(self):
        lead = self.create()
        original = lead.next_task_id
        self.move(lead, "unparsed")
        self.assertEqual((lead.status, lead.assignee_id, lead.next_task_id, lead.needs_review), ("working", self.user.pk, original, True))
        self.move(lead, "working")
        self.assertFalse(lead.needs_review)
        self.assertEqual(lead.next_task_id, original)
        self.assertFalse(ServiceTask.objects.get(pk=original).is_archived)
        events = list(CrmEvent.objects.filter(lead=lead, event_type="lead.disposition").order_by("pk"))
        self.assertEqual(events[0].safe_diff["column"], {"before": "working", "after": "unparsed"})
        self.assertEqual(events[0].safe_diff["needs_review"], {"before": False, "after": True})

    def test_free_spam_claim_requires_both_caps_no_fabricated_task_or_theft(self):
        lead = CrmLead.objects.create(organization=self.org, title="Free synthetic inquiry")
        self.grant.take_leads = False; self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.move(lead, "spam")
        self.grant.take_leads = True; self.grant.save()
        self.move(lead, "spam")
        self.assertEqual(lead.assignee_id, self.user.pk)
        self.assertFalse(ServiceTask.objects.exists())
        other = get_user_model().objects.create_user("other-synthetic-owner")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        lead.assignee = other; lead.save()
        with self.assertRaises(CommercialConflict):
            self.move(lead, "deleted")

    def test_trash_restore_retains_qualified_deals_and_never_reopens(self):
        lead = self.create(client_id=self.client.pk)
        self.command("lead.qualify", {"deals": [self.deal()]}, lead)
        lead.refresh_from_db()
        deal_ids = list(CrmLeadDealLink.objects.filter(lead=lead).values_list("deal_id", flat=True))
        old_task = lead.next_task_id
        self.move(lead, "deleted")
        self.move(lead, "restore")
        self.assertEqual((lead.status, lead.result, lead.next_task_id), ("closed", "qualified", old_task))
        self.assertEqual(list(CrmLeadDealLink.objects.filter(lead=lead).values_list("deal_id", flat=True)), deal_ids)
        with self.assertRaises(ValidationError):
            self.move(lead, "working", next_action=self.action())
        with self.assertRaises(ValidationError):
            self.move(lead, "spam")

    def test_cancel_checks_task_version_and_restore_requires_real_next_step(self):
        lead = self.create()
        stale = version_for(lead.next_task)
        task = lead.next_task; task.title = "Changed in calendar"; task.save()
        with self.assertRaises(CommercialConflict):
            self.move(lead, "deleted", expected_task_version=stale)
        task.refresh_from_db(); self.assertFalse(task.is_archived)
        self.move(lead, "deleted")
        self.move(lead, "restore")
        self.assertTrue(lead.needs_review)
        with self.assertRaises(ValidationError):
            self.move(lead, "working")
        self.move(lead, "working", next_action=self.action())
        self.assertFalse(lead.needs_review)

    def test_replay_stale_revision_payload_and_current_capability(self):
        lead = self.create()
        values = {"column": "spam", "reason": "Explicit synthetic reason", "expected_task_version": version_for(lead.next_task)}
        first = self.command("lead.disposition", values, lead, key="spam-replay")
        self.assertEqual(first, self.command("lead.disposition", values, lead, key="spam-replay"))
        with self.assertRaises(CommercialConflict):
            self.command("lead.disposition", {**values, "reason": "Changed"}, lead, key="spam-replay")
        with self.assertRaises(CommercialConflict):
            self.command("lead.disposition", {"column": "deleted", "reason": "Stale"}, lead)
        self.grant.transition_own_leads = False; self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.command("lead.disposition", values, lead, key="spam-replay")


    def test_d01_working_without_task_requires_real_step_and_waiting_restore_keeps_check_date(self):
        lead = CrmLead.objects.create(organization=self.org, assignee=self.user,
            title="Confirmed synthetic answer", status="working", needs_review=True)
        with self.assertRaises(ValidationError):
            self.move(lead, "working")
        self.move(lead, "working", next_action=self.action())
        self.command("lead.transition", {"status": "waiting", "reason": "Customer decision", "follow_up_at": self.due.isoformat(),
            "expected_task_version": version_for(lead.next_task)}, lead)
        lead.refresh_from_db()
        self.move(lead, "deleted")
        self.move(lead, "restore")
        self.move(lead, "working", next_action=self.action("New check after restoration"))
        self.assertEqual(lead.status, "waiting")
        self.assertEqual(lead.follow_up_at, lead.next_task.due_at)
