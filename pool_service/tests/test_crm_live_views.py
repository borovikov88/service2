"""HTTP/security acceptance for the real, default-off commercial workspace."""
from datetime import timedelta
import json
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client as HttpClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmEvent, CrmPipeline
from pool_service.models import Client, DataAuditLog, Employee, Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_commercial_commands import create_commercial_deal
from pool_service.services.crm_pilot_activation import activate_owner_pilot


class LiveWorkspaceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic workspace", plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30))
        self.actor = get_user_model().objects.create_user("live-owner")
        self.owner_role = OrganizationAccess.objects.create(organization=self.org, user=self.actor, role="owner")
        self.employee = Employee.objects.create(organization=self.org, user=self.actor, display_name="Synthetic owner")
        self.customer = Client.objects.create(organization=self.org, name="Synthetic customer")
        self.client.force_login(self.actor)
        activate_owner_pilot(actor=self.actor, organization_id=self.org.pk)
        self.org.refresh_from_db()
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.deadline = (timezone.now() + timedelta(days=1)).isoformat()

    def url(self, name, *args, org=None):
        return reverse(name, args=args) + "?org_id=" + str(org or self.org.pk)

    def post(self, payload):
        return self.client.post(self.url("crm_live_command"), json.dumps(payload), content_type="application/json")

    def payload(self, **changes):
        values = {"title": "Live synthetic deal", "client_id": self.customer.pk, "pipeline_id": self.pipeline.pk,
            "next_action": {"title": "Call synthetic customer", "due_at": self.deadline}}
        values.update(changes)
        return {"operation_key": str(uuid.uuid4()), "command_type": "deal.create", "subject_id": None,
            "expected_revision": None, "changes": values}

    def test_real_create_detail_transition_reload_and_same_key_replay(self):
        payload = self.payload()
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["deal"]
        self.assertEqual(row["client"]["id"], self.customer.pk)
        self.assertEqual(row["manager"]["id"], self.actor.pk)
        self.assertTrue(row["can_transition"])
        self.assertEqual(len(row["next_action"]["version"]), 64)
        self.assertEqual(self.post(payload).json()["result"], response.json()["result"])
        self.assertEqual(CrmDeal.objects.count(), 1)
        self.assertEqual(ServiceTask.objects.count(), 1)
        target = self.pipeline.stages.get(code="technical")
        move = {"operation_key": str(uuid.uuid4()), "command_type": "deal.transition",
            "subject_id": row["id"], "expected_revision": row["revision"], "changes": {"stage_id": target.pk, "reason": ""}}
        saved = self.post(move)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["deal"]["stage_id"], target.pk)
        self.assertEqual(self.post(move).json()["result"], saved.json()["result"])
        fetched = self.client.get(self.url("crm_live_detail", row["id"])).json()
        self.assertEqual(fetched["deal"]["stage_id"], target.pk)
        self.assertEqual(len(fetched["events"]), 2)
        reloaded = self.client.get(self.url("crm_live_data")).json()
        self.assertEqual(reloaded["deals"][0]["stage_id"], target.pk)
        self.assertEqual(self.client.get(self.url("crm_live_workspace")).status_code, 200)

    def test_next_action_checks_calendar_version_and_keeps_real_task(self):
        created = self.post(self.payload()).json()["deal"]
        current = created["next_action"]
        payload = {"operation_key": str(uuid.uuid4()), "command_type": "deal.next_action", "subject_id": created["id"],
            "expected_revision": created["revision"], "changes": {"title": "Updated action", "due_at": self.deadline,
            "expected_task_version": current["version"]}}
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["deal"]["next_action"]["id"], current["id"])
        self.assertEqual(ServiceTask.objects.get().title, "Updated action")
        payload["operation_key"] = str(uuid.uuid4())
        payload["expected_revision"] = response.json()["deal"]["revision"]
        payload["changes"]["title"] = "Stale task form"
        self.assertEqual(self.post(payload).status_code, 409)
        self.assertEqual(ServiceTask.objects.get().title, "Updated action")

    def test_team_deals_do_not_reveal_private_task_or_grant_writes(self):
        created = self.post(self.payload()).json()["deal"]
        manager = get_user_model().objects.create_user("live-read-manager")
        OrganizationAccess.objects.create(organization=self.org, user=manager, role="manager")
        self.client.force_login(manager)
        response = self.client.get(self.url("crm_live_detail", created["id"]))
        self.assertEqual(response.status_code, 200)
        row = response.json()["deal"]
        self.assertEqual(row["next_action"], {"restricted": True})
        self.assertFalse(row["can_edit"] or row["can_next_action"] or row["can_transition"])
        self.assertNotContains(response, "Call synthetic customer")
        self.assertEqual(self.client.get(self.url("crm_live_clients") + "&q=Synthetic").status_code, 403)
        self.assertEqual(self.post(self.payload()).status_code, 403)
        self.assertEqual(CrmDeal.objects.count(), 1)

    def test_current_membership_hr_flags_and_foreign_organizations_fail_closed(self):
        created = self.post(self.payload()).json()["deal"]
        foreign = Organization.objects.create(name="Foreign workspace")
        self.assertEqual(self.client.get(self.url("crm_live_data", org=foreign.pk)).status_code, 403)
        self.assertEqual(self.client.get(self.url("crm_live_data") + "&org_id=" + str(foreign.pk)).status_code, 403)
        foreign_client = Client.objects.create(organization=foreign, name="Foreign private customer")
        self.assertEqual(self.post(self.payload(client_id=foreign_client.pk)).status_code, 403)
        self.employee.is_active = False
        self.employee.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.url("crm_live_detail", created["id"])).status_code, 403)
        self.employee.is_active = True
        self.employee.save(update_fields=["is_active"])
        self.org.crm_v2_read = False
        self.org.save(update_fields=["crm_v2_read"])
        self.assertEqual(self.client.get(self.url("crm_live_data")).status_code, 403)
        self.assertEqual(self.post(self.payload()).status_code, 403)

    def test_client_lookup_is_scoped_bounded_and_excludes_merged_identity(self):
        from pool_service.client_crm_models import ClientCRMProfile
        old = Client.objects.create(organization=self.org, name="Synthetic merged identity")
        ClientCRMProfile.objects.create(client=old, merged_into=self.customer)
        foreign = Organization.objects.create(name="Lookup foreign")
        Client.objects.create(organization=foreign, name="Synthetic foreign identity")
        self.assertEqual(self.client.get(self.url("crm_live_clients") + "&q=Sy").json(), {"clients": []})
        rows = self.client.get(self.url("crm_live_clients") + "&q=Synthetic").json()["clients"]
        self.assertEqual(rows, [{"id": self.customer.pk, "name": self.customer.name}])

    def test_filters_and_unsupported_stages_use_canonical_server_policy(self):
        created = self.post(self.payload(title="Find this deal")).json()["deal"]
        self.assertEqual(len(self.client.get(self.url("crm_live_data") + "&q=Find&owner=me").json()["deals"]), 1)
        self.assertEqual(self.client.get(self.url("crm_live_data") + "&q=Absent").json()["deals"], [])
        self.assertEqual(self.client.get(self.url("crm_live_data") + "&pipeline=999999").status_code, 404)
        self.assertEqual(self.client.get(self.url("crm_live_data") + "&owner=someone").status_code, 400)
        terminal = self.pipeline.stages.get(code="completed")
        response = self.post({"operation_key": str(uuid.uuid4()), "command_type": "deal.transition",
            "subject_id": created["id"], "expected_revision": created["revision"], "changes": {"stage_id": terminal.pk}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CrmDeal.objects.get().stage_id, created["stage_id"])

    def test_csrf_and_strict_method_and_payload_boundaries(self):
        csrf = HttpClient(enforce_csrf_checks=True)
        csrf.force_login(self.actor)
        self.assertEqual(csrf.post(self.url("crm_live_command"), json.dumps(self.payload()), content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get(self.url("crm_live_command")).status_code, 405)
        for malformed in ("[]", "null", "{", "{\"unexpected\":true}"):
            self.assertEqual(self.client.post(self.url("crm_live_command"), malformed, content_type="application/json").status_code, 400)
        self.assertEqual(self.post(self.payload(title="x" * 17000)).status_code, 400)
        payload = self.payload()
        payload["actor_id"] = self.actor.pk + 1
        self.assertEqual(self.post(payload).status_code, 400)
        self.assertFalse(CrmDeal.objects.exists())
        self.client.logout()
        self.assertEqual(self.client.get(self.url("crm_live_data")).status_code, 401)

    def test_post_commit_read_failure_is_uncertain_and_replay_creates_no_duplicate(self):
        payload = self.payload()
        with patch("pool_service.crm_live_views._serialize_deals", side_effect=PermissionDenied):
            response = self.post(payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "verify_required")
        response = self.post(payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((CrmDeal.objects.count(), ServiceTask.objects.count(), CrmEvent.objects.count()), (1, 1, 1))

    def test_scope_is_rechecked_on_replay_and_html_bootstrap_escapes_user_content(self):
        payload = self.payload(title="</script><script>alert('synthetic')</script>")
        self.assertEqual(self.post(payload).status_code, 200)
        response = self.client.get(self.url("crm_live_workspace"))
        self.assertNotContains(response, "<script>alert('synthetic')</script>")
        self.owner_role.delete()
        self.assertEqual(self.post(payload).status_code, 403)
        self.assertEqual(CrmDeal.objects.count(), 1)

    def test_calendar_move_updates_actual_deadline_and_invalidates_old_crm_task_form(self):
        created = self.post(self.payload()).json()["deal"]
        task = ServiceTask.objects.get(pk=created["next_action"]["id"])
        moved_date = task.start_date + timedelta(days=3)
        response = self.client.post(reverse("task_move"), json.dumps({
            "task_id": task.pk, "target_date": moved_date.isoformat(),
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(timezone.localtime(task.due_at, timezone.get_fixed_timezone(420)).date(), moved_date)
        row = self.client.get(self.url("crm_live_detail", created["id"])).json()["deal"]
        self.assertEqual(row["next_action"]["due_date"], moved_date.isoformat())
        self.assertNotEqual(row["next_action"]["version"], created["next_action"]["version"])
        stale = {"operation_key": str(uuid.uuid4()), "command_type": "deal.next_action", "subject_id": created["id"],
            "expected_revision": created["revision"], "changes": {"title": "Stale before calendar move", "due_at": self.deadline,
            "expected_task_version": created["next_action"]["version"]}}
        self.assertEqual(self.post(stale).status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.start_date, moved_date)

    def test_existing_task_form_preserves_date_time_and_date_only_deadline_semantics(self):
        created = self.post(self.payload()).json()["deal"]
        task = ServiceTask.objects.get(pk=created["next_action"]["id"])
        moved_date = (task.start_date + timedelta(days=5)).isoformat()
        values = {"title": task.title, "description": "", "start_date": moved_date, "end_date": moved_date,
            "start_time": "11:30", "end_time": "12:45", "responsibles": [str(self.actor.pk)], "modal": "1"}
        response = self.client.post(reverse("task_edit", args=[task.pk]), values)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        task.refresh_from_db()
        local = timezone.localtime(task.due_at, timezone.get_fixed_timezone(420))
        self.assertEqual((local.date().isoformat(), local.strftime("%H:%M")), (moved_date, "12:45"))
        row = self.client.get(self.url("crm_live_detail", created["id"])).json()["deal"]
        self.assertEqual(row["next_action"]["due_at"], task.due_at.isoformat())
        values.update(start_time="", end_time="")
        response = self.client.post(reverse("task_edit", args=[task.pk]), values)
        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertIsNone(task.due_at)
        row = self.client.get(self.url("crm_live_detail", created["id"])).json()["deal"]
        self.assertIsNone(row["next_action"]["due_at"])
        self.assertEqual(row["next_action"]["due_date"], moved_date)


class PilotActivationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Activation synthetic", plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30))
        self.user = get_user_model().objects.create_user("activation-owner")
        self.role = OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        self.client.force_login(self.user)
        self.url = reverse("crm_live_start") + "?org_id=" + str(self.org.pk)

    def test_explicit_owner_activation_is_idempotent_and_grants_no_other_user(self):
        colleague = get_user_model().objects.create_user("activation-manager")
        OrganizationAccess.objects.create(organization=self.org, user=colleague, role="manager")
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertFalse(CrmAccess.objects.exists())
        for _ in range(2):
            self.assertEqual(self.client.post(self.url, {"action": "activate_self"}).status_code, 302)
        self.org.refresh_from_db()
        grant = CrmAccess.objects.get()
        self.assertEqual(grant.user_id, self.user.pk)
        self.assertTrue(grant.create_own_deals and grant.edit_own_deals and grant.transition_own_deals and grant.manage_own_next_action and grant.view_team_deals)
        self.assertFalse(grant.edit_own_leads or grant.transition_own_leads or grant.resolve_contact_identity)
        self.assertTrue(self.org.crm_v2_read and self.org.crm_v2_write)
        self.assertFalse(any(getattr(self.org, name) for name in ("crm_intake_process", "crm_accounting", "crm_materials", "crm_close", "tender_mail", "tender_autoselect")))
        self.assertEqual(CrmPipeline.objects.count(), 4)
        self.assertEqual(DataAuditLog.objects.filter(entity_type="crm_pilot_activation").count(), 1)
        self.assertContains(self.client.get(reverse("crm_index")), reverse("crm_live_workspace"))

    def test_no_admin_superuser_foreign_scope_or_dismissed_owner_bypass(self):
        self.role.role = "admin"
        self.role.save()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.assertEqual(self.client.post(self.url, {"action": "activate_self"}).status_code, 403)
        self.role.role = "owner"
        self.role.save()
        Employee.objects.create(organization=self.org, user=self.user, display_name="Former owner", is_active=False)
        self.assertEqual(self.client.post(self.url, {"action": "activate_self"}).status_code, 403)
        self.assertFalse(CrmAccess.objects.exists())
        foreign = Organization.objects.create(name="Activation foreign")
        self.assertEqual(self.client.post(reverse("crm_live_start") + "?org_id=" + str(foreign.pk), {"action": "activate_self"}).status_code, 403)

    def test_failed_audit_rolls_back_flags_grants_and_pipeline_bootstrap(self):
        with patch("pool_service.services.crm_pilot_activation.DataAuditLog.objects.create", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                activate_owner_pilot(actor=self.user, organization_id=self.org.pk)
        self.org.refresh_from_db()
        self.assertFalse(self.org.crm_v2_read or self.org.crm_v2_write)
        self.assertFalse(CrmAccess.objects.exists() or CrmPipeline.objects.exists())
