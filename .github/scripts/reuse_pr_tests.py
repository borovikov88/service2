#!/usr/bin/env python3
"""Reuse successful PR checks only for identical trees and unchanged trusted policy.

Any missing/ambiguous/unavailable evidence selects the normal full test path.
Only public job metadata is read, with no token, artifacts, downloads or new permissions.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

REPO = 'borovikov88/service2'
WORKFLOW = '.github/workflows/ci-deploy.yml'
POLICY = '.github/scripts/reuse_pr_tests.py'
PROOF_PATTERN = re.compile(r'PR_TESTED_TREE_V1 ([0-9a-f]{40}) ([0-9a-f]{40})')
LIMIT = 2 * 1024 * 1024
MAX_AGE = 24 * 60 * 60


class EvidenceError(Exception):
    pass


def require(value, reason):
    if not value:
        raise EvidenceError(reason)


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL).decode().strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def policy_hashes(ref='HEAD'):
    return {path: digest(subprocess.check_output(['git', 'show', f'{ref}:{path}'], stderr=subprocess.DEVNULL))
            for path in (WORKFLOW, POLICY)}


def capture_tree(env):
    require(env.get('GITHUB_REPOSITORY') == REPO, 'repository_mismatch')
    commit = git('rev-parse', 'HEAD')
    require(commit == env.get('GITHUB_SHA'), 'checkout_mismatch')
    tree = git('rev-parse', 'HEAD^{tree}')
    require(re.fullmatch('[0-9a-f]{40}', tree), 'tree_invalid')
    with open(env['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'commit={commit}\ntree={tree}\n')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self):
        self.deadline = time.monotonic() + 25
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path):
        # Public GitHub API only. Never attach credentials, follow redirects,
        # retry denied requests or fetch artifact/log payloads.
        require(path.startswith(('/commits/', '/git/commits/', '/actions/workflows/', '/actions/runs/')),
                'api_path')
        require('/artifacts' not in path and '/logs' not in path, 'payload_forbidden')
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'lookup_timeout')
        request = urllib.request.Request(f'https://api.github.com/repos/{REPO}' + path, headers={
            'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        with self.opener.open(request, timeout=min(5, remaining)) as response:
            body = response.read(LIMIT + 1)
        require(len(body) <= LIMIT, 'response_size')
        return json.loads(body)


def verify(env, api, commit, parents, tree, current_policy, parent_policy, now=None):
    require(env.get('GITHUB_EVENT_NAME') == 'push' and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_REPOSITORY') == REPO, 'full_tests_for_event')
    require(re.fullmatch('[0-9a-f]{40}', commit or '') and commit == env.get('GITHUB_SHA'), 'checkout_mismatch')
    require(len(parents) == 2, 'not_single_pr_merge')
    # A PR changing this workflow or its verifier must bootstrap through full main CI.
    # Job names alone cannot establish trust in the workflow that produced them.
    require(current_policy == parent_policy, 'ci_policy_changed')
    associated = api.get(f'/commits/{commit}/pulls?per_page=10')
    prs = [p for p in associated if p.get('merged_at') and p.get('merge_commit_sha') == commit
           and p.get('base', {}).get('ref') == 'main'
           and p.get('base', {}).get('repo', {}).get('full_name') == REPO
           and p.get('head', {}).get('repo', {}).get('full_name') == REPO
           and p.get('head', {}).get('sha') == parents[1]]
    require(len(prs) == 1, 'merged_pr_unproven')
    workflow = api.get('/actions/workflows/ci-deploy.yml')
    require(workflow.get('path') == WORKFLOW and workflow.get('state') == 'active', 'workflow_untrusted')
    # Do not filter by success: a newer failed/in-progress attempt must not be hidden.
    listing = api.get('/actions/workflows/ci-deploy.yml/runs?event=pull_request&head_sha='
                      + parents[1] + '&per_page=10')
    runs = [r for r in listing['workflow_runs'] if r.get('head_sha') == parents[1]]
    require(runs, 'pr_run_missing')
    run = runs[0]
    require(run.get('event') == 'pull_request' and run.get('status') == 'completed'
            and run.get('conclusion') == 'success' and run.get('path') == WORKFLOW
            and run.get('workflow_id') == workflow['id']
            and run.get('repository', {}).get('full_name') == REPO
            and run.get('head_repository', {}).get('full_name') == REPO, 'pr_ci_not_successful')
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))).total_seconds()
    require(0 <= age <= MAX_AGE, 'pr_ci_expired')
    jobs = api.get(f'/actions/runs/{int(run["id"])}/attempts/{int(run["run_attempt"])}/jobs?per_page=100')
    require(jobs.get('total_count') == len(jobs['jobs']), 'jobs_truncated')
    choices = [j for j in jobs['jobs'] if j.get('name') == 'Django checks and tests']
    require(len(choices) == 1, 'test_job_ambiguous')
    job = choices[0]
    require(job.get('run_id') == run['id'] and job.get('status') == 'completed'
            and job.get('conclusion') == 'success', 'test_job_not_successful')
    proofs = [step for step in job.get('steps', []) if step.get('name', '').startswith('PR_TESTED_TREE_V1')]
    require(len(proofs) == 1, 'proof_missing_or_ambiguous')
    proof = proofs[0]
    match = PROOF_PATTERN.fullmatch(proof['name'])
    require(match and proof.get('status') == 'completed' and proof.get('conclusion') == 'success', 'proof_invalid')
    tested_tree, tested_commit = match.groups()
    require(tested_tree == tree, 'proof_tree_mismatch')
    # Cross-check the actual tested merge commit through GitHub, not just a self-reported hash.
    tested = api.get('/git/commits/' + tested_commit)
    require(tested.get('tree', {}).get('sha') == tree
            and [p['sha'] for p in tested.get('parents', [])] == parents, 'tested_tree_mismatch')
    return run['id']


def main():
    env = os.environ
    if sys.argv[1:] == ['capture']:
        capture_tree(env)
        return
    reused, reason = False, 'full_tests_for_event'
    try:
        if env.get('GITHUB_EVENT_NAME') == 'push' and env.get('GITHUB_REF') == 'refs/heads/main':
            commit = git('rev-parse', 'HEAD')
            parents = git('show', '-s', '--format=%P', 'HEAD').split()
            run = verify(env, GitHub(), commit, parents,
                         git('rev-parse', 'HEAD^{tree}'), policy_hashes(), policy_hashes('HEAD^1'))
            reused, reason = True, f'pr_run_{int(run)}_same_tree'
    except Exception:
        # No response bodies or exception payloads in logs.
        reason = 'evidence_unavailable_or_mismatched'
    with open(env['GITHUB_OUTPUT'], 'a') as output:
        output.write('verified=' + str(reused).lower() + '\n')
    print('CI_REUSE_VERIFIED ' + reason if reused else 'CI_FULL_TESTS ' + reason)


if __name__ == '__main__':
    main()
