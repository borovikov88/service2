from datetime import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.communication_models import CallAnalysis, PhoneCall, TelephonyConnection
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask
from pool_service.services.call_commitments import materialize_call_commitments


@override_settings(COMMUNICATION_TIME_ZONE="Asia/Barnaul")
class CallCommitmentMaterializationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Commitment org")
        self.manager = User.objects.create_user("manager", password="test")
        OrganizationAccess.objects.create(
            user=self.manager,
            organization=self.organization,
            role="manager",
        )
        self.client_entity = Client.objects.create(
            organization=self.organization,
            name="Клиент",
            phone="+79000000000",
        )
        self.telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="МегаФон",
            external_id="commitment-tests",
        )

    def _call(self, external_id):
        return PhoneCall.objects.create(
            organization=self.organization,
            connection=self.telephony,
            external_id=external_id,
            employee=self.manager,
            client=self.client_entity,
            phone_number=self.client_entity.phone,
            direction=PhoneCall.DIRECTION_OUT,
            started_at=timezone.make_aware(datetime(2026, 10, 5, 10, 0)),
            duration_seconds=90,
            result=PhoneCall.RESULT_ANSWERED,
        )

    def test_high_confidence_employee_commitment_becomes_reviewable_proposal(self):
        call = self._call("employee-promise")
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="Менеджер: Завтра отправлю расчёт.",
            summary="Менеджер обещал отправить расчёт.",
            facts={
                "commitments": [
                    {
                        "actor": "employee",
                        "action": "Отправить клиенту расчёт оборудования",
                        "kind": "proposal",
                        "due_date": "2026-10-06",
                        "due_time": "10:00",
                        "evidence": "Завтра отправлю расчёт.",
                        "confidence": "high",
                    }
                ]
            },
        )

        tasks = materialize_call_commitments(call.id)

        self.assertEqual(len(tasks), 1)
        proposal = tasks[0]
        self.assertEqual(proposal["call_id"], call.id)
        self.assertEqual(proposal["commitment_index"], 0)
        self.assertEqual(proposal["status"], "ready_for_review")
        self.assertEqual(proposal["due_date"], "2026-10-06")
        self.assertTrue(proposal["proposal_id"].startswith(f"call-{call.id}-commitment-0-"))
        self.assertFalse(ServiceTask.objects.exists())

    def test_client_commitment_is_a_proposal_and_does_not_create_a_task(self):
        call = self._call("client-promise")
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="Клиент: В пятницу оплачу счёт.",
            summary="Клиент обещал оплатить счёт.",
            facts={
                "commitments": [
                    {
                        "actor": "client",
                        "action": "Оплатить счёт",
                        "kind": "payment",
                        "due_date": "2026-10-09",
                        "due_time": None,
                        "evidence": "В пятницу оплачу счёт.",
                        "confidence": "high",
                    }
                ]
            },
        )

        first = materialize_call_commitments(call.id)
        second = materialize_call_commitments(call.id)

        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["status"], "ready_for_review")
        self.assertEqual(first[0]["actor"], "client")
        self.assertFalse(ServiceTask.objects.filter(
            payload_json__source_call_id=call.id
        ).exists())

    def test_uncertain_commitment_is_not_materialized(self):
        call = self._call("uncertain-promise")
        CallAnalysis.objects.create(
            call=call,
            status=CallAnalysis.STATUS_READY,
            transcript="Можно будет потом обсудить расчёт.",
            summary="Обсудили возможность расчёта.",
            facts={
                "commitments": [
                    {
                        "actor": "employee",
                        "action": "Подготовить расчёт",
                        "kind": "proposal",
                        "due_date": None,
                        "due_time": None,
                        "evidence": "Можно будет потом обсудить расчёт.",
                        "confidence": "medium",
                    }
                ]
            },
        )

        proposals = materialize_call_commitments(call.id)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "needs_clarification")
        self.assertTrue(proposals[0]["needs_clarification"])
        self.assertFalse(
            ServiceTask.objects.filter(payload_json__source_call_id=call.id).exists()
        )
