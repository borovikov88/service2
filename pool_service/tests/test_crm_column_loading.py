"""Cursor portions, tracked invalidation, current authority and unchanged table."""
from datetime import timedelta
from copy import deepcopy
import time
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.test import TestCase, SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.crm_commercial_models import CrmDeal, CrmLead, CrmAccess
from pool_service.crm_board_models import CrmBoardEpoch
from pool_service.models import Organization, OrganizationAccess, Employee


class ColumnLoadingTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type=Organization.PLAN_COMPANY_PAID
        self.org.paid_until=timezone.now()+timedelta(days=30)
        self.org.save(update_fields=['plan_type','paid_until'])
        self.grant.view_leads=True;self.grant.save()
        self.client.force_login(self.user)
        self.stage=self.pipeline.stages.get(code='need');self.target=self.pipeline.stages.get(code='technical')
        self.rows=self.seed(self.stage,57);self.other=self.seed(self.target,53)

    def seed(self,stage,count):
        return [CrmDeal.objects.create(organization=self.org,pipeline=self.pipeline,stage=stage,manager=self.user,
            client=self.client_record,title=f'Synthetic cursor {stage.pk}-{n}',amount='10.01',amount_basis='agreed',currency='RUB') for n in range(count)]

    def data(self,**params):
        return self.client.get(reverse('crm_live_data'),{'org_id':self.org.pk,'pipeline':self.pipeline.pk,'layout':'board',**params})

    def token(self,data=None):
        return next(w['cursor'] for w in (data or self.data().json())['column_windows'] if w['stage_id']==self.stage.pk)

    def test_all_rows_over_200_two_columns_replay_and_full_totals(self):
        self.rows+=self.seed(self.stage,154)
        first=self.data().json();self.assertEqual(len(first['deals']),100)
        total=next(r for r in first['stage_totals'] if r['stage_id']==self.stage.pk)
        self.assertEqual((total['count'],total['amount']),(211,'2112.11'))
        seen={r['id'] for r in first['deals']}
        issued=int(time.time())
        def authenticated_content(value):
            result=deepcopy(value)
            for part in result['column_windows']:
                if part['cursor']:
                    part['cursor']=signing.loads(part['cursor'],salt='crm-board-keyset-v1',max_age=3600)
            return result
        for window in first['column_windows']:
            token=window['cursor']
            while token:
                args={'board_column':window['stage_id'],'cursor':token}
                # Reissuing a valid token in another second changes its envelope,
                # not its authenticated keyset/scope or the returned cards.
                with patch.object(signing.TimestampSigner,'timestamp',return_value=signing.b62_encode(issued)):
                    response=self.data(**args)
                self.assertEqual(response.status_code,200,response.content)
                data=response.json()
                with patch.object(signing.TimestampSigner,'timestamp',return_value=signing.b62_encode(issued+2)):
                    replay=self.data(**args)
                self.assertEqual(replay.status_code,200,replay.content)
                repeated=replay.json()
                self.assertEqual(authenticated_content(data),authenticated_content(repeated))
                if data['column_windows'][0]['cursor']:
                    self.assertNotEqual(data['column_windows'][0]['cursor'],repeated['column_windows'][0]['cursor'])
                self.assertLessEqual(len(data['deals']),50)
                ids={r['id'] for r in data['deals']};self.assertFalse(seen & ids);seen|=ids
                token=data['column_windows'][0]['cursor']
        self.assertEqual(seen,{r.pk for r in self.rows+self.other})

    def test_temporal_attention_keeps_signed_as_of_without_database_writes(self):
        from datetime import datetime, timezone as dt_timezone
        before=datetime(2026,11,1,16,59,50,tzinfo=dt_timezone.utc)
        after=before+timedelta(seconds=20)
        from pool_service.crm_live_views import _attention_ids
        # The classifier sees the original signed instant even across local midnight.
        def classify(queryset,user,org,attention,as_of=None):
            self.assertEqual(as_of,before)
            return list(queryset.values_list('pk',flat=True))
        with patch('pool_service.crm_live_views.timezone.now',return_value=before), patch('pool_service.crm_live_views._attention_ids',side_effect=classify):
            first=self.data(attention='today').json()
        token=self.token(first)
        with patch('pool_service.crm_live_views.timezone.now',return_value=after), patch('pool_service.crm_live_views._attention_ids',side_effect=classify):
            response=self.data(attention='today',board_column=self.stage.pk,cursor=token)
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['attention_as_of'],before.isoformat())

    def test_real_attention_classifier_freezes_midnight_and_due_boundary(self):
        from datetime import datetime, timezone as dt_timezone
        from pool_service.crm_live_views import _attention_ids
        before=datetime(2026,11,1,16,59,50,tzinfo=dt_timezone.utc)
        due=before+timedelta(seconds=5)
        result=self.create(operation_key='temporal-boundary',next_action={'title':'Synthetic boundary','due_at':due})
        query=CrmDeal.objects.filter(pk=result['deal_id'])
        after=before+timedelta(seconds=20)
        with patch('pool_service.crm_live_views.timezone.now',return_value=after):
            self.assertEqual(_attention_ids(query,self.user,self.org,'today',as_of=before),[result['deal_id']])
            self.assertEqual(_attention_ids(query,self.user,self.org,'today'),[])
            self.assertEqual(_attention_ids(query,self.user,self.org,'overdue',as_of=before),[])
            self.assertEqual(_attention_ids(query,self.user,self.org,'overdue'),[result['deal_id']])

    def test_delete_freezes_candidates_before_org_lock(self):
        from django.db.models.query import QuerySet
        original=QuerySet.select_for_update
        inserted=[]
        def hook(queryset,*args,**kwargs):
            if queryset.model is Organization and not inserted:
                inserted.append(True)
                inserted.append(self.seed(self.stage,1)[0])
            return original(queryset,*args,**kwargs)
        with patch.object(QuerySet,'select_for_update',hook):
            CrmDeal.objects.filter(stage=self.stage).delete()
        self.assertTrue(CrmDeal.objects.filter(pk=inserted[-1].pk).exists())
        self.assertFalse(CrmDeal.objects.filter(pk__in=[r.pk for r in self.rows]).exists())

    def test_move_insert_archive_delete_stage_edit_and_grant_invalidate(self):
        def move():
            row=self.rows[0];row.stage=self.target;row.save()
        def archive():
            row=self.rows[1];row.is_archived=True;row.save()
        def rename_stage():
            self.target.name='Synthetic changed';self.target.save()
        def grant():
            self.grant.view_leads=False;self.grant.save()
        for change in [move,lambda:self.seed(self.stage,1),archive,lambda:self.rows[2].delete(),rename_stage,grant]:
            token=self.token();change()
            response=self.data(board_column=self.stage.pk,cursor=token)
            self.assertEqual((response.status_code,response.json()['code']),(409,'reset_required'))
        self.assertEqual(self.data().status_code,200)

    def test_tamper_expiry_filter_column_and_actor_scope(self):
        token=self.token()
        for args in [{'cursor':token+'x'},{'cursor':token,'q':'different'},{'cursor':token,'board_column':self.target.pk},{'cursor':token,'owner':'me'}]:
            response=self.data(**({'board_column':self.stage.pk}|args))
            self.assertEqual(response.status_code,409)
        with patch('django.core.signing.time.time',return_value=timezone.now().timestamp()+7200):
            self.assertEqual(self.data(board_column=self.stage.pk,cursor=token).status_code,409)
        actor=get_user_model().objects.create_user('other-cursor-reader')
        OrganizationAccess.objects.create(organization=self.org,user=actor,role='manager')
        Employee.objects.create(organization=self.org,user=actor,display_name='Synthetic reader')
        self.client.force_login(actor)
        self.assertEqual(self.data(board_column=self.stage.pk,cursor=token).status_code,409)
        self.client.logout();self.assertEqual(self.data().status_code,401)

    def test_table_filters_unknown_amount_and_org_scope_unchanged(self):
        row=self.rows[0];row.amount=None;row.amount_basis='unknown';row.currency='';row.save()
        first=self.data(layout='page').json();second=self.data(layout='page',page=2).json()
        self.assertEqual(len(first['deals']),50);self.assertEqual(len(second['deals']),50)
        self.assertFalse({r['id'] for r in first['deals']} & {r['id'] for r in second['deals']})
        result=self.data(q=f'Synthetic cursor {self.target.pk}-').json()
        self.assertEqual(sum(r['count'] for r in result['stage_totals']),53)
        self.assertEqual(self.data(org_id=Organization.objects.create(name='Foreign synthetic').pk).status_code,403)
        response=self.client.get(reverse('crm_live_workspace'),{'org_id':self.org.pk})
        self.assertContains(response,'workspace.js?v=20261101-cursor')
        self.assertEqual(response.context['crm_bootstrap']['layout'],'board')

    def test_epoch_is_transactional_and_get_does_not_write(self):
        revision=CrmBoardEpoch.objects.get(organization=self.org).revision
        self.data();self.data()
        self.assertEqual(CrmBoardEpoch.objects.get(organization=self.org).revision,revision)
        try:
            with transaction.atomic():
                self.seed(self.stage,1);raise RuntimeError('rollback synthetic')
        except RuntimeError:pass
        self.assertEqual(CrmBoardEpoch.objects.get(organization=self.org).revision,revision)
        token=self.token();CrmDeal.objects.filter(pk=self.rows[0].pk).delete()
        self.assertEqual(self.data(board_column=self.stage.pk,cursor=token).status_code,409)

    def test_leads_over_200_two_columns_stable_tuple_and_reset(self):
        stamp=timezone.now()
        leads=[]
        for state,n in [('new',211),('working',63)]:
            # Actual working lifecycle code is contacting; the board groups it.
            for i in range(n):
                leads.append(CrmLead.objects.create(organization=self.org,title=f'Synthetic {state} {i}',
                    status='new' if state=='new' else 'working',assignee=self.user,received_at=stamp))
        url=reverse('crm_live_lead_data');args={'org_id':self.org.pk,'layout':'board'}
        first=self.client.get(url,args);self.assertEqual(first.status_code,200,first.content);data=first.json()
        seen={r['id'] for r in data['leads']};self.assertEqual(len(seen),100)
        for column in data['columns']:
            token=column['cursor']
            while token:
                more=self.client.get(url,args|{'board_column':column['key'],'cursor':token})
                self.assertEqual(more.status_code,200,more.content);rows=more.json()
                self.assertLessEqual(len(rows['leads']),50);ids={r['id'] for r in rows['leads']}
                self.assertFalse(ids&seen);seen|=ids;token=rows['columns'][0]['cursor']
        self.assertEqual(seen,{r.pk for r in leads})
        token=next(c['cursor'] for c in data['columns'] if c['key']=='unparsed')
        leads[0].received_at=stamp+timedelta(days=1);leads[0].save()
        changed=self.client.get(url,args|{'board_column':'unparsed','cursor':token})
        self.assertEqual(changed.status_code,409)
        self.grant.view_leads=False;self.grant.save()
        self.assertEqual(self.client.get(url,args|{'board_column':'unparsed','cursor':token}).status_code,403)


class ColumnBrowserSettingsTests(SimpleTestCase):
    def test_standalone_browser_flag_uses_private_file_database(self):
        import os, subprocess, sys
        env={key:value for key,value in os.environ.items() if not key.endswith('_BROWSER_TEST') and key!='SERVICE2_SQLITE_LIVE_SERVER_TEST'}
        env['CRM_COLUMN_BROWSER_TEST']='1'
        result=subprocess.run([sys.executable,'-c',"from service_site.test_settings import DATABASES; name=DATABASES['default']['TEST']['NAME']; assert name and name != ':memory:'"],env=env,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
