"""Actual Chromium acceptance; run explicitly in the pinned CI browser gate."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmLead, CrmLeadTaskLink
from pool_service.models import Employee, Organization, OrganizationAccess, ServiceTask


@skipUnless(os.environ.get("CRM_LEADS_BROWSER_TEST") == "1", "Dedicated Chromium acceptance step enables this test")
class LeadKanbanBrowserTests(StaticLiveServerTestCase):
    def test_drag_persistence_replay_restoration_search_mobile_keyboard(self):
        org = Organization.objects.create(name="Synthetic kanban", crm_v2_read=True, crm_v2_write=True,
            plan_type=Organization.PLAN_COMPANY_PAID, paid_until=timezone.now() + timedelta(days=30))
        user = get_user_model().objects.create_user("synthetic-browser-lead")
        OrganizationAccess.objects.create(organization=org, user=user, role="manager")
        Employee.objects.create(organization=org, user=user, display_name="Synthetic manager")
        CrmAccess.objects.create(organization=org, user=user, view_leads=True, take_leads=True,
            transition_own_leads=True, create_own_leads=True, edit_own_leads=True)
        lead = CrmLead.objects.create(organization=org, title="Synthetic browser inquiry", needs_review=True)
        self.client.force_login(user)
        fixture = {"base": self.live_server_url, "workspace": reverse("crm_live_leads") + "?org_id=" + str(org.pk),
            "command": reverse("crm_live_lead_command"), "leadId": lead.pk,
            "sessionCookie": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            "sessionCookieName": settings.SESSION_COOKIE_NAME}
        result = subprocess.run(["node", str(Path(__file__).with_name("crm_lead_kanban.browser.cjs"))],
            input=json.dumps(fixture), text=True, capture_output=True, timeout=150, check=False)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-9000:])
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead.is_archived, lead.needs_review), ("working", "", False, True))
        self.assertEqual(CrmLeadTaskLink.objects.filter(lead=lead).count(), 2)
        self.assertEqual(ServiceTask.objects.filter(is_archived=False).count(), 0)


from pool_service.tests.test_crm_inbox import Fixture as InboxFixture
import pool_service.tests.test_crm_lead_intake as intake_fixtures


@skipUnless(os.environ.get('CRM_LEADS_BROWSER_TEST') == '1', 'Dedicated Chromium acceptance step enables this test')
class LeadSourceBrowserTests(InboxFixture, StaticLiveServerTestCase):
    mapped_call = intake_fixtures.LeadIntakeTests.mapped_call
    deliver = intake_fixtures.LeadIntakeTests.deliver

    def test_saved_call_source_suggestions_audio_and_mobile(self):
        from io import BytesIO
        import tempfile
        import wave
        from django.core.files.base import ContentFile
        from django.test import override_settings
        from pool_service.communication_models import CommunicationAccess, CallAnalysis
        from pool_service.crm_commercial_models import CrmIntakeActivation
        self.org.crm_v2_read = self.org.crm_v2_write = self.org.crm_intake_process = True
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = self.now + timedelta(days=30); self.org.save()
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now - timedelta(minutes=30))
        self.user = get_user_model().objects.create_user('synthetic-source-browser')
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role='manager')
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name='Synthetic source employee')
        CrmAccess.objects.create(organization=self.org, user=self.user, view_leads=True, edit_own_leads=True, take_leads=True)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_view_conversations=True,
            can_view_all_calls=True, can_view_own_calls=True, can_listen_calls=True)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root,
                COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False):
            call = self.mapped_call(); self.deliver('call')
            lead = CrmLead.objects.get(); lead.client = None; lead.save()
            call.client = None; call.save(update_fields=['client'])
            sound = BytesIO()
            with wave.open(sound, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000); wav.writeframes(b'\0\0' * 2000)
            call.recording_file.save('synthetic.wav', ContentFile(sound.getvalue()), save=True)
            analysis, _ = CallAnalysis.objects.update_or_create(call=call, defaults=dict(status=CallAnalysis.STATUS_READY,
                transcript='Synthetic transcript <script>neverExecute()</script>', facts={'client_name':'Synthetic person',
                    'phone':call.phone_number, 'request':'Synthetic suggested requirement'}))
            before_analysis = (analysis.attempts, analysis.requested_at)
            self.incoming(participant_name='Synthetic message contact'); self.deliver()
            message_lead = CrmLead.objects.exclude(pk=lead.pk).get()
            self.client.force_login(self.user)
            fixture = dict(base=self.live_server_url, workspace=reverse('crm_live_leads') + '?org_id=' + str(self.org.pk),
                leadId=lead.pk, messageLeadId=message_lead.pk, phone=call.phone_number, employee=self.employee.display_name,
                command=reverse('crm_live_lead_command'), recording=reverse('communication_call_recording', args=[call.pk]),
                sessionCookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value, sessionCookieName=settings.SESSION_COOKIE_NAME)
            # File-backed logs avoid waiting for EOF on inherited Chromium pipes
            # after Node has exited; the process timeout and assertions stay strict.
            with tempfile.TemporaryFile(mode='w+') as browser_log, tempfile.TemporaryFile(mode='w+') as browser_input:
                browser_input.write(json.dumps(fixture)); browser_input.seek(0)
                result = subprocess.run(['node', str(Path(__file__).with_name('crm_lead_source.browser.cjs'))],
                    stdin=browser_input, text=True, stdout=browser_log, stderr=browser_log, timeout=150, check=False)
                browser_log.seek(0)
                self.assertEqual(result.returncode, 0, browser_log.read()[-9000:])
            lead.refresh_from_db(); analysis.refresh_from_db()
            self.assertEqual(lead.need, 'Synthetic suggested requirement')
            self.assertIsNone(lead.client_id)
            self.assertEqual((analysis.attempts, analysis.requested_at), before_analysis)
