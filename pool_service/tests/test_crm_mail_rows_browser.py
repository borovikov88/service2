"""Exercise the inbox row as a signed-in employee in desktop and mobile Chromium."""
import os
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.mailbox_models import CrmMailbox, MailFolder, MailLocation, MailReadReceipt
from pool_service.models import Organization, OrganizationAccess
from pool_service.services import mailbox_imap as imap
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
