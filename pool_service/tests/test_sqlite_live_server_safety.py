"""Regression for isolated SQLite HTTP threads; synthetic records only."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import SimpleTestCase
from django.urls import reverse
from pool_service.models import Organization
from pool_service.tests.test_crm_object_needs import ReviewFixture

FLAGS = ('CRM_CARD_BROWSER_TEST', 'CRM_NEEDS_BROWSER_TEST', 'CRM_LEADS_BROWSER_TEST',
         'CONTACTS_BROWSER_TEST', 'CRM_NON_SALES_BROWSER_TEST', 'SERVICE2_SQLITE_LIVE_SERVER_TEST')


class SQLiteTestConfigurationTests(SimpleTestCase):
    def test_explicit_live_server_flags_isolate_and_cleanup_database(self):
        script = '''import json,os,stat
from service_site import test_settings as s
path=s.DATABASES['default'].get('TEST',{}).get('NAME')
print(json.dumps({'path':path,'private': bool(path and stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)==0o700)}))
'''
        for flag in (None, *FLAGS):
            with self.subTest(flag=flag):
                env = {key:value for key,value in os.environ.items() if key not in FLAGS}
                if flag: env[flag]='1'
                result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,check=True,timeout=15)
                value=json.loads(result.stdout)
                if flag:
                    self.assertTrue(value['private'])
                    self.assertEqual(Path(value['path']).name,'test.sqlite3')
                    self.assertFalse(Path(value['path']).parent.exists(), 'Temporary directory must be cleaned on process exit')
                else:
                    self.assertIsNone(value['path'])


@skipUnless(getattr(settings,'SQLITE_LIVE_SERVER_TEST',False), 'Explicit local SQLite LiveServer gate')
class SQLiteHTTPIsolationTests(ReviewFixture, StaticLiveServerTestCase):
    def test_concurrent_list_reads_preserve_rows_and_organization_guard(self):
        self.assertFalse(self._make_connections_override(), 'HTTP threads must not borrow the in-memory connection')
        self.client.force_login(self.user)
        cookie=settings.SESSION_COOKIE_NAME+'='+self.client.cookies[settings.SESSION_COOKIE_NAME].value
        path=self.live_server_url+reverse('crm_object_need_data')
        foreign=Organization.objects.create(name='Synthetic foreign HTTP organization',crm_v2_read=True)
        with self.assertRaises(HTTPError) as denied:
            urlopen(Request(path+'?org_id='+str(foreign.pk),headers={'Cookie':cookie}),timeout=10)
        self.assertEqual(denied.exception.code,403)
        url=path+'?org_id='+str(self.org.pk)+'&q=Chlorine&scope=available'
        barrier=Barrier(8)
        def read(_):
            counts=Counter(); barrier.wait(timeout=10)
            for _ in range(40):
                try:
                    with urlopen(Request(url,headers={'Cookie':cookie}),timeout=10) as response:
                        data=json.load(response)
                        rows=data.get('rows',[])
                        correct=(response.status==200 and data.get('total')==1 and len(rows)==1
                                 and rows[0]['id']==self.task.pk and rows[0]['state']=='pending'
                                 and rows[0]['review_revision']==0 and rows[0]['source_revision']==self.need.source_revision)
                        counts['correct' if correct else 'incorrect_response']+=1
                except HTTPError as exc: counts['http_'+str(exc.code)]+=1
                except Exception as exc: counts[type(exc).__name__]+=1
            return counts
        with ThreadPoolExecutor(max_workers=8) as pool:
            counts=sum(pool.map(read,range(8)),Counter())
        self.assertEqual(counts,{'correct':320})
