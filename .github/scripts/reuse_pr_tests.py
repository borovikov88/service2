#!/usr/bin/env python3
"""Reuse successful PR checks only for identical trees and unchanged trusted policy.

Any missing/ambiguous/unavailable evidence selects the normal full test path.
No artifact is executed or extracted; this process has read-only GitHub access.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

REPO = 'borovikov88/service2'
WORKFLOW = '.github/workflows/ci-deploy.yml'
POLICY = '.github/scripts/reuse_pr_tests.py'
PROOF = 'service2-ci-proof.json'
LIMIT = 2 * 1024 * 1024
ZIP_LIMIT = 64 * 1024
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


def create_proof(env, event):
    pr = event['pull_request']
    require(env['GITHUB_EVENT_NAME'] == 'pull_request' and env['GITHUB_REPOSITORY'] == REPO, 'not_pr')
    require(pr['head']['repo']['full_name'] == REPO and pr['base']['repo']['full_name'] == REPO
            and pr['base']['ref'] == 'main', 'not_local_main_pr')
    commit = git('rev-parse', 'HEAD')
    require(commit == env['GITHUB_SHA'], 'checkout_mismatch')
    return {'schema': 1, 'repository': REPO, 'commit': commit, 'tree': git('rev-parse', 'HEAD^{tree}'),
            'run_id': int(env['GITHUB_RUN_ID']), 'run_attempt': int(env['GITHUB_RUN_ATTEMPT']),
            'pr': pr['number'], 'head': pr['head']['sha'], 'base': pr['base']['sha'],
            'policy': policy_hashes()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        require(token, 'token_missing')
        self.token = token
        self.deadline = time.monotonic() + 30
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, url, authenticated=True, limit=LIMIT):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'lookup_timeout')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if authenticated:
            require(url.startswith(f'https://api.github.com/repos/{REPO}/'), 'api_origin')
            headers['Authorization'] = 'Bearer ' + self.token
        with self.opener.open(urllib.request.Request(url, headers=headers), timeout=min(8, remaining)) as response:
            body = response.read(limit + 1)
        require(len(body) <= limit, 'response_size')
        return body

    def get(self, path):
        return json.loads(self.open(f'https://api.github.com/repos/{REPO}{path}'))

    def archive(self, artifact):
        try:
            self.open(f'https://api.github.com/repos/{REPO}/actions/artifacts/{int(artifact["id"])}/zip', limit=ZIP_LIMIT)
        except urllib.error.HTTPError as error:
            require(error.code == 302, 'archive_unavailable')
            location = error.headers.get('Location', '')
        else:
            raise EvidenceError('archive_redirect_missing')
        url = urllib.parse.urlsplit(location)
        require(url.scheme == 'https' and not url.username and not url.password and url.port in (None, 443)
                and url.hostname and any(url.hostname.endswith(suffix) for suffix in
                    ('.blob.core.windows.net', '.githubusercontent.com')),
                'archive_origin')
        # Signed artifact URL receives no GitHub Authorization header, and no redirects.
        body = self.open(location, authenticated=False, limit=ZIP_LIMIT)
        require(artifact.get('digest') == 'sha256:' + digest(body), 'archive_digest')
        return read_proof(body)


def read_proof(body):
    require(len(body) <= ZIP_LIMIT, 'archive_size')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        require(archive.namelist() == [PROOF], 'archive_entries')
        info = archive.getinfo(PROOF)
        require(info.file_size <= 16384 and not info.is_dir(), 'proof_size')
        value = json.loads(archive.read(info))
    require(isinstance(value, dict) and value.get('schema') == 1, 'proof_schema')
    return value


def verify(env, api, commit, parents, tree, current_policy, parent_policy, now=None):
    require(env.get('GITHUB_EVENT_NAME') == 'push' and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_REPOSITORY') == REPO, 'full_tests_for_event')
    require(re.fullmatch('[0-9a-f]{40}', commit or '') and commit == env.get('GITHUB_SHA'), 'checkout_mismatch')
    require(len(parents) == 2, 'not_single_pr_merge')
    # A PR changing this workflow or its verifier must bootstrap through full main CI.
    # Artifact fields alone cannot establish trust in the policy that produced them.
    require(current_policy == parent_policy, 'ci_policy_changed')
    associated = api.get(f'/commits/{commit}/pulls?per_page=10')
    prs = [p for p in associated if p.get('merged_at') and p.get('merge_commit_sha') == commit
           and p.get('base', {}).get('ref') == 'main'
           and p.get('base', {}).get('repo', {}).get('full_name') == REPO
           and p.get('head', {}).get('repo', {}).get('full_name') == REPO
           and p.get('head', {}).get('sha') == parents[1]]
    require(len(prs) == 1, 'merged_pr_unproven')
    pr = prs[0]
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
    artifacts = api.get(f'/actions/runs/{int(run["id"])}/artifacts?per_page=100')
    expected = f'service2-tested-tree-{run["id"]}-{run["run_attempt"]}'
    choices = [a for a in artifacts['artifacts'] if a.get('name') == expected]
    require(len(choices) == 1, 'artifact_missing_or_ambiguous')
    artifact = choices[0]
    origin = artifact.get('workflow_run', {})
    require(not artifact.get('expired', True) and 0 < artifact.get('size_in_bytes', 0) <= ZIP_LIMIT
            and origin.get('id') == run['id'] and origin.get('head_sha') == parents[1]
            and origin.get('repository_id') == run['repository']['id']
            and origin.get('head_repository_id') == run['head_repository']['id'], 'artifact_origin')
    proof = api.archive(artifact)
    expected_fields = {'schema': 1, 'repository': REPO, 'tree': tree, 'run_id': run['id'],
                       'run_attempt': run['run_attempt'], 'pr': pr['number'], 'head': parents[1],
                       'base': parents[0], 'policy': current_policy}
    require(all(proof.get(k) == v for k, v in expected_fields.items()), 'proof_mismatch')
    require(re.fullmatch('[0-9a-f]{40}', proof.get('commit', '')), 'proof_commit')
    # Cross-check the actual tested merge commit through GitHub, not just a self-reported hash.
    tested = api.get('/git/commits/' + proof['commit'])
    require(tested.get('tree', {}).get('sha') == tree
            and [p['sha'] for p in tested.get('parents', [])] == parents, 'tested_tree_mismatch')
    return run['id']


def main():
    env = os.environ
    if sys.argv[1:] == ['record']:
        proof = create_proof(env, json.loads(Path(env['GITHUB_EVENT_PATH']).read_text()))
        Path(env['RUNNER_TEMP'], PROOF).write_text(json.dumps(proof, sort_keys=True))
        return
    reused, reason = False, 'full_tests_for_event'
    try:
        if env.get('GITHUB_EVENT_NAME') == 'push' and env.get('GITHUB_REF') == 'refs/heads/main':
            commit = git('rev-parse', 'HEAD')
            parents = git('show', '-s', '--format=%P', 'HEAD').split()
            run = verify(env, GitHub(env.get('GH_TOKEN', '')), commit, parents,
                         git('rev-parse', 'HEAD^{tree}'), policy_hashes(), policy_hashes('HEAD^1'))
            reused, reason = True, f'pr_run_{int(run)}_same_tree'
    except Exception:
        # No response bodies, tokens, signed URLs or exception payloads in logs.
        reason = 'evidence_unavailable_or_mismatched'
    with open(env['GITHUB_OUTPUT'], 'a') as output:
        output.write('verified=' + str(reused).lower() + '\n')
    print('CI_REUSE_VERIFIED ' + reason if reused else 'CI_FULL_TESTS ' + reason)


if __name__ == '__main__':
    main()
