"""Personal-call privacy must be enforced beyond the settings preview."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import (
    CallAnalysis,
    CommunicationAccess,
    PhoneCall,
    TelephonyConnection,
)
from pool_service.models import Notification, Organization, OrganizationAccess, ServiceTask
from pool_service.operations_mcp_views import _get_call_analysis, _list_control_tasks
from pool_service.communication_recordings import download_call_recording
from pool_service.services.call_ai import process_call_analysis, request_call_analysis
from pool_service.services.call_commitments import materialize_call_commitments
from pool_service.services.call_commitment_control import process_call_commitment_controls
from pool_service.services.call_privacy import is_private_call, private_call_ids


class PrivateCallEnforcementTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Private call enforcement")
        self.owner = User.objects.create_user("79990000001", password="test")
        self.employee = User.objects.create_user("79990000002", password="test")
        OrganizationAccess.objects.create(
            organization=self.org, user=self.owner, role="owner"
        )
        OrganizationAccess.objects.create(
            organization=self.org, user=self.employee, role="manager"
        )
        CommunicationAccess.objects.create(
            organization=self.org,
            user=self.owner,
            can_view_all_calls=True,
            can_view_own_calls=True,
            can_listen_calls=True,
        )
        CommunicationAccess.objects.create(
            organization=self.org,
            user=self.employee,
            can_view_own_calls=True,
            can_listen_calls=True,
        )
        self.connection = TelephonyConnection.objects.create(
            organization=self.org,
            external_id="privacy-test",
        )
        CallPrivateNumber.objects.create(
            organization=self.org,
            owner=self.owner,
            label="Private",
            phone_key="9990000001",
        )
        self.client.force_login(self.owner)

    def call(self, *, employee=None, external_id=None, phone="+7 999 000-00-01", **changes):
        values = dict(
            organization=self.org,
            source_kind=PhoneCall.SOURCE_TELEPHONY,
            connection=self.connection,
            external_id=external_id or f"call-{PhoneCall.objects.count()}",
            employee=employee or self.owner,
            provider_user=(employee or self.owner).username,
            provider_extension="101",
            phone_number=phone,
            direction=PhoneCall.DIRECTION_IN,
            started_at="2026-10-08T01:00:00Z",
            duration_seconds=120,
            result=PhoneCall.RESULT_ANSWERED,
            recording_status=PhoneCall.RECORDING_PENDING,
        )
        values.update(changes)
        return PhoneCall.objects.create(**values)

    def test_private_match_is_scoped_to_participating_owner(self):
        private = self.call()
        employee_call = self.call(employee=self.employee, external_id="employee-call")
        self.assertTrue(is_private_call(private))
        self.assertFalse(is_private_call(employee_call))
        self.assertEqual(private_call_ids([private, employee_call]), {private.pk})

    @patch("pool_service.communication_crm.internal_call_sync_due", return_value=False)
    def test_private_call_is_absent_from_work_call_list(self, _sync_due):
        hidden = self.call()
        visible = self.call(employee=self.employee, external_id="visible")
        response = self.client.get(reverse("communications_calls"))
        self.assertEqual(response.status_code, 200)
        ids = [call.pk for call in response.context["calls"]]
        self.assertNotIn(hidden.pk, ids)
        self.assertIn(visible.pk, ids)

    def test_export_omits_private_transcript_and_summary(self):
        call = self.call()
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="PRIVATE_TRANSCRIPT_MUST_NOT_LEAK",
            summary="PRIVATE_SUMMARY_MUST_NOT_LEAK",
        )
        response = self.client.get(
            reverse("communication_call_transcripts_export"),
            {"format": "txt"},
        )
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8")
        self.assertNotIn("PRIVATE_TRANSCRIPT_MUST_NOT_LEAK", text)
        self.assertNotIn("PRIVATE_SUMMARY_MUST_NOT_LEAK", text)
        self.assertIn("Количество: 0", text)

    def test_direct_audio_and_analysis_urls_hide_call_existence(self):
        call = self.call(recording_file="communications/private.mp3")
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="private",
            summary="private",
        )
        for name in (
            "communication_call_recording",
            "communication_call_analysis_status",
            "communication_call_analysis_transcript",
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[call.pk]))
                self.assertEqual(response.status_code, 404)

    def test_private_call_cannot_be_queued_for_ai(self):
        call = self.call(recording_file="communications/private.mp3")
        self.assertFalse(request_call_analysis(call.pk))
        self.assertFalse(CallAnalysis.objects.filter(call=call).exists())

    @patch("pool_service.services.call_ai._client")
    def test_queued_call_is_rechecked_before_any_ai_request(self, ai_client):
        call = self.call(recording_file="communications/private.mp3")
        analysis = CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_PENDING,
            requested_at=timezone.now(),
        )
        self.assertFalse(process_call_analysis(call.pk))
        ai_client.assert_not_called()
        analysis.refresh_from_db()
        self.assertEqual(analysis.status, CallAnalysis.STATUS_PENDING)
        self.assertIsNone(analysis.requested_at)
        self.assertEqual(analysis.processing_token, "")

    @patch("pool_service.communication_recordings._open_recording")
    def test_private_call_is_blocked_before_provider_recording_request(self, open_recording):
        call = self.call(
            recording_ref="https://megapbx.ru/private-recording",
            recording_file="",
        )
        self.assertFalse(download_call_recording(call.pk))
        open_recording.assert_not_called()
        call.refresh_from_db()
        self.assertEqual(call.recording_status, PhoneCall.RECORDING_PENDING)

    def test_ready_private_analysis_is_invisible_to_operations_mcp(self):
        call = self.call()
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="private",
            summary="private",
            facts={"commitments": []},
        )
        with self.assertRaises(ValueError):
            _get_call_analysis(self.org, {"call_id": call.pk})

    def test_private_ready_analysis_cannot_materialize_task(self):
        call = self.call()
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="private",
            summary="private",
            facts={
                "commitments": [{
                    "actor": "employee",
                    "confidence": "high",
                    "action": "Call client",
                    "evidence": "private promise",
                }]
            },
        )
        self.assertEqual(materialize_call_commitments(call.pk), [])
        self.assertFalse(ServiceTask.objects.exists())

    def test_private_source_task_is_hidden_from_mcp_and_control_notifications(self):
        call = self.call()
        task = ServiceTask.objects.create(
            organization=self.org,
            title="Private derived task",
            start_date=timezone.localdate() - timedelta(days=1),
            end_date=timezone.localdate() - timedelta(days=1),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_SYSTEM,
            status=ServiceTask.STATUS_NEW,
            primary_responsible=self.owner,
            auto_created=True,
            payload_json={
                "source": "call_analysis",
                "source_call_id": call.pk,
                "actor": "employee",
            },
        )
        task.responsibles.add(self.owner)
        self.assertEqual(_list_control_tasks(self.org, {})["tasks"], [])
        result = process_call_commitment_controls(now=timezone.now())
        self.assertEqual(result["checked"], 0)
        self.assertFalse(Notification.objects.exists())
