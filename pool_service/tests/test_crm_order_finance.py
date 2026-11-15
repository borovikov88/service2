"""Synthetic source evidence, permissions, replay and financial completion gates."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.crm_commercial_models import CrmDeal, CrmEvent, CrmPipeline
from pool_service.crm_order_models import CrmDealOrderLink, CrmOneCOrder, CrmOrderEvidenceSnapshot, CrmOrderLinkEvent, CrmOrderSyncState
from pool_service.finance_imports.odata_crm_orders import CrmOrderEvidence, CrmOrdersSnapshot, source_identity
from pool_service.models import Organization, OrganizationAccess, ServiceTask, Pool
from pool_service.services.crm_order_finance import (apply_crm_order_snapshot, execute_order_command, finance_projection,
    mark_crm_order_sync_error, order_candidates)
from pool_service.services.crm_order_finance import refresh_order_candidates
from pool_service.services.crm_commercial_commands import CommercialConflict
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture

EXTERNAL_ORG = '11111111-1111-4111-8111-111111111111'
PARTY = '22222222-2222-4222-8222-222222222222'
AGREEMENT = '33333333-3333-4333-8333-333333333333'
CURRENCY = '44444444-4444-4444-8444-444444444444'
ORDER = '55555555-5555-4555-8555-555555555555'


class OrderFinanceFixture(PilotFixture):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['plan_type', 'paid_until'])
        self.member.role = 'owner'
        self.member.save(update_fields=['role'])
        self.grant.view_team_deals = True
        self.grant.save(update_fields=['view_team_deals'])
        self.source_settings = self.settings(ONEC_ODATA_BASE_URL='https://synthetic.example.test/odata/standard.odata/',
            ONEC_ODATA_USERNAME='synthetic', ONEC_ODATA_PASSWORD='synthetic-not-secret',
            ONEC_ODATA_ORGANIZATION_GUIDS=(EXTERNAL_ORG,), ONEC_ODATA_TARGET_ORGANIZATION_ID=self.org.pk,
            ONEC_ODATA_TIMEOUT_SECONDS=5, ONEC_ODATA_MAX_PAGES=20, ONEC_ODATA_MAX_ROWS=1000)
        self.source_settings.enable()
        self.addCleanup(self.source_settings.disable)
        ClientCRMProfile.objects.create(client=self.client_record, source='onec', onec_ref=PARTY,
            last_synced_at=timezone.now()-timedelta(minutes=1))
        self.observed = timezone.now()-timedelta(minutes=3)

    def evidence(self, **changes):
        values = dict(order_guid=ORDER, organization_guid=EXTERNAL_ORG, customer_guid=PARTY,
            agreement_guid=AGREEMENT, currency_guid=CURRENCY, currency_code='RUB', number='SYN-001',
            order_date=self.observed-timedelta(days=5), amount=Decimal('1000'), realized_amount=Decimal('600'),
            paid_amount=Decimal('400'), realization_complete=True, payment_complete=True, order_valid=True,
            cancelled=False, blockers=(), documents=({'kind':'realization','number':'SYN-INVOICE',
                'date':(self.observed-timedelta(days=2)).isoformat(),'amount':'600.00','paid_amount':'400.00'},
                {'kind':'payment','number':'SYN-PAYMENT','date':(self.observed-timedelta(days=1)).isoformat(),'amount':'400.00'}))
        values.update(changes)
        fingerprint = hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()
        return CrmOrderEvidence(fingerprint=fingerprint, **values)

    def store_orders(self, *rows, **kwargs):
        self.observed += timedelta(seconds=1)
        snapshot = CrmOrdersSnapshot(source_at=self.observed, fetched_at=self.observed,
            source_id=source_identity(), rows=tuple(rows or [self.evidence()]), diagnostics={}, **kwargs)
        return apply_crm_order_snapshot(actor=self.user, organization=self.org, snapshot=snapshot)

    def prepare_order_deal(self, **row_changes):
        self.store_orders(self.evidence(**row_changes))
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        return deal, CrmOneCOrder.objects.get(order_guid=ORDER)

    def bind(self, deal, order, **kwargs):
        values = dict(actor=self.user, organization=self.org, operation_key='order-bind', command_type='deal.bind_order',
            subject_id=deal.pk, expected_revision=deal.revision, changes={'order_id':order.pk,'recurring':False})
        values.update(kwargs)
        return execute_order_command(**values)


class OrderFinanceTests(OrderFinanceFixture, TestCase):
    def test_partial_values_are_independent_and_link_replay_does_not_duplicate(self):
        deal, order = self.prepare_order_deal()
        result = self.bind(deal, order)
        self.assertEqual(self.bind(deal, order), result)
        deal.refresh_from_db()
        projection = finance_projection(deal, self.user, self.org)
        self.assertEqual((projection['completion_percent'], projection['payment_percent']), ('60.00','40.00'))
        self.assertEqual(deal.outcome, 'open')
        self.assertEqual(CrmOrderLinkEvent.objects.count(), 1)
        self.assertEqual(CrmDealOrderLink.objects.count(), 1)

    def test_full_fact_auto_completes_and_preserves_open_task(self):
        deal, order = self.prepare_order_deal(realized_amount=Decimal('1000'), paid_amount=Decimal('1000'))
        task = deal.next_action.task_link.task
        self.bind(deal, order)
        deal.refresh_from_db(); task.refresh_from_db()
        self.assertEqual((deal.outcome, deal.stage.code), ('won','completed'))
        self.assertIsNotNone(deal.completed_at); self.assertIsNotNone(deal.settled_at)
        self.assertLess(deal.completed_at, order.source_at)
        self.assertLess(deal.settled_at, order.source_at)
        self.assertIsNone(task.completed_at); self.assertFalse(task.is_archived)
        self.assertEqual(deal.next_action.task_link.task_id, task.pk)
        projection = finance_projection(deal,self.user,self.org)
        self.assertTrue(projection['completed_automatically']); self.assertTrue(projection['open_action_preserved'])
        self.assertEqual(CrmEvent.objects.filter(event_type='deal.onec_completed').count(),1)

    def test_snapshot_refresh_completes_bound_order_once(self):
        deal, order = self.prepare_order_deal()
        self.bind(deal,order)
        full = self.evidence(realized_amount=Decimal('1000'), paid_amount=Decimal('1000'))
        self.assertEqual(self.store_orders(full)['completed'],1)
        self.assertEqual(self.store_orders(full)['completed'],0)
        self.assertEqual(CrmEvent.objects.filter(event_type='deal.onec_completed').count(),1)

    def test_recurring_order_remains_open_at_full_coverage(self):
        deal,order = self.prepare_order_deal(realized_amount=Decimal('1000'), paid_amount=Decimal('1000'))
        self.bind(deal,order,changes={'order_id':order.pk,'recurring':True})
        deal.refresh_from_db()
        self.assertEqual(deal.outcome,'open')
        projection=finance_projection(deal,self.user,self.org)
        self.assertEqual(projection['completion_percent'],'100.00')
        self.assertFalse(projection['auto_complete'])

    def test_service_fulfillment_in_tender_defaults_to_recurring_without_moving_pipeline(self):
        self.store_orders(self.evidence(realized_amount=Decimal('1000'), paid_amount=Decimal('1000')))
        tender = CrmPipeline.objects.get(organization=self.org, code='tender')
        deal = CrmDeal.objects.get(pk=self.create(pipeline_id=tender.pk,
            card_fields={'fulfillment_kind': 'service'})['deal_id'])
        self.assertTrue(finance_projection(deal, self.user, self.org)['recurring'])
        order = CrmOneCOrder.objects.get(order_guid=ORDER)
        self.bind(deal, order, changes={'order_id': order.pk, 'recurring': True})
        deal.refresh_from_db()
        self.assertEqual((deal.pipeline_id, deal.outcome), (tender.pk, 'open'))

    def test_existing_service_contract_rejects_other_agreement_and_marks_changed_binding_review(self):
        from pool_service.crm_service_binding_models import ServiceContractBinding
        from pool_service.services.crm_service_bindings import _hash
        from pool_service.finance_imports.odata_finance_position import config_from_settings
        deal, order = self.prepare_order_deal()
        pool = Pool.objects.create(organization=self.org, client=self.client_record, address='Synthetic object')
        deal.object_kind = 'pool'; deal.pool = pool; deal.save()
        binding = ServiceContractBinding.objects.create(organization=self.org, deal=deal, client=self.client_record,
            source_id=_hash(config_from_settings().base_url), external_organization=EXTERNAL_ORG,
            external_client=PARTY, contract_ref='66666666-6666-4666-8666-666666666666',
            object_kind='pool', pool=pool, confirmed_by=self.user)
        with self.assertRaises(CommercialConflict):
            self.bind(deal, order)
        self.assertFalse(CrmDealOrderLink.objects.exists())
        binding.contract_ref = AGREEMENT; binding.save()
        self.bind(deal, order, changes={'order_id': order.pk, 'recurring': True})
        self.assertEqual(finance_projection(deal, self.user, self.org)['state'], 'current')
        binding.contract_ref = '66666666-6666-4666-8666-666666666666'; binding.save()
        self.assertEqual(finance_projection(deal, self.user, self.org)['state'], 'review')
        self.store_orders(self.evidence(realized_amount=Decimal('1000'), paid_amount=Decimal('1000')))
        deal.refresh_from_db(); self.assertEqual(deal.outcome, 'open')

    def test_confirmed_recurring_periods_cannot_be_bypassed_with_one_off_choice(self):
        from pool_service.crm_recurring_models import CrmRecurringDraft, CrmRecurringRevision, CrmRecurringConfirmation
        deal, order = self.prepare_order_deal()
        pool = Pool.objects.create(organization=self.org, client=self.client_record, address='Synthetic recurring object')
        draft = CrmRecurringDraft.objects.create(organization=self.org, deal=deal, client=self.client_record,
            payer=self.client_record, object_scope={'kind': 'pool', 'pool_id': pool.pk},
            contract_basis='Synthetic terms', created_by=self.user)
        revision = CrmRecurringRevision.objects.create(organization=self.org, draft=draft, revision=1,
            kind='prepare', payload={'state': 'draft', 'operational_effect': 'none'}, operation_key='synthetic-terms',
            payload_hash='a'*64, actor=self.user, reason='Synthetic recurring terms')
        CrmRecurringConfirmation.objects.create(organization=self.org, revision=revision, pool=pool,
            effective_from=timezone.localdate(), actor=self.user)
        self.assertTrue(finance_projection(deal, self.user, self.org)['recurring'])
        with self.assertRaises(ValidationError): self.bind(deal, order)
        self.assertFalse(CrmDealOrderLink.objects.exists())

    def test_missing_currency_does_not_guess_rubles_or_auto_complete(self):
        deal, order = self.prepare_order_deal(currency_code='', realized_amount=Decimal('1000'), paid_amount=Decimal('1000'))
        self.bind(deal, order); deal.refresh_from_db()
        projection = finance_projection(deal, self.user, self.org)
        self.assertEqual(deal.outcome, 'open')
        self.assertEqual(projection['completion_percent'], '100.00')
        self.assertEqual(projection['order']['currency'], '')
        self.assertFalse(projection['auto_complete'])

    def test_unknown_payment_never_becomes_zero_or_success(self):
        deal,order=self.prepare_order_deal(realized_amount=Decimal('1000'),paid_amount=None,
            payment_complete=False,blockers=('payment_unverified',))
        self.bind(deal,order);deal.refresh_from_db()
        projection=finance_projection(deal,self.user,self.org)
        self.assertEqual(projection['completion_percent'],'100.00')
        self.assertIsNone(projection['payment_percent']);self.assertIsNone(projection['paid_amount'])
        self.assertEqual(deal.outcome,'open')

    def test_refund_after_auto_success_marks_review_without_erasing_outcome_or_history(self):
        deal,order=self.prepare_order_deal(realized_amount=Decimal('1000'),paid_amount=Decimal('1000'))
        self.bind(deal,order)
        self.store_orders(self.evidence(realized_amount=Decimal('1000'),paid_amount=Decimal('500')))
        deal.refresh_from_db()
        self.assertEqual(deal.outcome,'won')
        self.assertEqual(finance_projection(deal,self.user,self.org)['state'],'review')
        self.assertEqual(CrmEvent.objects.filter(event_type='deal.onec_review').count(),1)
        self.store_orders(self.evidence(realized_amount=Decimal('1000'),paid_amount=Decimal('500')))
        self.assertEqual(CrmEvent.objects.filter(event_type='deal.onec_review').count(),1)

    def test_revoked_finance_rights_deny_replay_lookup_and_values(self):
        deal,order=self.prepare_order_deal(); self.bind(deal,order)
        self.member.role='manager';self.member.save(update_fields=['role'])
        with self.assertRaises(PermissionDenied):self.bind(deal,order)
        with self.assertRaises(PermissionDenied):order_candidates(actor=self.user,organization=self.org,deal=deal)
        projection=finance_projection(deal,self.user,self.org)
        self.assertEqual(projection['state'],'restricted');self.assertIsNone(projection['order'])

    def test_foreign_customer_order_and_missing_verified_identity_are_rejected(self):
        deal,order=self.prepare_order_deal(customer_guid='66666666-6666-4666-8666-666666666666')
        with self.assertRaises(PermissionDenied):self.bind(deal,order)
        self.assertFalse(CrmDealOrderLink.objects.exists())
        ClientCRMProfile.objects.filter(client=self.client_record).update(last_synced_at=None)
        self.assertEqual(order_candidates(actor=self.user,organization=self.org,deal=deal)['orders'],[])

    def test_same_order_cannot_be_linked_to_two_deals(self):
        deal,order=self.prepare_order_deal();self.bind(deal,order)
        second=CrmDeal.objects.get(pk=self.create(operation_key='second-deal')['deal_id'])
        with self.assertRaises(CommercialConflict):self.bind(second,order,operation_key='second-link')

    def test_replay_payload_and_revision_conflicts_are_atomic(self):
        deal,order=self.prepare_order_deal();self.bind(deal,order)
        with self.assertRaises(CommercialConflict):self.bind(deal,order,changes={'order_id':order.pk,'recurring':True})
        with self.assertRaises(CommercialConflict):self.bind(deal,order,operation_key='new-key')
        self.assertEqual(CrmOrderLinkEvent.objects.count(),1)

    def test_sync_failure_and_stale_evidence_hide_current_percentages(self):
        deal,order=self.prepare_order_deal();self.bind(deal,order)
        mark_crm_order_sync_error(actor=self.user,organization=self.org)
        result=finance_projection(deal,self.user,self.org)
        self.assertEqual(result['state'],'stale');self.assertIsNone(result['payment_percent'])
        with self.assertRaises(CommercialConflict):
            deal.refresh_from_db();self.bind(deal,order,operation_key='stale-bind')
        self.store_orders(self.evidence())
        self.assertEqual(finance_projection(deal,self.user,self.org,now=timezone.now()+timedelta(days=2))['state'],'stale')

    def test_source_change_and_identity_change_never_leak_replacement_order(self):
        deal,order=self.prepare_order_deal();self.bind(deal,order)
        self.store_orders(self.evidence(customer_guid='66666666-6666-4666-8666-666666666666',number='PRIVATE-REPLACEMENT'))
        result=finance_projection(deal,self.user,self.org)
        self.assertEqual(result['state'],'review');self.assertIsNone(result['order'])
        self.assertNotIn('PRIVATE-REPLACEMENT',str(result))

    def test_removed_external_organization_cannot_be_treated_as_current(self):
        deal, order = self.prepare_order_deal(); self.bind(deal, order)
        with self.settings(ONEC_ODATA_ORGANIZATION_GUIDS=('66666666-6666-4666-8666-666666666666',)):
            projection = finance_projection(deal, self.user, self.org)
            self.assertEqual(projection['state'], 'stale')
            self.assertIsNone(projection['payment_percent'])
            self.assertEqual(order_candidates(actor=self.user, organization=self.org, deal=deal)['orders'], [])

    def test_unlink_keeps_immutable_history_and_no_contact_deal_task_deletion(self):
        deal,order=self.prepare_order_deal();self.bind(deal,order);deal.refresh_from_db()
        execute_order_command(actor=self.user,organization=self.org,operation_key='unlink',command_type='deal.unbind_order',
            subject_id=deal.pk,expected_revision=deal.revision,changes={'reason':'Synthetic correction'})
        self.assertFalse(CrmDealOrderLink.objects.exists())
        self.assertEqual(CrmOrderLinkEvent.objects.count(),2)
        self.assertEqual(ServiceTask.objects.count(),1)
        with self.assertRaises(ValidationError):CrmOrderLinkEvent.objects.first().delete()

    def test_cancelled_order_and_currency_mismatch_cannot_close(self):
        deal,order=self.prepare_order_deal(cancelled=True)
        with self.assertRaises(CommercialConflict):self.bind(deal,order)
        self.store_orders(self.evidence(realized_amount=Decimal('1000'),paid_amount=Decimal('1000')))
        order.refresh_from_db()
        deal.amount=Decimal('1000');deal.amount_basis='agreed';deal.currency='USD';deal.save()
        self.bind(deal,order);deal.refresh_from_db()
        self.assertEqual(deal.outcome,'open')

    def test_older_snapshot_and_wrong_source_are_rejected(self):
        self.store_orders(self.evidence())
        snapshot=CrmOrdersSnapshot(self.observed-timedelta(hours=2),self.observed,source_identity(),(self.evidence(),),{})
        with self.assertRaises(ValidationError):apply_crm_order_snapshot(actor=self.user,organization=self.org,snapshot=snapshot)
        snapshot=replace(snapshot,source_at=self.observed,source_id='0'*64)
        with self.assertRaises(PermissionDenied):apply_crm_order_snapshot(actor=self.user,organization=self.org,snapshot=snapshot)

    def test_http_read_only_candidates_and_command_contract(self):
        deal,order=self.prepare_order_deal()
        browser=HttpClient();browser.force_login(self.user)
        before=CrmOrderLinkEvent.objects.count()
        response=browser.get(reverse('crm_order_candidates'),{'org_id':self.org.pk,'deal_id':deal.pk,'q':'SYN'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['orders'][0]['id'],order.pk)
        self.assertEqual(CrmOrderLinkEvent.objects.count(),before)
        response=browser.post(reverse('crm_live_command')+'?org_id='+str(self.org.pk),data=json.dumps({
            'operation_key':'http-link','command_type':'deal.bind_order','subject_id':deal.pk,
            'expected_revision':deal.revision,'changes':{'order_id':order.pk,'recurring':True}}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['deal']['onec_finance']['payment_percent'],'40.00')

    def test_partial_snapshot_does_not_invalidate_unrelated_orders(self):
        second=self.evidence(order_guid='77777777-7777-4777-8777-777777777777',number='SYN-002')
        self.store_orders(self.evidence(),second)
        self.store_orders(self.evidence(),full_scope=False,requested_order_guids=(ORDER,))
        self.assertFalse(CrmOneCOrder.objects.get(order_guid=second.order_guid).missing)

    def test_bounded_discovery_can_switch_configured_source_without_migrating_links(self):
        deal, old_order = self.prepare_order_deal(); self.bind(deal, old_order)
        with self.settings(ONEC_ODATA_BASE_URL='https://synthetic-new.example.test/odata/standard.odata/'):
            observed = self.observed + timedelta(seconds=1)
            snapshot = CrmOrdersSnapshot(observed, observed, source_identity(), (self.evidence(),), {},
                full_scope=False, requested_order_guids=(ORDER,))
            with self.assertRaises(CommercialConflict):
                apply_crm_order_snapshot(actor=self.user, organization=self.org, snapshot=snapshot)
            snapshot = replace(snapshot, diagnostics={'discovery_limited': True})
            apply_crm_order_snapshot(actor=self.user, organization=self.org, snapshot=snapshot)
            old_order.refresh_from_db()
            self.assertTrue(old_order.missing)
            self.assertEqual(CrmDealOrderLink.objects.get(deal=deal).order_id, old_order.pk)
            self.assertEqual(CrmOneCOrder.objects.filter(order_guid=ORDER).count(), 2)
            self.assertEqual(finance_projection(deal, self.user, self.org)['state'], 'review')
            self.assertEqual(CrmOrderSyncState.objects.get(organization=self.org).source_id, snapshot.source_id)

    def test_empty_bounded_discovery_does_not_invalidate_orders_outside_its_scope(self):
        self.store_orders(self.evidence())
        observed = self.observed + timedelta(seconds=1)
        snapshot = CrmOrdersSnapshot(observed, observed, source_identity(), (), {'discovery_limited': True},
            full_scope=False, requested_order_guids=())
        apply_crm_order_snapshot(actor=self.user, organization=self.org, snapshot=snapshot)
        self.assertFalse(CrmOneCOrder.objects.get(order_guid=ORDER).missing)

    def test_invalid_draft_without_customer_is_stored_but_never_offered(self):
        self.store_orders(self.evidence(order_valid=False,customer_guid=''))
        deal=CrmDeal.objects.get(pk=self.create()['deal_id'])
        self.assertEqual(order_candidates(actor=self.user,organization=self.org,deal=deal)['orders'],[])


class OrderRefreshTests(OrderFinanceFixture, TestCase):
    reader = 'pool_service.finance_imports.odata_crm_orders.read_crm_orders'

    def lookup_snapshot(self, rows=(), required=(), source_at=None, fetched_at=None):
        observed = source_at or timezone.now() - timedelta(seconds=1)
        return CrmOrdersSnapshot(observed, fetched_at or timezone.now(), source_identity(), tuple(rows),
            {'discovery_limited': True, 'discovery_mode': 'number'}, full_scope=False,
            requested_order_guids=tuple(sorted({row.order_guid for row in rows} | set(required))))

    def post_lookup(self, deal, query='SYN-OLD', browser=None, **extra):
        browser = browser or HttpClient()
        browser.force_login(self.user)
        payload = {'deal_id': deal.pk, 'q': query, **extra}
        return browser.post(reverse('crm_order_refresh') + '?org_id=' + str(self.org.pk),
            data=json.dumps(payload), content_type='application/json')

    def test_read_only_get_no_transport_and_explicit_csrf_post_discovers_old_order_without_binding(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        browser = HttpClient(enforce_csrf_checks=True); browser.force_login(self.user)
        with patch(self.reader) as reader:
            self.assertEqual(browser.get(reverse('crm_order_candidates'),
                {'org_id': self.org.pk, 'deal_id': deal.pk, 'q': 'SYN-OLD'}).status_code, 200)
            self.assertEqual(browser.get(reverse('crm_order_refresh') + '?org_id=' + str(self.org.pk)).status_code, 405)
            self.assertEqual(self.post_lookup(deal, browser=browser).status_code, 403)
            reader.assert_not_called()
        browser.get(reverse('crm_live_workspace'), {'org_id': self.org.pk})
        token = browser.cookies['csrftoken'].value
        snapshot = self.lookup_snapshot((self.evidence(number='SYN-OLD', order_date=self.observed-timedelta(days=730)),))
        with patch(self.reader, return_value=snapshot) as reader:
            response = browser.post(reverse('crm_order_refresh') + '?org_id=' + str(self.org.pk),
                data=json.dumps({'deal_id': deal.pk, 'q': 'SYN-OLD'}), content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['orders'][0]['number'], 'SYN-OLD')
        self.assertEqual(reader.call_args.kwargs['customer_guids'], (PARTY,))
        self.assertEqual(reader.call_args.kwargs['order_number'], 'SYN-OLD')
        self.assertFalse(CrmDealOrderLink.objects.exists())
        deal.refresh_from_db(); self.assertEqual(deal.revision, 1)

    def test_rights_revoked_during_transport_prevent_any_cache_write(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        snapshot = self.lookup_snapshot((self.evidence(number='SYN-OLD'),))
        def revoke(**kwargs):
            self.member.role = 'manager'; self.member.save(update_fields=['role'])
            return snapshot
        with patch(self.reader, side_effect=revoke):
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CrmOneCOrder.objects.exists())
        self.assertFalse(CrmOrderSyncState.objects.exists())

    def test_wrong_owner_and_missing_finance_rights_prevent_transport(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        other = get_user_model().objects.create_user('synthetic-other-order-owner')
        OrganizationAccess.objects.create(organization=self.org, user=other, role='manager')
        deal.manager = other; deal.save()
        with patch(self.reader) as reader:
            self.assertEqual(self.post_lookup(deal).status_code, 403)
            deal.manager = self.user; deal.save()
            self.member.role = 'manager'; self.member.save(update_fields=['role'])
            self.assertEqual(self.post_lookup(deal).status_code, 403)
            reader.assert_not_called()

    def test_post_rejects_customer_source_inputs_and_invalid_queries_before_transport(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        with patch(self.reader) as reader:
            for extra in ({'customer_guids': [PARTY]}, {'organization_guid': EXTERNAL_ORG}, {'url': 'https://invalid.example/'}):
                self.assertEqual(self.post_lookup(deal, **extra).status_code, 400)
            for query in ('', 'x'*81, '\nSYN-OLD', None):
                self.assertEqual(self.post_lookup(deal, query=query).status_code, 400)
            reader.assert_not_called()

    def test_absent_old_cached_order_is_invalidated_and_not_returned_as_verified(self):
        deal, old = self.prepare_order_deal(number='SYN-OLD')
        snapshot = self.lookup_snapshot(required=(ORDER,))
        with patch(self.reader, return_value=snapshot) as reader:
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(reader.call_args.kwargs['required_order_guids'], (ORDER,))
        self.assertEqual(response.json()['orders'], [])
        old.refresh_from_db(); self.assertTrue(old.missing)
        self.assertEqual(order_candidates(actor=self.user, organization=self.org, deal=deal, query='SYN-OLD')['orders'], [])

    def test_renamed_known_order_updates_cache_but_does_not_match_old_number(self):
        deal, old = self.prepare_order_deal(number='SYN-OLD')
        snapshot = self.lookup_snapshot((self.evidence(number='SYN-NEW'),), required=(ORDER,))
        with patch(self.reader, return_value=snapshot):
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['orders'], [])
        old.refresh_from_db(); self.assertEqual(old.number, 'SYN-NEW')

    def test_exact_lookup_does_not_revive_a_failed_general_refresh(self):
        deal, old = self.prepare_order_deal(number='SYN-OLD')
        mark_crm_order_sync_error(actor=self.user, organization=self.org)
        with patch(self.reader, return_value=self.lookup_snapshot((self.evidence(number='SYN-OLD'),), required=(ORDER,))):
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['orders'], [])
        self.assertTrue(response.json()['reason'])
        self.assertTrue(CrmOrderSyncState.objects.get(organization=self.org).failed)

    def test_source_failure_is_sanitized_and_retryable(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        with patch(self.reader, side_effect=RuntimeError('private upstream credential and customer details')):
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'source_unavailable')
        self.assertNotIn('private', response.content.decode())
        self.assertFalse(CrmOneCOrder.objects.exists())

    def test_failed_recheck_with_known_order_withdraws_freshness_before_any_bind(self):
        deal, order = self.prepare_order_deal(number='SYN-OLD', realized_amount=Decimal('1000'), paid_amount=Decimal('1000'))
        snapshots = CrmOrderEvidenceSnapshot.objects.count()
        with patch(self.reader, side_effect=RuntimeError('private source scope changed')):
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 503)
        order.refresh_from_db(); self.assertTrue(order.missing)
        self.assertEqual(order.paid_amount, Decimal('1000'))
        self.assertEqual(CrmOrderEvidenceSnapshot.objects.count(), snapshots)
        self.assertEqual(order_candidates(actor=self.user, organization=self.org, deal=deal, query='SYN-OLD')['orders'], [])
        with self.assertRaises(CommercialConflict): self.bind(deal, order)
        deal.refresh_from_db(); self.assertEqual(deal.outcome, 'open')

    def test_source_failure_after_revocation_cannot_invalidate_cached_order(self):
        deal, order = self.prepare_order_deal(number='SYN-OLD')
        def revoke_and_fail(**kwargs):
            self.member.role = 'manager'; self.member.save(update_fields=['role'])
            raise RuntimeError('private source failure')
        with patch(self.reader, side_effect=revoke_and_fail):
            response = self.post_lookup(deal)
        self.assertEqual(response.status_code, 403)
        order.refresh_from_db(); self.assertFalse(order.missing)

    def test_failed_unknown_lookup_does_not_invalidate_other_cached_orders(self):
        deal, order = self.prepare_order_deal(number='SYN-CURRENT')
        with patch(self.reader, side_effect=RuntimeError('private unknown lookup failure')):
            self.assertEqual(self.post_lookup(deal, query='SYN-UNKNOWN').status_code, 503)
        order.refresh_from_db(); self.assertFalse(order.missing)
        self.assertEqual(len(order_candidates(actor=self.user, organization=self.org, deal=deal)['orders']), 1)

    def test_reader_start_and_apply_completion_use_separate_clock_instants(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        finished = timezone.now(); started = finished - timedelta(seconds=20)
        moments = iter((started, finished))
        snapshot = self.lookup_snapshot((self.evidence(number='SYN-OLD'),), source_at=started, fetched_at=finished)
        with patch(self.reader, return_value=snapshot) as reader, patch(
                'pool_service.services.crm_order_finance.apply_crm_order_snapshot', wraps=apply_crm_order_snapshot) as apply:
            refresh_order_candidates(actor=self.user, organization=self.org, deal_id=deal.pk, query='SYN-OLD', clock=lambda: next(moments))
        self.assertEqual(reader.call_args.kwargs['now'], started)
        self.assertEqual(apply.call_args.kwargs['now'], finished)
