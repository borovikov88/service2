"""Mailbox import, manual CRM actions and shared-mail permissions, without real mail."""

from contextlib import nullcontext
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DataError
from django.test import Client as Browser, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole, ClientCompanyLink, ClientContact
from pool_service.communication_secrets import decrypt_secret, encrypt_secret
from pool_service.crm_commercial_models import CrmDeal, CrmLead
from pool_service.mailbox_forms import ComposeForm
from pool_service.mailbox_models import CrmMailbox, MailFolder, MailLocation, MailReadReceipt, MailSignature, MailSubmission
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.services import mailbox_imap as imap
from pool_service.services.crm_commercial_commands import create_commercial_deal
from pool_service.services.mailbox_access import mailbox_role
from pool_service.services.mailbox_mime import CONTENT_VERSION, body_preview, header_data, parse_content, preview_from_prefix
from pool_service.services.mailbox_routing import bind_mail, contact_letters
from pool_service.services.mailbox_sending import deliver
from pool_service.services.mailbox_sync import store_letter, sync_mailbox
from pool_service.storage import PrivateMediaStorage
from pool_service.staff_call_models import StaffAccount
from pool_service.staff_structure_models import DepartmentAssignment, StaffDepartment
from pool_service.tests.test_crm_lead_commands import LeadFixture


