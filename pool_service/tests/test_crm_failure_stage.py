"""Failure is an explicit terminal result; financial/source records survive it."""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client as HttpClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_board_models import CrmBoardEpoch
from pool_service.crm_commercial_models import CrmDeal, CrmEvent, CrmLead, CrmPipeline, CrmStage
from pool_service.crm_order_models import CrmDealOrderLink, CrmOrderEvidenceSnapshot
from pool_service.models import Organization, ServiceTask
from pool_service.services.crm_commercial_commands import close_commercial_deal_negative, execute_commercial_command, CommercialConflict
from pool_service.services.crm_lead_commands import lead_column
from pool_service.services.crm_order_finance import finance_projection
from pool_service.services.task_feedback import version_for
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.tests.test_crm_order_finance import OrderFinanceFixture


class FailedLeadTests(LeadFixture, TestCase):
    def test_reason_version_replay_and_distinct_board_column(self):
        lead = self.create(client_id=self.client.pk)
        task = lead.next_task
        changes = {'column': 'failed', 'reason': 'Synthetic refusal', 'expected_task_version': version_for(task)}
        before = self.state()
        for invalid, error in ((dict(changes, reason='  '), ValidationError),
                (dict(changes, expected_task_version='0'*64), CommercialConflict)):
            with self.assertRaises(error): self.command('lead.disposition', invalid, lead)
            self.assertEqual(self.state(), before)
        saved = self.command('lead.disposition', changes, lead, key='failure-lead')
        self.assertEqual(self.command('lead.disposition', changes, lead, key='failure-lead'), saved)
        lead.refresh_from_db(); task.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead_column(lead)), ('closed', 'lost', 'failed'))
        self.assertEqual(lead.client_id, self.client.pk)
        self.assertEqual(lead.next_task_id, task.pk)  # Keep the cancelled task in history.
        self.assertEqual(task.status, ServiceTask.STATUS_CANCELLED)
        self.assertIsNone(task.completed_at)
        self.assertEqual(CrmEvent.objects.filter(lead=lead, event_type='lead.disposition').count(), 1)
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=2); self.org.save()
        http = HttpClient(); http.force_login(self.user)
        data = http.get(reverse('crm_live_lead_data'), {'org_id': self.org.pk}).json()
        columns = {row['key']: row for row in data['columns']}
        self.assertEqual([row['id'] for row in columns['failed']['leads']], [lead.pk])
        self.assertEqual(columns['history']['total'], 0)
        # A new incoming communication remains visible for review without losing the result.
        lead.needs_review = True; lead.save()
        self.assertEqual(lead_column(lead), 'unparsed')
        data = http.get(reverse('crm_live_lead_data'), {'org_id': self.org.pk}).json()
        columns = {row['key']: row for row in data['columns']}
        self.assertEqual(columns['failed']['total'], 0)
        self.assertEqual([row['id'] for row in columns['unparsed']['leads']], [lead.pk])

    def test_final_result_and_revoked_permission_are_not_overwritten(self):
        lead = self.create()
        changes = {'column': 'failed', 'reason': 'Synthetic refusal', 'expected_task_version': version_for(lead.next_task)}
        # The legacy transition must not bypass the task version checked by disposition.
        with self.assertRaises(ValidationError):
            self.command('lead.transition', {'status': 'closed', 'result': 'lost', 'reason': 'Synthetic refusal'}, lead)
        self.grant.transition_own_leads = False; self.grant.save()
        with self.assertRaises(PermissionDenied): self.command('lead.disposition', changes, lead)
        self.grant.transition_own_leads = True; self.grant.save()
        self.command('lead.disposition', dict(changes, column='history'), lead)
        lead.refresh_from_db()
        with self.assertRaises(ValidationError): self.command('lead.disposition', changes, lead)
        lead.refresh_from_db(); self.assertEqual(lead.result, 'no_deal')


class FailedDealTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.crm_close = True
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=2); self.org.save()
        self.client.force_login(self.user)

    def close(self, deal, **kwargs):
        values = dict(actor=self.user, organization=self.org, operation_key=uuid4().hex,
            subject_id=deal.pk, expected_revision=deal.revision, outcome='lost', reason='Synthetic refusal',
            expected_task_version=None)
        values.update(kwargs)
        return close_commercial_deal_negative(**values)

    def test_execution_stages_in_every_pipeline_can_fail_but_not_via_plain_transition(self):
        for pipeline in CrmPipeline.objects.filter(organization=self.org):
            with self.subTest(pipeline=pipeline.code):
                stage = pipeline.stages.exclude(code__in=('completed', 'failed')).last()
                deal = CrmDeal.objects.create(organization=self.org, pipeline=pipeline, stage=stage,
                    manager=self.user, client=self.client_record, title='Synthetic active contract')
                failure = pipeline.stages.get(code='failed')
                with self.assertRaises(ValidationError):
                    execute_commercial_command(actor=self.user, organization=self.org, operation_key=uuid4().hex,
                        command_type='deal.transition', subject_id=deal.pk, expected_revision=0,
                        changes={'stage_id': failure.pk, 'reason': 'Must explicitly close'})
                saved = self.close(deal)
                deal.refresh_from_db()
                self.assertEqual((deal.outcome, deal.stage_id), ('lost', failure.pk))
                event = CrmEvent.objects.get(pk=saved['event_id'])
                self.assertEqual(event.safe_diff['stage_id'], {'before': stage.pk, 'after': failure.pk})
                with self.assertRaises(ValidationError): self.close(deal)

    def test_disabled_failure_stage_is_not_a_drop_target_or_a_command(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        stage = self.pipeline.stages.get(code='failed'); stage.is_active = False; stage.save()
        with self.assertRaises(ValidationError):
            self.close(deal, expected_task_version=version_for(deal.next_action.task_link.task))
        data = self.client.get(reverse('crm_live_detail', args=[deal.pk]), {'org_id': self.org.pk}).json()
        self.assertFalse(data['deal']['can_close_negative'])

    def test_old_losses_are_projected_without_rewriting_history_and_filters_agree(self):
        original = self.pipeline.stages.get(code='need'); failed = self.pipeline.stages.get(code='failed')
        lost = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline, stage=original,
            manager=self.user, title='Synthetic old loss', outcome='lost', close_reason='Old refusal',
            closed_at=timezone.now(), amount='15.00', amount_basis='agreed', currency='RUB')
        params = {'org_id': self.org.pk, 'pipeline': self.pipeline.pk, 'layout': 'board'}
        url = reverse('crm_live_data')
        data = self.client.get(url, params).json()
        self.assertEqual(data['outcome'], 'all')
        self.assertEqual([(row['id'], row['board_stage_id']) for row in data['deals']], [(lost.pk, failed.pk)])
        self.assertEqual(data['stage_totals'][0]['stage_id'], failed.pk)
        self.assertEqual(self.client.get(url, dict(params, stage=original.pk)).json()['deals'], [])
        self.assertEqual(len(self.client.get(url, dict(params, stage=failed.pk, outcome='lost')).json()['deals']), 1)
        self.assertEqual(self.client.get(url, dict(params, outcome='open')).json()['deals'], [])
        lost.refresh_from_db(); self.assertEqual((lost.stage_id, lost.revision), (original.pk, 0))
        self.assertEqual(CrmEvent.objects.count(), 0)

    def test_migration_adds_missing_custom_pipeline_stage_once_without_resetting_settings(self):
        historical = MigrationExecutor(connection).loader.project_state([('pool_service', '0185_crm_failure_stage')]).apps
        Stage = historical.get_model('pool_service', 'CrmStage')
        custom = CrmPipeline.objects.create(organization=self.org, code='custom', name='Synthetic custom')
        CrmStage.objects.create(pipeline=custom, code='work', name='Synthetic work', position=75)
        existing = self.pipeline.stages.get(code='failed')
        existing.name = 'Synthetic custom failure'; existing.position = 99; existing.is_active = False; existing.save()
        before = CrmBoardEpoch.objects.get(organization=self.org).revision
        migrate = import_module('pool_service.migrations.0185_crm_failure_stage').add_failure_stages
        migrate(historical, SimpleNamespace(connection=connection))
        self.assertEqual(Stage.objects.get(pipeline_id=custom.pk, code='failed').position, 76)
        after = CrmBoardEpoch.objects.get(organization=self.org).revision
        self.assertEqual(after, before + 1)
        migrate(historical, SimpleNamespace(connection=connection))
        self.assertEqual(CrmBoardEpoch.objects.get(organization=self.org).revision, after)
        existing.refresh_from_db()
        self.assertEqual((existing.name, existing.position, existing.is_active), ('Synthetic custom failure', 99, False))


class FailedDealFinanceTests(OrderFinanceFixture, TestCase):
    def test_failure_preserves_order_evidence_and_later_full_payment_cannot_reopen_or_win_it(self):
        deal, order = self.prepare_order_deal()
        self.bind(deal, order); deal.refresh_from_db()
        self.org.crm_close = True; self.org.save(update_fields=['crm_close'])
        documents = list(CrmOrderEvidenceSnapshot.objects.values())
        close_commercial_deal_negative(actor=self.user, organization=self.org, operation_key='failure-linked-order',
            subject_id=deal.pk, expected_revision=deal.revision, outcome='lost', reason='Synthetic refusal',
            expected_task_version=version_for(deal.next_action.task_link.task))
        self.assertEqual(list(CrmOrderEvidenceSnapshot.objects.values()), documents)
        self.assertEqual(CrmDealOrderLink.objects.get(deal=deal).order_id, order.pk)
        self.store_orders(self.evidence(realized_amount=Decimal('1000'), paid_amount=Decimal('1000')))
        deal.refresh_from_db()
        self.assertEqual((deal.outcome, deal.stage.code), ('lost', 'failed'))
        data = finance_projection(deal, self.user, self.org)
        self.assertEqual((data['completion_percent'], data['payment_percent']), ('100.00', '100.00'))
        self.assertFalse(data['auto_complete'])
