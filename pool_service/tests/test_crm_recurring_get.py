"""GET eligibility with production error handling, outside TestCase transactions."""
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.core.exceptions import ValidationError, PermissionDenied
from pool_service.tests.test_crm_recurring_confirmed import ConfirmedFixture
from pool_service.crm_recurring_models import CrmRecurringDraft, CrmRecurringRevision, CrmRecurringConfirmation
from pool_service.services import crm_recurring_drafts as service


@override_settings(DEBUG=False)
class RecurringGetTests(ConfirmedFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.url=reverse('crm_recurring',args=[self.deal.pk])+'?org_id='+str(self.org.pk)

    def assert_blocked(self, response, status=200):
        self.assertContains(response,'data-recurring-blocked',status_code=status)
        self.assertNotContains(response,'data-draft-form',status_code=status)
        self.assertNotContains(response,'data-confirm-panel',status_code=status)
        for model in (CrmRecurringDraft,CrmRecurringRevision,CrmRecurringConfirmation):
            self.assertEqual(model.objects.count(),0)

    def test_empty_eligible_existing_contract_renders(self):
        self.assertContains(self.http.get(self.url),'data-draft-form')
        self.assertEqual(CrmRecurringDraft.objects.count(),0)

    def test_existing_incomplete_service_deal_get_is_explained(self):
        self.deal.object_kind='none';self.deal.pool=None;self.deal.save()
        self.assertContains(self.http.get(reverse('crm_quotes',args=[self.deal.pk])+'?org_id='+str(self.org.pk)),'Договор обслуживания')
        self.assert_blocked(self.http.get(self.url))
        self.assert_blocked(self.http.post(self.url,{'action':'confirm'}),409)

    def test_all_business_eligibility_branches_without_writes(self):
        cases=[dict(is_archived=True),dict(outcome='lost',close_reason='Synthetic refusal'),
            dict(fulfillment_kind='construction'),dict(client=None,object_kind='none',pool=None),
            dict(object_kind='planned',pool=None,planned_object_name='Synthetic planned object')]
        fields=set().union(*(c.keys() for c in cases))
        original={k:getattr(self.deal,k) for k in fields}
        for changes in cases:
            with self.subTest(changes=list(changes)):
                for k,v in {**original,**changes}.items():setattr(self.deal,k,v)
                self.deal.save()
                self.assert_blocked(self.http.get(self.url))

    def test_authorization_not_relaxed_or_exposed(self):
        original=self.deal.manager
        self.deal.manager=self.recipient;self.deal.save()
        response=self.http.get(self.url)
        self.assertEqual(response.status_code,403)
        self.assertNotContains(response,'data-recurring-blocked',status_code=403)
        self.deal.manager=original;self.deal.save()
        with patch.object(service,'read_history',side_effect=PermissionDenied):
            self.assertEqual(self.http.get(self.url).status_code,403)
        self.assertEqual(self.http.get(self.url.replace('org_id='+str(self.org.pk),'org_id=999999')).status_code,403)

    def test_history_recheck_and_invalid_cursor(self):
        with patch.object(service,'read_history',side_effect=ValidationError('synthetic private internal detail')):
            response=self.http.get(self.url);self.assert_blocked(response)
            self.assertNotContains(response,'synthetic private internal detail')
        for cursor in ('0','-1','bad','9'*20):
            self.assertEqual(self.http.get(self.url+'&before='+cursor).status_code,400)
