"""Pure fail-closed dispatch guard. Never emits caller-supplied values."""
import os
import re


def valid(env):
    sha = env.get('COMMIT_SHA', '')
    return bool(
        env.get('EVENT_NAME') == 'workflow_dispatch'
        and env.get('REF') == 'refs/heads/main'
        and env.get('REPOSITORY') == 'borovikov88/service2'
        and re.fullmatch(r'[0-9a-f]{40}', sha)
        and env.get('CONFIRM_MAIN_SHA') == sha
        and env.get('CURRENT_MAIN_SHA') == sha
        and env.get('CONFIRM_SCOPE') == 'all-organizations-default-mysql'
    )


if __name__ == '__main__':
    if not valid(os.environ):
        print('CRM_BACKUP_REQUEST_BLOCKED invalid_confirmation_or_main')
        raise SystemExit(2)
    print('CRM_BACKUP_REQUEST_VERIFIED scope=all-organizations-default-mysql')
