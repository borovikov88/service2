"""Request-level coverage for the connected CRM/call pages, not a browser test."""
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import io

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client as Browser, RequestFactory, TestCase
from django.urls import resolve, reverse
from django.utils import timezone
from docx import Document

from pool_service.client_crm_models import ClientContact, ClientCRMProfile
from pool_service.communication_models import CallAnalysis, CommunicationAccess, PhoneCall, TelephonyConnection
from pool_service.models import Client, Organization, OrganizationAccess


class CRMCallPageIntegrationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Call page integration", paid_until=timezone.now() + timedelta(days=30),
        )
        self.foreign = Organization.objects.create(name="Other call page tenant")
        self.owner = get_user_model().objects.create_user(username="call-page-owner")
        self.worker = get_user_model().objects.create_user(username="call-page-worker")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        OrganizationAccess.objects.create(user=self.worker, organization=self.org, role="manager")
        CommunicationAccess.objects.update_or_create(
            user=self.worker, organization=self.org,
            defaults={"can_view_own_calls": True, "can_view_all_calls": False, "can_listen_calls": True},
        )
        self.connection = TelephonyConnection.objects.create(organization=self.org, name="Test line")
        self.client.force_login(self.owner)
        self.calls_url = reverse("communications_calls")
        self.files_url = reverse("communication_manual_recordings")
        worker = patch("pool_service.communication_views._wake_uploaded_audio_worker_if_needed")
        worker.start()
        self.addCleanup(worker.stop)

    def customer(self, phone="", **kwargs):
        return Client.objects.create(
            organization=kwargs.pop("organization", self.org),
            name=kwargs.pop("name", "Page customer"),
            client_type=kwargs.pop("client_type", "private"), phone=phone, **kwargs,
        )

    def call(self, phone="+7 900 123 4567", **kwargs):
        source = kwargs.pop("source_kind", PhoneCall.SOURCE_TELEPHONY)
        direction = kwargs.pop("direction", PhoneCall.DIRECTION_IN)
        result = kwargs.pop("result", PhoneCall.RESULT_ANSWERED)
        started_at = kwargs.pop("started_at", timezone.now() - timedelta(days=7))
        return PhoneCall.objects.create(
            organization=kwargs.pop("organization", self.org),
            connection=self.connection if source == PhoneCall.SOURCE_TELEPHONY else None,
            source_kind=source, external_id=str(PhoneCall.objects.count()),
            phone_number=phone, direction=direction, result=result,
            started_at=started_at, **kwargs,
        )

    def page_ids(self, url, data=None):
        response = self.client.get(url, data or {})
        self.assertEqual(response.status_code, 200)
        return {call.pk for call in response.context["calls"]}

    def card_ids(self, customer):
        response = self.client.get(reverse("client_detail", args=[customer.pk]), {"tab": "calls"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["card_tab"], "calls")
        self.assertEqual(response.context["calls_total"], len(response.context["calls"]))
        return {call.pk for call in response.context["calls"]}

    def test_urls_use_connected_crm_handlers(self):
        for name in ("communications_calls", "communication_manual_recordings", "communication_call_transcripts_export"):
            self.assertEqual(resolve(reverse(name)).func.__module__, "pool_service.communication_crm")
        self.assertEqual(
            resolve(reverse("communication_client_lookup", args=["telephony"])).func.__name__, "client_lookup",
        )

    def test_client_card_marks_missed_call_and_successful_callback_within_hour(self):
        customer = self.customer(phone="+7 900 123 4567")
        missed_at = timezone.now() - timedelta(minutes=30)
        missed = self.call(
            phone="+7 900 123 4567", client=customer,
            direction=PhoneCall.DIRECTION_IN, result=PhoneCall.RESULT_MISSED,
            started_at=missed_at,
        )
        for index in range(51):
            self.call(
                phone=f"+1 200 {index:03d} 01234567",
                direction=PhoneCall.DIRECTION_OUT, result=PhoneCall.RESULT_ANSWERED,
                started_at=missed_at + timedelta(minutes=index + 1),
            )
        self.call(
            phone="8 900 123 45 67", client=customer,
            direction=PhoneCall.DIRECTION_OUT, result=PhoneCall.RESULT_ANSWERED,
            started_at=missed_at + timedelta(minutes=55),
        )
        page = self.client.get(reverse("client_detail", args=[customer.pk]), {"tab": "calls"})
        self.assertEqual(page.status_code, 200)
        row = next(call for call in page.context["calls"] if call.pk == missed.pk)
        self.assertIsNotNone(row.callback_at)
        self.assertFalse(row.missed_unreturned)
        self.assertContains(page, "Перезвонили")

    def test_unreturned_missed_call_is_marked_red(self):
        customer = self.customer(phone="+7 900 123 4567")
        missed = self.call(
            phone="+7 900 123 4567", client=customer,
            direction=PhoneCall.DIRECTION_IN, result=PhoneCall.RESULT_MISSED,
            started_at=timezone.now() - timedelta(minutes=30),
        )
        page = self.client.get(reverse("client_detail", args=[customer.pk]), {"tab": "calls"})
        row = next(call for call in page.context["calls"] if call.pk == missed.pk)
        self.assertTrue(row.missed_unreturned)
        self.assertContains(page, "text-danger")

    def test_unknown_number_plus_creates_person_returns_to_filters_and_labels_history(self):
        call = self.call()
        filters = {"direction": "in", "date_from": (timezone.localdate() - timedelta(days=8)).isoformat()}
        page = self.client.get(self.calls_url, filters)
        self.assertContains(page, "data-call-create-client")
        create_url = page.context["calls"][0].create_client_url
        count = Client.objects.count()
        form = self.client.get(create_url)
        self.assertEqual(form.status_code, 200)
        self.assertEqual(Client.objects.count(), count)
        back = parse_qs(urlsplit(create_url).query)["back"][0]
        result = self.client.post(urlsplit(create_url).path, {
            "back": back, "client_type": "private", "name": "Later person",
            "phone": "+34 900 999 9999",  # The call's server-side phone must win.
        })
        self.assertEqual(result.status_code, 302)
        self.assertEqual(urlsplit(result.url).path, self.calls_url)
        self.assertEqual(parse_qs(urlsplit(result.url).query), {key: [value] for key, value in filters.items()})
        customer = Client.objects.get(name="Later person")
        self.assertEqual(customer.phone, "+7 900 123 4567")
        call.refresh_from_db()
        self.assertIsNone(call.client_id)
        page = self.client.get(result.url)
        self.assertContains(page, "Later person")
        self.assertContains(page, reverse("client_detail", args=[customer.pk]))
        self.assertNotContains(page, "data-call-create-client")
        self.assertEqual(self.page_ids(self.calls_url, {"client": customer.pk}), {call.pk})
        self.assertEqual(self.card_ids(customer), {call.pk})

    def test_create_company_and_duplicate_resubmission(self):
        call = self.call()
        url = reverse("communication_call_client_create", args=[call.pk])
        data = {"client_type": "legal", "name": "Later company", "profile-legal_name": "Later Company LLC"}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        customer = Client.objects.get(name="Later company")
        self.assertEqual(customer.company_name, "Later company")
        self.assertEqual(customer.client_type, "legal")
        self.assertEqual(customer.crm_profile.legal_name, "Later Company LLC")
        count = Client.objects.count()
        repeated = self.client.post(url, data)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(Client.objects.count(), count)
        self.assertEqual([item.pk for item in repeated.context["existing_matches"]], [customer.pk])
        call.refresh_from_db()
        self.assertIsNone(call.client_id)

    def test_invalid_form_is_atomic_and_external_return_is_not_used(self):
        call = self.call()
        url = reverse("communication_call_client_create", args=[call.pk])
        counts = (Client.objects.count(), ClientCRMProfile.objects.count(), ClientContact.objects.count())
        response = self.client.post(url, {
            "name": "Invalid customer", "client_type": "private", "profile-birth_date": "invalid",
            "next": "https://example.invalid/", "organization": self.foreign.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("birth_date", response.context["profile_form"].errors)
        self.assertEqual(counts, (Client.objects.count(), ClientCRMProfile.objects.count(), ClientContact.objects.count()))
        response = self.client.post(url, {
            "name": "Safe customer", "client_type": "private", "next": "https://example.invalid/",
            "organization": self.foreign.pk,
        })
        self.assertRedirects(response, self.calls_url, fetch_redirect_response=False)
        self.assertEqual(Client.objects.get(name="Safe customer").organization_id, self.org.pk)

    def test_return_token_tampering_and_csrf_are_rejected(self):
        call = self.call()
        url = reverse("communication_call_client_create", args=[call.pk])
        self.assertEqual(self.client.get(url, {"back": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"back": "invalid", "name": "No write"}).status_code, 400)
        browser = Browser(enforce_csrf_checks=True)
        browser.force_login(self.owner)
        self.assertEqual(browser.post(url, {"name": "No write"}).status_code, 403)
        self.assertFalse(Client.objects.exists())

    def test_creation_and_lookup_respect_role_and_organization(self):
        call = self.call(employee=self.worker)
        self.client.force_login(self.worker)
        self.assertNotContains(self.client.get(self.calls_url), "data-call-create-client")
        self.assertEqual(self.client.get(reverse("communication_call_client_create", args=[call.pk])).status_code, 403)
        self.client.force_login(self.owner)
        foreign_call = self.call(organization=self.foreign)
        self.assertEqual(self.client.get(reverse("communication_call_client_create", args=[foreign_call.pk])).status_code, 403)
        accountant = get_user_model().objects.create_user(username="call-page-accountant")
        OrganizationAccess.objects.create(user=accountant, organization=self.org, role="accountant")
        self.client.force_login(accountant)
        lookup_url = reverse("communication_client_lookup", args=["telephony"])
        # FinanceOnlyRoleMiddleware redirects safe requests before the view runs.
        self.assertRedirects(
            self.client.get(lookup_url, {"q": "Page"}),
            reverse("finance_dashboard"), fetch_redirect_response=False,
        )
        # The view must independently deny access when called without middleware.
        request = RequestFactory().get(lookup_url, {"q": "Page"})
        request.user = accountant
        with self.assertRaises(PermissionDenied):
            resolve(lookup_url).func(request, source_kind="telephony")
        self.assertFalse(Client.objects.exists())

    def test_lazy_fields_do_not_render_the_directory_and_selected_value_survives(self):
        customer = self.customer(name="Directory sentinel 001")
        for url in (self.calls_url, self.files_url):
            page = self.client.get(url)
            self.assertContains(page, "data-crm-lookup")
            self.assertContains(page, "crm-client-picker.js")
            self.assertNotContains(page, "Directory sentinel 001")
            self.assertNotContains(page, '<select class="form-select" name="client">')
            page = self.client.get(url, {"client": customer.pk})
            self.assertContains(page, 'value="Directory sentinel 001"')
            self.assertContains(page, f'name="client" value="{customer.pk}"')
        page = self.client.get(self.files_url)
        self.assertContains(page, 'id="upload-client-lookup"')
        self.assertContains(page, 'id="filter-client-lookup"')

    def test_real_lookup_route_enforces_minimum_and_match_limit(self):
        self.customer(name="Needle hidden foreign", organization=self.foreign)
        for index in range(25):
            self.customer(name=f"Needle {index:02d}")
        url = reverse("communication_client_lookup", args=["telephony"])
        for query in ("", "Ne", " Ne - "):
            response = self.client.get(url, {"q": query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"results": [], "has_more": False})
        response = self.client.get(url, {"q": "Needle"})
        self.assertEqual(len(response.json()["results"]), 20)
        self.assertTrue(response.json()["has_more"])
        self.assertNotIn("Needle hidden foreign", response.content.decode())
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.get(url, {"q": "x" * 161}).json()["results"], [])

    def test_additional_contact_updates_calls_files_card_and_real_exports(self):
        call = self.call()
        upload = self.call(source_kind=PhoneCall.SOURCE_UPLOADED)
        for row in (call, upload):
            CallAnalysis.objects.create(call=row, status="ready", transcript="Historical transcript")
        customer = self.customer(name="Additional phone customer")
        self.assertEqual(self.card_ids(customer), set())
        ClientContact.objects.create(client=customer, kind="phone", value="89001234567", match_value="9001234567")
        self.assertEqual(self.page_ids(self.calls_url, {"client": customer.pk}), {call.pk})
        self.assertEqual(self.page_ids(self.files_url, {"client": customer.pk}), {upload.pk})
        self.assertEqual(self.card_ids(customer), {call.pk, upload.pk})
        for name in ("communication_call_transcripts_export", "communication_audio_transcripts_export"):
            for export_format in ("txt", "csv", "docx"):
                response = self.client.get(reverse(name), {"client": customer.pk, "format": export_format})
                self.assertEqual(response.status_code, 200)
                if export_format == "docx":
                    document = Document(io.BytesIO(response.content))
                    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
                else:
                    text = response.content.decode("utf-8-sig")
                self.assertIn("Additional phone customer", text)
                self.assertIn("Historical transcript", text)
        call.refresh_from_db()
        upload.refresh_from_db()
        self.assertIsNone(call.client_id)
        self.assertIsNone(upload.client_id)

    def test_explicit_assignment_and_ambiguity_are_consistent_on_pages(self):
        first = self.customer(phone="89001234567", name="Shared one")
        second = self.customer(phone="+7 900 123 4567", name="Shared two")
        explicit = self.customer(name="Manual choice")
        unknown = self.call()
        assigned = self.call(client=explicit)
        page = self.client.get(self.calls_url)
        self.assertNotContains(page, "data-call-create-client")
        self.assertContains(page, "Shared one")
        self.assertContains(page, "Shared two")
        self.assertContains(page, "Manual choice")
        for customer in (first, second):
            self.assertEqual(self.page_ids(self.calls_url, {"client": customer.pk}), set())
            self.assertEqual(self.card_ids(customer), set())
        self.assertEqual(self.card_ids(explicit), {assigned.pk})
        self.assertEqual(self.page_ids(self.calls_url, {"client": explicit.pk}), {assigned.pk})
        unknown.refresh_from_db()
        assigned.refresh_from_db()
        self.assertIsNone(unknown.client_id)
        self.assertEqual(assigned.client_id, explicit.pk)

    def test_staff_history_never_exposes_other_staff_or_uploaded_audio(self):
        customer = self.customer(phone="89001234567")
        own = self.call(employee=self.worker)
        self.call(employee=self.owner)
        self.call(source_kind=PhoneCall.SOURCE_UPLOADED, employee=self.worker, client=customer)
        self.client.force_login(self.worker)
        self.assertEqual(self.page_ids(self.calls_url, {"client": customer.pk}), {own.pk})
        self.assertEqual(self.card_ids(customer), {own.pk})
        self.assertEqual(self.client.get(self.files_url).status_code, 403)
        self.assertEqual(self.client.get(reverse("communication_client_lookup", args=["uploaded"]), {"q": "Page"}).status_code, 403)

    def test_foreign_suffix_does_not_enter_selected_client_history(self):
        russian = self.customer(phone="+7 900 123 4567")
        international = self.customer(phone="+34 9001234567", name="International")
        first = self.call()
        second = self.call(phone="+34 9001234567")
        self.assertEqual(self.page_ids(self.calls_url, {"client": russian.pk}), {first.pk})
        self.assertEqual(self.page_ids(self.calls_url, {"client": international.pk}), {second.pk})
        self.assertEqual(self.card_ids(russian), {first.pk})
        self.assertEqual(self.card_ids(international), {second.pk})

    def test_long_international_call_phone_creates_both_client_types_and_keeps_history(self):
        cases = (
            ("private", "telephony", "+358 (40) 123-456-78-90"),
            ("legal", "telephony", "+358 (40) 123-456-78-91"),
            ("private", "uploaded", "+123 (4567) 8901-2345"),
            ("legal", "uploaded", "+123 (4567) 8901-2346"),
        )
        for index, (kind, source, raw) in enumerate(cases):
            with self.subTest(kind=kind, source=source):
                self.assertGreater(len(raw), Client._meta.get_field("phone").max_length)
                self.assertLessEqual(len(raw), PhoneCall._meta.get_field("phone_number").max_length)
                expected = "+" + "".join(ch for ch in raw if ch.isdigit())
                call = self.call(phone=raw, source_kind=source)
                url = reverse("communication_call_client_create", args=[call.pk])
                count = Client.objects.count()
                page = self.client.get(url, {"client_type": kind})
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.context["client_form"]["phone"].value(), expected)
                self.assertEqual(Client.objects.count(), count)
                data = {"client_type": kind, "name": f"International caller {index}", "phone": "89000000000"}
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302)
                customer = Client.objects.get(name=data["name"])
                self.assertEqual(customer.phone, expected)
                self.assertEqual(customer.client_type, kind)
                contact = ClientContact.objects.get(client=customer, kind="phone")
                self.assertEqual(contact.match_value, expected)
                list_url = self.calls_url if source == "telephony" else self.files_url
                self.assertEqual(self.page_ids(list_url, {"client": customer.pk}), {call.pk})
                self.assertEqual(self.card_ids(customer), {call.pk})
                repeat = self.client.post(url, data)
                self.assertEqual(repeat.status_code, 200)
                self.assertEqual(Client.objects.count(), count + 1)
                call.refresh_from_db()
                self.assertEqual(call.phone_number, raw)
                self.assertIsNone(call.client_id)
