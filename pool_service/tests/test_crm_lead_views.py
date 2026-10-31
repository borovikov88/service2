"""Synthetic HTTP acceptance for the lead -> qualified deals vertical slice."""
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client as HttpClient, TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmLead, CrmLeadDealLink, CrmPipeline
from pool_service.models import Client, Employee, Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_pilot_activation import activate_owner_pilot


class LeadWorkspaceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic lead workspace", plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30))
        self.actor = get_user_model().objects.create_user("lead-owner")
        self.membership = OrganizationAccess.objects.create(organization=self.org, user=self.actor, role="owner")
        self.employee = Employee.objects.create(organization=self.org, user=self.actor, display_name="Synthetic owner")
        self.client.force_login(self.actor)
        activate_owner_pilot(actor=self.actor, organization_id=self.org.pk)
        self.org.refresh_from_db()
        self.grant = CrmAccess.objects.get(organization=self.org, user=self.actor)
        for name in ("view_leads", "create_own_leads", "take_leads", "edit_own_leads", "transition_own_leads", "qualify_own_leads"):
            setattr(self.grant, name, True)
        self.grant.save()
        self.customer = Client.objects.create(organization=self.org, name="Synthetic customer")
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.due = (timezone.now() + timedelta(days=1)).isoformat()

    def url(self, name, *args, org=None):
        return reverse(name, args=args) + "?org_id=" + str(org or self.org.pk)

    def payload(self, kind="lead.create", lead=None, changes=None):
        return {"operation_key": str(uuid.uuid4()), "command_type": kind,
            "subject_id": lead["id"] if lead else None, "expected_revision": lead["revision"] if lead else None,
            "changes": changes if changes is not None else {"title": "Synthetic need", "need": "Customer asks for construction",
                "next_action": {"title": "Clarify synthetic requirement", "due_at": self.due}}}

    def post(self, payload):
        return self.client.post(self.url("crm_live_lead_command"), json.dumps(payload), content_type="application/json")

    def create(self, **changes):
        payload = self.payload()
        payload["changes"].update(changes)
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["lead"]

    def test_continuation_candidates_filter_before_limit_and_exclude_self(self):
        source = self.create(title="Synthetic candidate source")
        target = self.create(title="Synthetic candidate target")
        for n in range(25):
            CrmLead.objects.create(organization=self.org, assignee=self.actor,
                title=f"Synthetic candidate closed {n}", status="closed", result="non_target")
        CrmLead.objects.create(organization=self.org, title="Synthetic candidate unassigned")
        response = self.client.get(self.url("crm_live_lead_targets")+
            "&q=Synthetic%20candidate&kind=lead&exclude_id="+str(source["id"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["targets"]], [target["id"]])

    def test_serving_runtime_uses_web_settings_and_administrative_policy(self):
        from django.test import override_settings
        from pool_service.services.crm_intake_wakeup import MODULE_LOADED_AT
        url = self.url("crm_live_lead_data")
        with override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=False):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        runtime = response.json()["intake"]["serving_runtime"]
        self.assertEqual(set(runtime), {"contract", "module_loaded_at", "observed_at",
            "event_wakeup_enabled", "configuration_gate", "event_gate", "scope"})
        self.assertFalse(runtime["event_wakeup_enabled"])
        self.assertEqual(runtime["module_loaded_at"], MODULE_LOADED_AT)
        self.assertEqual(runtime["event_gate"], "not_observed")
        self.assertIn("no-store", response["Cache-Control"])
        with override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=True):
            second = self.client.get(url).json()["intake"]["serving_runtime"]
        self.assertTrue(second["event_wakeup_enabled"])
        self.assertEqual(second["module_loaded_at"], runtime["module_loaded_at"])
        self.membership.role = "manager"
        self.membership.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("serving_runtime", response.json()["intake"])
        self.membership.role = "admin"
        self.membership.save()
        self.assertIn("serving_runtime", self.client.get(url).json()["intake"])
        self.membership.role = "manager"
        self.membership.save()
        self.actor.is_superuser = True
        self.actor.save(update_fields=["is_superuser"])
        self.assertIn("serving_runtime", self.client.get(url).json()["intake"])
        foreign = Organization.objects.create(name="Synthetic foreign runtime")
        self.assertEqual(self.client.get(self.url("crm_live_lead_data", org=foreign.pk)).status_code, 403)
        self.grant.view_leads = False
        self.grant.save(update_fields=["view_leads"])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.grant.view_leads = True
        self.grant.save(update_fields=["view_leads"])
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_intake_status_is_scoped_without_queue_scan_or_false_zero(self):
        from pool_service.crm_commercial_models import CrmIntakeActivation
        activation=CrmIntakeActivation.objects.create(organization=self.org, last_run_at=timezone.now(), last_error="worker_failed")
        with CaptureQueriesContext(connection) as queries:
            response=self.client.get(self.url("crm_live_lead_data"))
        self.assertEqual(response.status_code,200)
        status=response.json()["intake"]
        self.assertEqual(status["last_attempt_result"],"error")
        self.assertIsNone(status["last_successful_pass_at"])
        self.assertIsNone(status["backlog"]["count"])
        self.assertNotIn("worker_failed",str(status))
        self.assertFalse(any('from pool_service_crminboxevent' in q['sql'].replace('`', '').replace(chr(34), '').lower() for q in queries))

    def test_intake_status_rejects_foreign_organization(self):
        foreign=Organization.objects.create(name="Synthetic foreign status")
        response=self.client.get(self.url("crm_live_lead_data",org=foreign.pk))
        self.assertEqual(response.status_code,403)
        self.assertNotIn("intake",response.json())

    def test_manual_without_contact_real_task_and_qualification_in_two_independent_deals(self):
        lead = self.create()
        self.assertIsNone(lead["client"])
        self.assertEqual(lead["status"], "working")
        self.assertEqual(lead["next_action"]["title"], "Clarify synthetic requirement")
        self.assertEqual(self.client.get(self.url("crm_live_leads")).status_code, 200)
        self.assertEqual(len(self.client.get(self.url("crm_live_lead_data") + "&mode=mine").json()["leads"]), 1)
        deals = [{"pipeline_id": self.pipeline.pk, "client_id": self.customer.pk, "title": title,
            "next_action": {"title": "Discuss " + title, "due_at": self.due}} for title in ("Build pool", "Build sauna")]
        payload = self.payload("lead.qualify", lead, {"deals": deals})
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["lead"]["result"], "qualified")
        self.assertEqual(len(body["deals"]), 2)
        self.assertEqual(self.post(payload).json()["result"], body["result"])
        self.assertEqual((CrmDeal.objects.count(), CrmLeadDealLink.objects.count(), ServiceTask.objects.count()), (2, 2, 3))
        self.assertEqual(self.client.get(self.url("crm_live_lead_data") + "&mode=mine").json()["total"], 1)
        self.assertEqual(self.client.get(self.url("crm_live_lead_data") + "&mode=closed").json()["total"], 1)

    def test_take_is_explicit_and_stale_competitor_gets_conflict(self):
        lead = CrmLead.objects.create(organization=self.org, title="Shared synthetic inquiry", need="Requirement", intake_reason="unassigned")
        before = self.client.get(self.url("crm_live_lead_detail", lead.pk)).json()["lead"]
        self.assertEqual(before["intake_reason_label"], "Требуется ответственный")
        lead.refresh_from_db()
        self.assertIsNone(lead.assignee_id)
        payload = self.payload("lead.take", before, {"next_action": {"title": "Call back", "due_at": self.due}})
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["lead"]["assignee"]["id"], self.actor.pk)
        self.assertEqual(response.json()["lead"]["intake_reason_label"], "")
        lead.refresh_from_db()
        self.assertEqual(lead.intake_reason, "unassigned")
        self.assertEqual(self.post(payload).json()["result"], response.json()["result"])
        other = get_user_model().objects.create_user("lead-other")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        CrmAccess.objects.create(organization=self.org, user=other, view_leads=True, take_leads=True)
        self.client.force_login(other)
        competitor = self.payload("lead.take", before, payload["changes"])
        self.assertEqual(self.post(competitor).status_code, 409)
        self.assertEqual(ServiceTask.objects.count(), 1)

    def test_server_filters_pagination_review_and_no_history_per_list(self):
        for index in range(53):
            CrmLead.objects.create(organization=self.org, title="Paginated synthetic " + str(index), need="bounded")
        reviewed = CrmLead.objects.create(organization=self.org, title="Follow-up after closed", status="closed", result="non_target", needs_review=True)
        first = self.client.get(self.url("crm_live_lead_data")).json()
        self.assertEqual((first["total"], len(first["leads"]), first["pages"]), (54, 50, 2))
        second = self.client.get(self.url("crm_live_lead_data") + "&page=2").json()
        self.assertEqual(len(second["leads"]), 4)
        self.assertFalse({row["id"] for row in first["leads"]} & {row["id"] for row in second["leads"]})
        self.assertNotIn("events", first)
        self.assertEqual(self.client.get(self.url("crm_live_lead_data") + "&mode=review").json()["leads"][0]["id"], reviewed.pk)
        self.assertEqual(self.client.get(self.url("crm_live_lead_data") + "&q=Absent").json()["total"], 0)
        for suffix in ("&mode=foreign", "&status=bad", "&page=-1"):
            self.assertEqual(self.client.get(self.url("crm_live_lead_data") + suffix).status_code, 400)

    def test_team_read_does_not_expose_private_next_task_or_allow_others_edit(self):
        lead = self.create(client_id=self.customer.pk)
        other = get_user_model().objects.create_user("lead-reader")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        CrmAccess.objects.create(organization=self.org, user=other, view_leads=True, edit_own_leads=True, transition_own_leads=True)
        self.client.force_login(other)
        response = self.client.get(self.url("crm_live_lead_detail", lead["id"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lead"]["next_action"], {"restricted": True})
        self.assertNotContains(response, "Clarify synthetic requirement")
        self.assertFalse(response.json()["lead"]["can_edit"])
        self.assertEqual(self.post(self.payload("lead.edit", lead, {"title": "Unauthorized"})).status_code, 403)

    def test_explicit_scope_hr_revocation_and_foreign_targets_fail_closed(self):
        lead = self.create()
        foreign = Organization.objects.create(name="Foreign synthetic scope")
        foreign_client = Client.objects.create(organization=foreign, name="Hidden synthetic customer")
        for url in (reverse("crm_live_lead_data"), self.url("crm_live_lead_data", org=foreign.pk), self.url("crm_live_lead_data") + "&org_id=1"):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.post(self.payload(changes={"title": "Need", "need": "Requirement", "client_id": foreign_client.pk,
            "next_action": {"title": "Call", "due_at": self.due}})).status_code, 403)
        self.employee.is_active = False
        self.employee.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.url("crm_live_lead_detail", lead["id"])).status_code, 403)

    def test_strict_payload_csrf_and_source_access_rechecked_on_detail(self):
        csrf = HttpClient(enforce_csrf_checks=True)
        csrf.force_login(self.actor)
        self.assertEqual(csrf.post(self.url("crm_live_lead_command"), json.dumps(self.payload()), content_type="application/json").status_code, 403)
        for value in ("[]", "null", "{", '{"unexpected":true}', '{"operation_key":"x","command_type":[],"changes":{}}'):
            self.assertEqual(self.client.post(self.url("crm_live_lead_command"), value, content_type="application/json").status_code, 400)
        lead = self.create(title="</script><script>synthetic()</script>")
        self.assertNotContains(self.client.get(self.url("crm_live_leads") + "&mode=mine"), "<script>synthetic()</script>")
        with patch("pool_service.services.crm_lead_access.can_read_lead", return_value=False):
            self.assertEqual(self.client.get(self.url("crm_live_lead_detail", lead["id"])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url("crm_live_lead_data")).status_code, 401)

    def test_lost_postcommit_read_is_verifiable_same_operation(self):
        payload = self.payload()
        with patch("pool_service.crm_lead_views._detail", side_effect=PermissionDenied):
            response = self.post(payload)
        self.assertEqual((response.status_code, response.json()["code"]), (503, "verify_required"))
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((CrmLead.objects.count(), ServiceTask.objects.count()), (1, 1))

    def test_task_version_conflict_preserves_calendar_edit(self):
        lead = self.create(client_id=self.customer.pk)
        task = ServiceTask.objects.get(pk=lead["next_action"]["id"])
        task.title = "Changed in calendar"
        task.save(update_fields=["title", "updated_at"])
        response = self.post(self.payload("lead.edit", lead, {"title": "Need changed", "expected_task_version": lead["next_action"]["version"],
            "next_action": {"title": "Stale input", "due_at": self.due}}))
        self.assertEqual(response.status_code, 409, response.content)
        task.refresh_from_db()
        self.assertEqual(task.title, "Changed in calendar")

    def test_closed_followup_can_be_acknowledged_without_a_fabricated_deal(self):
        lead = CrmLead.objects.create(organization=self.org, title="Resolved synthetic requirement", assignee=self.actor,
            status="closed", result="non_target", needs_review=True)
        before = self.client.get(self.url("crm_live_lead_detail", lead.pk)).json()["lead"]
        self.assertTrue(before["can_acknowledge"])
        payload = self.payload("lead.acknowledge", before, {})
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["lead"]["status"], response.json()["lead"]["result"], response.json()["lead"]["needs_review"]), ("closed", "non_target", False))
        self.assertFalse(CrmDeal.objects.exists())
        self.assertEqual(self.post(payload).json()["result"], response.json()["result"])

    def test_list_query_count_does_not_grow_per_visible_next_task(self):
        self.create()
        with CaptureQueriesContext(connection) as first:
            response = self.client.get(self.url("crm_live_lead_data") + "&mode=mine")
            self.assertEqual(response.status_code, 200)
        for index in range(5):
            self.create(title="Independent synthetic requirement " + str(index))
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.url("crm_live_lead_data") + "&mode=mine")
            self.assertEqual(len(response.json()["leads"]), 6)
        self.assertLessEqual(len(many), len(first) + 2, "List must batch task privacy and avoid per-row version hashing")

    def test_completed_history_pagination_and_new_incoming_are_disjoint(self):
        for index in range(51):
            CrmLead.objects.create(organization=self.org, assignee=self.actor,
                title='Completed synthetic ' + str(index), status='closed', result='no_deal', reason='Synthetic completed inquiry')
        reopened = CrmLead.objects.create(organization=self.org, assignee=self.actor,
            title='Completed followup', status='closed', result='qualified', needs_review=True)
        data = self.client.get(self.url('crm_live_lead_data') + '&page_history=2').json()
        columns = {row['key']: row for row in data['columns']}
        self.assertEqual((columns['history']['total'], columns['history']['page'], len(columns['history']['leads'])), (51,2,1))
        self.assertEqual(columns['unparsed']['leads'][0]['id'], reopened.pk)
        self.assertNotIn(reopened.pk, [row['id'] for row in columns['history']['leads']])
        self.assertFalse(columns['history']['leads'][0]['can_transition'])

    def test_five_columns_independent_pages_scoped_counts_and_menu(self):
        for index in range(51):
            CrmLead.objects.create(organization=self.org, title="Free board " + str(index))
        own = self.create(title="Active board")
        spam = CrmLead.objects.create(organization=self.org, assignee=self.actor, title="Spam board", status="closed", result="spam", needs_review=True)
        trash = CrmLead.objects.create(organization=self.org, assignee=self.actor, title="Trash board", is_archived=True, needs_review=True)
        done = CrmLead.objects.create(organization=self.org, assignee=self.actor, title="Done board", status="closed", result="non_target")
        foreign = Organization.objects.create(name="Foreign board scope")
        CrmLead.objects.create(organization=foreign, title="Foreign hidden board")
        response = self.client.get(self.url("crm_live_lead_data")).json()
        self.assertEqual([column["label"] for column in response["columns"]], ["Не разобраны", "В работе", "Завершённые", "Спам", "Удалённые"])
        self.assertEqual([column["total"] for column in response["columns"]], [51, 1, 1, 1, 1])
        second = self.client.get(self.url("crm_live_lead_data") + "&page_unparsed=2").json()
        self.assertEqual([len(column["leads"]) for column in second["columns"]], [1, 1, 1, 1, 1])
        self.assertEqual(second["columns"][2]["leads"][0]["id"], done.pk)
        self.assertEqual(second["columns"][3]["leads"][0]["id"], spam.pk)
        self.assertEqual(second["columns"][4]["leads"][0]["id"], trash.pk)
        self.assertEqual(self.client.get(self.url("crm_live_lead_data") + "&mode=closed").json()["leads"][0]["id"], done.pk)
        html = self.client.get(self.url("crm_live_leads")).content.decode()
        self.assertNotIn("Входящие и лиды", html)
        self.assertIn("<h1>Лиды</h1>", html)
        for css in ("desktop-sidebar__subnav", "crm-nav__menu", "mobile-submenu__body"):
            menu = html.split('class="' + css + '"', 1)[1]
            self.assertLess(menu.index("Лиды"), menu.index("Сделки"))
