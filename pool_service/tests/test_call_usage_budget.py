"""Budget reservation and usage accounting for call AI."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone

from pool_service.call_processing_models import (
    CallProcessingBudget,
    CallProcessingUsage,
)
from pool_service.communication_models import CallAnalysis, PhoneCall
from pool_service.models import Organization, OrganizationAccess
from pool_service.services.call_ai import process_call_analysis
from pool_service.services.call_processing_settings import save_budget
from pool_service.services.call_usage import (
    CallBudgetExceeded,
    finish_stage,
    reserve_stage,
    usage_summary,
)


class CallUsageBudgetTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Call usage budget test")
        self.owner = User.objects.create_user("usage-owner", password="test")
        OrganizationAccess.objects.create(
            organization=self.org, user=self.owner, role="owner"
        )
        self.call = PhoneCall.objects.create(
            organization=self.org,
            source_kind=PhoneCall.SOURCE_UPLOADED,
            connection=None,
            external_id="usage-call",
            employee=self.owner,
            phone_number="",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            duration_seconds=60,
            result=PhoneCall.RESULT_ANSWERED,
            recording_status=PhoneCall.RECORDING_STORED,
        )

    def test_owner_budget_has_no_assistant_chosen_default_and_is_optimistic(self):
        budget = save_budget(
            user=self.owner,
            organization=self.org,
            monthly_limit_usd=Decimal("5.0000"),
            expected_revision=0,
        )
        self.assertEqual(budget.monthly_limit_usd, Decimal("5.0000"))
        self.assertEqual(budget.revision, 1)

        budget = save_budget(
            user=self.owner,
            organization=self.org,
            monthly_limit_usd=None,
            expected_revision=1,
        )
        self.assertIsNone(budget.monthly_limit_usd)
        self.assertEqual(budget.revision, 2)

    def test_atomic_reservation_rejects_amount_above_owner_limit(self):
        CallProcessingBudget.objects.create(
            organization=self.org,
            monthly_limit_usd=Decimal("0.0050"),
        )
        with self.assertRaises(CallBudgetExceeded):
            reserve_stage(
                call=self.call,
                attempt_key="attempt-1",
                stage=CallProcessingUsage.STAGE_TRANSCRIPTION,
                model="gpt-4o-transcribe-diarize",
                estimated_cost_usd=Decimal("0.0060"),
                duration_seconds=60,
            )
        self.assertFalse(CallProcessingUsage.objects.exists())

    def test_usage_tokens_are_costed_but_not_claimed_as_confirmed_charge(self):
        usage = reserve_stage(
            call=self.call,
            attempt_key="analysis-1",
            stage=CallProcessingUsage.STAGE_ANALYSIS,
            model="gpt-5.6-luna",
            estimated_cost_usd=Decimal("2.0"),
        )
        finish_stage(
            usage.pk,
            succeeded=True,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        usage.refresh_from_db()
        self.assertEqual(usage.usage_cost_usd, Decimal("1.400000"))
        self.assertIsNone(usage.confirmed_cost_usd)
        self.assertEqual(usage.tariff_version, "openai-2026-10-08")

    def test_summary_separates_unique_audio_from_retry_minutes(self):
        for key in ("try-1", "try-2"):
            usage = reserve_stage(
                call=self.call,
                attempt_key=key,
                stage=CallProcessingUsage.STAGE_TRANSCRIPTION,
                model="gpt-4o-transcribe-diarize",
                estimated_cost_usd=Decimal("0.006"),
                duration_seconds=60,
            )
            finish_stage(usage.pk, succeeded=True)

        summary = usage_summary(self.org)
        self.assertEqual(summary["totals"]["processed_calls"], 1)
        self.assertEqual(summary["totals"]["unique_audio_seconds"], 60)
        self.assertEqual(summary["totals"]["attempt_audio_seconds"], 120)
        self.assertEqual(summary["totals"]["confirmed_unknown"], 2)
        self.assertEqual(summary["by_employee"][0]["audio_seconds"], 60)

    @patch("pool_service.services.call_ai._client")
    def test_budget_stops_processing_before_any_external_ai_client(self, ai_client):
        self.call.recording_file.save(
            "budget-block.mp3",
            ContentFile(b"ID3budget"),
            save=True,
        )
        CallProcessingBudget.objects.create(
            organization=self.org,
            monthly_limit_usd=Decimal("0.0001"),
        )

        self.assertFalse(process_call_analysis(self.call.pk))
        ai_client.assert_not_called()
        analysis = CallAnalysis.objects.get(call=self.call)
        self.assertEqual(analysis.status, CallAnalysis.STATUS_PENDING)
        self.assertEqual(
            analysis.error,
            "call_budget_monthly_budget_exceeded",
        )
        self.assertIsNone(analysis.requested_at)
        self.assertFalse(CallProcessingUsage.objects.exists())

    @patch("pool_service.services.call_ai._client")
    def test_successful_processing_records_both_provider_usage_stages(self, client_factory):
        self.call.recording_file.save(
            "usage-success.mp3",
            ContentFile(b"ID3usage"),
            save=True,
        )
        client = client_factory.return_value
        client.audio.transcriptions.create.return_value = SimpleNamespace(
            text="Нужен бассейн.",
            segments=[],
            usage={"input_tokens": 1000, "output_tokens": 100},
        )
        client.responses.create.return_value = SimpleNamespace(
            output_text='{"summary":"Клиенту нужен бассейн.","facts":{"request":"Бассейн"}}',
            usage={"input_tokens": 2000, "output_tokens": 200},
        )

        self.assertTrue(process_call_analysis(self.call.pk))
        rows = list(
            CallProcessingUsage.objects.filter(call=self.call).order_by("stage")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row.stage for row in rows},
            {
                CallProcessingUsage.STAGE_TRANSCRIPTION,
                CallProcessingUsage.STAGE_ANALYSIS,
            },
        )
        self.assertTrue(all(
            row.status == CallProcessingUsage.STATUS_SUCCEEDED for row in rows
        ))
        transcription = next(
            row for row in rows
            if row.stage == CallProcessingUsage.STAGE_TRANSCRIPTION
        )
        analysis = next(
            row for row in rows
            if row.stage == CallProcessingUsage.STAGE_ANALYSIS
        )
        self.assertEqual(transcription.input_tokens, 1000)
        self.assertEqual(transcription.output_tokens, 100)
        self.assertEqual(transcription.usage_cost_usd, Decimal("0.003500"))
        self.assertEqual(analysis.input_tokens, 2000)
        self.assertEqual(analysis.output_tokens, 200)
        self.assertEqual(analysis.usage_cost_usd, Decimal("0.000640"))
        self.assertIsNone(transcription.confirmed_cost_usd)
        self.assertIsNone(analysis.confirmed_cost_usd)

    @patch("pool_service.services.call_ai._client")
    def test_saved_transcript_retry_does_not_reserve_transcription_again(self, client_factory):
        self.call.recording_file.save(
            "saved-transcript.mp3",
            ContentFile(b"ID3saved"),
            save=True,
        )
        CallAnalysis.objects.create(
            call=self.call,
            status=CallAnalysis.STATUS_FAILED,
            transcript="A: Уже сохранённый текст.",
            transcription_model="gpt-4o-transcribe-diarize",
            error="analysis_invalid_json",
        )
        client = client_factory.return_value
        client.responses.create.return_value = SimpleNamespace(
            output_text='{"summary":"Готово.","facts":{}}',
            usage={"input_tokens": 500, "output_tokens": 50},
        )

        self.assertTrue(process_call_analysis(self.call.pk))
        self.assertFalse(
            CallProcessingUsage.objects.filter(
                call=self.call,
                stage=CallProcessingUsage.STAGE_TRANSCRIPTION,
            ).exists()
        )
        self.assertEqual(
            CallProcessingUsage.objects.filter(
                call=self.call,
                stage=CallProcessingUsage.STAGE_ANALYSIS,
            ).count(),
            1,
        )
        client.audio.transcriptions.create.assert_not_called()
