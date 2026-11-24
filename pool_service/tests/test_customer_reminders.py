from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.communication_models import (
    ChannelConnection, CommunicationChannel, Conversation, ConversationMessage, PhoneCall, TelephonyConnection,
)
from pool_service.crm_commercial_models import CrmLead
from pool_service.crm_followup_models import CustomerReminderPolicy, CustomerReminderReceipt, MissedCallFollowup
from pool_service.models import Employee, Notification, Organization, OrganizationAccess
from pool_service.staff_structure_models import DepartmentAssignment, StaffDepartment
from pool_service.services.customer_reminder_calendar import BARNAUL, reminder_step, working_deadline
from pool_service.services.customer_reminders import RecipientDirectory, Work, notify_due, work_items


class BusinessTimeTests(SimpleTestCase):
    def local(self, day, hour, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=BARNAUL)

    def test_friday_carries_the_remainder_to_monday(self):
        arrived = self.local(20, 17, 58)
        self.assertEqual(working_deadline(arrived, 5), self.local(23, 9, 3))
        self.assertEqual(working_deadline(arrived, 30), self.local(23, 9, 28))
        self.assertIsNone(reminder_step(arrived, self.local(21, 10)))
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 3)), "5")
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 28)), "30")
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 34)), "30")
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 35)), "morning")

    def test_after_hours_arrival_starts_at_next_opening(self):
        arrived = self.local(20, 18, 3)
        self.assertIsNone(reminder_step(arrived, self.local(23, 9, 4)))
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 5)), "5")
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 30)), "30")
        self.assertEqual(reminder_step(arrived, self.local(24, 9, 35)), "morning")


class CustomerReminderTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test reminder organization")
        self.manager = self.membership("reminder-manager", "manager")
        self.owner = self.membership("reminder-owner", "owner")
        self.start = datetime(2026, 11, 23, 10, tzinfo=BARNAUL)
        self.policy = CustomerReminderPolicy.objects.create(
            organization=self.org, fallback_owner=self.owner,
            started_at=self.start - timedelta(minutes=1),
        )
        channel = CommunicationChannel.objects.create(
            organization=self.org, name="Test site", kind=CommunicationChannel.KIND_WEBSITE,
        )
        connection = ChannelConnection.objects.create(channel=channel, external_id="site", name="Website")
        self.dialog = Conversation.objects.create(
            organization=self.org, connection=connection, external_id="chat:reminder-test",
        )

    def membership(self, username, role):
        user = get_user_model().objects.create_user(username)
        OrganizationAccess.objects.create(user=user, organization=self.org, role=role)
        return user

    def test_storefront_manager_reply_removes_work_before_delivery(self):
        incoming = ConversationMessage.objects.create(
            conversation=self.dialog, direction=ConversationMessage.DIRECTION_IN,
            body="Вопрос", intake_historical=False,
        )
        self.assertEqual([item.marker for item in work_items(self.org)], [incoming.pk])
        ConversationMessage.objects.create(
            conversation=self.dialog, direction=ConversationMessage.DIRECTION_OUT,
            delivery_status=ConversationMessage.DELIVERY_PENDING, sent_by=self.manager, body="Ответ",
        )
        self.assertEqual(list(work_items(self.org)), [])

    def test_reminder_groups_chat_and_lead_deduplicates_and_escalates(self):
        lead = CrmLead.objects.create(organization=self.org, title="Test lead", received_at=self.start)
        second_manager = self.membership("reminder-second-manager", "manager")
        items = [Work("message", self.dialog, self.start, 1, lead), Work("lead", lead, self.start, lead.pk, lead)]
        with patch("pool_service.services.customer_reminders.work_items", side_effect=lambda org, **filters: iter(items)), patch(
            "pool_service.services.customer_reminders.may_see", return_value=True,
        ), patch("pool_service.services.customer_reminders._current_work", side_effect=lambda work, org: work):
            earlier = self.start + timedelta(minutes=5)
            self.assertEqual(notify_due(self.policy, earlier), 2)
            self.assertEqual(notify_due(self.policy, earlier), 0)
            notice = Notification.objects.get(user=self.manager)
            self.assertIn("Ответить в чате", notice.message)
            self.assertIn("Разобрать лид", notice.message)
            self.assertEqual(Notification.objects.filter(user=second_manager).count(), 1)
            self.assertFalse(Notification.objects.filter(user=self.owner).exists())

            overdue = self.start + timedelta(minutes=30)
            self.assertEqual(notify_due(self.policy, overdue), 1)
            self.assertEqual(notify_due(self.policy, overdue), 0)
            self.assertEqual(Notification.objects.filter(user=self.owner).count(), 1)
            self.assertEqual(CustomerReminderReceipt.objects.filter(step="30").count(), 2)
            self.assertEqual(Notification.objects.filter(user=self.manager).count(), 1)

    def test_assigned_manager_and_active_department_head_are_resolved_from_structure(self):
        head = self.membership("reminder-sales-head", "manager")
        parent = StaffDepartment.objects.create(organization=self.org, name="Sales", created_by=self.owner)
        child = StaffDepartment.objects.create(organization=self.org, name="Retail", parent=parent, created_by=self.owner)
        head_employee = Employee.objects.create(organization=self.org, user=head, display_name="Sales head")
        member = Employee.objects.create(organization=self.org, user=self.manager, display_name="Sales manager")
        head_assignment = DepartmentAssignment.objects.create(
            department=parent, employee=head_employee, user_at_assignment=head, assigned_by=self.owner,
            position="sales_head", includes_children=True, valid_from=self.start - timedelta(days=1),
        )
        DepartmentAssignment.objects.create(
            department=child, employee=member, user_at_assignment=self.manager, assigned_by=self.owner,
            position="member", valid_from=self.start - timedelta(days=1),
        )
        self.dialog.assignee = self.manager
        work = Work("message", self.dialog, self.start, 1)
        with patch("pool_service.services.customer_reminders.may_see", return_value=True):
            directory = RecipientDirectory(self.policy, self.start)
            self.assertEqual(directory.recipients(work, "5"), (self.manager,))
            self.assertEqual(directory.recipients(work, "30"), (head,))
            self.assertFalse(directory.can_handle(work, head))
            self.dialog.assignee = head
            self.assertEqual(directory.recipients(work, "30"), (self.owner,))
            self.dialog.assignee = self.manager
            head_assignment.valid_to = self.start
            head_assignment.save(update_fields=["valid_to"])
            self.assertEqual(RecipientDirectory(self.policy, self.start).recipients(work, "30"), (self.owner,))

    def test_unassigned_escalation_uses_single_sales_head_or_owner(self):
        work = Work("message", self.dialog, self.start, 1)
        with patch("pool_service.services.customer_reminders.may_see", return_value=True):
            for index in range(2):
                head = self.membership(f"reminder-head-{index}", "manager")
                department = StaffDepartment.objects.create(
                    organization=self.org, name=f"Sales team {index}", created_by=self.owner,
                )
                employee = Employee.objects.create(organization=self.org, user=head, display_name=f"Head {index}")
                DepartmentAssignment.objects.create(
                    department=department, employee=employee, user_at_assignment=head, assigned_by=self.owner,
                    position="sales_head", valid_from=self.start - timedelta(days=1),
                )
                directory = RecipientDirectory(self.policy, self.start)
                self.assertEqual(directory.recipients(work, "30"), (head,) if index == 0 else (self.owner,))

    def test_unassigned_work_only_notifies_managers_who_can_see_it(self):
        denied = self.membership("reminder-restricted-manager", "manager")
        self.membership("reminder-service", "service")
        with patch("pool_service.services.customer_reminders.may_see", side_effect=lambda work, user, org: user != denied):
            directory = RecipientDirectory(self.policy, self.start)
            self.assertEqual(directory.recipients(Work("message", self.dialog, self.start, 1), "5"), (self.manager,))

    def test_shared_morning_summary_sees_all_carried_deadlines_once(self):
        morning = datetime(2026, 11, 24, 9, 35, tzinfo=BARNAUL)
        late = self.start.replace(hour=17, minute=58)
        lead1 = CrmLead.objects.create(organization=self.org, title="Private origin text 0000000000", received_at=self.start)
        lead2 = CrmLead.objects.create(organization=self.org, title="Test lead 2", received_at=late)
        items = [
            Work("lead", lead1, self.start, lead1.pk, lead1),
            Work("lead", lead2, late, lead2.pk, lead2),
        ]
        self.assertEqual(items[0].title, f"Лид №{lead1.pk}")
        with patch("pool_service.services.customer_reminders.work_items", side_effect=lambda org, **filters: iter(items)) as query, patch(
            "pool_service.services.customer_reminders.may_see", return_value=True,
        ):
            self.assertEqual(notify_due(self.policy, morning), 2)
            self.assertEqual(CustomerReminderReceipt.objects.filter(step="morning", work_key__startswith="lead:").count(), 4)
            self.assertEqual(notify_due(self.policy, morning + timedelta(minutes=1)), 0)
            self.assertEqual(sum(call.kwargs["since"] is None for call in query.call_args_list), 1)

    def test_proposed_then_answered_callback_leaves_queue(self):
        connection = TelephonyConnection.objects.create(
            organization=self.org, name="Test phone connection", external_id="reminder-pbx",
        )
        call = PhoneCall.objects.create(
            organization=self.org, connection=connection, external_id="test-missed",
            phone_number="+79000000001", direction=PhoneCall.DIRECTION_IN,
            result=PhoneCall.RESULT_MISSED, started_at=self.start, intake_historical=False,
        )
        self.assertEqual([item.kind for item in work_items(self.org)], ["call"])
        MissedCallFollowup.objects.create(call=call, next_attempt_at=self.start + timedelta(hours=1), actor=self.owner)
        self.assertTrue(next(work_items(self.org)).retry)
        PhoneCall.objects.create(
            organization=self.org, connection=connection, external_id="test-no-answer",
            phone_number="+79000000001", direction=PhoneCall.DIRECTION_OUT,
            result=PhoneCall.RESULT_MISSED, started_at=self.start + timedelta(minutes=35),
        )
        self.assertEqual([item.kind for item in work_items(self.org)], ["call"])
        PhoneCall.objects.create(
            organization=self.org, connection=connection, external_id="test-answered",
            phone_number="+79000000001", direction=PhoneCall.DIRECTION_OUT,
            result=PhoneCall.RESULT_ANSWERED, started_at=self.start + timedelta(minutes=37), duration_seconds=125,
        )
        self.assertEqual(list(work_items(self.org)), [])

    def test_only_owner_sets_policy_and_becomes_fallback(self):
        url = reverse("customer_reminder_settings") + f"?org_id={self.org.pk}"
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, {"enabled": True}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(url, {}).status_code, 302)
        self.policy.refresh_from_db()
        self.assertIsNone(self.policy.started_at)
        self.assertEqual(self.client.post(url, {"enabled": True}).status_code, 302)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.fallback_owner_id, self.owner.pk)
        self.assertIsNotNone(self.policy.started_at)

    def test_settings_find_owner_role_even_when_admin_role_was_added_first(self):
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(role="admin")
        other = Organization.objects.create(name="Other reminder organization")
        OrganizationAccess.objects.create(user=self.owner, organization=other, role="admin")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.client.force_login(self.owner)

        settings = reverse("customer_reminder_settings")
        queue = reverse("customer_followups")
        own_settings = f"{settings}?org_id={self.org.pk}"
        self.assertContains(self.client.get(queue, {"org_id": self.org.pk}), own_settings)
        self.assertContains(self.client.get(own_settings), "Напоминания включены")
        self.assertRedirects(self.client.post(own_settings, {}), f"{queue}?org_id={self.org.pk}")
        self.policy.refresh_from_db()
        self.assertIsNone(self.policy.started_at)
        self.assertEqual(self.client.post(own_settings, {"enabled": True}).status_code, 302)
        self.policy.refresh_from_db()
        self.assertIsNotNone(self.policy.started_at)
        self.assertEqual(self.policy.fallback_owner_id, self.owner.pk)

        other_settings = f"{settings}?org_id={other.pk}"
        self.assertNotContains(self.client.get(queue, {"org_id": other.pk}), other_settings)
        self.assertEqual(self.client.get(other_settings).status_code, 403)
        self.assertEqual(self.client.post(other_settings, {"enabled": True}).status_code, 403)
        self.assertFalse(CustomerReminderPolicy.objects.filter(organization=other).exists())

