"""Synthetic CRM-derived read boundaries; no production data."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, ClientAccess, CrmItem, Employee, Notification, Organization, OrganizationAccess, ServiceTask


@patch("pool_service.views._redirect_if_access_blocked", return_value=None)
class TaskReadPrivacyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Task privacy A")
        self.actor = User.objects.create_user("privacy-reader")
        self.other = User.objects.create_user("privacy-author")
        self.access = OrganizationAccess.objects.create(organization=self.org, user=self.actor, role="manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.other, role="manager")
        self.item = CrmItem.objects.create(organization=self.org, title="Permitted deal", direction="service", stage="srv_new")
        self.task = ServiceTask.objects.create(
            organization=self.org, crm_item=self.item, title="PRIVATE_TASK_SENTINEL",
            description="PRIVATE_HISTORY_SENTINEL", start_date=timezone.localdate(),
            visibility=ServiceTask.VISIBILITY_PRIVATE, created_by=self.other,
            primary_responsible=self.other,
        )
        self.client.force_login(self.actor)

    def card(self):
        return self.client.get(reverse("crm_view", args=["service", self.item.pk]))

    def test_private_unrelated_task_hidden_from_card_and_archive(self, _blocked):
        response = self.card()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["related_tasks"]), [])
        self.assertNotContains(response, self.task.title)
        self.task.is_archived = True
        self.task.save(update_fields=["is_archived"])
        response = self.client.get(reverse("archive_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["archived_tasks"]), [])
        self.assertEqual(response.context["archive_task_count"], 0)
        self.assertNotContains(response, self.task.title)

    def test_unrelated_private_task_hidden_from_client_card(self, _blocked):
        client = Client.objects.create(organization=self.org, name="Synthetic client", client_type="private")
        self.task.client = client
        self.task.save(update_fields=["client"])
        response = self.client.get(reverse("client_detail", args=[client.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.task.pk, [task.pk for task in response.context["tasks"]])
        self.assertNotContains(response, self.task.title)

    def test_unrelated_private_task_hidden_from_archive(self, _blocked):
        self.task.is_archived = True
        self.task.save(update_fields=["is_archived"])
        response = self.client.get(reverse("archive_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.task.pk, [task.pk for task in response.context["archived_tasks"]])

    def test_stale_task_notification_hidden(self, _blocked):
        Notification.objects.create(user=self.actor, organization=self.org, kind="task_assignment",
            title="Task", message=self.task.title, action_url=reverse("task_edit", args=[self.task.pk]))
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["notifications"]), [])

    def test_membership_and_participant_revocation_close_next_read(self, _blocked):
        self.task.responsibles.add(self.actor)
        note = Notification.objects.create(
            user=self.actor, organization=self.org, kind="task_assignment",
            title="Task", message=self.task.title,
            action_url=reverse("task_edit", args=[self.task.pk]),
        )
        self.assertContains(self.card(), self.task.title)
        self.assertContains(self.client.get(reverse("notifications")), self.task.title)
        self.task.responsibles.remove(self.actor)
        self.assertNotContains(self.card(), self.task.title)
        response = self.client.get(reverse("notifications"))
        self.assertNotContains(response, self.task.title)
        self.assertNotIn(note.pk, [row.pk for row in response.context["notifications"]])
        self.task.responsibles.add(self.actor)
        self.access.delete()
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 403)
        self.assertNotContains(self.client.get(reverse("notifications")), self.task.title)

    def test_role_demotion_and_cached_authority_do_not_keep_derived_read(self, _blocked):
        from pool_service.services.task_read_permissions import readable_tasks
        self.access.role = "owner"
        self.access.save(update_fields=["role"])
        self.assertContains(self.card(), self.task.title)
        self.access.role = "manager"
        self.access.save(update_fields=["role"])
        self.assertNotContains(self.card(), self.task.title)
        self.actor.is_superuser = True
        self.assertEqual(readable_tasks(ServiceTask.objects.all(), self.actor), [])
        self.task.responsibles.add(self.actor)
        User.objects.filter(pk=self.actor.pk).update(is_active=False)
        self.assertEqual(readable_tasks(ServiceTask.objects.all(), self.actor), [])

    def test_public_and_primary_client_preview_do_not_grant_direct_read(self, _blocked):
        client = Client.objects.create(organization=self.org, name="Preview client", client_type="private")
        self.task.client = client
        self.task.visibility = ServiceTask.VISIBILITY_PUBLIC
        self.task.save(update_fields=["client", "visibility"])
        for visibility in (ServiceTask.VISIBILITY_PUBLIC, ServiceTask.VISIBILITY_PRIVATE):
            self.task.visibility = visibility
            if visibility == ServiceTask.VISIBILITY_PRIVATE:
                self.task.primary_responsible = self.actor
            self.task.save(update_fields=["visibility", "primary_responsible"])
            self.assertContains(self.client.get(reverse("client_detail", args=[client.pk])), self.task.title)
            self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 403)

    def test_batch_filter_has_constant_query_count(self, _blocked):
        from pool_service.services.task_read_permissions import readable_tasks, visible_task_notifications
        self.task.responsibles.add(self.actor)
        with self.assertNumQueries(2):
            self.assertEqual(len(readable_tasks(ServiceTask.objects.all(), self.actor)), 1)
        for index in range(12):
            task = ServiceTask.objects.create(organization=self.org, title=f"Batch {index}",
                start_date=timezone.localdate(), created_by=self.actor)
            Notification.objects.create(user=self.actor, organization=self.org, kind="task_assignment",
                title="Task", message=task.title, action_url=reverse("task_edit", args=[task.pk]))
        with self.assertNumQueries(3):
            self.assertEqual(len(visible_task_notifications(Notification.objects.all(), self.actor)), 12)
        with self.assertNumQueries(2):
            self.assertEqual(len(readable_tasks(ServiceTask.objects.all(), self.actor)), 13)

    def test_client_affiliation_is_current_and_scoped_to_task_client(self, _blocked):
        client = Client.objects.create(organization=self.org, name="Customer", client_type="private", user=self.actor)
        self.task.client = client
        self.task.created_by = self.actor
        self.task.save(update_fields=["client", "created_by"])
        self.access.delete()
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 200)
        client.user = None
        client.save(update_fields=["user"])
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 403)
        access = ClientAccess.objects.create(user=self.actor, client=client)
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 200)
        access.delete()
        other = Client.objects.create(organization=self.org, name="Other customer", client_type="private", user=self.actor)
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 403)

    def test_staff_deactivation_closes_read_but_keeps_separate_client_contract(self, _blocked):
        from pool_service.services.task_read_permissions import readable_tasks
        self.task.responsibles.add(self.actor)
        note = Notification.objects.create(user=self.actor, organization=self.org, kind="task_assignment",
            title="Task", message=self.task.title, action_url=reverse("task_edit", args=[self.task.pk]))
        employee = Employee.objects.create(organization=self.org, user=self.actor, display_name="Synthetic staff")
        for changes in ({"is_active": False}, {"is_active": True, "employment_status": Employee.STATUS_DISMISSED}):
            Employee.objects.filter(pk=employee.pk).update(**changes)
            self.assertEqual(readable_tasks(ServiceTask.objects.all(), self.actor), [])
            self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 403)
            self.assertNotContains(self.client.get(reverse("notifications")), self.task.title)
        client = Client.objects.create(organization=self.org, name="Own customer", client_type="private", user=self.actor)
        self.task.client = client
        self.task.save(update_fields=["client"])
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 200)
        other = Organization.objects.create(name="Active staff in B")
        OrganizationAccess.objects.create(user=self.actor, organization=other, role="manager")
        task = ServiceTask.objects.create(organization=other, title="Allowed B", created_by=self.actor,
            start_date=timezone.localdate())
        self.assertIn(task.pk, [row.pk for row in readable_tasks(ServiceTask.objects.filter(pk=task.pk), self.actor)])

    def test_existing_direct_read_roles_do_not_gain_public_or_primary_only_access(self, _blocked):
        for visibility in (ServiceTask.VISIBILITY_PUBLIC, ServiceTask.VISIBILITY_PRIVATE):
            self.task.visibility = visibility
            self.task.primary_responsible = self.actor
            self.task.save(update_fields=["visibility", "primary_responsible"])
            self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 403)
            self.task.responsibles.add(self.actor)
            self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 200)
            self.task.responsibles.remove(self.actor)
        self.task.created_by = self.actor
        self.task.save(update_fields=["created_by"])
        self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 200)
        self.task.created_by = self.other
        self.task.save(update_fields=["created_by"])
        for role in ("owner", "admin"):
            self.access.role = role
            self.access.save(update_fields=["role"])
            self.assertEqual(self.client.get(reverse("task_edit", args=[self.task.pk])).status_code, 200)
