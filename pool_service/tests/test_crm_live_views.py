"""HTTP/security acceptance for the real, default-off commercial workspace."""
from datetime import timedelta
import json
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import Client as HttpClient, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
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

    def test_all_pipeline_manual_stages_are_exposed_and_writes_preserve_tasks(self):
        for pipeline in CrmPipeline.objects.filter(organization=self.org):
            row = self.post(self.payload(pipeline_id=pipeline.pk)).json()["deal"]
            data = self.client.get(self.url("crm_live_data") + "&pipeline=" + str(pipeline.pk)).json()
            stages = next(p for p in data["pipelines"] if p["id"] == pipeline.pk)["stages"]
            task_count = ServiceTask.objects.count()
            for stage in stages:
                self.assertEqual(stage["can_transition"], stage["code"] != "completed")
                if stage["code"] == "completed":
                    self.assertIn("подтверждения", stage["transition_block_reason"])
                    continue
                if stage["id"] == row["stage_id"]: continue
                response = self.post({"operation_key": str(uuid.uuid4()), "command_type": "deal.transition",
                    "subject_id": row["id"], "expected_revision": row["revision"], "changes": {"stage_id": stage["id"]}})
                self.assertEqual(response.status_code, 200, response.content)
                row = response.json()["deal"]
                self.assertTrue(row["can_transition"])
                self.assertEqual(row["outcome"], "open")
                self.assertEqual(ServiceTask.objects.count(), task_count)
                self.assertFalse(ServiceTask.objects.filter(completed_at__isnull=False).exists())

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


