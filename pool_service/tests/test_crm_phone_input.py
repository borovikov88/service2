from html.parser import HTMLParser

from django import forms
from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.client_crm_models import ClientContact
from pool_service.forms import ClientCreateForm, ClientInviteForm
from pool_service.models import Client, Organization, OrganizationAccess


class InputParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.inputs.append(dict(attrs))


def crm_input(html):
    return next(
        item for item in InputParser(html).inputs
        if item.get("data-crm-phone") == "true"
    )


class CRMPhoneWidgetTests(SimpleTestCase):
    def render_phone(self, form):
        return Template(
            "{% load crm_phone_fields %}{% crm_phone_input form.phone %}"
        ).render(Context({"form": form}))

    def test_render_preserves_foreign_value_and_excludes_account_mask(self):
        form = ClientCreateForm(initial={"phone": "+358 40 123 4567"})
        attrs_before = dict(form.fields["phone"].widget.attrs)
        field = crm_input(self.render_phone(form))
        self.assertEqual(field["value"], "+358 40 123 4567")
        self.assertNotIn("phone-mask", field["class"].split())
        self.assertEqual(field["inputmode"], "tel")
        self.assertEqual(field["autocomplete"], "tel")
        self.assertIn("required", field)
        self.assertEqual(form.fields["phone"].widget.attrs, attrs_before)

    def test_bound_value_and_prefix_are_preserved_and_escaped(self):
        class PhoneForm(forms.Form):
            phone = forms.CharField(widget=forms.TextInput(
                attrs={"class": "form-control phone-mask extra-class"}
            ))

        value = '\"><img src=x onerror=alert(1)>'
        form = PhoneForm({"crm-phone": value}, prefix="crm")
        html = self.render_phone(form)
        field = crm_input(html)
        self.assertEqual(field["name"], "crm-phone")
        self.assertEqual(field["id"], "id_crm-phone")
        self.assertEqual(field["value"], value)
        self.assertIn("extra-class", field["class"].split())
        self.assertNotIn("<img", html)

    def test_account_invite_widget_keeps_its_russian_mask(self):
        self.assertIn(
            "phone-mask", ClientInviteForm().fields["phone"].widget.attrs["class"].split()
        )

    def test_client_template_has_no_local_phone_rewriter(self):
        source = get_template("pool_service/client_create.html").template.source
        self.assertNotIn("formatPhone", source)
        self.assertNotIn(".phone-mask", source)
        self.assertIn("{% crm_phone_input form.phone %}", source)


class CRMPhoneBrowserFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="CRM phone form tests")
        cls.owner = get_user_model().objects.create_user(username="phone-form-owner")
        OrganizationAccess.objects.create(
            user=cls.owner, organization=cls.organization, role="owner"
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def payload(self, phone):
        return {
            "client_type": "private", "first_name": "Example", "last_name": "Person",
            "phone": phone, "email": "example@example.test", "company_name": "",
            "inn": "", "contact_position": "",
        }

    def test_create_form_phone_is_not_selected_by_global_mask(self):
        response = self.client.get(reverse("client_create"))
        self.assertEqual(response.status_code, 200)
        field = crm_input(response.content.decode())
        self.assertEqual(field["name"], "phone")
        self.assertNotIn("phone-mask", field["class"].split())

    def test_foreign_phone_survives_edit_of_another_field(self):
        for phone in ("+358 40 123 4567", "358401234567", "+49 30 123456"):
            with self.subTest(phone=phone):
                customer = Client.objects.create(
                    organization=self.organization, client_type="private", name="Example Person",
                    first_name="Example", last_name="Person", phone=phone,
                )
                url = reverse("client_edit", args=[customer.pk])
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                field = crm_input(page.content.decode())
                self.assertEqual(field["value"], phone)
                self.assertNotIn("phone-mask", field["class"].split())
                data = self.payload(field["value"])
                data["first_name"] = "Updated"
                self.assertEqual(self.client.post(url, data).status_code, 302)
                customer.refresh_from_db()
                self.assertEqual(customer.phone, phone)
                self.assertEqual(customer.first_name, "Updated")
                contact = ClientContact.objects.get(client=customer, kind="phone")
                self.assertEqual(contact.value, phone)

    def test_invalid_form_redisplays_foreign_phone_unchanged(self):
        data = self.payload("+358 40 123 4567")
        data["last_name"] = ""
        response = self.client.post(reverse("client_create"), data)
        self.assertEqual(response.status_code, 200)
        field = crm_input(response.content.decode())
        self.assertEqual(field["value"], data["phone"])
        self.assertNotIn("phone-mask", field["class"].split())
        self.assertFalse(Client.objects.filter(organization=self.organization).exists())

    def test_unmasked_russian_input_is_formatted_by_server(self):
        for phone in ("89001234567", "79001234567", "9001234567"):
            with self.subTest(phone=phone):
                form = ClientCreateForm(self.payload(phone))
                self.assertTrue(form.is_valid(), form.errors)
                customer = form.save()
                self.assertEqual(customer.phone, "+7 900 123 4567")
                self.assertEqual(
                    ClientContact.objects.get(client=customer, kind="phone").match_value,
                    "9001234567",
                )
