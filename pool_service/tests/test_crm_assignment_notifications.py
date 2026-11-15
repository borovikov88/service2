"""Synthetic assignment delivery: transaction, preferences, privacy and replay."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_assignment_models import CrmAssignmentDelivery
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmEvent, CrmLead, CrmPipeline, CrmStage
from pool_service.models import Notification, Organization, OrganizationAccess, Profile
from pool_service.services.crm_assignment_notifications import notify_assignment, deliver_assignment_push


class AssignmentNotificationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic assignment delivery', crm_v2_read=True,
            crm_v2_write=True, paid_until=timezone.now() + timedelta(days=30))
        self.user = get_user_model().objects.create_user('synthetic-assignee')
        self.membership = OrganizationAccess.objects.create(organization=self.org, user=self.user, role='manager')
        CrmAccess.objects.create(organization=self.org, user=self.user, view_leads=True,
            edit_own_leads=True, view_team_deals=True, edit_own_deals=True)
        self.lead = CrmLead.objects.create(organization=self.org, assignee=self.user,
            title='Synthetic private title', need='Synthetic private need', status='working')
        self.event = CrmEvent.objects.create(organization=self.org, lead=self.lead, event_type='lead.assign',
            safe_diff={'assignee_id': {'before': None, 'after': self.user.pk}})
        push = patch('pool_service.services.crm_assignment_notifications.send_push_to_users')
        self.push = push.start()
        self.addCleanup(push.stop)

    def preferences(self, **values):
        Profile.objects.update_or_create(user=self.user, defaults=values)

    def test_assignment_is_transactional_deduplicated_and_privacy_safe(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = notify_assignment(self.event, self.user)
            self.assertEqual(notify_assignment(self.event, self.user).pk, first.pk)
            self.push.assert_not_called()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(CrmAssignmentDelivery.objects.count(), 1)
        self.push.assert_called_once()
        note = Notification.objects.get()
        self.assertEqual(note.kind, 'crm_assignment')
        self.assertEqual(note.user_id, self.user.pk)
        self.assertIn(f'lead={self.lead.pk}', note.action_url)
        self.assertIn(f'org_id={self.org.pk}', note.action_url)
        self.assertNotIn('private', note.title + note.message)
        deliver_assignment_push(first.pk)
        self.push.assert_called_once()

    def test_push_only_preferences_still_have_a_durable_deduplication_receipt(self):
        self.preferences(in_app_notifications_enabled=False, push_notifications_enabled=True)
        with self.captureOnCommitCallbacks(execute=True):
            receipt = notify_assignment(self.event, self.user)
            notify_assignment(self.event, self.user)
        self.assertFalse(Notification.objects.exists())
        self.push.assert_called_once()
        deliver_assignment_push(receipt.pk)
        self.push.assert_called_once()

    def test_both_disabled_and_in_app_only_preferences(self):
        self.preferences(in_app_notifications_enabled=False, push_notifications_enabled=False)
        with self.captureOnCommitCallbacks(execute=True):
            notify_assignment(self.event, self.user)
        self.assertFalse(Notification.objects.exists())
        self.push.assert_not_called()
        other = CrmEvent.objects.create(organization=self.org, lead=self.lead, event_type='lead.assign',
            safe_diff={'assignee_id': {'before': None, 'after': self.user.pk}})
        self.preferences(in_app_notifications_enabled=True, push_notifications_enabled=False)
        with self.captureOnCommitCallbacks(execute=True):
            notify_assignment(other, self.user)
        self.assertEqual(Notification.objects.count(), 1)
        self.push.assert_not_called()

    def test_rollback_discards_notification_and_push(self):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError), transaction.atomic():
                notify_assignment(self.event, self.user)
                raise ValueError('Synthetic rollback')
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(CrmAssignmentDelivery.objects.exists())
        self.push.assert_not_called()

    def test_revoked_membership_suppresses_deferred_push(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify_assignment(self.event, self.user)
            self.membership.delete()
        self.push.assert_not_called()
        self.assertNotIn('private', Notification.objects.get().message)

    def test_reassignment_suppresses_obsolete_push_even_if_owner_returns(self):
        with self.captureOnCommitCallbacks(execute=True):
            old = notify_assignment(self.event, self.user)
            current = CrmEvent.objects.create(organization=self.org, lead=self.lead, event_type='lead.assign',
                safe_diff={'assignee_id': {'before': self.user.pk + 100, 'after': self.user.pk}})
            notify_assignment(current, self.user)
        self.push.assert_called_once()
        old.refresh_from_db()
        self.assertIsNone(old.push_attempted_at)

    def test_new_privacy_restriction_and_preference_are_rechecked_at_delivery(self):
        with patch(
            'pool_service.services.crm_assignment_notifications.can_read_lead', side_effect=[True, False]
        ), self.captureOnCommitCallbacks(execute=True):
            notify_assignment(self.event, self.user)
        self.push.assert_not_called()
        with self.captureOnCommitCallbacks(execute=True):
            receipt = notify_assignment(self.event, self.user)
            self.preferences(push_notifications_enabled=False)
            deliver_assignment_push(receipt.pk)
        self.push.assert_not_called()

    def test_transport_failure_cannot_rollback_assignment_or_retry_uncertain_push(self):
        self.push.side_effect = RuntimeError('Synthetic private transport details')
        with self.captureOnCommitCallbacks(execute=True):
            receipt = notify_assignment(self.event, self.user)
        self.assertEqual(Notification.objects.count(), 1)
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.push_attempted_at)
        deliver_assignment_push(receipt.pk)
        self.push.assert_called_once()

    def test_deal_notice_has_protected_pipeline_and_card_link(self):
        bootstrap_commercial_settings(self.org.pk)
        pipeline = CrmPipeline.objects.get(organization=self.org, code='construction')
        deal = CrmDeal.objects.create(organization=self.org, pipeline=pipeline,
            stage=CrmStage.objects.get(pipeline=pipeline, code='need'), manager=self.user,
            title='Synthetic private deal')
        event = CrmEvent.objects.create(organization=self.org, deal=deal, event_type='deal.assign',
            safe_diff={'manager_id': {'before': None, 'after': self.user.pk}})
        with self.captureOnCommitCallbacks(execute=True):
            notify_assignment(event, self.user)
        note = Notification.objects.get()
        self.assertTrue(note.action_url.startswith(reverse('crm_live_workspace')))
        self.assertIn(f'pipeline={pipeline.pk}', note.action_url)
        self.assertIn(f'deal={deal.pk}', note.action_url)
        self.assertNotIn('private', note.title + note.message)

    def test_same_assignee_event_and_wrong_recipient_do_not_notify(self):
        other = get_user_model().objects.create_user('synthetic-wrong-recipient')
        self.assertIsNone(notify_assignment(self.event, other))
        event = CrmEvent.objects.create(organization=self.org, lead=self.lead, event_type='lead.assign',
            safe_diff={'assignee_id': {'before': self.user.pk, 'after': self.user.pk}})
        self.assertIsNone(notify_assignment(event, self.user))
        self.assertFalse(Notification.objects.exists())

    def test_allowed_recipient_with_transition_permission_receives_notification(self):
        grant = CrmAccess.objects.get(user=self.user, organization=self.org)
        grant.edit_own_leads = False
        grant.transition_own_leads = True
        grant.save()
        with self.captureOnCommitCallbacks(execute=True):
            notify_assignment(self.event, self.user)
        self.assertEqual(Notification.objects.count(), 1)
        self.push.assert_called_once()

    def test_recipient_sees_notice_in_existing_feed_and_opens_its_card(self):
        notify_assignment(self.event, self.user)
        note = Notification.objects.get()
        self.client.force_login(self.user)
        response = self.client.get(reverse('notifications'))
        self.assertContains(response, note.title)
        self.assertContains(response, f'№{self.lead.pk}')
        opened = self.client.get(reverse('notification_push_open', args=[note.pk]))
        self.assertEqual(opened.status_code, 302)
        self.assertEqual(opened.url, note.action_url)
        self.assertEqual(self.client.get(opened.url).status_code, 200)
