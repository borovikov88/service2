"""Publish an accepted independent AI review and arm GitHub auto-merge."""

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
    """Deliver the separate AI review for its exact PR head, then enable auto-merge.

    The credential must belong to a reviewer identity distinct from the PR author.
    Missing configuration is a fail-closed, non-mutating state.
    """
    token = settings.GITHUB_DEVELOPMENT_REVIEW_TOKEN
    expected_login = settings.GITHUB_DEVELOPMENT_REVIEW_LOGIN.strip().lower()
    if not settings.GITHUB_DEVELOPMENT_AUTO_MERGE_ENABLED:
        return DeliveryResult("disabled")
    if not token or not expected_login:
        return DeliveryResult("not_configured")

    task = DevelopmentTask.objects.get(pk=task_id)
    metadata = task.automation_metadata if isinstance(task.automation_metadata, dict) else {}
    if (
        task.status != DevelopmentTask.STATUS_READY_FOR_DEPLOY
        or metadata.get(AUTO_CYCLE_METADATA_KEY) is not True
    ):
        return DeliveryResult("not_available")
    review = _accepted_review(task)
    review_meta = review.automation_metadata if review else {}
    pr_number = review_meta.get("pr_number")
    head_sha = review_meta.get("head_sha")
    if not isinstance(pr_number, int) or not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return DeliveryResult("not_available")
    delivered = metadata.get("github_delivery", {})
    if delivered.get("head_sha") == head_sha and delivered.get("state") == "auto_merge_enabled":
        return DeliveryResult("auto_merge_enabled")

    repository = settings.GITHUB_DEVELOPMENT_REPOSITORY
    if not SAFE_REPOSITORY_RE.fullmatch(repository):
        raise DevelopmentDeliveryError("invalid GitHub repository")
    api = f"https://api.github.com/repos/{repository}"
    identity = _request("GET", "https://api.github.com/user", token)
    actual_login = str(identity.get("login", "")).lower()
    if actual_login != expected_login:
        raise DevelopmentDeliveryError("review credential identity does not match configured login")
    pull = _request("GET", f"{api}/pulls/{pr_number}", token)
    snapshot = review_meta.get("evidence_snapshot", {})
    expected_head_ref = snapshot.get("head_ref")
    linkage_ok = _pull_matches_review(
        pull, repository=repository, pr_number=pr_number,
        head_sha=head_sha, head_ref=expected_head_ref,
    )
    if not linkage_ok:
        raise DevelopmentDeliveryError("pull request head changed after independent review")
    if str(pull.get("user", {}).get("login", "")).lower() == actual_login:
        raise DevelopmentDeliveryError("reviewer identity must differ from pull request author")

    reviews = _all_reviews(api, pr_number, token)
    decisive = [item for item in reviews if str(item.get("user", {}).get("login", "")).lower() == actual_login and item.get("commit_id") == head_sha and item.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}]
    latest = max(decisive, key=lambda item: (item.get("submitted_at") or "", int(item.get("id") or 0)), default={})
    already_approved = latest.get("state") == "APPROVED"
    if not already_approved:
        _request(
            "POST",
            f"{api}/pulls/{pr_number}/reviews",
            token,
            payload={"event": "APPROVE", "commit_id": head_sha, "body": "Independent service2 AI review accepted this exact head."},
            expected=(200, 201),
        )

    pull = _request("GET", f"{api}/pulls/{pr_number}", token)
    if not _pull_matches_review(
        pull, repository=repository, pr_number=pr_number,
        head_sha=head_sha, head_ref=expected_head_ref,
    ):
        raise DevelopmentDeliveryError("pull request changed before auto-merge")
    node_id = pull.get("node_id")
    if not node_id:
        raise DevelopmentDeliveryError("pull request node id is missing")
    mutation = "mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:MERGE}){pullRequest{id autoMergeRequest{enabledAt}}}}"
    graph = _request(
        "POST",
        "https://api.github.com/graphql",
        token,
        payload={"query": mutation, "variables": {"id": node_id}},
    )
    enabled = graph.get("data", {}).get("enablePullRequestAutoMerge", {}).get("pullRequest", {})
    if graph.get("errors") or enabled.get("id") != node_id or not enabled.get("autoMergeRequest", {}).get("enabledAt"):
        raise DevelopmentDeliveryError("GitHub rejected auto-merge")

    with transaction.atomic():
        locked = DevelopmentTask.objects.select_for_update().get(pk=task_id)
        current = locked.automation_metadata if isinstance(locked.automation_metadata, dict) else {}
        current["github_delivery"] = {
            "state": "auto_merge_enabled",
            "pr_number": pr_number,
            "head_sha": head_sha,
            "review_iteration_id": review.pk,
            "reviewer_login": actual_login,
        }
        locked.automation_metadata = current
        locked.current_activity = "Independent GitHub approval published; auto-merge enabled"
        locked.save(update_fields=["automation_metadata", "current_activity", "updated_at"])
        DevelopmentTaskEvent.objects.create(
            task=locked,
            event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
            message="GitHub approval published and auto-merge enabled",
            metadata={"action": "github_auto_merge_enabled", "pr_number": pr_number, "head_sha": head_sha, "review_iteration_id": review.pk},
        )
    return DeliveryResult("auto_merge_enabled", True)
