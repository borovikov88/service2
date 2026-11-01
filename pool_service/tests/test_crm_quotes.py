"""Synthetic immutable quote decisions, no external transmission or execution."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, Client as HttpClient, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_commercial_commands import CommandFixture
from pool_service.tests.test_estimate_documents import fixture
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask
from pool_service.estimate_models import PoolEstimate, PoolEstimateVersion
from pool_service.crm_quote_models import CrmQuote, CrmQuoteEvent
from pool_service.crm_commercial_models import CrmCommand, CrmDeal, CrmEvent
from pool_service.services import crm_quotes as quotes
from pool_service.services.crm_commercial_commands import CommercialConflict


class QuoteFixture(CommandFixture):
    def setUp(self):
        super().setUp()
        self.org.plan_type=Organization.PLAN_COMPANY_PAID
        self.org.paid_until=timezone.now()+timedelta(days=30);self.org.save()
        self.customer=Client.objects.create(organization=self.org,name='Synthetic quote customer')
        self.deal.client=self.customer;self.deal.save()
        self.estimate=PoolEstimate.objects.create(organization=self.org,title='Synthetic quote',created_by=self.user,revision=1)
        self.version=PoolEstimateVersion.objects.create(estimate=self.estimate,number=1,snapshot=fixture(),created_by=self.user)
        self.http=HttpClient();self.http.force_login(self.user)
        self.url=reverse('crm_quotes',args=[self.deal.pk])+'?org_id='+str(self.org.pk)

    def run_command(self,action,data,**overrides):
        self.deal.refresh_from_db()
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,operation_key=uuid4().hex,
            expected_revision=self.deal.revision,action=action,data=data)
        args.update(overrides)
        return quotes.execute(**args)

    def prepare(self,variant='economy',version=None):
        version=version or self.version
        return self.run_command('prepare',dict(version_id=version.pk,variant=variant,source_hash=quotes.digest(version.snapshot),confirm_scope=True))

    def accept(self,quote_id,**more):
        return self.run_command('accept',dict(quote_id=quote_id,evidence='Synthetic explicit customer agreement',confirm_amount=True,**more))


class QuoteTests(QuoteFixture,TestCase):
    def test_accepted_variant_is_frozen_and_no_tasks_execution_or_money_events(self):
        before_tasks=ServiceTask.objects.count();stage=self.deal.stage_id
        result=self.prepare();self.accept(result['quote_id'])
        self.deal.refresh_from_db();quote=CrmQuote.objects.get(pk=result['quote_id'])
        self.assertEqual(self.deal.amount,Decimal(self.version.snapshot['variants']['economy']['total']))
        self.assertNotEqual(self.deal.amount,sum(Decimal(v['total']) for v in self.version.snapshot['variants'].values()))
        self.assertEqual((self.deal.stage_id,self.deal.outcome,ServiceTask.objects.count()),(stage,'open',before_tasks))
        newer=deepcopy(self.version.snapshot);newer['variants']['economy']['total']='20000.00'
        PoolEstimateVersion.objects.create(estimate=self.estimate,number=2,snapshot=newer)
        quote.refresh_from_db();self.assertEqual(quote.amount,Decimal('10000'))
        self.assertEqual(quotes.source(quote,self.deal),self.version.snapshot)
        response=self.http.get(self.url)
        self.assertContains(response,'Основание для подготовки передачи')
        self.assertNotContains(response,'PREMIUM-SECRET')
        self.assertNotContains(response,'INTERNAL-CHECK-SECRET')

    def test_preview_print_and_get_never_mark_sent_or_create_quote(self):
        response=self.http.get(self.url+'&version='+str(self.version.pk)+'&variant=economy')
        self.assertEqual(response.status_code,200)
        self.assertEqual(CrmQuote.objects.count(),0)
        result=self.prepare();before=CrmQuoteEvent.objects.count()
        self.assertEqual(self.http.get(reverse('pool_estimate_document',args=[self.estimate.pk,1])+'?variants=economy').status_code,200)
        self.http.get(self.url)
        self.assertEqual(CrmQuoteEvent.objects.count(),before)
        self.assertEqual(CrmQuote.objects.get(pk=result['quote_id']).status,'prepared')

    def test_manual_send_unknown_is_not_delivery_and_replay_is_single_event(self):
        result=self.prepare()
        data=dict(quote_id=result['quote_id'],recipient='Synthetic recipient',channel='manual email',
            occurred_at=(timezone.now()-timedelta(minutes=1)).isoformat(),evidence='Synthetic external receipt',outcome='unknown')
        self.deal.refresh_from_db();revision=self.deal.revision
        sent=self.run_command('send',data,operation_key='send-once',expected_revision=revision)
        self.assertEqual(CrmQuote.objects.get(pk=sent['quote_id']).status,'sending_unknown')
        self.assertEqual(sent,self.run_command('send',data,operation_key='send-once',expected_revision=revision))
        self.assertEqual(CrmQuoteEvent.objects.count(),2)
        self.run_command('send',{**data,'outcome':'sent'})
        self.assertEqual(CrmQuote.objects.get(pk=sent['quote_id']).status,'sent')

    def test_replacement_is_explicit_and_atomic(self):
        first=self.prepare();second=self.prepare('standard')
        self.accept(first['quote_id'])
        with self.assertRaises(CommercialConflict):self.accept(second['quote_id'])
        self.accept(second['quote_id'],replace_quote_id=first['quote_id'])
        self.assertEqual(CrmQuote.objects.filter(status='accepted').count(),1)
        self.assertEqual(CrmQuote.objects.get(pk=first['quote_id']).status,'replaced')
        self.assertEqual(CrmQuoteEvent.objects.filter(quote_id=first['quote_id'],action='replace').count(),1)
        third=self.prepare('premium')
        self.run_command('reject',dict(quote_id=third['quote_id'],evidence='Synthetic rejection'))
        with self.assertRaises(CommercialConflict):self.accept(third['quote_id'],replace_quote_id=second['quote_id'])

    def test_source_or_context_change_blocks_agreement_without_recalculation(self):
        result=self.prepare()
        changed=deepcopy(self.version.snapshot);changed['variants']['economy']['total']='999999.00'
        PoolEstimateVersion.objects.filter(pk=self.version.pk).update(snapshot=changed)
        with self.assertRaises(CommercialConflict):self.accept(result['quote_id'])
        response=self.http.get(self.url)
        self.assertContains(response,'Сохранённый источник изменён')
        self.assertNotContains(response,'999999')
        PoolEstimateVersion.objects.filter(pk=self.version.pk).update(snapshot=self.version.snapshot)
        self.deal.client=Client.objects.create(organization=self.org,name='Synthetic changed client');self.deal.save()
        with self.assertRaises(CommercialConflict):self.accept(result['quote_id'])

    def test_foreign_version_own_card_rights_revocation_and_csrf(self):
        foreign=Organization.objects.create(name='Synthetic foreign quote')
        estimate=PoolEstimate.objects.create(organization=foreign,title='Hidden')
        version=PoolEstimateVersion.objects.create(estimate=estimate,number=1,snapshot=fixture())
        with self.assertRaises(PermissionDenied):self.prepare(version=version)
        result=self.prepare();self.grant.edit_own_deals=False;self.grant.save()
        with self.assertRaises(PermissionDenied):self.accept(result['quote_id'])
        self.assertEqual(self.http.get(self.url).status_code,200)
        self.assertNotContains(self.http.get(self.url),'Подтвердить принятие КП')
        self.employee.is_active=False;self.employee.save()
        self.assertEqual(self.http.get(self.url).status_code,403)
        csrf=HttpClient(enforce_csrf_checks=True);csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url,{}).status_code,403)

    def test_unknown_total_incomplete_version_stale_revision_and_legacy_currency(self):
        result=self.prepare()
        with self.assertRaises(CommercialConflict):
            self.run_command('accept',dict(quote_id=result['quote_id'],evidence='Synthetic stale',confirm_amount=True),expected_revision=0)
        # run_command override belongs outside the decision data.
        self.deal.refresh_from_db();self.deal.currency='USD';self.deal.amount=Decimal('1');self.deal.amount_basis='indicative';self.deal.save()
        with self.assertRaises(ValidationError):self.accept(result['quote_id'])
        self.deal.currency='RUB';self.deal.save()
        changed=deepcopy(self.version.snapshot);changed['variants']['economy']['missing_prices']=1
        second=PoolEstimateVersion.objects.create(estimate=self.estimate,number=2,snapshot=changed)
        incomplete=self.prepare(version=second)
        with self.assertRaises(ValidationError):self.accept(incomplete['quote_id'])
        self.assertFalse(CrmQuote.objects.filter(status='accepted').exists())

    def test_malformed_frozen_amount_and_rows_are_rejected_without_writes(self):
        for mutation in ('total','row'):
            changed=deepcopy(self.version.snapshot)
            if mutation=='total': changed['variants']['economy']['total']='99999.00'
            else:
                row=next(row for block in changed['variants']['economy']['blocks'] if not block['excluded'] for row in block['rows'] if not row['excluded'] and not row['existing'])
                row['price']='NaN'
            version=PoolEstimateVersion.objects.create(estimate=self.estimate,number=2 if mutation=='total' else 3,snapshot=changed)
            with self.assertRaises(ValidationError): self.prepare(version=version)
            response=self.http.get(self.url+'&version='+str(version.pk)+'&variant=economy')
            self.assertEqual(response.status_code,400)
        self.assertFalse(CrmQuote.objects.exists())

    def test_frozen_total_validation_does_not_depend_on_decimal_context(self):
        changed=deepcopy(self.version.snapshot)
        blocks=changed['variants']['economy']['blocks']
        block=next(block for block in blocks if not block['excluded'] and any(not row['excluded'] and not row['existing'] for row in block['rows']))
        row=deepcopy(next(row for row in block['rows'] if not row['excluded'] and not row['existing']))
        row['amount']='1.00';block['rows'].append(row)
        version=PoolEstimateVersion(estimate=self.estimate,number=2,snapshot=changed)
        with localcontext() as ctx:
            ctx.prec=2
            with self.assertRaises(ValidationError): quotes._amount(version,'economy')
        row['amount']='1e-30';version.snapshot=changed
        with self.assertRaises(ValidationError): quotes._amount(version,'economy')

    def test_signed_preview_scope_and_immutable_evidence(self):
        from django.core import signing
        from pool_service.crm_quote_views import SALT
        response=self.http.get(self.url+'&version='+str(self.version.pk)+'&variant=economy')
        token=response.context['preview']['selection']
        values=dict(action='prepare',selection=token,confirm_scope='on',operation_key='html-once')
        self.assertEqual(self.http.post(self.url,values).status_code,302)
        self.assertEqual(self.http.post(self.url,values).status_code,302)
        self.assertEqual(CrmQuote.objects.count(),1)
        forged=signing.loads(token,salt=SALT);forged['deal']+=1
        self.assertEqual(self.http.post(self.url,{**values,'selection':signing.dumps(forged,salt=SALT)}).status_code,403)
        quote=CrmQuote.objects.get();quote.variant='premium'
        with self.assertRaises(ValidationError):quote.save()
        with self.assertRaises(ValidationError):CrmQuoteEvent.objects.all().delete()


@skipUnlessDBFeature('has_select_for_update')
class QuoteConcurrencyTests(QuoteFixture,TransactionTestCase):
    def parallel(self,functions):
        barrier=Barrier(2)
        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            except (CommercialConflict,ValidationError) as error:return type(error).__name__
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:return list(executor.map(run,functions))

    def test_two_accepts_one_winner_and_no_partial_replacement(self):
        first=self.prepare();second=self.prepare('standard');self.deal.refresh_from_db();revision=self.deal.revision
        def accept(identifier):
            return quotes.execute(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                operation_key=uuid4().hex,expected_revision=revision,action='accept',
                data=dict(quote_id=identifier,evidence='Synthetic concurrent approval',confirm_amount=True))
        results=self.parallel([lambda:accept(first['quote_id']),lambda:accept(second['quote_id'])])
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(CrmQuote.objects.filter(status='accepted').count(),1)
        self.assertEqual(CrmQuoteEvent.objects.filter(action='accept').count(),1)

    def test_same_prepare_key_is_idempotent(self):
        data=dict(version_id=self.version.pk,variant='economy',source_hash=quotes.digest(self.version.snapshot),confirm_scope=True)
        def prepare():
            return quotes.execute(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                operation_key='parallel-prepare',expected_revision=0,action='prepare',data=data)
        results=self.parallel([prepare,prepare])
        self.assertEqual(results[0],results[1]);self.assertIsInstance(results[0],dict)
        self.assertEqual(CrmQuote.objects.count(),1)
