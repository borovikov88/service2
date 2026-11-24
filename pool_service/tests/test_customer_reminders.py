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
from pool_service.models import Notification, Organization, OrganizationAccess
from pool_service.services.customer_reminder_calendar import BARNAUL, reminder_step, working_deadline
from pool_service.services.customer_reminders import Work, notify_due, work_items


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
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 33)), "morning")

    def test_after_hours_arrival_starts_at_next_opening(self):
        arrived = self.local(20, 18, 3)
        self.assertIsNone(reminder_step(arrived, self.local(23, 9, 4)))
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 5)), "5")
        self.assertEqual(reminder_step(arrived, self.local(23, 9, 30)), "30")
        self.assertEqual(reminder_step(arrived, self.local(24, 9, 15)), "morning")


class CustomerReminderTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test reminder organization")
        self.manager = self.membership("reminder-manager", "manager")
        self.owner = self.membership("reminder-owner", "owner")
        self.start = datetime(2026, 11, 23, 10, tzinfo=BARNAUL)
        self.policy = CustomerReminderPolicy.objects.create(
            organization=self.org, duty_manager=self.manager, supervisor=self.owner,
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
        items = [Work("message", self.dialog, self.start, 1, lead), Work("lead", lead, self.start, lead.pk, lead)]
        with patch("pool_service.services.customer_reminders.work_items", side_effect=lambda org: iter(items)), patch(
            "pool_service.services.customer_reminders.may_see", return_value=True,
        ), patch("pool_service.services.customer_reminders._current_work", side_effect=lambda work, org: work):
            earlier = self.start + timedelta(minutes=5)
            self.assertEqual(notify_due(self.policy, earlier), 1)
            self.assertEqual(notify_due(self.policy, earlier), 0)
            notice = Notification.objects.get(user=self.manager)
            self.assertIn("Ответить в чате", notice.message)
            self.assertIn("Разобрать лид", notice.message)
            self.assertFalse(Notification.objects.filter(user=self.owner).exists())

            overdue = self.start + timedelta(minutes=30)
            self.assertEqual(notify_due(self.policy, overdue), 2)
            self.assertEqual(notify_due(self.policy, overdue), 0)
            self.assertEqual(Notification.objects.filter(user=self.owner).count(), 1)
            self.assertEqual(CustomerReminderReceipt.objects.filter(step="30").count(), 4)

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

    def test_only_owner_sets_policy_and_rejects_unrelated_users(self):
        outside = get_user_model().objects.create_user("reminders-outside")
        url = reverse("customer_reminder_settings") + f"?org_id={self.org.pk}"
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.post(url, {
            "duty_manager": outside.pk, "supervisor": self.owner.pk, "enabled": True,
        })
        self.assertContains(response, "Настройки напоминаний")
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.duty_manager_id, self.manager.pk)
        response = self.client.post(url, {
            "duty_manager": self.manager.pk, "supervisor": self.owner.pk, "enabled": False,
        })
        self.assertEqual(response.status_code, 302)
        self.policy.refresh_from_db()
        self.assertIsNone(self.policy.started_at)
