"""Offline provenance, tamper and fallback regressions; never contact GitHub."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import urllib.error
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('reuse', ROOT / '.github/scripts/reuse_pr_tests.py')
reuse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reuse)
SHA, BASE, HEAD, TREE, TESTED = ('a'*40, 'b'*40, 'c'*40, 'd'*40, 'e'*40)
NOW = datetime(2026, 10, 23, 8, tzinfo=timezone.utc)
POLICY = {reuse.WORKFLOW:'f'*64, reuse.POLICY:'0'*64}


class Api:
    def __init__(self):
        self.pr = {'number': 374, 'merged_at':'2026-10-23T08:00:00Z','merge_commit_sha':SHA,
                   'base':{'ref':'main','repo':{'full_name':reuse.REPO}},
                   'head':{'sha':HEAD,'repo':{'full_name':reuse.REPO}}}
        self.run = {'id':22,'run_attempt':2,'event':'pull_request','status':'completed','conclusion':'success',
                    'workflow_id':11,'path':reuse.WORKFLOW,'repository':{'full_name':reuse.REPO,'id':1},
                    'head_repository':{'full_name':reuse.REPO,'id':1},'head_sha':HEAD,
                    'updated_at':'2026-10-23T07:59:00Z'}
        self.artifact = {'id':33,'name':'service2-tested-tree-22-2','expired':False,'size_in_bytes':400,
                         'workflow_run':{'id':22,'head_sha':HEAD,'repository_id':1,'head_repository_id':1}}
        self.proof = {'schema':1,'repository':reuse.REPO,'tree':TREE,'commit':TESTED,'run_id':22,'run_attempt':2,
                      'pr':374,'head':HEAD,'base':BASE,'policy':deepcopy(POLICY)}
        self.workflow = {'id':11,'path':reuse.WORKFLOW,'state':'active'}
        self.tested = {'tree':{'sha':TREE},'parents':[{'sha':BASE},{'sha':HEAD}]}
        self.runs = [self.run];self.artifacts = [self.artifact];self.prs = [self.pr]
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path.startswith('/commits/'):return self.prs
        if path == '/actions/workflows/ci-deploy.yml':return self.workflow
        if '/runs?' in path:return {'workflow_runs':self.runs}
        if '/artifacts?' in path:return {'artifacts':self.artifacts}
        if path.startswith('/git/commits/'):return self.tested
        raise AssertionError(path)

    def archive(self, artifact):return self.proof


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.env = {'GITHUB_REPOSITORY':reuse.REPO,'GITHUB_EVENT_NAME':'push',
                    'GITHUB_REF':'refs/heads/main','GITHUB_SHA':SHA}
        self.api = Api()

    def verify(self, **changes):
        values = dict(env=self.env,api=self.api,commit=SHA,parents=[BASE,HEAD],tree=TREE,
                      current_policy=POLICY,parent_policy=POLICY,now=NOW)
        return reuse.verify(**(values|changes))

    def test_identical_tree_successful_local_pr(self):
        self.assertEqual(self.verify(),22)
        self.assertTrue(self.api.calls[-1].endswith(TESTED))

    def test_event_checkout_parent_and_trusted_policy_guards(self):
        cases=[{'env':self.env|{'GITHUB_EVENT_NAME':event}} for event in ('pull_request','workflow_dispatch','schedule')]
        cases += [{'env':self.env|{'GITHUB_REPOSITORY':'other/repo'}},
                  {'env':self.env|{'GITHUB_REF':'refs/heads/other'}}, {'commit':BASE},
                  {'parents':[BASE]}, {'parent_policy':{}}, {'parent_policy':POLICY|{reuse.POLICY:'forged'}}]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(reuse.EvidenceError):self.verify(**case)
        self.assertEqual(self.api.calls,[])

    def test_proof_identity_and_tree_must_match(self):
        changes={'schema':2,'repository':'other/repo','tree':BASE,'run_id':23,'run_attempt':1,
                 'pr':375,'head':BASE,'base':HEAD,'policy':{},'commit':'not-a-sha'}
        for key,value in changes.items():
            with self.subTest(key=key),self.assertRaises(reuse.EvidenceError):
                self.api=Api();self.api.proof[key]=value;self.verify()

    def test_actual_tested_commit_and_merge_parents_cross_checked(self):
        for tested in ({'tree':{'sha':BASE},'parents':[{'sha':BASE},{'sha':HEAD}]},
                       {'tree':{'sha':TREE},'parents':[{'sha':BASE}]},
                       {'tree':{'sha':TREE},'parents':[{'sha':HEAD},{'sha':BASE}]}):
            with self.subTest(tested=tested),self.assertRaises(reuse.EvidenceError):
                self.api.tested=tested;self.verify()

    def test_main_merge_and_pr_ownership_not_just_commit_association(self):
        for key,value in (('merge_commit_sha',BASE),('merged_at',None),
                          ('head',{'sha':HEAD,'repo':{'full_name':'fork/repo'}}),
                          ('base',{'ref':'other','repo':{'full_name':reuse.REPO}})):
            with self.subTest(key=key),self.assertRaises(reuse.EvidenceError):
                self.api=Api();self.api.pr[key]=value;self.verify()
        self.api=Api();self.api.prs*=2
        with self.assertRaises(reuse.EvidenceError):self.verify()

    def test_latest_attempt_event_workflow_and_expiry(self):
        for key,value in (('conclusion','failure'),('status','in_progress'),('event','push'),
                          ('path','.github/workflows/other.yml'),('workflow_id',12),
                          ('repository',{'full_name':'other/repo'}),('head_repository',{'full_name':'fork/repo'}),
                          ('updated_at','2026-10-21T08:00:00Z')):
            with self.subTest(key=key),self.assertRaises(reuse.EvidenceError):
                self.api=Api();self.api.run[key]=value;self.verify()
        self.api=Api();self.api.runs.insert(0,self.api.run|{'conclusion':'failure'})
        with self.assertRaises(reuse.EvidenceError):self.verify()

    def test_artifact_attempt_origin_expired_missing_duplicate_size(self):
        for key,value in (('name','service2-tested-tree-22-1'),('expired',True),('size_in_bytes',100000),
                          ('workflow_run',{'id':23,'head_sha':HEAD,'repository_id':1,'head_repository_id':1})):
            with self.subTest(key=key),self.assertRaises(reuse.EvidenceError):
                self.api=Api();self.api.artifact[key]=value;self.verify()
        for choices in ([],[self.api.artifact,self.api.artifact]):
            self.api=Api();self.api.artifacts=choices
            with self.assertRaises(reuse.EvidenceError):self.verify()

    def test_archive_not_extracted_or_executed_and_size_bounded(self):
        for name,value in ((reuse.PROOF,{'schema':1}),('../'+reuse.PROOF,{'schema':1}),
                           (reuse.PROOF,{'schema':2}),(reuse.PROOF,{'schema':1,'extra':'x'*17000})):
            content=io.BytesIO()
            with zipfile.ZipFile(content,'w') as archive:archive.writestr(name,json.dumps(value))
            if name==reuse.PROOF and value=={'schema':1}:self.assertEqual(reuse.read_proof(content.getvalue()),value)
            else:
                with self.assertRaises(reuse.EvidenceError):reuse.read_proof(content.getvalue())

    def test_archive_digest_and_signed_download_have_no_token(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr(reuse.PROOF,json.dumps({'schema':1}))
        body=stream.getvalue()
        artifact={'id':33,'digest':'sha256:'+hashlib.sha256(body).hexdigest()}
        location='https://example.blob.core.windows.net/signed?signature=PRIVATE'
        redirect=urllib.error.HTTPError('https://api.github.com',302,'Found',{'Location':location},None)
        api=reuse.GitHub('PRIVATE_TOKEN')
        with patch.object(api,'open',side_effect=[redirect,body]) as opened:
            self.assertEqual(api.archive(artifact),{'schema':1})
        self.assertEqual(opened.call_args_list[1].kwargs,{'authenticated':False,'limit':reuse.ZIP_LIMIT})
        with patch.object(api,'open',side_effect=[redirect,body]),self.assertRaises(reuse.EvidenceError):
            api.archive(artifact|{'digest':'sha256:bad'})
        for target in ('http://example.blob.core.windows.net/a','https://evil.example/a',
                       'https://user@example.blob.core.windows.net/a',
                       'https://example.blob.core.windows.net:8443/a'):
            redirect=urllib.error.HTTPError('https://api.github.com',302,'Found',{'Location':target},None)
            with patch.object(api,'open',side_effect=redirect) as opened,self.assertRaises(reuse.EvidenceError):
                api.archive(artifact)
            self.assertEqual(opened.call_count,1)

    def test_lookup_errors_fall_back_without_exposing_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory,'output')
            with patch.dict(reuse.os.environ,self.env|{'GITHUB_OUTPUT':str(output)},clear=True),\
                 patch.object(reuse,'git',return_value=SHA),\
                 patch.object(reuse,'GitHub',side_effect=ValueError('PRIVATE TOKEN')),\
                 patch('sys.stdout',new_callable=io.StringIO) as printed:
                reuse.main()
            self.assertEqual(output.read_text(),'verified=false\n')
            self.assertNotIn('PRIVATE',printed.getvalue())

    def test_record_actual_merge_checkout_not_head_only(self):
        env=self.env|{'GITHUB_EVENT_NAME':'pull_request','GITHUB_RUN_ID':'22','GITHUB_RUN_ATTEMPT':'2'}
        event={'pull_request':deepcopy(self.api.pr)};event['pull_request']['base']['sha']=BASE
        with patch.object(reuse,'git',side_effect=[SHA,TREE]),patch.object(reuse,'policy_hashes',return_value=POLICY):
            proof=reuse.create_proof(env,event)
        self.assertEqual((proof['commit'],proof['tree'],proof['head']),(SHA,TREE,HEAD))

    def test_workflow_keeps_protected_name_and_deploy_guards(self):
        text=(ROOT/reuse.WORKFLOW).read_text()
        self.assertIn('name: Django checks and tests',text)
        self.assertIn('environment: production',text)
        self.assertIn('needs: test',text)
        self.assertIn('Verify hosting compatibility without modifying checkout',text)
        self.assertIn('Verify deployed site health',text)
        self.assertIn('Check reuse policy regressions',text)
        self.assertIn('retention-days: 2',text)
        self.assertNotIn('actions: write',text)
        self.assertNotIn('contents: write',text)
        self.assertLess(text.index('name: Run test suite'),text.index('name: Record successful PR test tree'))
        for name in ('Install dependencies','Run test suite','Validate Django configuration and migration state'):
            self.assertIn('- name: '+name+"\n        if: steps.reuse.outputs.verified != 'true'",text)


if __name__ == '__main__':unittest.main()
