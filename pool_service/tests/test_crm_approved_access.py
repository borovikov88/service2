"""Narrow operator approval: transactional scope, replay, and actual authority."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from io import StringIO
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmPipeline, CrmStage
from pool_service.models import Client, DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.services import crm_approved_access as approval
from pool_service.services.crm_commercial_commands import create_commercial_deal, execute_commercial_command, set_deal_next_action
from pool_service.services.crm_pilot_activation import can_activate_pilot
from pool_service.services.task_feedback import version_for

SHA = "a" * 40
CONTEXT = {"expected_sha": SHA, "run_id": 123, "run_attempt": 2}


class ApprovedCrmAccessTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(pk=1, name="Synthetic approved organization",
            paid_until=timezone.now() + timedelta(days=10), crm_accounting=True)
        self.requester = get_user_model().objects.create_user(pk=5, username="synthetic-requester")
        self.admin = OrganizationAccess.objects.create(organization=self.org, user=self.requester, role="admin")
        self.manager = self.member("synthetic-manager", "manager")
        self.hr = Employee.objects.create(organization=self.org, user=self.manager, display_name="Synthetic staff")
        self.no_hr = self.member("synthetic-no-hr", "manager")

    def member(self, username, role, organization=None, **kwargs):
        user = get_user_model().objects.create_user(username=username, **kwargs)
        OrganizationAccess.objects.create(organization=organization or self.org, user=user, role=role)
        return user

    def state(self):
        return [list(model.objects.order_by("pk").values()) for model in (
            Organization, get_user_model(), OrganizationAccess, Employee,
            CrmAccess, CrmPipeline, CrmStage, DataAuditLog)]

    def apply(self):
        return approval.provision(apply=True, **CONTEXT)

    def command(self, *args, **kwargs):
        output = StringIO()
        with patch.dict(os.environ, {"SERVICE2_CRM_APPROVED_ACCESS": "1"}), patch(
                "pool_service.management.commands.provision_approved_crm_access.subprocess.run",
                return_value=SimpleNamespace(stdout=SHA + "\n")):
            call_command("provision_approved_crm_access", "--expected-sha", SHA,
                         *args, stdout=output, **kwargs)
        return json.loads(output.getvalue().removeprefix("CRM_APPROVED_ACCESS "))

    def test_dry_run_reads_without_bootstrap_grants_flags_or_audit(self):
        before = self.state()
        receipt = self.command()
        self.assertEqual(self.state(), before)
        self.assertEqual(receipt["status"], "dry_run")
        self.assertEqual(receipt["eligible_manager_count"], 2)
        self.assertEqual((receipt["run_id"], receipt["run_attempt"]), (0, 0))
        self.assertFalse(receipt["requester_access"] or receipt["managers_access_verified"])
        self.assertFalse(receipt["crm_v2_read"] or receipt["crm_v2_write"])

    def test_only_exact_current_scope_gets_five_additive_capabilities(self):
        excluded = [self.member("synthetic-" + role, role) for role in ("owner", "admin", "service", "installer", "accountant")]
        inactive = self.member("synthetic-inactive", "manager", is_active=False)
        dismissed = self.member("synthetic-dismissed", "manager")
        Employee.objects.create(organization=self.org, user=dismissed,
            display_name="Synthetic dismissed", employment_status=Employee.STATUS_DISMISSED)
        hr_inactive = self.member("synthetic-hr-inactive", "manager")
        Employee.objects.create(organization=self.org, user=hr_inactive,
            display_name="Synthetic inactive", is_active=False)
        foreign_org = Organization.objects.create(name="Synthetic foreign")
        foreign = self.member("synthetic-foreign", "manager", organization=foreign_org)
        # A foreign HR dismissal cannot veto legitimate membership in this org.
        Employee.objects.create(organization=foreign_org, user=self.no_hr,
            display_name="Synthetic foreign HR", is_active=False)
        old_grant = CrmAccess.objects.create(organization=self.org, user=self.manager,
            edit_own_leads=True, resolve_contact_identity=True)
        initial_memberships = list(OrganizationAccess.objects.order_by("pk").values())
        initial_users = list(get_user_model().objects.order_by("pk").values())
        receipt = self.command("--apply", "--approval-id", approval.APPROVAL_ID,
                               "--run-id", "123", "--run-attempt", "2")
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual((receipt["eligible_manager_count"], receipt["granted_manager_count"],
                          receipt["excluded_manager_count"]), (2, 2, 3))
        self.assertTrue(receipt["requester_access"] and receipt["managers_access_verified"])
        self.assertEqual(set(CrmAccess.objects.values_list("user_id", flat=True)),
                         {self.requester.pk, self.manager.pk, self.no_hr.pk})
        old_grant.refresh_from_db()
        self.assertTrue(old_grant.edit_own_leads and old_grant.resolve_contact_identity)
        for user in (self.requester, self.no_hr):
            grant = CrmAccess.objects.get(user=user, organization=self.org)
            self.assertTrue(all(getattr(grant, field) for field in approval.OWN_CAPABILITIES))
            self.assertFalse(grant.edit_own_leads or grant.transition_own_leads or grant.resolve_contact_identity)
        self.assertFalse(CrmAccess.objects.filter(user_id__in=[u.pk for u in excluded + [inactive, dismissed, hr_inactive, foreign]]).exists())
        self.assertEqual(initial_memberships, list(OrganizationAccess.objects.order_by("pk").values()))
        self.assertEqual(initial_users, list(get_user_model().objects.order_by("pk").values()))
        self.org.refresh_from_db()
        self.assertTrue(self.org.crm_accounting)
        self.assertFalse(self.org.crm_close or self.org.crm_materials or self.org.tender_mail)
        self.assertFalse(can_activate_pilot(self.requester, self.org))
        audit = DataAuditLog.objects.get(entity_type=approval.AUDIT_ENTITY)
        self.assertIsNone(audit.actor_id)
        self.assertEqual(audit.after["operator"]["requester_user_id"], 5)
        self.assertEqual(audit.after["operator"]["source"], "protected_deployment")
        self.assertEqual(audit.after["operator"]["expected_sha"], SHA)
        self.assertEqual(audit.after["operator"]["execution_ref"],
                         "https://github.com/borovikov88/service2/actions/runs/123/attempts/2")
        self.assertNotIn("synthetic", json.dumps(receipt))
        self.assertEqual(set(receipt), {"status", "expected_sha", "run_id", "run_attempt", "approval_id",
            "crm_v2_read", "crm_v2_write", "requester_access", "managers_access_verified",
            "eligible_manager_count", "granted_manager_count", "excluded_manager_count", "pipelines_created", "stages_created"})

    def test_replay_never_regrants_or_adds_new_managers_and_reports_current_truth(self):
        self.apply()
        grant = CrmAccess.objects.get(user=self.manager, organization=self.org)
        grant.edit_own_deals = False
        grant.save(update_fields=["edit_own_deals"])
        self.member("synthetic-new-manager", "manager")
        self.org.crm_v2_write = False
        self.org.save(update_fields=["crm_v2_write"])
        before = self.state()
        receipt = approval.provision(apply=True, expected_sha="b" * 40, run_id=124, run_attempt=1)
        self.assertEqual(before, self.state())
        self.assertEqual(receipt["status"], "already_applied")
        self.assertFalse(receipt["managers_access_verified"] or receipt["requester_access"] or receipt["crm_v2_write"])
        self.assertEqual((receipt["granted_manager_count"], receipt["pipelines_created"], receipt["stages_created"]), (0, 0, 0))
        self.assertEqual(DataAuditLog.objects.filter(entity_type=approval.AUDIT_ENTITY).count(), 1)
        self.org.crm_v2_write = True
        self.org.paid_until = None
        self.org.save(update_fields=["crm_v2_write", "paid_until"])
        before = self.state()
        expired = self.apply()
        self.assertFalse(expired["requester_access"] or expired["managers_access_verified"])
        self.assertEqual(before, self.state())

    def test_current_requester_identity_membership_hr_and_subscription_fail_closed(self):
        variants = ((get_user_model(), 5, {"is_active": False}),
                    (OrganizationAccess, self.admin.pk, {"role": "owner"}),
                    (Organization, 1, {"paid_until": None}))
        for model, pk, changes in variants:
            with self.subTest(changes=changes):
                old = model.objects.values().get(pk=pk)
                model.objects.filter(pk=pk).update(**changes)
                before = self.state()
                with self.assertRaisesRegex(approval.ProvisioningBlocked, "requester_or_organization_ineligible"):
                    self.apply()
                self.assertEqual(before, self.state())
                model.objects.filter(pk=pk).update(**{key: old[key] for key in changes})
        for changes in ({"is_active": False}, {"employment_status": Employee.STATUS_DISMISSED}):
            employee = Employee.objects.create(organization=self.org, user=self.requester,
                display_name="Synthetic requester", **changes)
            before = self.state()
            with self.assertRaises(approval.ProvisioningBlocked):
                self.apply()
            self.assertEqual(before, self.state())
            employee.delete()
        self.admin.delete()
        before = self.state()
        with self.assertRaises(approval.ProvisioningBlocked):
            self.apply()
        self.assertEqual(before, self.state())

    def test_bootstrap_preserves_custom_settings_and_fills_only_missing_canonical_rows(self):
        pipeline = CrmPipeline.objects.create(organization=self.org, code="construction",
            name="Synthetic customized", position=99, is_active=False)
        stage = CrmStage.objects.create(pipeline=pipeline, code="need",
            name="Synthetic customized stage", position=88, is_active=False)
        receipt = self.apply()
        pipeline.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual((pipeline.name, pipeline.position, pipeline.is_active), ("Synthetic customized", 99, False))
        self.assertEqual((stage.name, stage.position, stage.is_active), ("Synthetic customized stage", 88, False))
        self.assertEqual(receipt["pipelines_created"], 3)
        self.assertEqual(CrmPipeline.objects.count(), 4)
        self.assertGreater(receipt["stages_created"], 0)

    def test_failure_rolls_back_grants_flags_bootstrap_and_marker(self):
        for target in ("DataAuditLog.objects.create", "bootstrap_commercial_settings", "_access_verified"):
            with self.subTest(target=target):
                before = self.state()
                kwargs = {"return_value": False} if target == "_access_verified" else {"side_effect": RuntimeError("synthetic private error")}
                with patch("pool_service.services.crm_approved_access." + target, **kwargs):
                    with self.assertRaises((RuntimeError, approval.ProvisioningBlocked)):
                        self.apply()
                self.assertEqual(before, self.state())

    def test_command_guard_rejects_unapproved_sha_context_and_sanitizes_errors(self):
        cases = ((), ("--apply",), ("--apply", "--approval-id", approval.APPROVAL_ID),
                 ("--approval-id", "arbitrary"), ("--run-id", "0"),
                 ("--run-attempt", "-1"), ("--run-attempt", "1000000"), ("--run-id", "1;echo-private"))
        for args in cases[1:]:
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.command(*args)
        for value in ("0", ""):
            with patch.dict(os.environ, {"SERVICE2_CRM_APPROVED_ACCESS": value}), self.assertRaisesRegex(CommandError, "protected_deploy_required"):
                call_command("provision_approved_crm_access", "--expected-sha", SHA)
        with patch.dict(os.environ, {"SERVICE2_CRM_APPROVED_ACCESS": "1"}), patch(
                "pool_service.management.commands.provision_approved_crm_access.subprocess.run",
                return_value=SimpleNamespace(stdout="b" * 40)), self.assertRaisesRegex(CommandError, "commit_mismatch"):
            call_command("provision_approved_crm_access", "--expected-sha", SHA)
        with patch("pool_service.management.commands.provision_approved_crm_access.provision",
                   side_effect=RuntimeError("private identity")):
            with self.assertRaisesRegex(CommandError, "^crm_access_operation_failed$"):
                self.command()

    def test_current_membership_recheck_excludes_role_change_after_discovery(self):
        user_model = get_user_model()
        locking = user_model.objects.select_for_update
        def changed():
            OrganizationAccess.objects.filter(user=self.manager, organization=self.org).update(role="service")
            return locking()
        with patch.object(user_model.objects, "select_for_update", side_effect=changed):
            receipt = self.apply()
        self.assertEqual(receipt["eligible_manager_count"], 1)
        self.assertFalse(CrmAccess.objects.filter(user=self.manager).exists())

    def test_new_manager_after_discovery_fails_closed_without_partial_writes(self):
        user_model = get_user_model()
        new_user = user_model.objects.create_user("synthetic-discovery-race")
        locking = user_model.objects.select_for_update
        def changed():
            OrganizationAccess.objects.create(user=new_user, organization=self.org, role="manager")
            return locking()
        with patch.object(user_model.objects, "select_for_update", side_effect=changed):
            with self.assertRaisesRegex(approval.ProvisioningBlocked, "membership_changed"):
                self.apply()
        self.org.refresh_from_db()
        self.assertFalse(self.org.crm_v2_read or self.org.crm_v2_write)
        self.assertFalse(CrmAccess.objects.exists() or CrmPipeline.objects.exists() or DataAuditLog.objects.exists())

    def test_granted_manager_can_write_own_deal_but_not_other_or_private_task(self):
        self.apply()
        client = Client.objects.create(organization=self.org, name="Synthetic customer")
        pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        result = create_commercial_deal(actor=self.manager, organization=self.org,
            operation_key="approved-create", client_id=client.pk, pipeline_id=pipeline.pk,
            title="Synthetic deal", next_action={"title": "Synthetic action", "due_at": timezone.now() + timedelta(days=1)})
        execute_commercial_command(actor=self.manager, organization=self.org,
            operation_key="approved-edit", command_type="deal.edit", subject_id=result["deal_id"],
            expected_revision=1, changes={"title": "Synthetic edited"})
        with self.assertRaises(PermissionDenied):
            execute_commercial_command(actor=self.no_hr, organization=self.org,
                operation_key="approved-other-edit", command_type="deal.edit", subject_id=result["deal_id"],
                expected_revision=2, changes={"title": "Synthetic unauthorized"})
        deal = CrmDeal.objects.get(pk=result["deal_id"])
        task = deal.next_action.task_link.task
        with patch("pool_service.services.call_privacy.private_source_task_ids", return_value={task.pk}):
            with self.assertRaises(PermissionDenied):
                set_deal_next_action(actor=self.manager, organization=self.org,
                    operation_key="approved-private-task", subject_id=deal.pk, expected_revision=deal.revision,
                    expected_task_version=version_for(task), title="Synthetic follow-up",
                    due_at=timezone.now() + timedelta(days=2))


# SQLite cannot prove the organization mutex or durable marker serialization.
@skipUnlessDBFeature("has_select_for_update")
class ApprovedCrmAccessConcurrencyTests(TransactionTestCase):
    def test_two_approved_runs_commit_only_one_grant_and_one_marker(self):
        Organization.objects.create(pk=1, name="Synthetic concurrent approval",
            paid_until=timezone.now() + timedelta(days=10))
        requester = get_user_model().objects.create_user(pk=5, username="synthetic-concurrent-requester")
        OrganizationAccess.objects.create(organization_id=1, user=requester, role="admin")
        manager = get_user_model().objects.create_user(username="synthetic-concurrent-manager")
        OrganizationAccess.objects.create(organization_id=1, user=manager, role="manager")
        ready = Barrier(2)
        def worker():
            close_old_connections()
            try:
                ready.wait(timeout=10)
                return approval.provision(apply=True, **CONTEXT)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(lambda _: worker(), range(2)))
        self.assertEqual(sorted(item["status"] for item in receipts), ["already_applied", "applied"])
        self.assertEqual(DataAuditLog.objects.filter(entity_type=approval.AUDIT_ENTITY).count(), 1)
        self.assertEqual(CrmAccess.objects.count(), 2)
        self.assertEqual(sum(item["granted_manager_count"] for item in receipts), 1)