class ColdWorkspaceEntryTests(TestCase):
    """Entry discovery is separate from current, scoped API/activation authority."""

    def setUp(self):
        self.org = Organization.objects.create(name="Cold synthetic organization",
            plan_type=Organization.PLAN_COMPANY_PAID, paid_until=timezone.now() + timedelta(days=30))
        self.user = get_user_model().objects.create_user("cold-owner")
        self.role = OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        self.client.force_login(self.user)
        self.workspace = reverse("crm_live_workspace")
        self.start = reverse("crm_live_start")
        self.scope = "?org_id=" + str(self.org.pk)

    def assert_no_activation(self):
        self.org.refresh_from_db()
        self.assertFalse(self.org.crm_v2_read or self.org.crm_v2_write)
        self.assertFalse(CrmAccess.objects.exists() or CrmPipeline.objects.exists())
        self.assertFalse(DataAuditLog.objects.filter(entity_type="crm_pilot_activation").exists())

    def assert_three_navigation_links(self, response, url):
        # These are the actual base's desktop sidebar, top dropdown and mobile menu.
        for css_class in ("desktop-sidebar__sublink", "ui-dropdown__item", "mobile-submenu__item"):
            self.assertContains(response, 'href="' + url + '" class="' + css_class)

    def test_cold_owner_visible_navigation_direct_entry_csrf_post_and_idempotence(self):
        overview = self.client.get(reverse("crm_index"))
        self.assert_three_navigation_links(overview, self.start + self.scope)
        self.assertContains(overview, reverse("crm_list", kwargs={"direction": "sales"}))
        csrf = HttpClient(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        response = csrf.get(self.workspace, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(self.start + self.scope, 302)])
        self.assertContains(response, 'action="' + self.start + self.scope + '"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, "Включить рабочую CRM для меня")
        self.assert_three_navigation_links(response, self.start + self.scope)
        self.assert_no_activation()
        for name in ("crm_live_data", "crm_live_clients"):
            self.assertEqual(csrf.get(reverse(name) + self.scope).status_code, 403)
        self.assertEqual(self.client.post(reverse("crm_live_command") + self.scope, "{}", content_type="application/json").status_code, 403)
        self.assertEqual(csrf.post(reverse("crm_live_command") + self.scope, "{}", content_type="application/json").status_code, 403)
        self.assertEqual(csrf.post(self.start + self.scope, {"action": "activate_self"}).status_code, 403)
        self.assert_no_activation()
        token = csrf.cookies["csrftoken"].value
        for _ in range(2):
            response = csrf.post(self.start + self.scope, {"action": "activate_self", "csrfmiddlewaretoken": token})
            self.assertRedirects(response, self.workspace + self.scope)
        self.assertEqual(CrmAccess.objects.count(), 1)
        self.assertEqual(CrmPipeline.objects.count(), 4)
        self.assertEqual(DataAuditLog.objects.filter(entity_type="crm_pilot_activation").count(), 1)
        self.assertEqual(csrf.get(reverse("crm_live_data") + self.scope).status_code, 200)
        self.assert_three_navigation_links(csrf.get(self.workspace + self.scope), self.workspace + self.scope)

    def test_start_without_scope_is_usable_but_post_requires_explicit_scope(self):
        self.assertRedirects(self.client.get(self.start), self.start + self.scope)
        self.assertEqual(self.client.post(self.start, {"action": "activate_self"}).status_code, 403)
        self.assertEqual(self.client.post(self.start, {"action": "activate_self", "org_id": self.org.pk}).status_code, 403)
        self.assert_no_activation()

    def test_multiple_eligible_organizations_require_choice_and_skip_ineligible_first(self):
        self.role.role = "admin"
        self.role.save(update_fields=["role"])
        owner_org = Organization.objects.create(name="Second eligible owner", plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30))
        reader_org = Organization.objects.create(name="Third eligible reader", crm_v2_read=True,
            plan_type=Organization.PLAN_COMPANY_PAID, paid_until=timezone.now() + timedelta(days=30))
        OrganizationAccess.objects.create(organization=owner_org, user=self.user, role="owner")
        OrganizationAccess.objects.create(organization=reader_org, user=self.user, role="manager")
        for path in (self.workspace, self.start):
            response = self.client.get(path)
            self.assertContains(response, "Сделки: выберите организацию")
            self.assertContains(response, owner_org.name)
            self.assertContains(response, reader_org.name)
            self.assertNotContains(response, self.org.name)
            self.assertContains(response, self.start + "?org_id=" + str(owner_org.pk))
            self.assertContains(response, self.workspace + "?org_id=" + str(reader_org.pk))
            self.assert_three_navigation_links(response, self.workspace)
        self.assertEqual(self.client.get(self.workspace + "?org_id=" + str(reader_org.pk)).status_code, 200)
        self.assertEqual(self.client.get(self.workspace + self.scope).status_code, 403)
        self.assert_no_activation()
        self.assertRedirects(self.client.post(self.start + "?org_id=" + str(owner_org.pk), {"action": "activate_self"}),
            self.workspace + "?org_id=" + str(owner_org.pk))
        self.org.refresh_from_db()
        reader_org.refresh_from_db()
        self.assertFalse(self.org.crm_v2_read or self.org.crm_v2_write or reader_org.crm_v2_write)
        self.assertEqual(CrmAccess.objects.get().organization_id, owner_org.pk)

    def test_enabled_reader_skips_inaccessible_first_org_without_activation(self):
        self.role.role = "admin"
        self.role.save(update_fields=["role"])
        reader_org = Organization.objects.create(name="Enabled read scope", crm_v2_read=True,
            plan_type=Organization.PLAN_COMPANY_PAID, paid_until=timezone.now() + timedelta(days=30))
        OrganizationAccess.objects.create(organization=reader_org, user=self.user, role="manager")
        scoped = self.workspace + "?org_id=" + str(reader_org.pk)
        self.assertRedirects(self.client.get(self.workspace), scoped)
        self.assertRedirects(self.client.get(self.start), scoped)
        response = self.client.get(scoped)
        self.assertEqual(response.status_code, 200)
        self.assert_three_navigation_links(response, scoped)
        self.assert_no_activation()

    def test_explicit_malformed_duplicate_foreign_or_ineligible_scope_never_falls_back(self):
        foreign = Organization.objects.create(name="Foreign private entry")
        for query in ("?org_id=", "?org_id=0", "?org_id=-1", "?org_id=invalid", "?org_id=１２", "?org_id=" + "9" * 19,
                "?org_id=" + str(foreign.pk), "?org_id=999999", self.scope + "&org_id=" + str(self.org.pk)):
            for path in (self.workspace, self.start):
                self.assertEqual(self.client.get(path + query).status_code, 403, path + query)
            self.assertEqual(self.client.post(self.start + query, {"action": "activate_self"}).status_code, 403)
        self.assert_no_activation()

    def test_explicit_read_grant_navigation_is_independent_of_legacy_menu_flag(self):
        from pool_service.crm_live_views import workspace

        self.role.role = "accountant"
        self.role.save(update_fields=["role"])
        self.org.crm_v2_read = True
        self.org.save(update_fields=["crm_v2_read"])
        CrmAccess.objects.create(organization=self.org, user=self.user, view_team_deals=True)
        # Isolate the shared template from the separate accountant-only routing middleware.
        # That middleware's existing redirects are outside the CRM entry policy.
        request = RequestFactory().get(self.workspace + self.scope)
        request.user = self.user
        request.session = {}
        response = workspace(request)
        self.assertEqual(response.status_code, 200)
        self.assert_three_navigation_links(response, self.workspace + self.scope)
        self.assertNotContains(response, reverse("crm_tasks"))
        self.assertEqual(self.client.post(self.start + self.scope, {"action": "activate_self"}).status_code, 403)

    def test_admin_superuser_and_hr_denials_have_no_entry_or_activation_bypass(self):
        self.role.role = "admin"
        self.role.save(update_fields=["role"])
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        for path in (self.workspace, self.start, self.workspace + self.scope, self.start + self.scope):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertNotContains(self.client.get(reverse("crm_index")), self.start + self.scope)
        self.assertEqual(self.client.post(self.start + self.scope, {"action": "activate_self"}).status_code, 403)
        self.role.role = "owner"
        self.role.save(update_fields=["role"])
        employee = Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic former owner")
        for fields in ({"is_active": False}, {"is_active": True, "employment_status": Employee.STATUS_DISMISSED}):
            for field, value in fields.items():
                setattr(employee, field, value)
            employee.save()
            for path in (self.workspace, self.start, self.workspace + self.scope, self.start + self.scope):
                self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post(self.start + self.scope, {"action": "activate_self"}).status_code, 403)
            self.assertNotContains(self.client.get(reverse("crm_index")), self.start + self.scope)
        self.assert_no_activation()

    def test_navigation_is_constant_query_advisory_only_and_revocation_is_fresh(self):
        from pool_service.crm_live_views import workspace
        from pool_service.crm_pilot_views import start
        from pool_service.services.permissions import organization_accesses_for_user
        from pool_service.templatetags.crm_live import crm_workspace_entry

        request = RequestFactory().get(self.workspace)
        request.user = self.user
        # Base rendering already has this advisory snapshot from plan_status_context.
        organization_accesses_for_user(request.user)
        with self.assertNumQueries(2), CaptureQueriesContext(connection) as queries:
            self.assertEqual(crm_workspace_entry({"request": request}), self.start + self.scope)
        self.assertFalse(any("pool_service_organizationaccess" in query["sql"].lower() for query in queries.captured_queries))
        with self.assertNumQueries(0):
            for _ in range(4):
                self.assertEqual(crm_workspace_entry({"request": request}), self.start + self.scope)
        other_org = Organization.objects.create(name="Additional eligible choice")
        OrganizationAccess.objects.create(organization=other_org, user=self.user, role="owner")
        other_request = RequestFactory().get(self.workspace)
        other_request.user = get_user_model().objects.get(pk=self.user.pk)
        organization_accesses_for_user(other_request.user)
        with self.assertNumQueries(2):
            self.assertEqual(crm_workspace_entry({"request": other_request}), self.workspace)
        scoped_request = RequestFactory().get(self.workspace + self.scope)
        scoped_request.user = self.user
        self.assertEqual(crm_workspace_entry({"request": scoped_request}), self.start + self.scope)
        self.role.delete()
        with self.assertRaises(PermissionDenied):
            workspace(scoped_request)
        with self.assertRaises(PermissionDenied):
            start(scoped_request)
        # Page discovery must ignore the stale role snapshot still held by request.user.
        self.assertEqual(workspace(request).url, self.start + "?org_id=" + str(other_org.pk))
        self.assertEqual(start(request).url, self.start + "?org_id=" + str(other_org.pk))
        # A still-authenticated, previously loaded user object cannot bypass a fresh DB check.
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            workspace(scoped_request)
        with self.assertRaises(PermissionDenied):
            start(scoped_request)
        fresh_request = RequestFactory().get(self.workspace)
        fresh_request.user = self.user
        self.assertEqual(crm_workspace_entry({"request": fresh_request}), "")
        self.assert_no_activation()

    def test_deal_and_lead_navigation_share_one_advisory_snapshot(self):
        from pool_service.crm_lead_views import data
        from pool_service.services.permissions import organization_accesses_for_user
        from pool_service.templatetags.crm_live import crm_leads_entry, crm_workspace_entry

        self.org.crm_v2_read = True
        self.org.save(update_fields=["crm_v2_read"])
        grant = CrmAccess.objects.create(organization=self.org, user=self.user, view_team_deals=True, view_leads=True)
        request = RequestFactory().get(self.workspace)
        request.user = self.user
        organization_accesses_for_user(request.user)
        with self.assertNumQueries(2), CaptureQueriesContext(connection) as queries:
            self.assertEqual(crm_leads_entry({"request": request}), reverse("crm_live_leads") + self.scope)
            self.assertEqual(crm_workspace_entry({"request": request}), self.workspace + self.scope)
        self.assertFalse(any("pool_service_organizationaccess" in query["sql"].lower() for query in queries.captured_queries))
        with self.assertNumQueries(0):
            for _ in range(3):
                crm_workspace_entry({"request": request})
                crm_leads_entry({"request": request})

        scoped = RequestFactory().get(reverse("crm_live_lead_data") + self.scope)
        scoped.user = self.user
        self.assertTrue(crm_leads_entry({"request": scoped}))
        grant.view_leads = False
        grant.save(update_fields=["view_leads"])
        # A cached menu cannot authorize the same request after access revocation.
        self.assertEqual(data(scoped).status_code, 403)
        fresh = RequestFactory().get(self.workspace)
        fresh.user = get_user_model().objects.get(pk=self.user.pk)
        self.assertEqual(crm_leads_entry({"request": fresh}), "")
        self.assertEqual(crm_workspace_entry({"request": fresh}), self.workspace + self.scope)

    def test_lead_only_navigation_does_not_imply_deal_access_or_owner_activation(self):
        from pool_service.templatetags.crm_live import crm_leads_entry, crm_workspace_entry

        self.org.crm_v2_read = True
        self.org.save(update_fields=["crm_v2_read"])
        self.role.role = "service"
        self.role.save(update_fields=["role"])
        CrmAccess.objects.create(organization=self.org, user=self.user, view_leads=True)
        request = RequestFactory().get(self.workspace)
        request.user = get_user_model().objects.get(pk=self.user.pk)
        self.assertEqual(crm_workspace_entry({"request": request}), "")
        self.assertEqual(crm_leads_entry({"request": request}), reverse("crm_live_leads") + self.scope)
        Employee.objects.create(organization=self.org, user=self.user, display_name="Synthetic service", is_active=False)
        fresh = RequestFactory().get(self.workspace)
        fresh.user = get_user_model().objects.get(pk=self.user.pk)
        self.assertEqual(crm_workspace_entry({"request": fresh}), "")
        self.assertEqual(crm_leads_entry({"request": fresh}), "")
