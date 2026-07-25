from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from pool_service.models import Notification, Organization, OrganizationAccess, PushSubscription


class DatabaseConstraintTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="constraints-user", password="pass")
        self.organization = Organization.objects.create(name="Constraints organization")

    def test_second_organization_owner_is_rejected(self):
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="owner",
        )
        other_user = User.objects.create_user(username="second-owner", password="pass")

        with self.assertRaises(ValidationError):
            OrganizationAccess.objects.create(
                user=other_user,
                organization=self.organization,
                role="owner",
            )

    def test_notifications_allow_multiple_empty_dedupe_keys(self):
        Notification.objects.create(user=self.user, kind="new_company", title="First")
        Notification.objects.create(user=self.user, kind="new_company", title="Second")

        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)

    def test_notification_dedupe_key_is_unique_per_user(self):
        Notification.objects.create(
            user=self.user,
            kind="limits",
            title="First",
            dedupe_key="limits:1",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(
                user=self.user,
                kind="limits",
                title="Duplicate",
                dedupe_key="limits:1",
            )

    def test_push_endpoint_hash_is_unique(self):
        endpoint = "https://push.example.test/subscription"
        first = PushSubscription.objects.create(
            user=self.user,
            endpoint=endpoint,
            p256dh="key",
            auth="auth",
        )
        other_user = User.objects.create_user(username="push-user", password="pass")

        self.assertEqual(first.endpoint_hash, PushSubscription.hash_endpoint(endpoint))
        with self.assertRaises(IntegrityError), transaction.atomic():
            PushSubscription.objects.create(
                user=other_user,
                endpoint=endpoint,
                p256dh="other-key",
                auth="other-auth",
            )
