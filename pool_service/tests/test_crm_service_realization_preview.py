"""Synthetic isolated ORM regressions; no OData/import/production operations."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from pool_service.models import Client, Organization, OrganizationAccess, OneCImportBatch, OneCMonthlyProfit, OneCReportPeriodState
from pool_service.reward_models import OneCCustomerIdentity
from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmPipeline
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.services.crm_service_realization_preview import preview_service_rows


class ServiceRealizationPreviewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic preview org', crm_v2_read=True,
            paid_until=timezone.now()+timedelta(days=30))
        self.actor = get_user_model().objects.create_user('synthetic-service-reviewer')
        self.membership = OrganizationAccess.objects.create(user=self.actor, organization=self.org, role='owner')
        CrmAccess.objects.create(organization=self.org, user=self.actor, view_team_deals=True)
        bootstrap_commercial_settings(self.org.pk)
        self.client_record = Client.objects.create(organization=self.org, name='Synthetic service client')
        pipeline = CrmPipeline.objects.get(organization=self.org, code='service')
        self.deal = CrmDeal.objects.create(organization=self.org, client=self.client_record, pipeline=pipeline,
            stage=pipeline.stages.order_by('position').first(), title='Synthetic service scope')
        self.party, self.external_org, self.contract, self.recorder = [str(uuid4()) for _ in range(4)]
        self.mapping = OneCCustomerIdentity.objects.create(organization=self.org, client=self.client_record,
            onec_customer_id=self.party, confirmed_at=timezone.now(), confirmed_by=self.actor)
        self.month = date(2026, 1, 1)
        self.batch = self.make_batch()
        self.state = OneCReportPeriodState.objects.create(organization=self.org,
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT, period_month=self.month, active_batch=self.batch)
        self.row = self.make_row(self.batch)

    def make_batch(self, **overrides):
        values = dict(organization=self.org, source_type='odata', import_type='monthly_profit',
            original_filename='synthetic.json', stored_file='synthetic.json', file_sha256=uuid4().hex*2,
            status='confirmed', uploaded_by=self.actor)
        values.update(overrides)
        return OneCImportBatch.objects.create(**values)

    def make_row(self, batch, number=1, **overrides):
        data = dict(source='odata', recorder=self.recorder, recorder_type='Document_Sales', line_number=number,
            organization_guid=self.external_org, customer_guid=self.party, nomenclature_guid=str(uuid4()),
            document_guid=self.recorder, document_type='Document_Sales', document_date='2026-01-15', vat='20.00')
        data.update(overrides)
        return OneCMonthlyProfit.objects.create(organization=self.org, import_batch=batch, period_month=self.month,
            source_recorder=self.recorder, source_row_number=number, nomenclature='Synthetic service candidate',
            quantity=Decimal('1'), revenue=Decimal('100'), cost=Decimal('60'), gross_profit=Decimal('40'), source_data=data)

    def preview(self, **overrides):
        values = dict(actor=self.actor, organization_id=self.org.pk, deal_id=self.deal.pk,
            row_ids=[self.row.pk], external_organization_guid=self.external_org, contract_guid=self.contract,
            proposed_months=[self.month])
        values.update(overrides)
        return preview_service_rows(**values)

    def test_read_only_candidate_never_proves_contract_month_payment_or_realization(self):
        with CaptureQueriesContext(connection) as captured:
            result = self.preview()
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE','REPLACE')) for q in captured))
        self.assertFalse(result.binding_allowed)
        self.assertIsNone(result.realized_amount)
        self.assertIsNone(result.paid_amount)
        self.assertEqual(result.confirmed_months, ())
        self.assertEqual(result.proposed_months, (self.month,))
        self.assertIn('contract_unverified', result.candidates[0].warnings)
        self.assertIn('consumption_registry_unavailable', result.candidates[0].warnings)
        self.assertNotIn('source_data', repr(asdict(result)))
        self.assertNotIn('gross_profit', repr(asdict(result)))
        self.assertNotIn('cost', asdict(result.candidates[0]))

    def test_duplicate_selection_and_duplicate_month_are_rejected(self):
        for changes in ({'row_ids':[self.row.pk]*2}, {'row_ids':[True]}, {'row_ids':list(range(1,52))},
                        {'proposed_months':[self.month]*2}, {'proposed_months':[date(2026,1,2)]}):
            with self.assertRaises(ValidationError): self.preview(**changes)

    def test_exact_replay_only_repeats_preview_not_consumption(self):
        old = self.preview().candidates[0].evidence
        result = self.preview(previous=[old])
        self.assertIn('unchanged_preview', result.candidates[0].warnings)
        self.assertFalse(result.binding_allowed)

    def test_changed_content_is_flagged_without_silent_reassignment(self):
        old = self.preview().candidates[0].evidence
        self.row.revenue = Decimal('101'); self.row.save()
        self.assertIn('source_version_requires_review', self.preview(previous=[old]).candidates[0].warnings)

    def test_reimport_renumbering_and_equal_lines_require_review(self):
        old = self.preview().candidates[0].evidence
        batch = self.make_batch()
        data = dict(self.row.source_data); data['line_number'] = 8
        new = self.make_row(batch, 8, **{k:v for k,v in data.items() if k!='line_number'})
        self.state.active_batch = batch; self.state.save()
        with self.assertRaises(PermissionDenied): self.preview()
        result = self.preview(row_ids=[new.pk], previous=[old])
        self.assertIn('possible_renumbering_requires_review', result.candidates[0].warnings)
        other = self.make_row(batch, 9, **{k:v for k,v in data.items() if k!='line_number'})
        result = self.preview(row_ids=[new.pk,other.pk])
        self.assertTrue(all('ambiguous_equal_content' in row.warnings for row in result.candidates))
        self.assertFalse(result.binding_allowed)

    def test_unconfirmed_foreign_and_xlsx_batches_fail_closed(self):
        for field, value in [('status','previewed'),('source_type','xlsx')]:
            previous = getattr(self.batch,field);setattr(self.batch,field,value);self.batch.save()
            with self.assertRaises(PermissionDenied):self.preview()
            setattr(self.batch,field,previous);self.batch.save()
        foreign = Organization.objects.create(name='Synthetic other org')
        self.batch.organization=foreign;self.batch.save()
        with self.assertRaises(PermissionDenied):self.preview()

    def test_source_customer_or_external_organization_mismatch_is_denied(self):
        for key in ('customer_guid','organization_guid'):
            data=dict(self.row.source_data);self.row.source_data={**data,key:str(uuid4())};self.row.save()
            with self.assertRaises(PermissionDenied):self.preview()
            self.row.source_data=data;self.row.save()

    def test_mapping_and_finance_rights_are_reread(self):
        self.preview()
        self.mapping.confirmed_at=None;self.mapping.save()
        with self.assertRaises(PermissionDenied):self.preview()
        self.mapping.confirmed_at=timezone.now();self.mapping.save()
        self.membership.role='manager';self.membership.save()
        with self.assertRaises(PermissionDenied):self.preview()

    def test_inactive_actor_and_foreign_deal_prior_evidence_are_denied(self):
        old=self.preview().candidates[0].evidence
        with self.assertRaises(PermissionDenied):self.preview(previous=[replace(old,deal_id=old.deal_id+1)])
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.preview()

    def test_future_document_and_missing_date_never_cover_period(self):
        self.row.source_data={**self.row.source_data,'document_date':'2100-01-01'};self.row.save()
        self.assertIn('future_document',self.preview().candidates[0].warnings)
        self.row.source_data.pop('document_date');self.row.save()
        self.assertIn('document_date_unavailable',self.preview().candidates[0].warnings)
        self.assertEqual(self.preview().confirmed_months,())

    def test_barnaul_today_boundary_and_cost_only_warning(self):
        self.row.source_data={**self.row.source_data,'document_date':'2026-05-01','row_kind':'direct_order_expense'};self.row.save()
        with patch('pool_service.services.crm_service_realization_preview.timezone.now',return_value=datetime(2026,4,30,18,tzinfo=dt_timezone.utc)):
            result=self.preview()
        self.assertNotIn('future_document',result.candidates[0].warnings)
        self.assertIn('cost_only_not_realization',result.candidates[0].warnings)

    def test_arbitrary_contract_is_not_verified_and_large_source_is_rejected(self):
        result=self.preview(contract_guid=str(uuid4()))
        self.assertFalse(result.binding_allowed)
        self.assertIn('contract_unverified',result.candidates[0].warnings)
        self.row.source_data={**self.row.source_data,'extra':'x'*65536};self.row.save()
        with self.assertRaises(ValidationError):self.preview()

    def test_unrelated_malformed_mapping_is_not_loaded(self):
        OneCCustomerIdentity.objects.create(organization=self.org, client=self.client_record,
            onec_customer_id='historical non-GUID mapping', confirmed_at=timezone.now(), confirmed_by=self.actor)
        self.assertFalse(self.preview().binding_allowed)

    def test_utf8_size_limit_counts_bytes(self):
        self.row.source_data={**self.row.source_data,'extra':'я'*33000};self.row.save()
        with self.assertRaises(ValidationError):self.preview()

    def test_crm_grant_revocation_on_stale_actor_is_denied(self):
        self.preview()
        grant=CrmAccess.objects.get(organization=self.org,user=self.actor)
        grant.view_team_deals=False;grant.save()
        with self.assertRaises(PermissionDenied):self.preview()

    def test_batch_switch_or_mapping_revocation_during_read_is_detected(self):
        from pool_service.services import crm_service_realization_preview as service
        original=service._hash
        batch=self.make_batch()
        switched=False
        def switching(value):
            nonlocal switched
            if not switched:
                switched=True
                self.state.active_batch=batch;self.state.save()
            return original(value)
        with patch.object(service,'_hash',side_effect=switching):
            with self.assertRaises(PermissionDenied):self.preview()
        self.state.active_batch=self.batch;self.state.save()
        switched=False
        def revoke(value):
            nonlocal switched
            if not switched:
                switched=True;self.mapping.confirmed_at=None;self.mapping.save()
            return original(value)
        with patch.object(service,'_hash',side_effect=revoke):
            with self.assertRaises(PermissionDenied):self.preview()