def message(sender="client@example.org", *, msgid=None, refs="", subject="Synthetic inquiry"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "office@example.org"
    msg["Subject"] = subject
    if msgid:
        msg["Message-ID"] = msgid
    if refs:
        msg["In-Reply-To"] = refs
    msg.set_content("Message text")
    return msg


class MailboxTests(LeadFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.at = timezone.now()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = self.at + timedelta(days=40)
        self.org.save()
        self.box = CrmMailbox.objects.create(
            organization=self.org,
            address="office@example.org",
            credential=encrypt_secret("synthetic-test-secret"),
            connected_at=self.at - timedelta(minutes=1),
        )
        self.grant.view_team_deals = self.grant.edit_own_deals = True
        self.grant.save()
        self.http = Browser()
        self.http.force_login(self.user)

    def url(self, key, *args):
        return reverse("crm_mail" + key, args=args) + f"?org_id={self.org.pk}"

    def letter(self, sender="client@example.org", *, msgid=None, refs="", historical=False):
        m = message(sender, msgid=msgid or f"<{uuid4().hex}@example.org>", refs=refs)
        return store_letter(
            self.box, header_data(m.as_bytes(), self.at), "unused", commercial=True, historical=historical
        )

    def test_every_message_has_a_link_and_import_creates_zero_cards(self):
        a = self.letter()
        b = self.letter(historical=True)
        self.assertTrue(a.needs_attention)
        self.assertFalse(b.needs_attention)
        listing = self.http.get(self.url(""))
        self.assertContains(listing, "Добавить в лид / сделку", count=2)
        self.assertNotContains(listing, "Настройки ящика")
        a.body_loaded = True
        a.save()
        self.assertContains(self.http.get(self.url("_detail", a.pk)), "Добавить в лид / сделку")
        self.assertFalse(CrmLead.objects.exists())
        self.assertFalse(CrmDeal.objects.exists())
        form = self.http.get(self.url("_link", a.pk))
        self.assertContains(form, "Создать лид из письма")
        data = {
            "kind": "new_lead",
            "revision": 0,
            "title": "Request from letter",
            "need": "Proposal",
            "next_title": "Call back",
            "due": self.due.strftime("%Y-%m-%dT%H:%M"),
        }
        self.assertEqual(self.http.post(self.url("_link", a.pk), data).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.lead.title, data["title"])
        self.assertFalse(a.needs_attention)
        self.http.post(self.url("_link", a.pk), data)
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertFalse(CrmDeal.objects.exists())

    def test_single_open_card_additional_address_and_shared_address_routing(self):
        addr = "office2@example.org"
        ClientContact.objects.create(client=self.client, kind="email", value=addr.upper())
        lead = self.create(client_id=self.client.pk)
        first = self.letter(addr)
        self.assertEqual(first.lead_id, lead.pk)
        self.assertIn(first, contact_letters(self.org, self.client))
        second_party = Client.objects.create(
            organization=self.org, client_type="legal", name="Synthetic firm", email=addr
        )
        self.assertFalse(contact_letters(self.org, second_party).exists())
        self.assertIsNone(self.letter(addr).lead_id)
        ClientCompanyLink.objects.create(person=self.client, company=second_party)
        result = create_commercial_deal(
            actor=self.user,
            organization=self.org,
            operation_key="paired-mail-deal",
            client_id=second_party.pk,
            title="Another open project",
            pipeline_id=self.pipeline.pk,
            next_action=self.action(),
        )
        other = self.letter(addr)
        self.assertEqual((other.lead_id, other.deal_id), (None, None))
        self.assertIn(other, contact_letters(self.org, second_party))
        self.assertIn(other, contact_letters(self.org, self.client))
        bind_mail(self.user, self.org, other.pk, 0, "deal", target=result["deal_id"])
        events = self.http.get(
            reverse("crm_deal_activity", args=[result["deal_id"]]) + f"?org_id={self.org.pk}&type=email"
        )
        self.assertEqual(events.status_code, 200)
        self.assertTrue(any(row["kind"] == "email" for row in events.json()["items"]))

    def test_closed_card_thread_keeps_history_without_reopening(self):
        self.client.email = "client@example.org"
        self.client.save()
        lead = self.create(client_id=self.client.pk)
        original = self.letter()
        self.assertEqual(original.lead_id, lead.pk)
        lead.status, lead.result, lead.reason = "closed", "no_deal", "Completed"
        lead.save()
        new_topic = self.letter()
        reply = self.letter(refs=original.message_id)
        self.assertIsNone(new_topic.lead_id)
        self.assertEqual(reply.lead_id, lead.pk)
        self.assertIsNone(self.letter("unrelated@example.org", refs=original.message_id).lead_id)
        bind_mail(self.user, self.org, new_topic.pk, 0, "lead", target=lead.pk)
        lead.refresh_from_db()
        self.assertEqual(lead.status, "closed")
        self.assertEqual(CrmLead.objects.count(), 1)

    def test_private_contact_does_not_populate_customer_history_or_auto_link(self):
        self.client.email = "private@example.org"
        self.client.save()
        self.create(client_id=self.client.pk)
        ClientBusinessRole.objects.create(client=self.client, role="personal", active=True)
        letter = self.letter(self.client.email)
        self.assertFalse(letter.needs_attention)
        self.assertIsNone(letter.lead_id)
        self.assertFalse(contact_letters(self.org, self.client).exists())
        self.assertContains(self.http.get(self.url("")), letter.subject)

    def test_role_checks_membership_live_and_settings_owner_only(self):
        self.assertEqual(self.http.get(self.url("_setup")).status_code, 403)
        member = OrganizationAccess.objects.get(user=self.user, organization=self.org)
        member.role = "service"
        member.save()
        self.assertEqual(self.http.get(self.url("")).status_code, 403)
        department = StaffDepartment.objects.create(
            organization=self.org, name="Synthetic sales", created_by=self.user
        )
        appointment = DepartmentAssignment.objects.create(
            employee=self.employee,
            department=department,
            user_at_assignment=self.user,
            assigned_by=self.user,
            position="sales_head",
            valid_from=self.at - timedelta(days=1),
        )
        self.assertEqual(mailbox_role(self.user, self.org), "sales_head")
        self.assertEqual(self.http.get(self.url("")).status_code, 200)
        appointment.valid_to = self.at
        appointment.save()
        self.assertEqual(self.http.get(self.url("")).status_code, 403)
        member.role = "owner"
        member.save()
        self.assertContains(self.http.get(self.url("_setup"), follow=True), "Настроить office@example.org")
        with patch.object(imap, "connect", return_value=nullcontext(None)), patch.object(
            imap, "folders", return_value=[]
        ), patch("pool_service.mailbox_views.smtp_session", return_value=nullcontext(None)):
            response = self.http.post(
                self.url("_setup"), {"address": self.box.address, "password": "rotated-synthetic"}
            )
        self.assertEqual(response.status_code, 302)
        self.box.refresh_from_db()
        self.assertNotEqual(self.box.credential, "rotated-synthetic")
        self.assertEqual(decrypt_secret(self.box.credential), "rotated-synthetic")
        StaffAccount.objects.create(organization=self.org, user=self.user, kind="system")
        self.assertEqual(self.http.get(self.url("_setup")).status_code, 403)

    def make_owner(self):
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")

    def test_gear_has_owner_only_mail_section_and_scopes_rows(self):
        from pool_service.communication_models import CommunicationAccess

        self.make_owner()
        other = Organization.objects.create(name="Other tenant")
        box = CrmMailbox.objects.create(organization=other, address="outside@example.org")
        gear = reverse("communications_channels") + f"?org_id={self.org.pk}"
        page = self.http.get(gear)
        self.assertContains(page, "Почтовые ящики")
        self.assertContains(page, self.box.address)
        self.assertNotContains(page, box.address)
        self.assertNotContains(page, "Мегафон")
        self.assertNotContains(page, self.box.credential)
        self.assertContains(self.http.get(self.url("")), gear)
        self.assertEqual(self.http.get(self.url("_account", box.pk)).status_code, 404)
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="manager")
        CommunicationAccess.objects.update_or_create(organization=self.org, user=self.user,
                                                      defaults={"can_manage_channels": True})
        self.assertNotContains(self.http.get(gear), self.box.address)
        self.assertContains(self.http.get(gear), "Мегафон")
        self.assertEqual(self.http.get(self.url("_add")).status_code, 403)

    def test_add_two_edit_noop_and_pause_independently(self):
        self.make_owner()
        cipher = self.box.credential
        data = {"provider": "yandex", "address": "second@example.org", "login": "separate-user",
                "password": "synthetic-only-password", "version": 0}
        with patch.object(imap, "connect", return_value=nullcontext(None)), patch.object(imap, "folders"), patch(
            "pool_service.mailbox_views.smtp_session", return_value=nullcontext(None)
        ) as smtp:
            self.assertEqual(self.http.post(self.url("_add"), data).status_code, 302)
            self.assertEqual(smtp.call_args.args[0].smtp_host, "smtp.yandex.ru")
            smtp.reset_mock()
            self.assertContains(self.http.post(self.url("_add"), data), "Не удалось добавить")
            smtp.assert_not_called()
        second = CrmMailbox.objects.get(address=data["address"])
        self.assertEqual((second.login, second.imap_host, second.smtp_port), ("separate-user", "imap.yandex.ru", 465))
        self.assertNotEqual(second.credential, data["password"])
        self.assertNotContains(self.http.get(self.url("_account", second.pk)), data["password"])
        with patch.object(imap, "connect") as dial:
            self.assertEqual(self.http.post(self.url("_account", self.box.pk), {"provider": "mailru", "version": 0}).status_code, 302)
            self.assertContains(self.http.post(self.url("_account", self.box.pk), {"provider": "gmail", "version": 0}), "Для смены сервера")
            dial.assert_not_called()
        self.assertEqual(self.http.post(self.url("_account", second.pk), {"action": "pause", "version": 0}).status_code, 302)
        self.box.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(self.box.enabled)
        self.assertEqual(self.box.credential, cipher)
        self.assertEqual((second.enabled, second.credential), (False, ""))

    def test_custom_endpoint_reset_preserves_letters_and_does_not_bind_old_uids(self):
        import smtplib
        self.make_owner()
        letter = self.letter()
        folder = self.box.folders.create(key="s", name="INBOX", wire_name="INBOX", generation=9, latest_uid=5)
        location = MailLocation.objects.create(folder=folder, letter=letter, generation=9, uid=5)
        data = {"provider": "custom", "imap_host": "imap.example.org", "imap_port": 143,
                "smtp_host": "smtp.example.org", "smtp_port": 587, "login": "other",
                "password": "test-only-secret", "version": 0}
        with patch.object(imap, "connect", return_value=nullcontext(None)), patch.object(imap, "folders"), patch(
            "pool_service.mailbox_views.smtp_session", side_effect=smtplib.SMTPException("provider detail")
        ):
            page = self.http.post(self.url("_account", self.box.pk), data)
            self.assertContains(page, "по SMTP")
            self.assertNotContains(page, data["password"])
            self.assertNotContains(page, "provider detail")
            self.assertTrue(MailLocation.objects.filter(pk=location.pk).exists())
        with patch.object(imap, "connect", return_value=nullcontext(None)), patch.object(imap, "folders"), patch(
            "pool_service.mailbox_views.smtp_session", return_value=nullcontext(None)
        ):
            self.assertEqual(self.http.post(self.url("_account", self.box.pk), data).status_code, 302)
        self.assertTrue(self.box.letters.filter(pk=letter.pk).exists())
        self.assertFalse(letter.locations.exists())
        folder.refresh_from_db()
        self.assertEqual((folder.available, folder.generation, folder.latest_uid), (False, 0, 0))
        self.http.post(self.url("_account", self.box.pk), {"action": "pause", "version": 0})
        self.box.refresh_from_db()
        self.assertTrue(self.box.enabled)
        self.assertEqual(self.box.config_version, 1)

    def test_archive_sender_and_replies_with_two_mailboxes(self):
        from django.core import signing

        self.client.email = "external@example.org"
        self.client.save()
        a = self.letter(self.client.email)
        b = CrmMailbox.objects.create(organization=self.org, address="sales@example.org")
        raw = message(self.client.email, msgid="<other@example.org>", subject="Other inbox").as_bytes()
        second = store_letter(b, header_data(raw, self.at), "", commercial=True, historical=False)
        page = self.http.get(self.url(""))
        self.assertContains(page, a.subject)
        self.assertContains(page, second.subject)
        page = self.http.get(self.url("") + f"&box={b.pk}")
        self.assertContains(page, second.subject)
        self.assertNotContains(page, a.subject)
        self.assertEqual(set(contact_letters(self.org, self.client)), {a, second})
        self.assertContains(self.http.get(self.url("_compose")), "С какого ящика написать")
        compose = self.http.get(self.url("_compose") + f"&box={b.pk}")
        token = compose.context["form"].initial["token"]
        self.assertEqual(signing.loads(token, salt="mail-send")[1:3], [b.pk, None])
        reply = self.http.get(self.url("_reply", second.pk))
        self.assertEqual(reply.context["box"], b)
        self.assertEqual(self.http.get(self.url("_reply", second.pk) + f"&box={self.box.pk}").status_code, 404)
        form = {"to": self.client.email, "subject": "Subject", "body": "Content", "box": self.box.pk, "token": token}
        with patch("pool_service.mailbox_views.deliver") as send:
            self.assertContains(self.http.post(self.url("_compose"), form), "Откройте новое письмо")
            send.assert_not_called()
        foreign = Organization.objects.create(name="Separate organization")
        other = CrmMailbox.objects.create(organization=foreign, address="another@example.org")
        self.assertEqual(self.http.get(self.url("") + f"&box={other.pk}").status_code, 404)
        self.assertEqual(self.http.get(self.url("_compose") + f"&box={other.pk}").status_code, 404)

    def test_scheduler_resumes_with_least_recent_attempt_after_timeout(self):
        from django.core.management import call_command

        second = CrmMailbox.objects.create(organization=self.org, address="sales@example.org")
        with patch.object(imap, "mailbox_session", side_effect=OSError):
            self.assertFalse(sync_mailbox(self.box.pk))
        path = "pool_service.management.commands.sync_crm_mail."
        # A slow run of the oldest mailbox uses the remaining cron budget.
        with patch(path + "sync_mailbox") as sync, patch(path + "time.monotonic", side_effect=[0, 1, 34]):
            call_command("sync_crm_mail", organization=self.org.pk)
        sync.assert_called_once_with(second.pk, seconds=34)

    def test_foreign_mail_and_other_assignee_are_inaccessible(self):
        foreign = Organization.objects.create(name="Other synthetic org")
        box = CrmMailbox.objects.create(organization=foreign, address="other@example.org")
        record = box.letters.create(
            fingerprint="a" * 64, subject="Foreign confidential", sent_at=self.at, body_loaded=True
        )
        for key, args in (("_detail", [record.pk]), ("_attachment", [record.pk, 2]), ("_link", [record.pk])):
            self.assertEqual(self.http.get(self.url(key, *args)).status_code, 404)
        stranger = get_user_model().objects.create_user("other-mail-assignee")
        OrganizationAccess.objects.create(user=stranger, organization=self.org, role="manager")
        lead = self.create()
        lead.assignee = stranger
        lead.save()
        mine = self.letter()
        with self.assertRaises(PermissionDenied):
            bind_mail(self.user, self.org, mine.pk, 0, "lead", target=lead.pk)
        self.assertNotContains(self.http.get(self.url("_link", mine.pk)), lead.title)

    def test_select_and_fetch_readonly_trailing_imap_metadata(self):
        client = MagicMock()
        client.select.return_value = ("OK", [b"1"])
        client.response.side_effect = [("UIDVALIDITY", [b"7"]), ("UIDNEXT", [b"10"])]
        self.assertEqual(imap.select(client, "INBOX"), (7, 10))
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        raw = message().as_bytes()
        client.uid.return_value = (
            "OK",
            [
                (b"1 (BODY[HEADER.FIELDS (FROM)] {100}", raw),
                b' UID 9 RFC822.SIZE 600 INTERNALDATE "04-Feb-2025 12:00:00 +0700")',
            ],
        )
        uid, size, at, parsed = next(iter(imap.headers(client, 9, 9)))
        self.assertEqual((uid, size, at.year, parsed), (9, 600, 2025, raw))
        self.assertIn("BODY.PEEK", client.uid.call_args.args[2])

    def test_first_sync_retries_failed_batch_and_preserves_unicode_subjects(self):
        subjects = ["Вопрос по заказу", "Заявка на обслуживание 🏊"]
        rows = []
        for uid, subject in enumerate(subjects, start=1):
            raw = message(msgid=f"<mail-{uid}@example.org>", subject=subject).as_bytes()
            rows.append((uid, len(raw), self.at, raw))

        def fail_on_second_letter(box, data, fallback, **kwargs):
            if data["subject"] == subjects[1]:
                raise DataError("synthetic storage error")
            return store_letter(box, data, fallback, **kwargs)

        with (
            patch.object(imap, "mailbox_session", return_value=nullcontext(MagicMock())),
            patch.object(imap, "folders", return_value=[("INBOX", "Inbox", "inbox")]),
            patch.object(imap, "select", return_value=(1, 3)),
            patch.object(imap, "headers", return_value=rows),
        ):
            with patch("pool_service.services.mailbox_sync.store_letter", side_effect=fail_on_second_letter):
                self.assertFalse(sync_mailbox(self.box.pk))
            self.box.refresh_from_db()
            self.assertIsNone(self.box.last_sync_at)
            self.assertIn("Не удалось сохранить", self.box.sync_error)
            self.assertNotIn("synthetic", self.box.sync_error)
            self.assertFalse(self.box.letters.exists())
            folder = self.box.folders.get()
            self.assertEqual((folder.generation, folder.latest_uid, folder.backfill_uid), (0, 0, 0))
            self.assertIsNone(folder.scanned_at)

            error = self.box.sync_error
            self.assertFalse(sync_mailbox(self.box.pk, seconds=0))
            self.box.refresh_from_db()
            self.assertIsNone(self.box.last_sync_at)
            self.assertEqual(self.box.sync_error, error)
            self.assertTrue(sync_mailbox(self.box.pk))

        self.box.refresh_from_db()
        self.assertIsNotNone(self.box.last_sync_at)
        self.assertEqual(self.box.sync_error, "")
        self.assertCountEqual(self.box.letters.values_list("subject", flat=True), subjects)

    def test_duplicate_move_expunge_and_uid_rollover_are_idempotent(self):
        raw = message().as_bytes()
        client = MagicMock()
        client.uid.return_value = ("OK", [None])
        arrived = self.at

        def tick(folders, generation, next_uid, rows):
            with patch.object(imap, "mailbox_session", return_value=nullcontext(client)), patch.object(
                imap, "folders", return_value=folders
            ), patch.object(imap, "select", return_value=(generation, next_uid)), patch.object(
                imap, "headers", return_value=rows
            ), patch.object(
                imap, "selected_message", return_value=raw
            ):
                self.assertTrue(sync_mailbox(self.box.pk))

        inbox = [("INBOX", "Inbox", "inbox")]
        tick(inbox, 2, 2, [(1, len(raw), arrived, raw)])
        letter = self.box.letters.get()
        self.assertTrue(letter.needs_attention)
        self.http.post(self.url("_processed", letter.pk))
        tick(inbox, 2, 2, [])
        letter.refresh_from_db()
        self.assertFalse(letter.commercial)
        tick([("Filed", "Filed", "")], 33, 7, [(6, len(raw), arrived, raw)])
        tick([("Filed", "Filed", "")], 34, 10, [(9, len(raw), arrived, raw)])
        letter.refresh_from_db()
        self.assertTrue(letter.commercial)
        self.assertIsNotNone(letter.processed_at)
        self.assertFalse(letter.needs_attention)
        self.assertEqual(self.box.letters.count(), 1)
        self.assertEqual(MailLocation.objects.count(), 1)
        self.assertFalse(CrmLead.objects.exists())

    def test_copies_match_contacts_and_confirmed_external_reply_closes_queue(self):
        self.client.email = "copied@example.org"
        self.client.save()
        m = message(msgid="<many@example.org>")
        m["Cc"] = self.client.email
        record = store_letter(
            self.box, header_data(m.as_bytes(), self.at), "", commercial=True, historical=False
        )
        self.assertIn(record, contact_letters(self.org, self.client))
        self.assertIsNone(record.lead_id)
        first = self.letter()
        outgoing = message(self.box.address, msgid="<replied@example.org>", refs=first.message_id)
        outgoing.replace_header("To", "client@example.org")
        outgoing_data = header_data(outgoing.as_bytes(), self.at)
        store_letter(self.box, outgoing_data, "", commercial=True, historical=False, sent_folder=False)
        first.refresh_from_db()
        self.assertTrue(first.needs_attention)
        store_letter(self.box, outgoing_data, "", commercial=True, historical=False, sent_folder=True)
        first.refresh_from_db()
        self.assertFalse(first.needs_attention)
        self.assertIsNotNone(first.processed_at)
        form = ComposeForm(
            {"to": "invalid", "subject": "Subject", "body": "Body", "token": "synthetic-token"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("to", form.errors)

    def test_attachment_and_text_are_rendered_as_data(self):
        record = self.letter()
        m = message()
        m.set_content(
            '<p>Price</p><script>secretScript()</script><img src="https://images.example.org/track">',
            subtype="html",
        )
        m.add_attachment(b"synthetic-data", maintype="text", subtype="html", filename="../offer.html")
        with patch.object(imap, "raw_message", return_value=m.as_bytes()):
            detail = self.http.get(self.url("_detail", record.pk))
            self.assertContains(detail, "Price")
            self.assertNotContains(detail, "secretScript")
            self.assertNotContains(detail, "images.example.org")
            record.refresh_from_db()
            response = self.http.get(self.url("_attachment", record.pk, record.attachments[0]["index"]))
            self.assertEqual(response.content, b"synthetic-data")
            self.assertIn("attachment", response["Content-Disposition"])
            self.assertEqual(response["Content-Security-Policy"], "sandbox")

    def test_cached_blank_newsletter_is_repaired_and_real_file_downloads(self):
        letter = self.letter()
        letter.body_loaded = True
        letter.save(update_fields=["body_loaded"])
        folder = MailFolder.objects.create(mailbox=self.box, key="inbox", name="Inbox",
                                           wire_name="INBOX", generation=5)
        MailLocation.objects.create(letter=letter, folder=folder, generation=5, uid=10)
        msg = message()
        msg.set_content("   ")
        msg.add_alternative(
            '<p>Добрый день!</p><a href="https://example.org/tenders">Новые тендеры</a>'
            '<a href="data:text/plain,hidden">Безопасный текст</a>'
            '<img src="https://images.example.org/track" alt="">', subtype="html",
        )
        msg.add_attachment(b"PDF sample", maintype="application", subtype="pdf", filename="Выписка.pdf")
        with patch.object(imap, "raw_message", return_value=msg.as_bytes()) as fetch:
            detail = self.http.get(self.url("_detail", letter.pk))
            self.assertContains(detail, 'href="https://example.org/tenders"')
            self.assertNotContains(detail, "data:text/plain,hidden")
            self.assertNotContains(detail, "images.example.org")
            self.assertContains(detail, "Выписка.pdf")
            self.http.get(self.url("_detail", letter.pk))
            fetch.assert_called_once()
            letter.refresh_from_db()
            self.assertTrue(letter.preview.startswith("Новые тендеры"))
            self.assertEqual(letter.content_version, CONTENT_VERSION)
            file = self.http.get(self.url("_attachment", letter.pk, letter.attachments[0]["index"]))
            self.assertEqual(file.content, b"PDF sample")
        self.assertEqual(MailReadReceipt.objects.filter(letter=letter).count(), 1)
        self.assertTrue(letter.needs_attention)

    def test_mark_read_from_list_is_personal_idempotent_and_does_not_process(self):
        letter = self.letter()
        url = self.url("_read", letter.pk)
        self.assertEqual(self.http.get(url).status_code, 405)
        with patch.object(imap, "mailbox_session") as session:
            result = self.http.post(url + "&folder=review&page=2", {"user": 0})
            self.assertRedirects(result, self.url("") + "&folder=review&page=2", fetch_redirect_response=False)
            self.assertEqual(self.http.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json(), {"read": True})
            session.assert_not_called()
        self.assertEqual(MailReadReceipt.objects.filter(letter=letter, user=self.user).count(), 1)
        letter.refresh_from_db()
        self.assertFalse(letter.body_loaded)
        self.assertTrue(letter.needs_attention)
        self.assertIsNone(letter.processed_at)
        self.assertFalse(self.http.get(self.url("")).context["page"][0].unread)
        other = get_user_model().objects.create_user("another-reader")
        OrganizationAccess.objects.create(user=other, organization=self.org, role="manager")
        self.http.force_login(other)
        self.assertTrue(self.http.get(self.url("")).context["page"][0].unread)
        guarded = Browser(enforce_csrf_checks=True)
        guarded.force_login(other)
        self.assertEqual(guarded.post(url).status_code, 403)
        foreign = Organization.objects.create(name="Other inbox")
        box = CrmMailbox.objects.create(organization=foreign, address="foreign@example.org")
        foreign_letter = box.letters.create(fingerprint="b" * 64, sent_at=self.at)
        self.assertEqual(self.http.post(self.url("_read", foreign_letter.pk)).status_code, 404)
        OrganizationAccess.objects.filter(organization=self.org, user=other).update(role="service")
        self.assertEqual(self.http.post(url).status_code, 403)
        self.assertFalse(MailReadReceipt.objects.filter(user=other).exists())

    def test_mail_row_avatar_timestamps_and_preexisting_attachment_metadata(self):
        today = timezone.localtime(self.at).replace(hour=12, minute=20)
        yesterday = today - timedelta(days=1)
        branded = self.letter("Alerts <sender@notice.avito.ru>")
        self.box.letters.filter(pk=branded.pk).update(sent_at=today)
        other = self.letter("Анна Орлова <manager@other-avito.ru>")
        self.box.letters.filter(pk=other.pk).update(
            sent_at=yesterday, body_loaded=True, attachments=[{"index": 2, "name": "example.pdf"}])
        outgoing = self.letter()
        self.box.letters.filter(pk=outgoing.pk).update(outgoing=True, sent_at=today)
        result = self.http.get(self.url(""))
        rows = {r.pk: r for r in result.context["page"]}
        self.assertTrue(rows[branded.pk].avito_avatar)
        self.assertFalse(rows[other.pk].avito_avatar)
        self.assertEqual(rows[other.pk].initials, "АО")
        self.assertTrue(rows[other.pk].shows_files)
        self.assertIsNone(rows[branded.pk].shows_files)
        self.assertFalse(rows[branded.pk].show_date)
        self.assertTrue(rows[outgoing.pk].show_date)
        self.assertContains(result, f">{today:%H:%M}</time>")
        self.assertContains(result, f">{today:%d.%m %H:%M}</time>")
        self.assertContains(result, f">{yesterday:%d.%m %H:%M}</time>")
        self.assertContains(result, 'title="Есть вложения"><svg', count=1)

    def test_attachment_flags_for_mime_parts_without_reading_files(self):
        plain = b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 50 2 NIL NIL)'
        html = b'("TEXT" "HTML" NIL NIL NIL "7BIT" 64 2 NIL NIL)'
        alternative = b'(' + plain + html + b' "ALTERNATIVE" NIL NIL)'
        pdf = b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 52428800 NIL NIL)'
        text_file = b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 80 2 NIL ("ATTACHMENT" NIL))'
        connection = MagicMock()
        connection.uid.return_value = ("OK", [
            b"1 (UID 7 BODYSTRUCTURE " + alternative + b")",
            b'2 (BODYSTRUCTURE (' + alternative + pdf + b' "MIXED") UID 8)',
            b"3 (UID 9 BODYSTRUCTURE " + text_file + b")",
            (b'4 (BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME*0*" {13}', b'file (01).txt'),
            b') NIL NIL "7BIT" 40 1 NIL NIL) UID 10)',
            b'11 (BODYSTRUCTURE ' + pdf + b')',  # unsolicited unkeyed sequence
            b'5 (UID 99 BODYSTRUCTURE ' + pdf + b')',
        ])
        self.assertEqual(imap.selected_attachment_flags(connection, {7, 8, 9, 10, 11}),
                         {7: False, 8: True, 9: True, 10: True})
        connection.uid.assert_called_once_with("FETCH", "7,8,9,10,11", "(UID BODYSTRUCTURE)")

    def test_preview_fetch_reads_only_requested_folder_generation_without_marking_read(self):
        letter = self.letter()
        folder = MailFolder.objects.create(mailbox=self.box, key="inbox", name="Inbox", wire_name="INBOX", generation=3)
        MailLocation.objects.create(folder=folder, generation=3, letter=letter, uid=4, size=500)
        second = self.letter()
        url = self.url("_previews", folder.pk)
        msg = message()
        msg.set_content("Здравствуйте, коллеги!\nУточните сроки поставки.")
        connection = MagicMock()
        def fetch(_command, _uids, fields):
            if fields == "(UID BODYSTRUCTURE)":
                return "OK", [b'1 (BODYSTRUCTURE ("APPLICATION" "PDF" NIL NIL NIL "BASE64" 9000000) UID 4)']
            return "OK", [(b"1 (BODY[]<0> {500}", msg.as_bytes()), b" UID 4)"]
        connection.uid.side_effect = fetch
        with (patch.object(imap, "mailbox_session", return_value=nullcontext(connection)),
              patch.object(imap, "select", return_value=(4, 6)) as select):
            self.assertEqual(self.http.post(url, {"letter": [letter.pk]}).json()["previews"], {})
            connection.uid.assert_not_called()
            select.return_value = (3, 5)
            data = {"letter": [letter.pk, second.pk]}
            expected = {"previews": {str(letter.pk): "Уточните сроки поставки."},
                        "attachments": {str(letter.pk): True}}
            self.assertEqual(self.http.post(url, data).json(), expected)
            self.assertEqual(self.http.post(url, data).json(), expected)
            self.assertEqual([call.args for call in connection.uid.call_args_list], [
                ("FETCH", "4", "(UID BODY.PEEK[]<0.65536>)"), ("FETCH", "4", "(UID BODYSTRUCTURE)")])
        self.assertFalse(MailReadReceipt.objects.exists())
        letter.refresh_from_db()
        self.assertFalse(letter.body_loaded)
        self.assertTrue(letter.needs_attention)
        self.assertContains(self.http.get(self.url("")), "Уточните сроки поставки.")
        foreign_org = Organization.objects.create(name="Another mail organization")
        foreign_box = CrmMailbox.objects.create(organization=foreign_org, address="separate@example.org")
        foreign_folder = MailFolder.objects.create(mailbox=foreign_box, name="inbox", key="i", wire_name="i")
        self.assertEqual(self.http.post(self.url("_previews", foreign_folder.pk), data).status_code, 404)
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role="service")
        self.assertEqual(self.http.post(url, data).status_code, 403)

    def test_mail_reader_and_signature_belong_to_each_staff_member(self):
        letter = self.letter()
        letter.body_loaded = True
        letter.body = "Доброе утро! Остатки на складе."
        letter.save()
        for age in (1, 5, 9, 41):
            past = self.letter()
            past.sent_at = self.at - timedelta(days=age)
            past.save(update_fields=["sent_at"])
        inbox = self.http.get(self.url(""))
        self.assertEqual([row.date_group for row in inbox.context["page"]], ["Сегодня", "Вчера", "Неделя", "Месяц", "Ранее"])
        self.assertContains(inbox, "Остатки на складе.")
        self.assertTrue(inbox.context["page"][0].unread)
        self.http.get(self.url("_detail", letter.pk))
        self.assertFalse(self.http.get(self.url("")).context["page"][0].unread)
        ajax = {"HTTP_X_MAIL_DIALOG": "1"}
        address = self.url("_signature")
        self.assertEqual(self.http.post(address, {"text": "Менеджер\r\nОтдел продаж"}, **ajax).json(),
                         {"signature": "Менеджер\nОтдел продаж"})
        compose = self.http.get(self.url("_compose"), **ajax)
        self.assertEqual(compose.headers["X-Mail-Dialog"], "1")
        self.assertNotContains(compose, "<html")
        self.assertEqual(compose.context["form"].initial["body"], "\n\nМенеджер\nОтдел продаж")
        reply = self.http.get(self.url("_reply", letter.pk), **ajax)
        self.assertEqual(reply.context["form"].initial["body"], compose.context["form"].initial["body"])
        other = get_user_model().objects.create_user("other-mail-reader")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        self.http.force_login(other)
        self.assertTrue(self.http.get(self.url("")).context["page"][0].unread)
        self.assertNotContains(self.http.get(address), "Отдел продаж")
        self.http.post(address, {"text": "", "user": self.user.pk}, **ajax)
        self.assertEqual(self.http.get(self.url("_compose")).context["form"].initial["body"], "")
        self.assertEqual(MailSignature.objects.get(organization=self.org, user=self.user).text, "Менеджер\nОтдел продаж")

    def test_truncated_base64_previews_and_html_after_large_stylesheet(self):
        expected = "Проверка длинного письма"
        for length in range(1, 6):
            msg = EmailMessage()
            msg["Subject"] = "x" * length
            msg.set_content("Здравствуйте! " + (expected + " ") * 1800, cte="base64")
            self.assertTrue(preview_from_prefix(msg.as_bytes()[:65536], partial=True).startswith(expected))

        msg = EmailMessage()
        msg.set_content("<style>" + " " * 1_100_000 + "</style><p>Доступен новый документ</p>",
                        subtype="html", cte="8bit")
        raw = msg.as_bytes()
        connection = MagicMock()
        def fetch(command, uids, params):
            size = int(params.split("<0.")[1].split(">")[0])
            return "OK", [(b"1 (UID 7 BODY[]<0>", raw[:size]), b")"]
        connection.uid.side_effect = fetch
        self.assertEqual(imap.selected_previews(connection, {7: len(raw)}), {7: "Доступен новый документ"})
        self.assertEqual(parse_content(raw), ("Доступен новый документ", []))
        connection.uid.side_effect = lambda *args: ("OK", [(b"1 (UID 7 BODY[]<0>", raw[:65536]), b")"])
        self.assertEqual(imap.selected_previews(connection, {7: len(raw)}), {})

    def test_preview_preserves_first_meaningful_sentence(self):
        for raw, expected in (
            ("Здравствуйте, Елена!\r\nСчёт приложен.", "Счёт приложен."),
            ("Добрый день, подскажите цену.", "подскажите цену."),
            ("Доброе утро! Отправили расчёт.", "Отправили расчёт."),
            ("Елена, добрый день! Подтвердите дату.", "Подтвердите дату."),
            ("Елена, уточните цену.", "Елена, уточните цену."),
            ("Остатки на складе", "Остатки на складе"),
        ):
            self.assertEqual(body_preview(raw), expected)

    def test_outgoing_retries_and_attachment_without_remote_sent_copy(self):
        key = uuid4()
        smtp = MagicMock()
        smtp.__enter__.return_value.sendmail.return_value = {}
        params = {"to": ["client@пример.рф"], "cc": [], "subject": "Quote", "body": "Please review"}
        with TemporaryDirectory() as directory, patch.object(
            MailSubmission._meta.get_field("attachment_source"), "storage", new=PrivateMediaStorage(directory)
        ), patch(
            "pool_service.services.mailbox_sending.smtp_session", return_value=smtp
        ) as dial, patch.object(
            imap, "mailbox_session", side_effect=OSError
        ):
            sent = deliver(self.box, self.user, key, params, [SimpleUploadedFile("cost.txt", b"quote")])
            self.assertEqual(sent.status, "sent")
            same = deliver(self.box, self.user, key, params, [])
            self.assertEqual(same.pk, sent.pk)
            self.assertEqual(dial.call_count, 1)
            self.assertEqual(
                smtp.__enter__.return_value.sendmail.call_args.args[1], ["client@xn--e1afmkfd.xn--p1ai"]
            )
            idx = sent.letter.attachments[0]["index"]
            self.assertEqual(self.http.get(self.url("_attachment", sent.letter.pk, idx)).content, b"quote")
            self.assertTrue(list(Path(directory).rglob("*.eml")))
            self.assertFalse(CrmLead.objects.exists())
        smtp.__enter__.return_value.sendmail.side_effect = TimeoutError
        with patch("pool_service.services.mailbox_sending.smtp_session", return_value=smtp) as dial:
            second = uuid4()
            self.assertEqual(deliver(self.box, self.user, second, params, []).status, "uncertain")
            self.assertEqual(deliver(self.box, self.user, second, params, []).status, "uncertain")
            self.assertEqual(dial.call_count, 1)


