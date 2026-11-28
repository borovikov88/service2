"""Exercise the inbox row as a signed-in employee in desktop and mobile Chromium."""
import os
from contextlib import nullcontext
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_secrets import encrypt_secret
from pool_service.mailbox_models import CrmMailbox, MailFolder, MailLocation, MailReadReceipt
from pool_service.models import Organization, OrganizationAccess
from pool_service.services import mailbox_imap as imap
from pool_service.services.mailbox_mime import cache_content
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process


@skipUnless(os.environ.get("CRM_LEADS_BROWSER_TEST") == "1", "Dedicated Chromium acceptance")
class MailRowBrowserTests(StaticLiveServerTestCase):
    def test_attachment_indicator_and_read_dot_without_navigation(self):
        from django.contrib.auth import get_user_model

        org = Organization.objects.create(name="Inbox test", plan_type=Organization.PLAN_COMPANY_PAID,
                                          paid_until=timezone.now() + timedelta(days=20))
        user = get_user_model().objects.create_user("inbox-browser")
        OrganizationAccess.objects.create(organization=org, user=user, role="owner")
        self.client.force_login(user)
        box = CrmMailbox.objects.create(organization=org, address="office@example.org")
        folder = MailFolder.objects.create(mailbox=box, key="inbox", name="Inbox", wire_name="INBOX", generation=1)
        letter = box.letters.create(fingerprint="f" * 64, sender="alerts@notice.avito.ru",
                                    sender_name="Авито", subject="Счёт на оборудование",
                                    preview="Сумма и сроки", sent_at=timezone.now() - timedelta(days=2),
                                    commercial=True, needs_attention=True)
        MailLocation.objects.create(folder=folder, letter=letter, uid=7, generation=1, size=16000000)
        connection = MagicMock()
        connection.uid.return_value = ("OK", [b'1 (UID 7 BODYSTRUCTURE ("APPLICATION" "PDF" NIL NIL NIL "BASE64" 16000000))'])
        fixture = {
            "base": self.live_server_url, "path": reverse("crm_mail") + f"?org_id={org.pk}",
            "name": settings.SESSION_COOKIE_NAME, "session": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
        }
        with (patch.object(imap, "mailbox_session", return_value=nullcontext(connection)),
              patch.object(imap, "select", return_value=(1, 8))):
            code, output = _bounded_browser_process(
                ["node", str(Path(__file__).with_name("crm_mail_rows.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        connection.uid.assert_called_once_with("FETCH", "7", "(UID BODYSTRUCTURE)")
        letter.refresh_from_db()
        self.assertTrue(letter.has_attachments)
        self.assertFalse(letter.body_loaded)
        self.assertTrue(letter.needs_attention)
        self.assertTrue(MailReadReceipt.objects.filter(user=user, letter=letter).exists())

    def test_rich_receipt_and_formatted_signature_reach_the_recipient(self):
        from django.contrib.auth import get_user_model

        org = Organization.objects.create(name="Mail test", plan_type=Organization.PLAN_COMPANY_PAID,
                                          paid_until=timezone.now() + timedelta(days=20))
        user = get_user_model().objects.create_user("rich-mail-browser")
        OrganizationAccess.objects.create(organization=org, user=user, role="owner")
        self.client.force_login(user)
        box = CrmMailbox.objects.create(organization=org, address="office@example.org", credential=encrypt_secret("sample"))
        letter = box.letters.create(fingerprint="a" * 64, sender="sample@taxcom.ru", subject="Тестовая квитанция",
                                    sent_at=timezone.now(), message_id="<sample@example.org>")
        receipt = EmailMessage()
        receipt.set_content("Квитанция")
        receipt.add_alternative('<table><tr><td>Итого</td><td>100</td></tr></table>'
                                '<a href="https://example.org/receipt">Открыть чек</a>'
                                '<img src="https://images.example.org/banner.png" alt="Баннер">', subtype="html")
        cache_content(letter, receipt.as_bytes())
        fixture = {"base": self.live_server_url, "path": reverse("crm_mail_detail", args=[letter.pk]) + f"?org_id={org.pk}",
                   "name": settings.SESSION_COOKIE_NAME, "session": self.client.cookies[settings.SESSION_COOKIE_NAME].value}
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        with (patch("pool_service.services.mailbox_sending.smtp_session", return_value=nullcontext(smtp)),
              patch.object(imap, "mailbox_session", side_effect=OSError)):
            code, output = _bounded_browser_process(["node", str(Path(__file__).with_name("crm_mail_rich.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        smtp.sendmail.assert_called_once()
        sent = BytesParser(policy=policy.default).parsebytes(smtp.sendmail.call_args.args[2])
        html = sent.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Courier New", html)
        self.assertIn("<strong>Команда проекта</strong>", html)
        self.assertIn("Добрый день", sent.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("Команда проекта", sent.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(sent["In-Reply-To"], "<sample@example.org>")
