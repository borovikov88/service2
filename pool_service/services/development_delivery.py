"""Retired legacy delivery; no saved AI decision may trigger GitHub writes."""

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener

from django.conf import settings
from django.db import transaction

from pool_service.models import DevelopmentIteration, DevelopmentTask, DevelopmentTaskEvent
from pool_service.services.development_codex import (
    AUTO_CYCLE_METADATA_KEY,
    SAFE_REPOSITORY_RE,
    SHA_RE,
    _github_ssl_context,
    _NoRedirectHandler,
)


class DevelopmentDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    state: str
    changed: bool = False


def _request(method, url, token, *, payload=None, expected=(200,)):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "service2-independent-review-delivery",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        opener = build_opener(HTTPSHandler(context=_github_ssl_context()), _NoRedirectHandler())
        with opener.open(request, timeout=settings.GITHUB_DEVELOPMENT_TIMEOUT_SECONDS) as response:
            if response.status not in expected:
                raise DevelopmentDeliveryError(f"unexpected GitHub status {response.status}")
            raw = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise DevelopmentDeliveryError(type(exc).__name__) from exc
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise DevelopmentDeliveryError("invalid GitHub JSON") from exc


def _accepted_review(task):
    return task.iterations.filter(
        executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
        automation_metadata__purpose="ai_review",
        automation_metadata__decision="accepted",
        automation_metadata__applied=True,
    ).order_by("-id").first()


def _all_reviews(api, pr_number, token):
    result = []
    for page in range(1, 101):
        items = _request("GET", f"{api}/pulls/{pr_number}/reviews?per_page=100&page={page}", token)
        if not isinstance(items, list):
            raise DevelopmentDeliveryError("invalid GitHub reviews response")
        result.extend(items)
        if len(items) < 100:
            return result
    raise DevelopmentDeliveryError("GitHub reviews exceed pagination limit")


def _pull_matches_review(pull, *, repository, pr_number, head_sha, head_ref):
    return (
        pull.get("number") == pr_number
        and pull.get("state") == "open"
        and pull.get("base", {}).get("ref") == "main"
        and pull.get("base", {}).get("repo", {}).get("full_name") == repository
        and pull.get("head", {}).get("sha") == head_sha
        and pull.get("head", {}).get("ref") == head_ref
        and pull.get("head", {}).get("repo", {}).get("full_name") == repository
    )


def publish_approval_and_enable_auto_merge(task_id):
    """Retired: stored API-era acceptance cannot authorize GitHub writes.

    Refuse before reading configuration, task state or saved delivery metadata.
    This also blocks poller replay of an already accepted review when old flags
    and credentials remain configured. Native Codex comments are not APPROVED
    reviews and are not converted into approval or auto-merge here.
    """
    return DeliveryResult("retired", changed=False)
