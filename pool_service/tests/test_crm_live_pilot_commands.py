"""Own-deal pilot commands: real task lifecycle, scope, replay and concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import (
    CrmAccess, CrmCommand, CrmDeal, CrmDealNextAction, CrmDealTaskLink, CrmEvent, CrmPipeline,
)
from pool_service.models import Client, CrmItem, Employee, Organization, OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.services import crm_commercial_commands as commands
from pool_service.services.crm_commercial_commands import (
    CommercialConflict, create_commercial_deal, execute_commercial_command, set_deal_next_action,
)
from pool_service.services.task_feedback import apply_feedback, version_for


class PilotFixture:
    def setUp(self):
        self.org = Organization.objects.create(name="Pilot synthetic", crm_v2_read=True, crm_v2_write=True)
        self.user = get_user_model().objects.create_user("pilot-synthetic")
        self.member = OrganizationAccess.objects.create(organization=self.org, user=self.user, role="manager")
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic")
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.user,
            create_own_deals=True, manage_own_next_action=True, edit_own_deals=True, transition_own_deals=True)
        self.client_record = Client.objects.create(organization=self.org, name="Pilot client")
        bootstrap_commercial_settings(self.org.pk)
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.due = datetime.fromisoformat("2026-10-27T10:30:00+07:00")

    def create(self, **kwargs):
        values = dict(actor=self.user, organization=self.org, operation_key="pilot-create",
            client_id=self.client_record.pk, pipeline_id=self.pipeline.pk, title="Synthetic project",
            next_action={"title": "Call back", "due_at": self.due})
        values.update(kwargs)
        return create_commercial_deal(**values)

    def next_action(self, result, **kwargs):
        task = ServiceTask.objects.get(pk=result["task_id"])
        values = dict(actor=self.user, organization=self.org, operation_key="pilot-next",
            subject_id=result["deal_id"], expected_revision=result["revision"], title="Prepare calculation",
            due_at=self.due + timedelta(days=1), expected_task_version=version_for(task))
        values.update(kwargs)
        return set_deal_next_action(**values)

    def state(self):
        return [list(model.objects.order_by("pk").values()) for model in
            (CrmDeal, CrmDealTaskLink, CrmDealNextAction, ServiceTask, ServiceTaskChange, CrmCommand, CrmEvent)]

    def reject_create(self, exception=ValidationError, **kwargs):
        before = self.state()
        with self.assertRaises(exception):
            self.create(**kwargs)
        self.assertEqual(before, self.state())


class LivePilotCommandTests(PilotFixture, TestCase):
    def test_create_has_one_real_next_task_and_safe_history_without_legacy_or_finance(self):
        result = self.create()
        deal = CrmDeal.objects.get(pk=result["deal_id"])
        task = ServiceTask.objects.get(pk=result["task_id"])
        self.assertEqual((deal.manager_id, deal.client_id, deal.revision, deal.stage.code),
                         (self.user.pk, self.client_record.pk, 1, "need"))
        self.assertEqual(deal.next_action.task_link.task_id, task.pk)
        self.assertEqual(list(task.responsibles.values_list("pk", flat=True)), [self.user.pk])
        self.assertEqual((task.created_by_id, task.primary_responsible_id, task.organization_id),
                         (self.user.pk, self.user.pk, self.org.pk))
        self.assertEqual(task.start_date.isoformat(), "2026-10-27")
        self.assertEqual(task.start_time.isoformat(), "10:30:00")
        self.assertEqual(task.due_at, self.due)
        self.assertIsNone(task.crm_item_id)
        self.assertIsNone(task.water_reading_id)
        self.assertEqual((CrmItem.objects.count(), ServiceTaskChange.objects.count()), (0, 1))
        self.assertIsNone(deal.amount)
        self.assertEqual((deal.amount_basis, deal.outcome), ("unknown", "open"))
        self.assertNotIn("Call back", str(CrmEvent.objects.get().safe_diff))
        self.assertNotIn("Synthetic project", str(CrmEvent.objects.get().safe_diff))
        same = self.create(title=" Synthetic project ", next_action={"title": "Call back ",
            "due_at": self.due.astimezone(timezone.get_default_timezone()).isoformat()})
        self.assertEqual(result, same)
        self.assertEqual((CrmDeal.objects.count(), ServiceTask.objects.count(), CrmEvent.objects.count()), (1, 1, 1))

    def test_required_next_action_strict_fields_dates_keys_and_foreign_identifiers(self):
        for invalid in (None, {}, {"title": "Call"}, {"title": "", "due_at": self.due},
                {"title": "Call", "due_at": "2026-10-27T10:00:00"},
                {"title": "Call", "due_at": "not a date"},
                {"title": "Call", "due_at": "9999-12-31T23:59:59-23:59"},
                {"title": "Call", "due_at": self.due, "assignee": self.user.pk}):
            with self.subTest(next_action=invalid):
                self.reject_create(next_action=invalid)
        for invalid in (True, "1", -1):
            self.reject_create(client_id=invalid)
        for key in ("UPPER", "", "trailing ", "a" * 101):
            self.reject_create(operation_key=key)
        foreign = Organization.objects.create(name="Foreign pilot")
        other_client = Client.objects.create(organization=foreign, name="Foreign client")
        self.reject_create(PermissionDenied, client_id=other_client.pk)
        self.reject_create(PermissionDenied, pipeline_id=self.pipeline.pk + 9999)

    def test_merged_client_and_disabled_initial_stage_are_not_selectable(self):
        target = Client.objects.create(organization=self.org, name="Merged target")
        ClientCRMProfile.objects.create(client=self.client_record, merged_into=target)
        self.reject_create(PermissionDenied)
        self.client_record.crm_profile.delete()
        stage = self.pipeline.stages.get(code="need")
        stage.is_active = False
        stage.save()
        self.reject_create()
        stage.is_active = True
        stage.position = 100
        stage.save()
        result = self.create()
        self.assertEqual(CrmDeal.objects.get(pk=result["deal_id"]).stage_id, stage.pk)

    def test_distinct_content_cannot_reuse_a_create_key(self):
        self.create()
        self.reject_create(CommercialConflict, title="Other project")
        self.reject_create(CommercialConflict, next_action={"title": "Other task", "due_at": self.due})

    def test_create_and_replay_recheck_both_explicit_capabilities_and_current_identity(self):
        self.create()
        for name in ("create_own_deals", "manage_own_next_action"):
            setattr(self.grant, name, False)
            self.grant.save()
            self.reject_create(PermissionDenied)
            setattr(self.grant, name, True)
            self.grant.save()
        for model, pk, changed, restored in (
            (Organization, self.org.pk, {"crm_v2_write": False}, {"crm_v2_write": True}),
            (Organization, self.org.pk, {"crm_v2_read": False}, {"crm_v2_read": True}),
            (get_user_model(), self.user.pk, {"is_active": False}, {"is_active": True}),
            (Employee, self.employee.pk, {"employment_status": Employee.STATUS_DISMISSED},
             {"employment_status": Employee.STATUS_EMPLOYED}),
        ):
            model.objects.filter(pk=pk).update(**changed)
            self.reject_create(PermissionDenied)
            model.objects.filter(pk=pk).update(**restored)
        self.member.delete()
        self.reject_create(PermissionDenied)

    def test_owner_has_no_implicit_crm_grant_and_new_capabilities_default_off(self):
        self.member.role = "owner"
        self.member.save()
        self.reject_create(PermissionDenied)
        self.grant.view_team_deals = True
        self.grant.save()
        self.create()
        self.grant.delete()
        grant = CrmAccess.objects.create(organization=self.org, user=self.user, view_team_deals=True)
        self.assertFalse(grant.create_own_deals)
        self.assertFalse(grant.manage_own_next_action)
        self.reject_create(PermissionDenied)

    def test_failed_event_rolls_back_deal_task_participants_pointer_and_command(self):
        before = self.state()
        with patch.object(CrmEvent, "save", side_effect=RuntimeError("Synthetic audit failure")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(before, self.state())
        self.assertFalse(ServiceTask.responsibles.through.objects.exists())
        self.assertEqual(self.create()["revision"], 1)

    def test_next_action_edit_replay_updates_existing_task_without_changing_deal_title_or_stage(self):
        created = self.create()
        task = ServiceTask.objects.get(pk=created["task_id"])
        original_version = version_for(task)
        updated = self.next_action(created)
        task.refresh_from_db()
        deal = CrmDeal.objects.get(pk=created["deal_id"])
        self.assertEqual((task.pk, task.title, deal.revision), (created["task_id"], "Prepare calculation", 2))
        self.assertEqual((deal.title, deal.stage.code, deal.outcome), ("Synthetic project", "need", "open"))
        self.assertEqual((ServiceTask.objects.count(), CrmDealTaskLink.objects.count()), (1, 1))
        self.assertEqual(updated, self.next_action(created, expected_task_version=original_version))
        with self.assertRaises(CommercialConflict):
            self.next_action(created, operation_key="stale-new-key")
        self.assertEqual(CrmEvent.objects.count(), 2)

    def test_task_version_detects_calendar_edit_even_when_deal_revision_is_unchanged(self):
        created = self.create()
        task = ServiceTask.objects.get(pk=created["task_id"])
        original_version = version_for(task)
        task.title = "Edited in calendar"
        task.save(update_fields=["title", "updated_at"])
        before = self.state()
        with self.assertRaises(CommercialConflict):
            self.next_action(created, expected_task_version=original_version)
        with self.assertRaises(CommercialConflict):
            self.next_action(created, expected_task_version=None)
        self.assertEqual(before, self.state())

    def test_failed_next_action_event_rolls_back_task_and_allows_same_key_retry(self):
        created = self.create()
        task_version = version_for(ServiceTask.objects.get(pk=created["task_id"]))
        before = self.state()
        with patch.object(CrmEvent, "save", side_effect=RuntimeError("Synthetic next-action failure")):
            with self.assertRaises(RuntimeError):
                self.next_action(created, expected_task_version=task_version)
        self.assertEqual(before, self.state())
        updated = self.next_action(created, expected_task_version=task_version)
        self.grant.manage_own_next_action = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.next_action(created, expected_task_version=task_version)
        self.assertEqual(updated["revision"], CrmDeal.objects.get(pk=created["deal_id"]).revision)

    def test_task_completion_does_not_complete_deal_and_successor_keeps_history(self):
        created = self.create()
        task = ServiceTask.objects.get(pk=created["task_id"])
        apply_feedback(task_id=task.pk, user=self.user, action="complete", comment="Synthetic done",
                       expected_version=version_for(task), request_id=str(uuid4()))
        task.refresh_from_db()
        deal = CrmDeal.objects.get(pk=created["deal_id"])
        self.assertTrue(task.completed_at)
        self.assertEqual((deal.outcome, deal.stage.code, deal.title, deal.revision),
                         ("open", "need", "Synthetic project", 1))
        updated = self.next_action(created)
        self.assertNotEqual(updated["task_id"], task.pk)
        self.assertEqual((ServiceTask.objects.count(), CrmDealTaskLink.objects.count(), CrmDealNextAction.objects.count()), (2, 2, 1))
        self.assertEqual(CrmDeal.objects.get(pk=deal.pk).next_action.task_link.task_id, updated["task_id"])
        task.refresh_from_db()
        self.assertTrue(task.is_archived)
        self.assertEqual(created, self.create())

    def test_waiting_task_is_not_silently_rewritten_as_an_agreed_appointment(self):
        created = self.create()
        task = ServiceTask.objects.get(pk=created["task_id"])
        apply_feedback(task_id=task.pk, user=self.user, action="wait", comment="Awaiting customer",
            expected_version=version_for(task), request_id=str(uuid4()), next_check_at=timezone.now() + timedelta(days=2))
        before = self.state()
        with self.assertRaises(ValidationError):
            self.next_action(created)
        self.assertEqual(before, self.state())

    def test_team_read_never_grants_write_other_and_source_privacy_is_rechecked(self):
        created = self.create()
        other = get_user_model().objects.create_user("pilot-other")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        CrmAccess.objects.create(organization=self.org, user=other, manage_own_next_action=True)
        with self.assertRaises(PermissionDenied):
            self.next_action(created, actor=other)
        with patch("pool_service.services.call_privacy.private_source_task_ids", return_value={created["task_id"]}):
            with self.assertRaises(PermissionDenied):
                self.next_action(created)
            self.reject_create(PermissionDenied)
        self.grant.manage_own_next_action = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.next_action(created)

    def test_legacy_task_scope_rebind_cannot_be_used_for_edit_or_replay(self):
        created = self.create()
        foreign = Organization.objects.create(name="Task foreign")
        ServiceTask.objects.filter(pk=created["task_id"]).update(organization=foreign)
        before = self.state()
        with self.assertRaises(ValidationError):
            self.next_action(created)
        self.reject_create()
        self.assertEqual(before, self.state())

    def test_next_task_retains_own_task_authority_and_immutable_link_scope(self):
        created = self.create()
        task = ServiceTask.objects.get(pk=created["task_id"])
        other = get_user_model().objects.create_user("pilot-task-owner")
        task.created_by = other
        task.save(update_fields=["created_by"])
        task.responsibles.clear()
        with self.assertRaises(PermissionDenied):
            self.next_action(created)
        link = CrmDealTaskLink.objects.get(task=task)
        link.created_by = other
        with self.assertRaises(ValidationError):
            link.save()

    def test_primary_pointer_cannot_reference_another_deals_task(self):
        first = self.create()
        second = self.create(operation_key="other-deal", title="Other deal")
        pointer = CrmDealNextAction.objects.get(deal_id=first["deal_id"])
        pointer.task_link = CrmDealTaskLink.objects.get(deal_id=second["deal_id"])
        with self.assertRaises(ValidationError):
            pointer.save()
        self.assertEqual(CrmDealNextAction.objects.get(pk=pointer.pk).task_link.task_id, first["task_id"])

    def test_stage_drag_preserves_next_action_and_blocks_unimplemented_close(self):
        created = self.create()
        stage = self.pipeline.stages.get(code="technical")
        result = execute_commercial_command(actor=self.user, organization=self.org,
            operation_key="drag-forward", command_type="deal.transition", subject_id=created["deal_id"],
            expected_revision=1, changes={"stage_id": stage.pk})
        deal = CrmDeal.objects.get(pk=created["deal_id"])
        self.assertEqual(deal.stage_id, stage.pk)
        self.assertEqual(deal.next_action.task_link.task_id, created["task_id"])
        with self.assertRaises(ValidationError):
            execute_commercial_command(actor=self.user, organization=self.org, operation_key="drag-close",
                command_type="deal.transition", subject_id=deal.pk, expected_revision=result["revision"],
                changes={"stage_id": self.pipeline.stages.get(code="completed").pk})

    def test_next_action_without_pointer_requires_current_own_open_deal_and_client(self):
        deal = CrmDeal.objects.create(organization=self.org, manager=self.user, client=self.client_record,
            pipeline=self.pipeline, stage=self.pipeline.stages.get(code="need"), title="Existing native")
        result = set_deal_next_action(actor=self.user, organization=self.org, operation_key="first-action",
            subject_id=deal.pk, expected_revision=0, title="First action", due_at=self.due)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(CrmDealNextAction.objects.get(deal=deal).task_link.task_id, result["task_id"])


@skipUnlessDBFeature("has_select_for_update")
class LivePilotConcurrencyTests(PilotFixture, TransactionTestCase):
    def threaded(self, fn):
        close_old_connections()
        try:
            return fn()
        finally:
            connections["default"].close()

    def race(self, first, second):
        barrier = Barrier(2)
        def run(fn):
            barrier.wait(timeout=10)
            try:
                return "ok", fn()
            except CommercialConflict:
                return "conflict", None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.threaded, lambda fn=fn: run(fn)) for fn in (first, second)]
            return [future.result(timeout=30) for future in futures]

    def test_same_create_key_has_one_deal_task_command_and_event(self):
        results = self.race(self.create, self.create)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], "ok")
        self.assertEqual((CrmDeal.objects.count(), ServiceTask.objects.count(), CrmEvent.objects.count()), (1, 1, 1))

    def test_same_task_and_deal_version_allows_only_one_edit(self):
        created = self.create()
        version = version_for(ServiceTask.objects.get(pk=created["task_id"]))
        results = self.race(
            lambda: self.next_action(created, operation_key="edit-a", expected_task_version=version),
            lambda: self.next_action(created, operation_key="edit-b", title="Other action", expected_task_version=version))
        self.assertEqual(sorted(status for status, _ in results), ["conflict", "ok"])
        self.assertEqual((CrmDeal.objects.get(pk=created["deal_id"]).revision, ServiceTask.objects.count()), (2, 1))

    def test_task_feedback_lock_order_and_external_edit_are_preserved(self):
        created = self.create()
        task = ServiceTask.objects.get(pk=created["task_id"])
        version = version_for(task)
        task_locked, command_locking_task, release = Event(), Event(), Event()
        original_lock = commands._locked_pilot_tasks
        def observe_lock(*args, **kwargs):
            command_locking_task.set()
            return original_lock(*args, **kwargs)
        def calendar_edit():
            with transaction.atomic():
                current = ServiceTask.objects.select_for_update().get(pk=task.pk)
                task_locked.set()
                self.assertTrue(release.wait(timeout=10))
                from pool_service.services.task_feedback_permissions import lock_feedback_actor
                lock_feedback_actor(current, self.user)
                current.title = "Concurrent calendar edit"
                current.save(update_fields=["title", "updated_at"])
        with patch.object(commands, "_locked_pilot_tasks", side_effect=observe_lock), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.threaded, calendar_edit)
            self.assertTrue(task_locked.wait(timeout=10))
            second = pool.submit(self.threaded, lambda: self.next_action(created, expected_task_version=version))
            self.assertTrue(command_locking_task.wait(timeout=10))
            release.set()
            first.result(timeout=20)
            with self.assertRaises(CommercialConflict):
                second.result(timeout=20)
        self.assertEqual(ServiceTask.objects.get(pk=task.pk).title, "Concurrent calendar edit")

    def test_committed_grant_revocation_prevents_create_replay(self):
        self.create()
        locked, release = Event(), Event()
        def revoke():
            with transaction.atomic():
                org = Organization.objects.select_for_update().get(pk=self.org.pk)
                grant = CrmAccess.objects.select_for_update().get(pk=self.grant.pk)
                grant.create_own_deals = False
                grant.save()
                locked.set()
                self.assertTrue(release.wait(timeout=10))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.threaded, revoke)
            self.assertTrue(locked.wait(timeout=10))
            second = pool.submit(self.threaded, self.create)
            release.set()
            first.result(timeout=20)
            with self.assertRaises(PermissionDenied):
                second.result(timeout=20)
        self.assertEqual(CrmDeal.objects.count(), 1)
