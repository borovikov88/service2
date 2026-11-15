"""Chromium acceptance of saved order evidence. No live 1C is contacted."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
from uuid import UUID

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmDeal, CrmPipeline
from pool_service.crm_order_models import CrmDealOrderLink, CrmOneCOrder, CrmOrderLinkEvent
from pool_service.finance_imports.odata_crm_orders import CrmOrderEvidence, CrmOrdersSnapshot, source_identity
from pool_service.models import Organization, ServiceTask
from pool_service.reward_models import OneCCustomerIdentity
from pool_service.services.crm_order_finance import apply_crm_order_snapshot
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.tests.test_odata_crm_orders import AGREEMENT, CURRENCY, CUSTOMER, ORG, config


def synthetic_order(index, *, number, realized, paid, now=None):
    """Only demonstration identities/amounts; shared with the local visual preview."""
    now = now or timezone.now()
    return CrmOrderEvidence(
        order_guid=str(UUID(int=1000 + index)), organization_guid=ORG, customer_guid=CUSTOMER,
        agreement_guid=AGREEMENT, currency_guid=CURRENCY, currency_code='RUB', number=number,
        order_date=now - timedelta(days=10), amount=Decimal('100000.00'),
        realized_amount=Decimal(realized), paid_amount=Decimal(paid), realization_complete=True,
        payment_complete=True, order_valid=True, cancelled=False, blockers=(),
        documents=({'kind': 'realization', 'number': 'РН-DEMO-' + str(index),
                    'date': (now - timedelta(days=2)).isoformat(),
                    'amount': realized, 'paid_amount': paid},
                   {'kind': 'payment', 'number': 'ПП-DEMO-' + str(index),
                    'date': (now - timedelta(days=1)).isoformat(), 'amount': paid}),
        fingerprint=hashlib.sha256(('synthetic-order-' + str(index) + realized + paid).encode()).hexdigest())


@skipUnless(os.environ.get('CRM_CARD_BROWSER_TEST') == '1', 'Dedicated Chromium acceptance enables this test')
class CrmOneCFinanceBrowserTests(PilotFixture, StaticLiveServerTestCase):
    def test_order_binding_progress_recurring_success_and_unlink(self):
        self.member.role = 'owner'
        self.member.save(update_fields=['role'])
        self.grant.view_team_deals = True
        self.grant.save(update_fields=['view_team_deals'])
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['plan_type', 'paid_until'])
        now = timezone.now()
        OneCCustomerIdentity.objects.create(organization=self.org, client=self.client_record,
            onec_customer_id=CUSTOMER, confirmed_by=self.user, confirmed_at=now)
        configurations = [
            ('service', 'Synthetic recurring service', True, 'DEMO-SERVICE-001', '60000.00', '40000.00', '60', '40', 'open'),
            ('tender', 'Synthetic service in tender pipeline', True, 'DEMO-SERVICE-002', '100000.00', '100000.00', '100', '100', 'open'),
            ('construction', 'Synthetic one-off completed order', False, 'DEMO-PROJECT-003', '100000.00', '100000.00', '100', '100', 'won'),
        ]
        rows, cases, tasks = [], [], []
        for index, (pipeline, title, recurring, number, realized, paid, completion, payment, outcome) in enumerate(configurations):
            created = self.create(operation_key='browser-onec-' + str(index), title=title,
                pipeline_id=CrmPipeline.objects.get(organization=self.org, code=pipeline).pk,
                card_fields={'fulfillment_kind': 'service' if recurring else 'construction'})
            tasks.append(created['task_id'])
            rows.append(synthetic_order(index, number=number, realized=realized, paid=paid, now=now))
            cases.append(dict(dealId=created['deal_id'], orderNumber=number, recurring=recurring,
                completionPercent=completion, paymentPercent=payment, expectedOutcome=outcome, requiresRefresh=index == 0))
        with override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=self.org.pk,
                ONEC_ODATA_BASE_URL=config().base_url, ONEC_ODATA_ORGANIZATION_GUIDS=(ORG,),
                ONEC_ODATA_USERNAME='synthetic', ONEC_ODATA_PASSWORD='synthetic'):
            snapshot = CrmOrdersSnapshot(source_at=now, fetched_at=now, source_id=source_identity(config()),
                rows=tuple(rows[1:]), diagnostics={})
            apply_crm_order_snapshot(actor=self.user, organization=self.org, snapshot=snapshot, config=config(), now=now)
            for case in cases:
                order = CrmOneCOrder.objects.filter(organization=self.org, number=case['orderNumber']).first()
                case['orderId'] = order.pk if order else None
            self.client.force_login(self.user)
            fixture = dict(base=self.live_server_url,
                workspace=reverse('crm_live_workspace') + '?org_id=' + str(self.org.pk), cases=cases,
                sessionCookieName=settings.SESSION_COOKIE_NAME,
                sessionCookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value)
            def exact_source(**kwargs):
                self.assertEqual(kwargs['order_number'], rows[0].number)
                self.assertIn(CUSTOMER, kwargs['customer_guids'])
                return CrmOrdersSnapshot(source_at=kwargs['now'], fetched_at=timezone.now(), source_id=source_identity(config()),
                    rows=(rows[0],), diagnostics={'discovery_limited': True, 'discovery_mode': 'number'},
                    full_scope=False, requested_order_guids=(rows[0].order_guid,))

            with patch('pool_service.finance_imports.odata_crm_orders.read_crm_orders', side_effect=exact_source) as source:
                code, output = _bounded_browser_process(['node', str(Path(__file__).with_name('crm_onec_finance.browser.cjs'))], fixture)
            self.assertEqual(code, 0, output)
            self.assertEqual(source.call_count, 1, 'Only the explicit exact-number POST may read 1C')
        for case in cases[:-1]:
            recurring = CrmDeal.objects.get(pk=case['dealId'])
            self.assertEqual(recurring.outcome, 'open')
            self.assertFalse(CrmDealOrderLink.objects.filter(deal=recurring).exists())
            self.assertEqual(CrmOrderLinkEvent.objects.filter(deal=recurring, kind='deal.unbind_order').count(), 1)
        once = CrmDeal.objects.get(pk=cases[-1]['dealId'])
        self.assertEqual(once.outcome, 'won')
        self.assertEqual(once.stage.code, 'completed')
        self.assertTrue(CrmDealOrderLink.objects.get(deal=once).completed_automatically)
        self.assertEqual(ServiceTask.objects.filter(pk__in=tasks, is_archived=False, completed_at__isnull=True).count(), 3)
