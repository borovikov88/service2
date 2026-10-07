"""PR287 must preserve the internal-call behavior delivered by PR288."""
import csv
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import CallAnalysis, CommunicationAccess, PhoneCall, TelephonyConnection
from pool_service.models import Client, Organization, OrganizationAccess


class CRMInternalCallIntegrationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Internal call CRM regression", paid_until=timezone.now() + timedelta(days=30))
        User = get_user_model()
        self.owner = User.objects.create_user(username="internal-crm-owner", first_name="Initiator")
        self.peer = User.objects.create_user(username="internal-crm-peer", first_name="Recipient")
        self.other = User.objects.create_user(username="internal-crm-other", first_name="Unrelated")
        for user, role in ((self.owner, "owner"), (self.peer, "manager"), (self.other, "manager")):
            OrganizationAccess.objects.create(user=user, organization=self.org, role=role)
        CommunicationAccess.objects.update_or_create(user=self.peer, organization=self.org, defaults={
            "can_view_own_calls": True, "can_view_all_calls": False, "can_listen_calls": True,
        })
        self.connection = TelephonyConnection.objects.create(organization=self.org, name="Regression PBX")
        self.client.force_login(self.owner)

    def call(self, **values):
        defaults = dict(
            organization=self.org, connection=self.connection, source_kind="telephony",
            external_id=str(PhoneCall.objects.count()), direction="internal", result="answered",
            employee=self.owner, peer_employee=self.peer, phone_number="+7 900 123 4567",
            provider_extension="101", peer_provider_extension="102", started_at=timezone.now(),
        )
        defaults.update(values)
        return PhoneCall.objects.create(**defaults)

    def test_both_participants_keep_list_filter_and_export_access(self):
        own = self.call()
        hidden = self.call(peer_employee=self.other)
        CallAnalysis.objects.create(call=own, status="ready", transcript="Own internal transcript")
        CallAnalysis.objects.create(call=hidden, status="ready", transcript="Hidden transcript")
        self.client.force_login(self.peer)
        page = self.client.get(reverse("communications_calls"), {"direction": "internal"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual([row.pk for row in page.context["calls"]], [own.pk])
        self.assertContains(page, "Initiator")
        self.assertContains(page, "Recipient")
        self.assertContains(page, 'value="internal"')
        self.assertContains(page, "Внутренний звонок")
        exported = self.client.get(reverse("communication_call_transcripts_export"), {"format": "csv", "direction": "internal"})
        self.assertEqual(exported.status_code, 200)
        rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1:3], ["", ""])
        self.assertIn("Initiator", rows[1][3])
        self.assertIn("Recipient", rows[1][3])
        self.assertIn("Own internal transcript", rows[1])
        self.assertNotIn("Hidden transcript", exported.content.decode("utf-8-sig"))

    def test_owner_employee_filter_includes_both_sides(self):
        as_peer = self.call()
        as_caller = self.call(employee=self.peer, peer_employee=self.other)
        self.call(employee=self.owner, peer_employee=self.other)
        page = self.client.get(reverse("communications_calls"), {"employee": self.peer.pk})
        self.assertEqual(page.status_code, 200)
        self.assertEqual({row.pk for row in page.context["calls"]}, {as_peer.pk, as_caller.pk})

    def test_internal_numbers_never_create_clients_or_enter_client_history(self):
        internal = self.call()
        customer = Client.objects.create(organization=self.org, name="Not a colleague", client_type="private", phone="89001234567")
        external = self.call(direction="in", peer_employee=None)
        page = self.client.get(reverse("communications_calls"), {"direction": "internal"})
        self.assertNotContains(page, "data-call-create-client")
        self.assertNotContains(page, "Not a colleague")
        self.assertIsNone(page.context["calls"][0].resolved_client)
        page = self.client.get(reverse("communications_calls"), {"client": customer.pk})
        self.assertEqual([row.pk for row in page.context["calls"]], [external.pk])
        card = self.client.get(reverse("client_detail", args=[customer.pk]), {"tab": "calls"})
        self.assertEqual([row.pk for row in card.context["calls"]], [external.pk])
        create_url = reverse("communication_call_client_create", args=[internal.pk])
        self.assertEqual(self.client.get(create_url).status_code, 403)
        self.assertEqual(self.client.post(create_url, {"name": "Must not be created"}).status_code, 403)
        self.assertEqual(Client.objects.count(), 1)
        internal.refresh_from_db()
        self.assertIsNone(internal.client_id)

    def test_unknown_internal_number_has_no_quick_create_action(self):
        internal = self.call()
        page = self.client.get(reverse("communications_calls"))
        self.assertContains(page, "Внутренний звонок")
        self.assertNotContains(page, "data-call-create-client")
        self.assertEqual(page.context["calls"][0].create_client_url, "")
        self.assertEqual(self.client.post(reverse("communication_call_client_create", args=[internal.pk]), {"name": "Must not be created"}).status_code, 403)
        self.assertFalse(Client.objects.exists())
