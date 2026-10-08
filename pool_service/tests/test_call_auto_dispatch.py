"""Activated call rules dispatch only new, verified, audio-ready calls."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone

from pool_service.communication_models import (
    CallAnalysis,
    PhoneCall,
    TelephonyConnection,
)
from pool_service.models import Organization, OrganizationAccess
from pool_service.services.call_processing_dispatch import (
    dispatch_call_if_ready,
    recover_auto_dispatch,
)
from pool_service.services.call_processing_settings import (
    activate_rule,
    pause_rule,
    save_budget,
    save_rule,
)


class CallAutoDispatchTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Auto dispatch test")
        self.owner = User.objects.create_user("79990000001")
        self.employee = User.objects.create_user("79990000002")
        OrganizationAccess.objects.create(
            organization=self.org, user=self.owner, role="owner"
        )
        OrganizationAccess.objects.create(
            organization=self.org, user=self.employee, role="manager"
        )
        self.connection = TelephonyConnection.objects.create(
            organization=self.org,
            external_id="auto-dispatch",
        )
        self.rule = save_rule(
            user=self.owner,
            organization=self.org,
            employee_id=self.employee.pk,
            mode="all_except",
            include_staff=True,
            numbers_text="",
            expected_revision=0,
        )

    def identity_rows(self):
        return [
            {
                "connection_id": self.connection.pk,
                "extension": "101",
                "external_user": "79990000002",
                "employee__user_id": self.employee.pk,
            }
        ]

    def call(self, *, started_at=None, external_id=None):
        call = PhoneCall.objects.create(
            organization=self.org,
            source_kind=PhoneCall.SOURCE_TELEPHONY,
            connection=self.connection,
            external_id=external_id or f"auto-{PhoneCall.objects.count()}",
            employee=self.employee,
            provider_extension="101",
            provider_user="79990000002",
            phone_number="+7 999 111-22-33",
            direction=PhoneCall.DIRECTION_IN,
            started_at=started_at or timezone.now(),
            duration_seconds=60,
            result=PhoneCall.RESULT_ANSWERED,
            recording_status=PhoneCall.RECORDING_STORED,
        )
        call.recording_file.save(
            f"{call.external_id}.mp3",
            ContentFile(b"ID3auto"),
            save=True,
        )
        return call

    def test_activation_requires_owner_selected_budget(self):
        with self.assertRaises(ValidationError):
            activate_rule(
                user=self.owner,
                organization=self.org,
                employee_id=self.employee.pk,
                expected_revision=self.rule.revision,
            )
        self.rule.refresh_from_db()
        self.assertIsNone(self.rule.effective_from)

    @patch(
        "pool_service.services.call_processing_settings._identity_rows"
    )
    def test_activation_is_hard_new_call_boundary_and_dispatch_is_idempotent(
        self, identity_rows
    ):
        identity_rows.return_value = self.identity_rows()
        old = self.call(started_at=timezone.now() - timedelta(hours=1), external_id="old")
        budget = save_budget(
            user=self.owner,
            organization=self.org,
            monthly_limit_usd="10.00",
            expected_revision=0,
        )
        self.assertEqual(str(budget.monthly_limit_usd), "10.00")
        self.rule = activate_rule(
            user=self.owner,
            organization=self.org,
            employee_id=self.employee.pk,
            expected_revision=self.rule.revision,
        )
        new = self.call(
            started_at=self.rule.effective_from + timedelta(seconds=1),
            external_id="new",
        )

        old_result = dispatch_call_if_ready(old.pk, start_worker=False)
        self.assertFalse(old_result["queued"])
        self.assertEqual(old_result["reason"], "historical")
        self.assertFalse(CallAnalysis.objects.filter(call=old).exists())

        first = dispatch_call_if_ready(new.pk, start_worker=False)
        second = dispatch_call_if_ready(new.pk, start_worker=False)
        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertEqual(second["reason"], "already_queued_or_done")
        analysis = CallAnalysis.objects.get(call=new)
        self.assertIsNotNone(analysis.requested_at)

    @patch(
        "pool_service.services.call_processing_dispatch.start_requested_call_analysis_worker",
        return_value=True,
    )
    @patch(
        "pool_service.services.call_processing_settings._identity_rows"
    )
    def test_recovery_queues_new_call_only_and_never_backfills_pre_activation(
        self, identity_rows, worker
    ):
        identity_rows.return_value = self.identity_rows()
        old = self.call(started_at=timezone.now() - timedelta(days=2), external_id="archive")
        save_budget(
            user=self.owner,
            organization=self.org,
            monthly_limit_usd="10.00",
            expected_revision=0,
        )
        self.rule = activate_rule(
            user=self.owner,
            organization=self.org,
            employee_id=self.employee.pk,
            expected_revision=self.rule.revision,
        )
        new = self.call(
            started_at=self.rule.effective_from + timedelta(seconds=1),
            external_id="recovery-new",
        )

        result = recover_auto_dispatch(limit=10)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["queued"], 1)
        self.assertFalse(CallAnalysis.objects.filter(call=old).exists())
        self.assertTrue(CallAnalysis.objects.filter(call=new).exists())
        worker.assert_called_once()

    @patch(
        "pool_service.services.call_processing_settings._identity_rows"
    )
    def test_pause_prevents_new_dispatch(self, identity_rows):
        identity_rows.return_value = self.identity_rows()
        save_budget(
            user=self.owner,
            organization=self.org,
            monthly_limit_usd="10.00",
            expected_revision=0,
        )
        self.rule = activate_rule(
            user=self.owner,
            organization=self.org,
            employee_id=self.employee.pk,
            expected_revision=self.rule.revision,
        )
        self.rule = pause_rule(
            user=self.owner,
            organization=self.org,
            employee_id=self.employee.pk,
            expected_revision=self.rule.revision,
        )
        call = self.call(
            started_at=timezone.now() + timedelta(seconds=1),
            external_id="paused",
        )
        result = dispatch_call_if_ready(call.pk, start_worker=False)
        self.assertFalse(result["queued"])
        self.assertEqual(result["reason"], "activation_required")
