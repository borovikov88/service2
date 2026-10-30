from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess, CrmCommand, CrmDeal, CrmEvent, CrmLead, CrmPipeline, CrmStage
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.services import crm_commercial_commands as commands
from pool_service.services.crm_commercial_commands import CommercialConflict, execute_commercial_command


class CommandFixture:
    def setUp(self):
        self.org = Organization.objects.create(name="Command synthetic", crm_v2_read=True, crm_v2_write=True)
        self.user = get_user_model().objects.create_user("command-synthetic")
        self.membership = OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic")
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.user,
            edit_own_leads=True, transition_own_leads=True, edit_own_deals=True, transition_own_deals=True)
        bootstrap_commercial_settings(self.org.pk)
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.stage = self.pipeline.stages.get(code="need")
        self.next_stage = self.pipeline.stages.get(code="technical")
        self.lead = CrmLead.objects.create(organization=self.org, assignee=self.user, title="Lead")
        self.deal = CrmDeal.objects.create(organization=self.org, manager=self.user, pipeline=self.pipeline, stage=self.stage, title="Deal")

    def execute(self, **kwargs):
        values = dict(actor=self.user, organization=self.org, operation_key="edit-1", command_type="lead.edit",
                      subject_id=self.lead.pk, expected_revision=0, changes={"title": "Changed"})
        values.update(kwargs)
        return execute_commercial_command(**values)

    def snapshot(self):
        return (list(CrmLead.objects.values()), list(CrmDeal.objects.values()),
                list(CrmCommand.objects.values()), list(CrmEvent.objects.values()))

    def rejected(self, exception=ValidationError, **kwargs):
        before = self.snapshot()
        with self.assertRaises(exception):
            self.execute(**kwargs)
        self.assertEqual(before, self.snapshot())


