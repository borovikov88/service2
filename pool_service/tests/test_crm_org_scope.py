"""Synthetic tenant isolation regressions for the existing CRM."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from pool_service.forms import CrmItemForm
from pool_service.models import (
    Client, CrmItem, CrmItemPhoto, DataAuditLog, Employee, Organization, OrganizationAccess, Pool, PoolAccess, ServiceTask, WaterReading,
)
from pool_service.services.permissions import organization_for_user
from pool_service import views


class CrmOrganizationScopeTests(TestCase):
    def setUp(self):
        self.a = Organization.objects.create(name="Synthetic A", trial_started_at=timezone.now())
        self.b = Organization.objects.create(name="Synthetic B", trial_started_at=timezone.now())
        self.user = User.objects.create_user(username="mixed")
        self.access_a = OrganizationAccess.objects.create(user=self.user, organization=self.a, role="service")
        self.access_b = OrganizationAccess.objects.create(user=self.user, organization=self.b, role="manager")
        self.client_a = Client.objects.create(organization=self.a, name="Synthetic A client")
        self.client_b = Client.objects.create(organization=self.b, name="Synthetic B client")
        self.pool_a = Pool.objects.create(organization=self.a, client=self.client_a, address="A")
        self.service = self.item(self.a, "service", "A service")
        self.project = self.item(self.a, "project", "A commercial")
        self.project_b = self.item(self.b, "project", "B commercial")
        self.client.force_login(self.user)

    def item(self, org, direction, title, **kwargs):
        return CrmItem.objects.create(
            organization=org, direction=direction, title=title,
            stage=(CrmItem.STAGE_SERVICE_NEW if direction == "service" else CrmItem.STAGE_PROJECT_IDEA),
            **kwargs,
        )

    def url(self, name, item=None, direction="project"):
        kwargs = {"direction": item.direction if item else direction}
        if item:
            kwargs["item_id"] = item.pk
        return reverse(name, kwargs=kwargs)

    def payload(self, **overrides):
        return {
            "title": "Synthetic edited", "stage": CrmItem.STAGE_SERVICE_NEW,
            "urgency": CrmItem.URGENCY_REQUIRED, "client": self.client_a.pk,
            "pool": self.pool_a.pk, "responsible": self.user.pk,
            **overrides,
        }

    def test_mixed_roles_never_open_commercial_a_in_either_access_order(self):
        for reverse_order in (False, True):
            if reverse_order:
                self.access_a.delete()
                self.access_a = OrganizationAccess.objects.create(user=self.user, organization=self.a, role="service")
            for role in ("service", "installer"):
                self.access_a.role = role
                self.access_a.save()
                for direction in ("project", "sales", "tender"):
                    with self.subTest(reverse_order=reverse_order, role=role, direction=direction):
                        self.assertEqual(self.client.get(self.url("crm_list", direction=direction), {"org_id": self.a.pk}).status_code, 403)
                self.assertEqual(self.client.get(self.url("crm_view", self.project)).status_code, 403)
                self.assertEqual(self.client.get(self.url("crm_edit", self.project)).status_code, 403)
                self.assertEqual(self.client.get(self.url("crm_create"), {"org_id": self.a.pk}).status_code, 403)
                self.assertEqual(self.client.get(self.url("crm_list", direction="service"), {"org_id": self.a.pk}).status_code, 200)
                self.assertEqual(self.client.get(self.url("crm_list"), {"org_id": self.b.pk}).status_code, 200)
                self.assertEqual(self.client.get(self.url("crm_view", self.project_b)).status_code, 200)

    def test_org_selector_is_validated_and_never_silently_falls_back(self):
        foreign = Organization.objects.create(name="Synthetic foreign")
        for org_id in (foreign.pk, "invalid", "999999"):
            self.assertEqual(self.client.get(self.url("crm_list", direction="service"), {"org_id": org_id}).status_code, 403)
        self.assertEqual(self.client.get(self.url("crm_view", self.service), {"org_id": self.b.pk}).status_code, 403)

    def test_mixed_bulk_rejected_before_any_item_or_task_change(self):
        foreign = self.item(self.b, "service", "Foreign service")
        response = self.client.post(self.url("crm_bulk_update", direction="service"), {
            "org_id": self.a.pk, "item_ids": [self.service.pk, foreign.pk],
            "bulk_action": "archive",
        })
        self.service.refresh_from_db()
        self.assertFalse(self.service.is_archived)
        self.assertEqual(response.status_code, 403)

    @patch("pool_service.views._crm_photo_file_response", return_value=HttpResponse("photo"))
    def test_photo_and_archive_paths_enforce_direction(self, photo_response):
        photo = CrmItemPhoto.objects.create(item=self.project, image="synthetic.jpg")
        self.assertEqual(self.client.get(reverse("crm_issue_photo_download", args=[photo.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("crm_issue_primary_photo_download", args=[self.project.pk])).status_code, 403)
        self.project.is_archived = True
        self.project.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.project.save()
        archive = self.client.get(reverse("archive_list"), {"org_id": self.a.pk})
        self.assertNotContains(archive, "A commercial")
        for name in ("archive_restore_crm_item", "archive_delete_crm_item_forever"):
            self.assertEqual(self.client.post(reverse(name, args=[self.project.pk])).status_code, 403)
        self.service.is_archived = True
        self.service.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.service.save()
        response = self.client.post(reverse("archive_bulk_update"), {
            "org_id": self.a.pk, "item_ids": [self.service.pk, self.project.pk], "archive_action": "restore",
        })
        self.assertEqual(response.status_code, 403)
        self.service.refresh_from_db()
        self.assertTrue(self.service.is_archived)

    def test_revocation_ignores_general_organization_cache_and_inactive_actor(self):
        organization_for_user(self.user)
        self.access_a.delete()
        request = RequestFactory().get(self.url("crm_list", direction="service"), {"org_id": self.a.pk})
        request.user = self.user
        with self.assertRaises(PermissionDenied):
            views.crm_list(request, "service")
        self.user.is_active = False
        self.assertFalse(views._can_access_crm(self.user))

    def test_fk_and_assignment_validation_matches_available_choices(self):
        bad_pool = Pool.objects.create(organization=self.b, client=self.client_a, address="Conflict")
        other_pool = Pool.objects.create(organization=self.a, client=self.client_b, address="Conflict client")
        inactive = User.objects.create_user(username="inactive", is_active=False)
        accountant = User.objects.create_user(username="accountant")
        dismissed = User.objects.create_user(username="dismissed")
        for user, role in ((inactive, "manager"), (accountant, "accountant"), (dismissed, "service")):
            OrganizationAccess.objects.create(user=user, organization=self.a, role=role)
        Employee.objects.create(organization=self.a, user=dismissed, display_name="Synthetic dismissed", is_active=False, employment_status=Employee.STATUS_DISMISSED)
        for overrides, field in (({"client": self.client_b.pk}, "client"), ({"pool": bad_pool.pk}, "pool"), ({"pool": other_pool.pk}, "pool"), ({"responsible": inactive.pk}, "responsible"), ({"responsible": accountant.pk}, "responsible"), ({"responsible": dismissed.pk}, "responsible")):
            with self.subTest(overrides=overrides):
                form = CrmItemForm(self.payload(**overrides), organization=self.a, direction="service")
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
        form = CrmItemForm(self.payload(), organization=self.a, direction="service")
        self.assertTrue(form.is_valid(), form.errors)

    def test_direct_post_cannot_assign_an_excluded_employee(self):
        inactive = User.objects.create_user(username="excluded", is_active=False)
        OrganizationAccess.objects.create(user=inactive, organization=self.a, role="manager")
        response = self.client.post(self.url("crm_edit", self.service), self.payload(responsible=inactive.pk))
        self.assertEqual(response.status_code, 200)
        self.assertIn("responsible", response.context["form"].errors)
        self.service.refresh_from_db()
        self.assertIsNone(self.service.responsible_id)

    def test_historical_inactive_assignment_is_retained_only_on_its_card(self):
        self.service.responsible = self.user
        self.service.save()
        self.user.is_active = False
        self.user.save()
        form = CrmItemForm(self.payload(), instance=self.service, organization=self.a, direction="service")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().responsible_id, self.user.pk)
        new_form = CrmItemForm(self.payload(), organization=self.a, direction="service")
        self.assertFalse(new_form.is_valid())

    def test_active_staff_contract_and_inactive_employee_actor(self):
        for role in ("owner", "admin", "manager"):
            self.access_a.role = role
            self.access_a.save()
            self.assertEqual(self.client.get(self.url("crm_list"), {"org_id": self.a.pk}).status_code, 200)
        Employee.objects.create(organization=self.a, user=self.user, display_name="Synthetic inactive", is_active=False)
        self.assertEqual(self.client.get(self.url("crm_list"), {"org_id": self.a.pk}).status_code, 403)
        self.assertEqual(self.client.get(self.url("crm_list"), {"org_id": self.b.pk}).status_code, 200)

    def test_selected_b_survives_navigation_forms_bulk_and_redirect(self):
        listing = self.client.get(self.url("crm_list"), {"org_id": self.b.pk})
        create_url = self.url("crm_create") + f"?org_id={self.b.pk}"
        self.assertEqual(listing.context["page_action_url"], create_url)
        self.assertContains(listing, f'name="org_id" value="{self.b.pk}"')
        form = self.client.get(create_url)
        self.assertContains(form, f'name="org_id" value="{self.b.pk}"')
        created = self.client.post(self.url("crm_create"), {
            "org_id": self.b.pk, "title": "New B", "stage": CrmItem.STAGE_PROJECT_IDEA,
            "responsible": self.user.pk,
        })
        self.assertEqual(created.status_code, 302)
        self.assertEqual(created.url, self.url("crm_list") + f"?org_id={self.b.pk}")
        self.assertEqual(CrmItem.objects.get(title="New B").organization, self.b)
        edited = self.client.post(self.url("crm_edit", self.project_b), {
            "org_id": self.b.pk, "title": "Edited B", "stage": CrmItem.STAGE_PROJECT_IDEA,
        })
        self.assertEqual(edited.status_code, 302)
        self.assertEqual(edited.url, self.url("crm_list") + f"?org_id={self.b.pk}")
        bulk = self.client.post(self.url("crm_bulk_update"), {
            "org_id": self.b.pk, "item_ids": [self.project_b.pk], "bulk_action": "archive",
            "return_query": f"org_id={self.a.pk}&q=B",
        })
        self.assertEqual(bulk.status_code, 302)
        self.assertEqual(bulk.url, self.url("crm_list") + f"?q=B&org_id={self.b.pk}")
        archive = self.client.get(reverse("archive_list"), {"org_id": self.b.pk})
        self.assertContains(archive, "Edited B")
        self.assertContains(archive, f'?kind=crm&org_id={self.b.pk}')

    def test_selected_b_uses_its_own_subscription(self):
        self.a.trial_started_at = None
        self.a.save()
        response = self.client.post(self.url("crm_create"), {
            "org_id": self.b.pk, "title": "Paid B", "stage": CrmItem.STAGE_PROJECT_IDEA,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CrmItem.objects.filter(title="Paid B", organization=self.b).exists())

    def test_foreign_responsible_filter_is_rejected_and_lookup_excludes_inactive(self):
        foreign = User.objects.create_user(username="foreign_filter")
        OrganizationAccess.objects.create(user=foreign, organization=self.b, role="manager")
        inactive = User.objects.create_user(username="inactive_filter", is_active=False)
        OrganizationAccess.objects.create(user=inactive, organization=self.a, role="manager")
        listing = self.client.get(self.url("crm_list", direction="service"), {"org_id": self.a.pk})
        self.assertNotIn(str(inactive.pk), {row["id"] for row in listing.context["responsible_options"]})
        for name in ("crm_list", "crm_tasks"):
            url = self.url(name, direction="service") if name == "crm_list" else reverse(name)
            for value in (foreign.pk, "invalid"):
                self.assertEqual(self.client.get(url, {"org_id": self.a.pk, "responsible": value}).status_code, 403)

    def test_task_create_form_action_keeps_selected_b(self):
        form = self.client.get(reverse("task_create"), {"org_id": self.b.pk})
        self.assertEqual(form.status_code, 200)
        action = reverse("task_create") + f"?org_id={self.b.pk}"
        self.assertEqual(form.context["form_action"], action)
        response = self.client.post(action, {
            "title": "Task B", "start_date": timezone.localdate().isoformat(),
            "end_date": timezone.localdate().isoformat(), "responsibles": [self.user.pk],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ServiceTask.objects.get(title="Task B").organization, self.b)

    def task(self, org, item=None):
        return ServiceTask.objects.create(
            organization=org, crm_item=item, title="Synthetic task",
            created_by=self.user, start_date=timezone.localdate(), end_date=timezone.localdate(),
        )

    def test_task_bulk_rejects_foreign_id_before_sync(self):
        own = self.task(self.a, self.service)
        foreign = self.task(self.b)
        response = self.client.post(reverse("crm_tasks_bulk_update"), {
            "org_id": self.a.pk, "task_ids": [own.pk, foreign.pk],
            "bulk_action": "set_status", "bulk_status": ServiceTask.STATUS_IN_PROGRESS,
        })
        own.refresh_from_db()
        self.assertEqual(own.status, ServiceTask.STATUS_NEW)
        self.assertEqual(response.status_code, 403)

    def test_task_actions_remain_allowed_without_commercial_crm_sync(self):
        task = self.task(self.a, self.project)
        task.responsibles.add(self.user)
        self.assertTrue(views._task_can_edit(task, self.user))
        response = self.client.post(reverse("task_edit", args=[task.pk]), {
            "title": "Task change", "description": "Allowed task description",
            "start_date": timezone.localdate().isoformat(), "end_date": timezone.localdate().isoformat(),
            "responsibles": [self.user.pk],
        })
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.title, "Task change")
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "A commercial")
        self.assertEqual(self.project.description, "")
        response = self.client.post(reverse("crm_tasks_bulk_update"), {
            "org_id": self.a.pk, "task_ids": [task.pk],
            "bulk_action": "set_responsible", "bulk_responsible": self.user.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.responsible_id)
        active = self.client.post(reverse("crm_tasks_bulk_update"), {
            "org_id": self.a.pk, "task_ids": [task.pk],
            "bulk_action": "set_status", "bulk_status": ServiceTask.STATUS_IN_PROGRESS,
        })
        self.assertEqual(active.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, ServiceTask.STATUS_IN_PROGRESS)
        completed = self.client.post(reverse("crm_tasks_bulk_update"), {
            "org_id": self.a.pk, "task_ids": [task.pk],
            "bulk_action": "set_status", "bulk_status": ServiceTask.STATUS_DONE,
        })
        self.assertEqual(completed.status_code, 302)
        task.refresh_from_db()
        self.assertTrue(task.completed_at)
        self.assertTrue(task.is_completed_archive)
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "A commercial")
        self.assertIsNone(self.project.responsible_id)

    def test_fk_direct_posts_fail_without_modifying_record(self):
        for field, foreign_id in (("client", self.client_b.pk), ("responsible", User.objects.create_user(username="foreign").pk)):
            response = self.client.post(self.url("crm_edit", self.service), self.payload(**{field: foreign_id}))
            self.assertEqual(response.status_code, 200)
            self.assertIn(field, response.context["form"].errors)
            self.service.refresh_from_db()
            self.assertEqual(self.service.title, "A service")

    def test_superuser_read_only_contract(self):
        superuser = User.objects.create_superuser(username="root", email="", password="test")
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(self.url("crm_view", self.project_b)).status_code, 200)
        self.assertEqual(self.client.get(self.url("crm_list"), {"org_id": self.b.pk}).status_code, 200)
        self.assertEqual(self.client.post(self.url("crm_edit", self.project_b), {"title": "Forbidden"}).status_code, 302)
        self.project_b.refresh_from_db()
        self.assertEqual(self.project_b.title, "B commercial")

    def test_permitted_direct_archive_delete_keeps_its_contract(self):
        self.service.is_archived = True
        self.service.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.service.save()
        response = self.client.post(reverse("archive_delete_crm_item_forever", args=[self.service.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CrmItem.objects.filter(pk=self.service.pk).exists())

    def test_unlinked_hr_record_does_not_hide_other_active_staff(self):
        Employee.objects.create(organization=self.a, display_name="Synthetic unlinked", is_active=False)
        form = CrmItemForm(self.payload(), organization=self.a, direction="service")
        self.assertTrue(form.is_valid(), form.errors)

    def test_review_archive_all_submitted_types_reject_foreign_missing_and_malformed(self):
        foreign_pool = Pool.objects.create(organization=self.b, client=self.client_b, address="Foreign deleted", is_deleted=True)
        foreign_reading = WaterReading.objects.create(pool=foreign_pool, date=timezone.now(), is_deleted=True)
        foreign_task = self.task(self.b)
        foreign_task.is_archived = True
        foreign_task.save()
        cases = [
            ("pool_uuids", str(foreign_pool.uuid)), ("reading_uuids", str(foreign_reading.uuid)),
            ("task_ids", foreign_task.pk), ("item_ids", self.project_b.pk),
            ("pool_uuids", "00000000-0000-0000-0000-000000000001"),
            ("reading_uuids", "00000000-0000-0000-0000-000000000001"),
            ("task_ids", "999999"), ("item_ids", "999999"),
            *[(field, "invalid") for field in ("task_ids", "item_ids", "pool_uuids", "reading_uuids")],
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.service.is_archived = True
                self.service.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
                self.service.save()
                before_audits = DataAuditLog.objects.count()
                data = {"org_id": self.a.pk, "item_ids": [self.service.pk], "archive_action": "restore"}
                data[field] = [self.service.pk, value] if field == "item_ids" else [value]
                response = self.client.post(reverse("archive_bulk_update"), data)
                self.service.refresh_from_db()
                foreign_pool.refresh_from_db()
                foreign_reading.refresh_from_db()
                self.assertTrue(self.service.is_archived)
                self.assertTrue(foreign_pool.is_deleted)
                self.assertTrue(foreign_reading.is_deleted)
                self.assertEqual(DataAuditLog.objects.count(), before_audits)
                self.assertEqual(response.status_code, 403)

    def test_review_archive_restore_permissions_are_checked_before_crm_side_effects(self):
        self.service.is_archived = True
        self.service.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.service.save()
        self.pool_a.is_deleted = True
        self.pool_a.save()
        reading = WaterReading.objects.create(pool=self.pool_a, date=timezone.now(), is_deleted=True)
        for field, value in (("pool_uuids", self.pool_a.uuid), ("reading_uuids", reading.uuid)):
            with self.subTest(field=field):
                response = self.client.post(reverse("archive_bulk_update"), {
                    "org_id": self.a.pk, "item_ids": [self.service.pk], "archive_action": "restore", field: [value],
                })
                self.service.refresh_from_db()
                self.assertTrue(self.service.is_archived)
                self.assertEqual(response.status_code, 403)
        self.access_a.role = "admin"
        self.access_a.save()
        response = self.client.post(reverse("archive_bulk_update"), {
            "org_id": self.a.pk, "item_ids": [self.service.pk], "archive_action": "restore",
            "pool_uuids": [self.pool_a.uuid], "reading_uuids": [reading.uuid],
        })
        self.assertEqual(response.status_code, 302)
        self.service.refresh_from_db()
        self.pool_a.refresh_from_db()
        reading.refresh_from_db()
        self.assertFalse(self.service.is_archived)
        self.assertFalse(self.pool_a.is_deleted)
        self.assertFalse(reading.is_deleted)

    def test_review_pool_crm_read_and_write_share_fresh_actor_policy(self):
        self.service.pool = self.pool_a
        self.service.save()
        pool_url = reverse("pool_detail", args=[self.pool_a.uuid])
        update_url = reverse("pool_issue_update", args=[self.pool_a.uuid, self.service.pk])
        allowed = self.client.get(pool_url)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn(self.service, allowed.context["service_issues"])
        employee = Employee.objects.create(organization=self.a, user=self.user, display_name="Synthetic inactive", is_active=False)
        hidden = self.client.get(pool_url)
        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(hidden.context["service_issues"], [])
        self.assertFalse(hidden.context["show_service_issues"])
        self.assertEqual(self.client.get(self.url("crm_view", self.service)).status_code, 403)
        self.assertEqual(self.client.post(update_url, {"stage": CrmItem.STAGE_SERVICE_DONE}).status_code, 403)
        self.service.refresh_from_db()
        self.assertEqual(self.service.stage, CrmItem.STAGE_SERVICE_NEW)
        employee.is_active = True
        employee.save()
        updated = self.client.post(update_url, {"stage": CrmItem.STAGE_SERVICE_IN_PROGRESS})
        self.assertEqual(updated.status_code, 302)
        self.service.refresh_from_db()
        self.assertEqual(self.service.stage, CrmItem.STAGE_SERVICE_IN_PROGRESS)

    def test_review_pool_crm_accountant_context_and_selected_b_subscription(self):
        self.service.pool = self.pool_a
        self.service.save()
        self.access_a.role = "accountant"
        self.access_a.save()
        response = self.client.get(reverse("pool_detail", args=[self.pool_a.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["service_issues"], [])
        self.assertEqual(self.client.post(reverse("pool_issue_update", args=[self.pool_a.uuid, self.service.pk]), {"stage": CrmItem.STAGE_SERVICE_DONE}).status_code, 403)
        pool_b = Pool.objects.create(organization=self.b, client=self.client_b, address="B pool")
        issue_b = self.item(self.b, "service", "B issue", pool=pool_b)
        self.a.trial_started_at = None
        self.a.save()
        update_url = reverse("pool_issue_update", args=[pool_b.uuid, issue_b.pk])
        self.assertEqual(self.client.post(update_url, {"org_id": self.a.pk, "stage": CrmItem.STAGE_SERVICE_DONE}).status_code, 403)
        self.assertEqual(self.client.post(update_url, {"org_id": self.b.pk, "stage": CrmItem.STAGE_SERVICE_IN_PROGRESS}).status_code, 302)
        issue_b.refresh_from_db()
        self.assertEqual(issue_b.stage, CrmItem.STAGE_SERVICE_IN_PROGRESS)

    def test_review_cross_org_link_never_mutates_any_selected_crm_or_foreign_task(self):
        self.access_b.delete()
        foreign = self.task(self.b, self.service)
        foreign.created_by = None
        foreign.save()
        good = self.item(self.a, "service", "Good A")
        self.assertFalse(views._task_can_edit(foreign, self.user))
        for action, extra in (
            ("set_responsible", {"bulk_responsible": self.user.pk}),
            ("set_stage", {"bulk_stage": CrmItem.STAGE_SERVICE_DONE}),
            ("archive", {}),
        ):
            with self.subTest(action=action):
                before_items = list(CrmItem.objects.filter(pk__in=[good.pk, self.service.pk]).order_by("pk").values())
                before_tasks = list(ServiceTask.objects.filter(pk=foreign.pk).values())
                response = self.client.post(self.url("crm_bulk_update", direction="service"), {
                    "org_id": self.a.pk, "item_ids": [good.pk, self.service.pk], "bulk_action": action, **extra,
                })
                self.assertEqual(list(CrmItem.objects.filter(pk__in=[good.pk, self.service.pk]).order_by("pk").values()), before_items)
                self.assertEqual(list(ServiceTask.objects.filter(pk=foreign.pk).values()), before_tasks)
                self.assertEqual(response.status_code, 403)

    def test_review_cross_org_graph_blocks_edit_restore_and_delete_cascades(self):
        foreign = self.task(self.b, self.service)
        edit = self.client.post(self.url("crm_edit", self.service), self.payload())
        self.service.refresh_from_db()
        self.assertEqual(self.service.title, "A service")
        self.assertEqual(edit.status_code, 403)
        self.service.is_archived = True
        self.service.archived_reason = CrmItem.ARCHIVE_REASON_DELETED
        self.service.save()
        foreign.is_archived = True
        foreign.archived_reason = ServiceTask.ARCHIVE_REASON_DELETED
        foreign.save()
        before_item = list(CrmItem.objects.filter(pk=self.service.pk).values())
        before_task = list(ServiceTask.objects.filter(pk=foreign.pk).values())
        for name in ("archive_restore_crm_item", "archive_delete_crm_item_forever"):
            response = self.client.post(reverse(name, args=[self.service.pk]))
            self.assertEqual(response.status_code, 403)
            self.assertEqual(list(CrmItem.objects.filter(pk=self.service.pk).values()), before_item)
            self.assertEqual(list(ServiceTask.objects.filter(pk=foreign.pk).values()), before_task)
        for action in ("restore", "delete_forever"):
            response = self.client.post(reverse("archive_bulk_update"), {
                "org_id": self.a.pk, "item_ids": [self.service.pk], "archive_action": action,
            })
            self.assertEqual(response.status_code, 403)
            self.assertEqual(list(CrmItem.objects.filter(pk=self.service.pk).values()), before_item)
            self.assertEqual(list(ServiceTask.objects.filter(pk=foreign.pk).values()), before_task)

    def test_review_foreign_linked_task_is_not_disclosed_on_own_crm_card(self):
        foreign = self.task(self.b, self.service)
        foreign.title = "Foreign B task must remain hidden"
        foreign.save()
        own = self.task(self.a, self.service)
        response = self.client.get(self.url("crm_view", self.service))
        self.assertEqual(response.status_code, 200)
        self.assertIn(own, response.context["related_tasks"])
        self.assertNotIn(foreign, response.context["related_tasks"])
        self.assertNotContains(response, foreign.title)

    def test_review_pool_crm_role_revocation_ignores_stale_organization_cache(self):
        self.service.pool = self.pool_a
        self.service.save()
        PoolAccess.objects.create(user=self.user, pool=self.pool_a, role="viewer")
        stale_accesses = list(OrganizationAccess.objects.filter(user=self.user).select_related("organization"))
        self.access_a.delete()
        with patch("pool_service.views.organization_accesses_for_user", return_value=stale_accesses):
            response = self.client.get(reverse("pool_detail", args=[self.pool_a.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["service_issues"], [])
        self.assertFalse(response.context["show_service_issues"])

    def test_review_inactive_pool_issue_post_never_changes_stage(self):
        self.service.pool = self.pool_a
        self.service.save()
        Employee.objects.create(organization=self.a, user=self.user, display_name="Synthetic inactive writer", is_active=False)
        response = self.client.post(reverse("pool_issue_update", args=[self.pool_a.uuid, self.service.pk]), {"stage": CrmItem.STAGE_SERVICE_DONE})
        self.service.refresh_from_db()
        self.assertEqual(self.service.stage, CrmItem.STAGE_SERVICE_NEW)
        self.assertEqual(response.status_code, 403)