class MailTransportTests(SimpleTestCase):
    def test_presets_and_custom_hosts_require_verified_tls(self):
        from pool_service.services import mailbox_transport as transport

        box = CrmMailbox(address="a@example.org", login="service-user", imap_host="imap.example.org",
                         smtp_host="smtp.example.org", imap_port=143, smtp_port=587)
        with patch.object(transport, "_Imap") as dial:
            with transport.imap_session(box, "synthetic"):
                pass
            client = dial.return_value
            self.assertEqual([args[0] for args in client.method_calls], ["starttls", "login", "logout"])
            self.assertTrue(client.starttls.call_args.kwargs["ssl_context"].check_hostname)
            self.assertEqual(client.login.call_args.args, ("service-user", "synthetic"))
            client.reset_mock()
            client.starttls.side_effect = OSError()
            with self.assertRaises(OSError), transport.imap_session(box, "synthetic"):
                pass
            client.login.assert_not_called()
        with patch.object(transport, "_Smtp") as dial:
            with transport.smtp_session(box, "synthetic"):
                pass
            client = dial.return_value.__enter__.return_value
            self.assertEqual([args[0] for args in client.method_calls], ["starttls", "login"])
            self.assertTrue(client.starttls.call_args.kwargs["context"].check_hostname)
            client.reset_mock()
            client.starttls.side_effect = OSError()
            with self.assertRaises(OSError), transport.smtp_session(box, "synthetic"):
                pass
            client.login.assert_not_called()
        for port in (25, 110):
            box.smtp_port = port
            with self.assertRaises(OSError), transport.smtp_session(box, "synthetic"):
                pass

    def test_public_dns_addresses_are_pinned_with_tls_hostname_and_fallback(self):
        import socket
        from pool_service.services import mailbox_transport as transport

        name = "mail.example.org"
        addresses = ["2606:4700:4700::1111", "1.1.1.1"]
        tls, raw = MagicMock(), MagicMock()
        with patch.object(transport, "_resolve", return_value=addresses) as dns, patch.object(
            transport, "_numeric_socket", side_effect=[OSError(), raw]
        ) as connect:
            self.assertIs(transport._socket(name, 465, 3, tls), tls.wrap_socket.return_value)
            self.assertEqual(dns.call_count, 1)
            self.assertEqual([call.args[0] for call in connect.call_args_list], addresses)
            tls.wrap_socket.assert_called_once_with(raw, server_hostname=name)
        replies = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 993)) for ip in ("1.1.1.1", "127.0.0.1")]
        with patch("socket.getaddrinfo", return_value=replies), patch.object(transport, "_numeric_socket") as connect:
            with self.assertRaises(OSError):
                transport._socket(name, 993, 3, tls)
            connect.assert_not_called()
