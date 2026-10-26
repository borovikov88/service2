"""New approval, frozen prior cohort and one-way production activation rules."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
import json
import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmIntakeActivation
from pool_service.models import DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.services import crm_approved_access as previous
from pool_service.services import crm_lead_activation as activation

CONTEXT = {"expected_sha": "a" * 40, "run_id": 321, "run_attempt": 1}


class Fixture:
    def setUp(self):
        self.org = Organization.objects.create(pk=1, name="Synthetic intake approval", paid_until=timezone.now() + timedelta(days=30))
        self.owner = get_user_model().objects.create_user(pk=5, username="synthetic-intake-requester")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="admin")
        self.manager = get_user_model().objects.create_user(username="synthetic-intake-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        Employee.objects.create(organization=self.org, user=self.manager, display_name="Synthetic manager")
        previous.provision(apply=True, **CONTEXT)

    def state(self):
        return [list(model.objects.order_by("pk").values()) for model in (Organization, CrmAccess, CrmIntakeActivation, DataAuditLog)]


class LeadActivationTests(Fixture, TestCase):
    def test_dry_run_has_no_write_and_does_not_reuse_previous_marker(self):
        before = self.state()
        result = activation.provision(**CONTEXT)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["crm_intake_process"])
        self.assertIsNone(result["starts_at"])
        self.assertEqual(self.state(), before)
        self.assertNotEqual(activation.APPROVAL_ID, previous.APPROVAL_ID)

    def test_only_existing_approved_cohort_receives_additive_lead_caps(self):
        extra = get_user_model().objects.create_user(username="synthetic-later-manager")
        OrganizationAccess.objects.create(organization=self.org, user=extra, role="manager")
        result = activation.provision(apply=True, **CONTEXT)
        self.assertEqual((result["status"], result["eligible_manager_count"], result["granted_manager_count"]), ("applied", 1, 1))
        self.assertTrue(result["requester_access"] and result["managers_access_verified"] and result["crm_intake_process"])
        self.assertFalse(CrmAccess.objects.filter(user=extra, view_leads=True).exists())
        grant = CrmAccess.objects.get(user=self.manager, organization=self.org)
        self.assertTrue(all(getattr(grant, name) for name in activation.LEAD_CAPABILITIES))
        self.assertFalse(grant.resolve_contact_identity)
        self.assertEqual(CrmIntakeActivation.objects.count(), 1)
        self.assertEqual(DataAuditLog.objects.filter(entity_type=activation.AUDIT_ENTITY).count(), 1)

    def test_replay_never_restores_revoked_rights_flag_or_cutoff(self):
        activation.provision(apply=True, **CONTEXT)
        grant = CrmAccess.objects.get(user=self.manager, organization=self.org)
        grant.view_leads = False
        grant.save(update_fields=["view_leads"])
        Organization.objects.filter(pk=self.org.pk).update(crm_intake_process=False)
        before = self.state()
        result = activation.provision(apply=True, **CONTEXT)
        self.assertEqual(result["status"], "already_applied")
        self.assertFalse(result["managers_access_verified"] or result["crm_intake_process"])
        self.assertEqual(result["granted_manager_count"], 0)
        self.assertEqual(before, self.state())

    def test_dismissed_cohort_member_excluded_and_wrong_requester_role_blocks(self):
        Employee.objects.filter(user=self.manager).update(employment_status=Employee.STATUS_DISMISSED)
        result = activation.provision(apply=True, **CONTEXT)
        self.assertEqual((result["eligible_manager_count"], result["excluded_manager_count"]), (0, 1))
        self.assertFalse(CrmAccess.objects.get(user=self.manager).view_leads)

    def test_missing_prior_approval_and_unexpected_intake_state_fail_closed(self):
        old = DataAuditLog.objects.get(entity_type=previous.AUDIT_ENTITY)
        old.delete()
        before = self.state()
        with self.assertRaises(activation.ProvisioningBlocked):
            activation.provision(apply=True, **CONTEXT)
        self.assertEqual(before, self.state())

    def test_failed_readback_rolls_back_grants_cutoff_flag_and_marker(self):
        before = self.state()
        with patch("pool_service.services.crm_lead_activation.can_view_leads", return_value=False), self.assertRaises(activation.ProvisioningBlocked):
            activation.provision(apply=True, **CONTEXT)
        self.assertEqual(before, self.state())

    def test_protected_command_requires_distinct_approval_exact_commit_and_context(self):
        args = ("--expected-sha", CONTEXT["expected_sha"], "--apply", "--approval-id", activation.APPROVAL_ID,
                "--run-id", "321", "--run-attempt", "1")
        with self.assertRaises(CommandError):
            call_command("provision_crm_lead_intake", *args, stdout=StringIO())
        with patch.dict(os.environ, {"SERVICE2_CRM_LEAD_INTAKE_ACCESS": "1"}), patch(
            "pool_service.management.commands.provision_crm_lead_intake.subprocess.run",
            return_value=SimpleNamespace(stdout=CONTEXT["expected_sha"] + "\n")):
            out = StringIO()
            call_command("provision_crm_lead_intake", *args, stdout=out)
            receipt = json.loads(out.getvalue().removeprefix("CRM_LEAD_INTAKE_ACCESS "))
            self.assertEqual(receipt["status"], "applied")


@skipUnlessDBFeature("has_select_for_update")
class LeadActivationConcurrencyTests(Fixture, TransactionTestCase):
    def test_two_workers_apply_one_approval_and_one_cutoff(self):
        barrier = Barrier(2)
        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return activation.provision(apply=True, **CONTEXT)["status"]
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sorted(results), ["already_applied", "applied"])
        self.assertEqual(CrmIntakeActivation.objects.count(), 1)
        self.assertEqual(DataAuditLog.objects.filter(entity_type=activation.AUDIT_ENTITY).count(), 1)