class CommercialCommandTests(CommandFixture, TestCase):
    def test_edit_replay_and_canonical_payload_are_atomic_and_safe(self):
        result = self.execute(changes={"need": "  bounded own need  ", "title": "Changed"})
        self.assertEqual(result, self.execute(changes={"title": " Changed ", "need": "bounded own need"}))
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.revision, self.lead.need), (1, "bounded own need"))
        self.assertIsNone(self.lead.budget_min)
        self.assertEqual(CrmCommand.objects.count(), 1)
        self.assertEqual(CrmEvent.objects.count(), 1)
        event = CrmEvent.objects.get()
        self.assertEqual(event.safe_diff, {"revision": {"before": 0, "after": 1},
            "title_changed": {"before": False, "after": True}, "need_changed": {"before": False, "after": True}})
        self.assertNotIn("bounded own need", str(event.safe_diff))
        self.rejected(CommercialConflict, changes={"title": "Different"})
        self.rejected(CommercialConflict, operation_key="other-key")

    def test_strict_keys_subject_revision_and_field_surface(self):
        for key in ("UPPER", "trailing ", "café", "final\n", "", "a" * 101):
            self.rejected(operation_key=key)
        for revision in (True, -1, "0"):
            self.rejected(expected_revision=revision)
        for changes in ({"manager_id": self.user.pk}, {"client_id": None}, {"budget_min": 0}, {"outcome": "won"}, {"title": ""}, {}):
            self.rejected(changes=changes)
        self.rejected(command_type="unknown")
        for malformed_type in ([], {}):
            self.rejected(command_type=malformed_type)
        self.rejected(command_type="lead.transition", changes={"status": "waiting", "reason": "Check", "follow_up_at": "2026-99-26T10:00:00+00:00"})

    def test_follow_up_utc_overflow_rejects_string_and_typed_boundaries_without_writes(self):
        boundaries = ("9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59")
        for boundary in boundaries:
            for value in (boundary, datetime.fromisoformat(boundary)):
                with self.subTest(value=value):
                    self.rejected(command_type="lead.transition", changes={
                        "status": "waiting", "reason": "Check date", "follow_up_at": value})

    def test_fresh_account_membership_hr_flags_and_explicit_capabilities(self):
        mutations = [
            (get_user_model(), self.user.pk, {"is_active": False}, {"is_active": True}),
            (Organization, self.org.pk, {"crm_v2_write": False}, {"crm_v2_write": True}),
            (Organization, self.org.pk, {"crm_v2_read": False}, {"crm_v2_read": True}),
            (Employee, self.employee.pk, {"is_active": False}, {"is_active": True}),
            (Employee, self.employee.pk, {"employment_status": Employee.STATUS_DISMISSED}, {"employment_status": Employee.STATUS_EMPLOYED}),
        ]
        self.execute()
        for model, pk, changed, restored in mutations:
            model.objects.filter(pk=pk).update(**changed)
            self.rejected(PermissionDenied)
            model.objects.filter(pk=pk).update(**restored)
        self.grant.edit_own_leads = False
        self.grant.save()
        self.rejected(PermissionDenied)
        self.grant.edit_own_leads = True
        self.grant.save()
        self.membership.delete()
        self.rejected(PermissionDenied)

    def test_team_read_role_does_not_grant_writes_or_other_ownership(self):
        for role in ("manager", "owner", "admin"):
            self.membership.role = role
            self.membership.save()
            self.grant.edit_own_leads = False
            self.grant.save()
            self.rejected(PermissionDenied)
        self.grant.edit_own_leads = True
        self.grant.save()
        other = get_user_model().objects.create_user("other-synthetic")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        self.lead.assignee = other
        self.lead.save()
        self.rejected(PermissionDenied)

    def test_each_own_capability_is_explicit_and_cannot_edit_another_kind(self):
        from pool_service.services.crm_commercial_access import can_write_commercial
        self.assertTrue(can_write_commercial(self.user, self.org))
        cases = (
            ("lead.edit", "edit_own_leads", self.lead.pk, {"title": "Changed"}),
            ("lead.transition", "transition_own_leads", self.lead.pk, {"status": "working"}),
            ("deal.edit", "edit_own_deals", self.deal.pk, {"title": "Changed"}),
            ("deal.transition", "transition_own_deals", self.deal.pk, {"stage_id": self.next_stage.pk}),
        )
        for command_type, capability, subject_id, changes in cases:
            setattr(self.grant, capability, False)
            self.grant.save()
            self.rejected(PermissionDenied, command_type=command_type, subject_id=subject_id, changes=changes)
            setattr(self.grant, capability, True)
            self.grant.save()
        self.execute(command_type="deal.edit", subject_id=self.deal.pk, changes={"title": "Own deal"})
        other = get_user_model().objects.create_user("other-deal-synthetic")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        self.deal.refresh_from_db()
        self.deal.manager = other
        self.deal.save()
        self.rejected(PermissionDenied, command_type="deal.edit", subject_id=self.deal.pk, expected_revision=1, changes={"title": "Forbidden"})

    def test_wrong_org_foreign_fk_and_current_graph_fail_without_writes(self):
        other_org = Organization.objects.create(name="Foreign synthetic", crm_v2_read=True, crm_v2_write=True)
        self.rejected(PermissionDenied, organization=other_org)
        self.rejected(PermissionDenied, subject_id=self.lead.pk + 9999)
        # Legacy client reparent is intentionally still allowed; commands/replay
        # must reject the resulting inconsistent graph rather than repair silently.
        client = Client.objects.create(organization=self.org, name="Synthetic")
        self.lead.client = client
        self.lead.save()
        self.execute()
        Client.objects.filter(pk=client.pk).update(organization=other_org)
        self.rejected()
        foreign = CrmPipeline.objects.create(organization=other_org, code="foreign", name="Foreign")
        stage = CrmStage.objects.create(pipeline=foreign, code="need", name="Foreign")
        self.rejected(command_type="deal.transition", subject_id=self.deal.pk, changes={"stage_id": stage.pk})
        same_org_pipeline = CrmPipeline.objects.get(organization=self.org, code="service")
        self.rejected(command_type="deal.transition", subject_id=self.deal.pk, changes={"stage_id": same_org_pipeline.stages.first().pk})

    def test_lead_waiting_backward_reason_date_and_safe_history(self):
        self.rejected(command_type="lead.transition", changes={"status": "waiting"})
        self.rejected(command_type="lead.transition", changes={"status": "waiting", "reason": "Check", "follow_up_at": "2026-10-26T10:00:00"})
        due = timezone.now() + timedelta(days=1)
        first = self.execute(command_type="lead.transition", changes={"status": "waiting", "reason": "Client asked to wait", "follow_up_at": due})
        self.assertEqual(first, self.execute(command_type="lead.transition", changes={"follow_up_at": due.isoformat(), "reason": "Client asked to wait", "status": "waiting"}))
        self.rejected(command_type="lead.transition", operation_key="back", expected_revision=1, changes={"status": "working"})
        self.execute(command_type="lead.transition", operation_key="back", expected_revision=1, changes={"status": "working", "reason": "Clarification resumed"})
        self.assertEqual(CrmEvent.objects.last().reason, "Clarification resumed")
        self.assertEqual(CrmEvent.objects.last().safe_diff["status"], {"before": "waiting", "after": "working"})
        self.rejected(command_type="lead.transition", operation_key="close", expected_revision=2, changes={"status": "closed"})

    def test_all_manual_stages_keep_financial_and_execution_facts_unknown(self):
        for pipeline in CrmPipeline.objects.filter(organization=self.org):
            stages = list(pipeline.stages.order_by("position", "pk"))
            deal = CrmDeal.objects.create(organization=self.org, manager=self.user,
                pipeline=pipeline, stage=stages[0], title="Synthetic manual progress")
            revision = 0
            for stage in stages[1:]:
                kwargs = dict(command_type="deal.transition", subject_id=deal.pk,
                    operation_key=f"manual-{pipeline.pk}-{stage.pk}", expected_revision=revision,
                    changes={"stage_id": stage.pk})
                if stage.code == "completed":
                    self.rejected(**kwargs)
                    continue
                result = self.execute(**kwargs)
                self.assertEqual(result, self.execute(**kwargs))
                revision += 1
                deal.refresh_from_db()
                self.assertEqual((deal.stage_id, deal.outcome, deal.revision), (stage.pk, "open", revision))
                self.assertIsNone(deal.completed_at)
                self.assertIsNone(deal.closed_at)
                self.assertIsNone(deal.amount)
                self.assertEqual(CrmEvent.objects.filter(deal=deal).count(), revision)
            self.rejected(command_type="deal.transition", subject_id=deal.pk,
                operation_key=f"manual-back-{pipeline.pk}", expected_revision=revision,
                changes={"stage_id": stages[0].pk})

    def test_deal_transitions_use_codes_and_preserve_unknown_finance(self):
        self.next_stage.position = 999
        self.next_stage.name = "Успешно завершено"
        self.next_stage.save()
        self.execute(command_type="deal.transition", subject_id=self.deal.pk, changes={"stage_id": self.next_stage.pk})
        self.rejected(command_type="deal.transition", subject_id=self.deal.pk, operation_key="back", expected_revision=1, changes={"stage_id": self.stage.pk})
        self.execute(command_type="deal.transition", subject_id=self.deal.pk, operation_key="back", expected_revision=1, changes={"stage_id": self.stage.pk, "reason": "Review needed"})
        for code in ("completed",):
            self.rejected(command_type="deal.transition", subject_id=self.deal.pk, operation_key=code, expected_revision=2, changes={"stage_id": self.pipeline.stages.get(code=code).pk})
        self.next_stage.is_active = False
        self.next_stage.save()
        self.rejected(command_type="deal.transition", subject_id=self.deal.pk, operation_key="disabled", expected_revision=2, changes={"stage_id": self.next_stage.pk})
        self.deal.refresh_from_db()
        self.assertEqual((self.deal.outcome, self.deal.revision), ("open", 2))
        self.assertIsNone(self.deal.amount)
        self.assertIsNone(self.deal.completed_at)
        self.assertEqual(CrmEvent.objects.last().reason, "Review needed")

    def test_event_failure_or_outer_rollback_leaves_no_partial_success(self):
        before = self.snapshot()
        with patch.object(CrmEvent, "save", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.execute()
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.execute()
                raise RuntimeError("outer rollback")
        self.assertEqual(before, self.snapshot())

    def test_additive_capabilities_default_off_no_automatic_grant(self):
        self.grant.delete()
        grant = CrmAccess.objects.create(organization=self.org, user=self.user)
        self.assertTrue(all(not getattr(grant, name) for name in ("edit_own_leads", "transition_own_leads", "edit_own_deals", "transition_own_deals")))
        bootstrap_commercial_settings(self.org.pk)
        self.rejected(PermissionDenied)


@skipUnlessDBFeature("has_select_for_update")
class CommercialCommandConcurrencyTests(CommandFixture, TransactionTestCase):
    """Real separate connections; SQLite skips are not lock evidence."""
    def threaded(self, fn):
        close_old_connections()
        try:
            return fn()
        finally:
            connections["default"].close()

    def race(self, first, second):
        barrier = Barrier(2)
        def run(kwargs):
            barrier.wait(timeout=10)
            try:
                return ("ok", self.execute(**kwargs))
            except CommercialConflict:
                return ("conflict", None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.threaded, lambda kwargs=kwargs: run(kwargs)) for kwargs in (first, second)]
            return [future.result(timeout=20) for future in futures]

    def test_same_revision_one_change_event_and_command(self):
        results = self.race({"operation_key": "one"}, {"operation_key": "two", "changes": {"title": "Other"}})
        self.assertEqual(sorted(result[0] for result in results), ["conflict", "ok"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.revision, 1)
        self.assertEqual((CrmEvent.objects.count(), CrmCommand.objects.count()), (1, 1))

    def test_same_key_same_content_replays_once(self):
        results = self.race({}, {})
        self.assertEqual(results[0], results[1])
        self.assertEqual((CrmEvent.objects.count(), CrmCommand.objects.count()), (1, 1))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.revision, 1)

    def test_same_key_different_content_conflicts_once(self):
        results = self.race({}, {"changes": {"title": "Different"}})
        self.assertEqual(sorted(result[0] for result in results), ["conflict", "ok"])
        self.assertEqual((CrmEvent.objects.count(), CrmCommand.objects.count()), (1, 1))

    def blocked_command_after_change(self, change, kwargs=None):
        """Change is uncommitted while command attempts its locking read."""
        held, release, started, done = Event(), Event(), Event(), Event()
        def mutate():
            with transaction.atomic():
                change()
                held.set()
                if not release.wait(10):
                    raise RuntimeError("Test release timed out")
        def execute():
            started.set()
            try:
                self.execute(**(kwargs or {}))
            except (PermissionDenied, ValidationError):
                return "denied"
            finally:
                done.set()
            return "ok"
        with ThreadPoolExecutor(max_workers=2) as pool:
            mutation = pool.submit(self.threaded, mutate)
            self.assertTrue(held.wait(10))
            command = pool.submit(self.threaded, execute)
            try:
                self.assertTrue(started.wait(10))
                self.assertFalse(done.wait(.2), "Command did not wait for the uncommitted authorization/settings change")
            finally:
                release.set()
            mutation.result(timeout=20)
            self.assertEqual(command.result(timeout=20), "denied")

    def test_concurrent_revocation_precedes_execute_and_replay(self):
        for replay in (False, True):
            if replay:
                self.execute()
            before = self.snapshot()
            def revoke():
                grant = CrmAccess.objects.get(pk=self.grant.pk)
                grant.edit_own_leads = False
                grant.save()
            self.blocked_command_after_change(revoke)
            self.assertEqual(before, self.snapshot())
            self.grant.edit_own_leads = True
            self.grant.save()

    def test_concurrent_user_membership_hr_and_flag_revocation(self):
        cases = [
            (lambda: get_user_model().objects.filter(pk=self.user.pk).update(is_active=False), lambda: get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)),
            (lambda: OrganizationAccess.objects.filter(pk=self.membership.pk).update(role="service"), lambda: OrganizationAccess.objects.filter(pk=self.membership.pk).update(role="manager")),
            (lambda: Employee.objects.filter(pk=self.employee.pk).update(is_active=False), lambda: Employee.objects.filter(pk=self.employee.pk).update(is_active=True)),
            (lambda: Organization.objects.filter(pk=self.org.pk).update(crm_v2_write=False), lambda: Organization.objects.filter(pk=self.org.pk).update(crm_v2_write=True)),
        ]
        for mutate, restore in cases:
            before = self.snapshot()
            self.blocked_command_after_change(mutate)
            self.assertEqual(before, self.snapshot())
            restore()

    def test_concurrent_stage_disable_is_seen_after_wait(self):
        def disable():
            stage = CrmStage.objects.get(pk=self.next_stage.pk)
            stage.is_active = False
            stage.save()
        before = self.snapshot()
        self.blocked_command_after_change(disable, {"command_type": "deal.transition", "subject_id": self.deal.pk, "changes": {"stage_id": self.next_stage.pk}})
        self.assertEqual(before, self.snapshot())

    def test_command_holds_current_access_until_commit_then_replay_is_denied(self):
        held, release, revoke_started, revoked = Event(), Event(), Event(), Event()
        original = commands._apply
        def pause(*args, **kwargs):
            held.set()
            if not release.wait(10):
                raise RuntimeError("Test release timed out")
            return original(*args, **kwargs)
        def revoke():
            revoke_started.set()
            grant = CrmAccess.objects.get(pk=self.grant.pk)
            grant.edit_own_leads = False
            grant.save()
            revoked.set()
        with patch.object(commands, "_apply", side_effect=pause), ThreadPoolExecutor(max_workers=2) as pool:
            command = pool.submit(self.threaded, self.execute)
            self.assertTrue(held.wait(10))
            revocation = pool.submit(self.threaded, revoke)
            try:
                self.assertTrue(revoke_started.wait(10))
                self.assertFalse(revoked.wait(.2))
            finally:
                release.set()
            result = command.result(timeout=20)
            revocation.result(timeout=20)
        self.assertEqual(result["revision"], 1)
        self.rejected(PermissionDenied)
        self.assertEqual((CrmCommand.objects.count(), CrmEvent.objects.count()), (1, 1))
