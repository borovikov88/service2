"""Offline provenance, tamper and fallback regressions; never contact GitHub."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import urllib.error
import unittest
from unittest.mock import patch

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
        self.proof = {'name':f'PR_TESTED_TREE_V1 {TREE} {TESTED}', 'status':'completed','conclusion':'success'}
        self.job = {'name':'Django checks and tests','run_id':22,'status':'completed','conclusion':'success','steps':[self.proof]}
        self.jobs = [self.job]
        self.total = 1
        self.workflow = {'id':11,'path':reuse.WORKFLOW,'state':'active'}
        self.tested = {'tree':{'sha':TREE},'parents':[{'sha':BASE},{'sha':HEAD}]}
        self.runs = [self.run];self.prs = [self.pr]
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path.startswith('/commits/'):return self.prs
        if path == '/actions/workflows/ci-deploy.yml':return self.workflow
        if '/runs?' in path:return {'workflow_runs':self.runs}
        if '/attempts/' in path:return {'total_count':self.total,'jobs':self.jobs}
        if path.startswith('/git/commits/'):return self.tested
        raise AssertionError(path)



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

    def test_proof_tree_format_success_and_uniqueness(self):
        for proof in ({'name':f'PR_TESTED_TREE_V1 {BASE} {TESTED}','status':'completed','conclusion':'success'},
                      {'name':f'PR_TESTED_TREE_V1 {TREE} bad','status':'completed','conclusion':'success'},
                      {'name':f'PR_TESTED_TREE_V1 {TREE} {TESTED}','status':'completed','conclusion':'skipped'},
                      {'name':f'PR_TESTED_TREE_V1 {TREE} {TESTED} extra','status':'completed','conclusion':'success'}):
            with self.subTest(proof=proof),self.assertRaises(reuse.EvidenceError):
                self.api=Api();self.api.job['steps']=[proof];self.verify()
        for proofs in ([],[self.api.proof,self.api.proof]):
            self.api=Api();self.api.job['steps']=proofs
            with self.assertRaises(reuse.EvidenceError):self.verify()

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

    def test_exact_attempt_and_single_successful_job(self):
        self.assertEqual(self.verify(),22)
        self.assertIn('/actions/runs/22/attempts/2/jobs?per_page=100',self.api.calls)
        for key,value in (('name','Other job'),('run_id',23),('status','in_progress'),('conclusion','failure')):
            with self.subTest(key=key),self.assertRaises(reuse.EvidenceError):
                self.api=Api();self.api.job[key]=value;self.verify()
        self.api=Api();self.api.jobs*=2;self.api.total=2
        with self.assertRaises(reuse.EvidenceError):self.verify()
        self.api=Api();self.api.total=101
        with self.assertRaises(reuse.EvidenceError):self.verify()

    def test_public_client_has_no_token_and_does_not_retry_or_follow_redirects(self):
        api=reuse.GitHub()
        with patch.object(api.opener,'open',return_value=io.BytesIO(b'{"ok":true}')) as opened:
            self.assertEqual(api.get('/actions/workflows/ci-deploy.yml'),{'ok':True})
        request=opened.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(request.full_url,'https://api.github.com/repos/'+reuse.REPO+'/actions/workflows/ci-deploy.yml')
        for status in (302,403,429):
            error=urllib.error.HTTPError(request.full_url,status,'DENIED',{},None)
            with patch.object(api.opener,'open',side_effect=error) as opened,self.assertRaises(urllib.error.HTTPError):
                api.get('/actions/workflows/ci-deploy.yml')
            self.assertEqual(opened.call_count,1)
        for path in ('https://other.example/','/actions/runs/1/artifacts','/actions/runs/1/logs'):
            with patch.object(api.opener,'open') as opened,self.assertRaises(reuse.EvidenceError):api.get(path)
            opened.assert_not_called()

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

    def test_capture_actual_merge_checkout_before_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory,'output')
            with patch.object(reuse,'git',side_effect=[SHA,TREE]):
                reuse.capture_tree(self.env|{'GITHUB_OUTPUT':str(output)})
            self.assertEqual(output.read_text(),f'commit={SHA}\ntree={TREE}\n')
            with patch.object(reuse,'git',return_value=BASE),self.assertRaises(reuse.EvidenceError):
                reuse.capture_tree(self.env|{'GITHUB_OUTPUT':str(output)})

    def test_workflow_keeps_protected_name_and_deploy_guards(self):
        text=(ROOT/reuse.WORKFLOW).read_text()
        self.assertIn('name: Django checks and tests',text)
        self.assertIn('environment: production',text)
        self.assertIn('needs: test',text)
        self.assertIn('Verify hosting compatibility without modifying checkout',text)
        self.assertIn('Verify deployed site health',text)
        self.assertIn('Check reuse policy regressions',text)
        self.assertNotIn('actions: read',text)
        self.assertNotIn('pull-requests: read',text)
        self.assertNotIn('upload-artifact',text)
        self.assertNotIn('actions: write',text)
        self.assertNotIn('contents: write',text)
        self.assertLess(text.index('name: Capture checked-out tree'),text.index('name: Check reuse policy regressions'))
        self.assertLess(text.index('name: Run test suite'),text.index('name: PR_TESTED_TREE_V1'))
        reuse_step=text[text.index('- name: Verify equivalent successful PR tests'):text.index('- name: Validate deploy workflow syntax')]
        self.assertNotIn('GH_TOKEN',reuse_step)
        for name in ('Install dependencies','Run test suite','Validate Django configuration and migration state'):
            self.assertIn('- name: '+name+"\n        if: steps.reuse.outputs.verified != 'true'",text)


if __name__ == '__main__':unittest.main()
