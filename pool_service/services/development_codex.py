import base64
import hashlib
import io
import json
import logging
import re
import ssl
import stat
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
    urlopen,
)
from uuid import uuid4

import certifi
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
)
from pool_service.services.development_ai import resolve_primary_analysis_iteration
from pool_service.services.ai_costs import codex_usage_record
from pool_service.services.development_db import run_external_io
from pool_service.services.development_model_selection import (
    ModelSelectionError,
    effective_model,
    selection_metadata,
)
from pool_service.services.development_notifications import notify_human_required


logger = logging.getLogger(__name__)

PROVIDER = "github_actions"
PURPOSE = "codex_execution"
AUTO_CYCLE_METADATA_KEY = "auto_cycle_enabled"
STATE_DISPATCHING = "dispatching"
STATE_DISPATCHED = "dispatched"
STATE_DISPATCH_UNKNOWN = "dispatch_unknown"
STATE_QUEUED = "queued"
STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_TIMED_OUT = "timed_out"
STATE_VALIDATION_FAILED = "validation_failed"
STATE_SECURITY_BLOCKED = "security_blocked"
STATE_INFRASTRUCTURE_FAILED = "infrastructure_failed"
STATE_NO_CHANGES = "no_changes"
ACTIVE_STATES = {
    STATE_DISPATCHING,
    STATE_DISPATCHED,
    STATE_DISPATCH_UNKNOWN,
    STATE_QUEUED,
    STATE_IN_PROGRESS,
}
RECOVERABLE_TASK_STATUSES = {
    DevelopmentTask.STATUS_READY_FOR_CODEX,
    DevelopmentTask.STATUS_CODEX_WORKING,
    DevelopmentTask.STATUS_BLOCKED,
}
GITHUB_API_URL = "https://api.github.com"
BASE_BRANCH = "main"
RUN_PAGE_SIZE = 50
SAFE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAFE_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
SAFE_BRANCH_RE = re.compile(r"^codex/dev-[0-9]+-[a-f0-9]{12}$")
CODEX_ARTIFACT_FILES = {
    "codex.patch",
    "codex-final.txt",
    "codex-usage.json",
    "manifest.json",
    "pr-title.txt",
}
CODEX_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}
CODEX_USAGE_SOURCE = "codex_exec_jsonl_turn_completed"
MAX_CODEX_ARCHIVE_BYTES = 6_000_000
MAX_CODEX_CONTENT_BYTES = 5_500_000
MAX_CODEX_SUMMARY_BYTES = 100_000
MAX_PR_TITLE_BYTES = 255
MAX_REVIEW_EVIDENCE_FILES = 200
MAX_REVIEW_EVIDENCE_PAGES = 3
MAX_REVIEW_EVIDENCE_BYTES = 120_000
REVIEW_EVIDENCE_PAGE_SIZE = 100
SHA_RE = re.compile(r"^[a-f0-9]{40}$")
PROMPT_TRUNCATION_MARKER = "[сокращено системой из-за лимита prompt]"
PROMPT_TRUNCATION_ORDER = (
    "analysis",
    "completed_work",
    "current_activity",
    "blockers",
    "business_goal",
    "description",
    "definition_of_done",
)


class CodexConfigurationError(RuntimeError):
    pass


class CodexPromptError(RuntimeError):
    pass


class GitHubRequestError(RuntimeError):
    """A diagnostic GitHub transport error containing only safe metadata."""

    def __init__(self, *, category, status_code=None, cause_type):
        self.category = category
        self.status_code = status_code
        self.cause_type = cause_type
        status = f" status={status_code}" if status_code is not None else ""
        super().__init__(
            f"GitHub API request failed: category={category}{status} cause_type={cause_type}"
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class CodexOperationResult:
    state: str
    changed: bool = False
    message: str = ""


@dataclass(frozen=True)
class CodexPromptBuild:
    prompt: str
    prompt_bytes: int
    prompt_limit_bytes: int
    truncated_sections: tuple[str, ...] = ()

    @property
    def truncated(self):
        return bool(self.truncated_sections)


@dataclass(frozen=True)
class PullRequestEvidence:
    snapshot: dict

    @property
    def pr_number(self):
        return self.snapshot["pr_number"]

    @property
    def head_sha(self):
        return self.snapshot["head_sha"]

    @property
    def sufficient(self):
        return self.snapshot["sufficient_for_automatic_acceptance"]


def is_configured():
    return bool(settings.GITHUB_DEVELOPMENT_TOKEN.strip())


def _configured_repository():
    repository = settings.GITHUB_DEVELOPMENT_REPOSITORY.strip()
    if not SAFE_REPOSITORY_RE.fullmatch(repository):
        raise CodexConfigurationError("Invalid GitHub development repository")
    owner, name = repository.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise CodexConfigurationError("Invalid GitHub development repository")
    return repository


def _configuration():
    if not settings.GITHUB_DEVELOPMENT_TOKEN.strip():
        raise CodexConfigurationError("GitHub development integration is not configured")
    repository = _configured_repository()
    workflow = settings.GITHUB_DEVELOPMENT_WORKFLOW.strip()
    if not SAFE_WORKFLOW_RE.fullmatch(workflow) or workflow.startswith("."):
        raise CodexConfigurationError("Invalid GitHub development workflow")
    return repository, workflow


def _metadata(instance):
    value = instance.automation_metadata
    return dict(value) if isinstance(value, dict) else {}


def _github_ssl_context():
    return ssl.create_default_context(cafile=certifi.where())


def _github_request_error(method, path, *, category, status_code=None, cause_type):
    error = GitHubRequestError(
        category=category,
        status_code=status_code,
        cause_type=cause_type,
    )
    logger.warning(
        "GitHub API request failed: category=%s method=%s path=%s status_code=%s cause_type=%s",
        category,
        method,
        path.partition("?")[0],
        status_code,
        cause_type,
    )
    return error


def _now_iso():
    return timezone.now().isoformat()


def _compact(text, limit=1000):
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _utf8_truncate(text, limit):
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore").rstrip()


def _branch_name(task, launch_token):
    branch = f"codex/dev-{task.pk}-{launch_token[:12]}"
    if not SAFE_BRANCH_RE.fullmatch(branch):
        raise RuntimeError("Generated branch name failed validation")
    return branch


def _render_codex_prompt(task, sections):
    return "\n".join(
        [
            "Выполни задачу разработки в текущем Django-репозитории.",
            "",
            f"Reference: {task.reference}",
            f"Название: {task.title}",
            f"Приоритет: {task.get_priority_display()}",
            "",
            "Исходная задача:",
            sections["description"],
            "",
            "Бизнес-цель:",
            sections["business_goal"],
            "",
            "Definition of Done:",
            sections["definition_of_done"],
            "",
            "Результат первичного AI-анализа:",
            sections["analysis"],
            "",
            "Текущий технический контекст:",
            f"Уже выполнено: {sections['completed_work']}",
            f"Текущая активность: {sections['current_activity']}",
            f"Blockers: {sections['blockers']}",
            "",
            "Инженерные ограничения:",
            "- Сначала исследуй текущую структуру репозитория и связанные реализации.",
            "- Работай только в пределах этой задачи и существующей архитектуры.",
            "- Сохраняй tenant isolation, permissions и целостность финансовых/пользовательских данных.",
            "- Не раскрывай и не изменяй секреты, .env и production-конфигурацию.",
            "- Не подключайся к production, Beget или production database.",
            "- Не выполняй deploy, merge, migrate production, push или commit.",
            "- Не меняй workflow-файлы и deploy-скрипты.",
            "- Не исправляй несвязанный код и не добавляй лишние функции.",
            "- Добавь необходимые локальные тесты и выполни релевантные проверки.",
            "- В финале кратко перечисли изменения, проверки и известные ограничения.",
            "Workflow сам создаст commit, push и Pull Request после изолированной проверки.",
        ]
    )


def _truncate_prompt_section(text, target_bytes):
    marker_bytes = len(PROMPT_TRUNCATION_MARKER.encode("utf-8"))
    if target_bytes < marker_bytes:
        return None
    raw = text.encode("utf-8")
    if len(raw) <= target_bytes:
        return text
    separator = "\n" if target_bytes >= marker_bytes + 1 else ""
    content_limit = target_bytes - marker_bytes - len(separator.encode("utf-8"))
    prefix = _utf8_truncate(text, max(content_limit, 0))
    return f"{prefix}{separator}{PROMPT_TRUNCATION_MARKER}" if prefix else PROMPT_TRUNCATION_MARKER


def _build_codex_prompt(task, analysis_iteration):
    analysis = (analysis_iteration.response or analysis_iteration.result_summary or "").strip()
    if not analysis:
        raise CodexPromptError("Primary AI analysis has no usable result")
    sections = {
        "description": task.description,
        "business_goal": task.business_goal or "Не указана.",
        "definition_of_done": task.definition_of_done or "Не указано.",
        "analysis": analysis,
        "completed_work": task.completed_work or "Не указано.",
        "current_activity": task.current_activity or "Не указана.",
        "blockers": task.blockers or "Нет.",
    }
    limit = settings.GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES
    prompt = _render_codex_prompt(task, sections)
    truncated_sections = []
    for name in PROMPT_TRUNCATION_ORDER:
        byte_count = len(prompt.encode("utf-8"))
        if byte_count <= limit:
            break
        value = sections[name]
        value_bytes = len(value.encode("utf-8"))
        marker_bytes = len(PROMPT_TRUNCATION_MARKER.encode("utf-8"))
        if value_bytes <= marker_bytes:
            continue
        target_bytes = max(marker_bytes, value_bytes - (byte_count - limit))
        truncated = _truncate_prompt_section(value, target_bytes)
        if truncated is None or truncated == value:
            continue
        sections[name] = truncated
        truncated_sections.append(name)
        prompt = _render_codex_prompt(task, sections)

    byte_count = len(prompt.encode("utf-8"))
    if byte_count > limit:
        raise CodexPromptError(
            f"Codex prompt exceeds the configured {limit}-byte limit"
        )
    return CodexPromptBuild(
        prompt=prompt,
        prompt_bytes=byte_count,
        prompt_limit_bytes=limit,
        truncated_sections=tuple(truncated_sections),
    )


def build_codex_prompt(task, analysis_iteration):
    return _build_codex_prompt(task, analysis_iteration).prompt


def _github_request(method, path, *, payload=None, expected_status=200):
    repository, _workflow = _configuration()
    del repository
    url = f"{GITHUB_API_URL}{path}"
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.GITHUB_DEVELOPMENT_TOKEN}",
        "User-Agent": "service2-development-automation",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=True).encode("ascii")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(
            request,
            timeout=settings.GITHUB_DEVELOPMENT_TIMEOUT_SECONDS,
            context=_github_ssl_context(),
        ) as response:
            if response.status != expected_status:
                raise _github_request_error(
                    method,
                    path,
                    category="response",
                    status_code=response.status,
                    cause_type="UnexpectedStatus",
                )
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raise _github_request_error(
            method,
            path,
            category="http",
            status_code=exc.code,
            cause_type="HTTPError",
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        cause_type = type(reason).__name__ if reason is not None else "URLError"
        raise _github_request_error(
            method,
            path,
            category="transport",
            cause_type=cause_type,
        ) from exc
    except TimeoutError as exc:
        raise _github_request_error(
            method,
            path,
            category="transport",
            cause_type="TimeoutError",
        ) from exc
    except json.JSONDecodeError as exc:
        raise _github_request_error(
            method,
            path,
            category="response",
            cause_type="JSONDecodeError",
        ) from exc


def _read_limited_response(response, limit):
    raw_length = response.headers.get("Content-Length")
    try:
        content_length = int(raw_length) if raw_length is not None else None
    except (TypeError, ValueError):
        raise GitHubRequestError(category="response", cause_type="InvalidContentLength")
    if content_length is not None and content_length > limit:
        raise GitHubRequestError(category="response", cause_type="ArtifactTooLarge")
    content = response.read(limit + 1)
    if len(content) > limit:
        raise GitHubRequestError(category="response", cause_type="ArtifactTooLarge")
    return content


def _download_artifact_archive(artifact_id):
    repository, _workflow = _configuration()
    path = f"/repos/{repository}/actions/artifacts/{int(artifact_id)}/zip"
    request = Request(
        f"{GITHUB_API_URL}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {settings.GITHUB_DEVELOPMENT_TOKEN}",
            "User-Agent": "service2-development-automation",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    opener = build_opener(
        HTTPSHandler(context=_github_ssl_context()),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(
            request,
            timeout=settings.GITHUB_DEVELOPMENT_TIMEOUT_SECONDS,
        ) as response:
            if response.status != 302:
                raise _github_request_error(
                    "GET",
                    path,
                    category="response",
                    status_code=response.status,
                    cause_type="ExpectedRedirect",
                )
            location = response.headers.get("Location")
    except HTTPError as exc:
        if exc.code != 302:
            raise _github_request_error(
                "GET",
                path,
                category="http",
                status_code=exc.code,
                cause_type="HTTPError",
            ) from exc
        location = exc.headers.get("Location")
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        raise _github_request_error(
            "GET",
            path,
            category="transport",
            cause_type=type(reason).__name__ if reason is not None else "URLError",
        ) from exc
    except TimeoutError as exc:
        raise _github_request_error(
            "GET", path, category="transport", cause_type="TimeoutError"
        ) from exc
    try:
        parsed = urlsplit(location or "")
        safe_redirect = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and not parsed.fragment
        )
    except ValueError:
        safe_redirect = False
    if not safe_redirect:
        raise _github_request_error(
            "GET", path, category="response", cause_type="UnsafeArtifactRedirect"
        )
    download = Request(
        location,
        headers={"Accept": "application/octet-stream", "User-Agent": "service2-development-automation"},
        method="GET",
    )
    try:
        with urlopen(
            download,
            timeout=settings.GITHUB_DEVELOPMENT_TIMEOUT_SECONDS,
            context=_github_ssl_context(),
        ) as response:
            if response.status != 200:
                raise _github_request_error(
                    "GET",
                    path,
                    category="response",
                    status_code=response.status,
                    cause_type="UnexpectedArtifactStatus",
                )
            return _read_limited_response(response, MAX_CODEX_ARCHIVE_BYTES)
    except HTTPError as exc:
        raise _github_request_error(
            "GET", path, category="http", status_code=exc.code, cause_type="HTTPError"
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        raise _github_request_error(
            "GET",
            path,
            category="transport",
            cause_type=type(reason).__name__ if reason is not None else "URLError",
        ) from exc
    except TimeoutError as exc:
        raise _github_request_error(
            "GET", path, category="transport", cause_type="TimeoutError"
        ) from exc


def _artifact_digest_matches(manifest, name, content):
    return (
        manifest.get(f"{name}_size") == len(content)
        and manifest.get(f"{name}_sha256") == hashlib.sha256(content).hexdigest()
    )


def _codex_artifact(run_id, task_reference, launch_token, branch_name, model, *, required=False):
    repository, _workflow = _configuration()
    artifact_name = f"codex-change-{launch_token}"
    query = urlencode({"name": artifact_name, "per_page": 100})
    data = _github_request(
        "GET", f"/repos/{repository}/actions/runs/{int(run_id)}/artifacts?{query}"
    ) or {}
    artifacts = [
        artifact
        for artifact in (data.get("artifacts") or [])
        if artifact.get("name") == artifact_name and not artifact.get("expired")
    ]
    if not artifacts and not required:
        return None
    if len(artifacts) != 1:
        raise GitHubRequestError(category="response", cause_type="ArtifactIdentityMismatch")
    artifact = artifacts[0]
    if int(artifact.get("size_in_bytes") or 0) > MAX_CODEX_ARCHIVE_BYTES:
        raise GitHubRequestError(category="response", cause_type="ArtifactTooLarge")
    archive = _download_artifact_archive(artifact["id"])
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            infos = zipped.infolist()
            names = [info.filename for info in infos]
            if len(infos) != len(CODEX_ARTIFACT_FILES) or set(names) != CODEX_ARTIFACT_FILES:
                raise GitHubRequestError(
                    category="response", cause_type="ArtifactStructureMismatch"
                )
            if any(
                info.is_dir()
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.file_size > MAX_CODEX_CONTENT_BYTES
                for info in infos
            ):
                raise GitHubRequestError(category="response", cause_type="UnsafeArtifactEntry")
            if sum(info.file_size for info in infos) > MAX_CODEX_CONTENT_BYTES:
                raise GitHubRequestError(category="response", cause_type="ArtifactTooLarge")
            files = {name: zipped.read(name) for name in CODEX_ARTIFACT_FILES}
    except GitHubRequestError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise GitHubRequestError(category="response", cause_type="InvalidArtifactZip") from exc
    try:
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitHubRequestError(category="response", cause_type="InvalidArtifactManifest") from exc
    expected_keys = {
        "task_reference",
        "launch_token",
        "branch_name",
        "workflow_run_id",
        "model",
        "result",
        "patch_sha256",
        "patch_size",
        "final_sha256",
        "final_size",
        "title_sha256",
        "title_size",
        "usage_sha256",
        "usage_size",
    }
    correlation_ok = (
        set(manifest) == expected_keys
        and manifest.get("task_reference") == task_reference
        and manifest.get("launch_token") == launch_token
        and manifest.get("branch_name") == branch_name
        and manifest.get("workflow_run_id") == int(run_id)
        and manifest.get("model") == model
        and manifest.get("result") in {"changes", STATE_NO_CHANGES}
    )
    if not correlation_ok:
        raise GitHubRequestError(category="response", cause_type="ArtifactCorrelationMismatch")
    patch = files["codex.patch"]
    final = files["codex-final.txt"]
    title = files["pr-title.txt"]
    usage_content = files["codex-usage.json"]
    if (
        (manifest.get("result") == STATE_NO_CHANGES and patch)
        or (manifest.get("result") == "changes" and not patch)
        or len(final) > MAX_CODEX_SUMMARY_BYTES
        or len(title) > MAX_PR_TITLE_BYTES
        or not _artifact_digest_matches(manifest, "patch", patch)
        or not _artifact_digest_matches(manifest, "final", final)
        or not _artifact_digest_matches(manifest, "title", title)
        or not _artifact_digest_matches(manifest, "usage", usage_content)
    ):
        raise GitHubRequestError(category="response", cause_type="ArtifactDigestMismatch")
    try:
        summary = final.decode("utf-8")
        title_text = title.decode("utf-8")
    except UnicodeError as exc:
        raise GitHubRequestError(category="response", cause_type="ArtifactEncodingError") from exc
    if not title_text.strip() or "\x00" in title_text:
        raise GitHubRequestError(category="response", cause_type="ArtifactTitleInvalid")
    try:
        usage = json.loads(usage_content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitHubRequestError(category="response", cause_type="InvalidUsageArtifact") from exc
    usage_keys = {
        "schema_version",
        "task_reference",
        "launch_token",
        "branch_name",
        "workflow_run_id",
        "model",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "usage_source",
    }
    usage_correlation_ok = (
        isinstance(usage, dict)
        and set(usage) == usage_keys
        and usage.get("schema_version") == 1
        and usage.get("task_reference") == task_reference
        and usage.get("launch_token") == launch_token
        and usage.get("branch_name") == branch_name
        and usage.get("workflow_run_id") == int(run_id)
        and usage.get("model") == model
        and model in CODEX_MODELS
        and usage.get("usage_source") == CODEX_USAGE_SOURCE
    )
    if not usage_correlation_ok:
        raise GitHubRequestError(category="response", cause_type="UsageCorrelationMismatch")
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GitHubRequestError(category="response", cause_type="InvalidUsageTokens")
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise GitHubRequestError(category="response", cause_type="InvalidCachedUsage")
    return {
        "summary": _compact(summary, 4000),
        "artifact_id": artifact["id"],
        "result": manifest["result"],
        "usage": usage,
    }


def _dispatch_workflow(payload):
    repository, workflow = _configuration()
    path = f"/repos/{repository}/actions/workflows/{quote(workflow, safe='')}/dispatches"
    return _github_request("POST", path, payload=payload, expected_status=204)


def _list_workflow_runs():
    repository, workflow = _configuration()
    query = urlencode({"event": "workflow_dispatch", "per_page": RUN_PAGE_SIZE})
    path = f"/repos/{repository}/actions/workflows/{quote(workflow, safe='')}/runs?{query}"
    return _github_request("GET", path) or {}


def _find_pull_request(branch_name):
    repository, _workflow = _configuration()
    owner = repository.split("/", 1)[0]
    query = urlencode({"state": "open", "head": f"{owner}:{branch_name}", "base": BASE_BRANCH})
    items = _github_request("GET", f"/repos/{repository}/pulls?{query}") or []
    return items[0] if len(items) == 1 else None


def _pull_request_files(number):
    repository, _workflow = _configuration()
    items = _github_request("GET", f"/repos/{repository}/pulls/{int(number)}/files?per_page=100") or []
    return [item.get("filename", "") for item in items if item.get("filename")][:100]


def _validated_repo_identity(value):
    if not isinstance(value, dict):
        return None
    name = value.get("full_name")
    return name if isinstance(name, str) else None


def _evidence_error(cause_type):
    return GitHubRequestError(category="evidence", cause_type=cause_type)


def _bounded_patch(value, remaining_bytes):
    if not isinstance(value, str):
        return None, 0, False
    raw = value.encode("utf-8")
    if len(raw) <= remaining_bytes:
        return value, len(raw), False
    if remaining_bytes <= 0:
        return "", 0, True
    bounded = raw[:remaining_bytes].decode("utf-8", errors="ignore")
    return bounded, len(bounded.encode("utf-8")), True


def is_valid_pull_request_linkage(pr_number, expected_head_ref):
    return (
        not isinstance(pr_number, bool)
        and isinstance(pr_number, int)
        and pr_number > 0
        and isinstance(expected_head_ref, str)
        and SAFE_BRANCH_RE.fullmatch(expected_head_ref) is not None
    )


def load_pull_request_evidence(pr_number, expected_head_ref):
    """Load a bounded, server-validated snapshot of the published PR."""
    if not is_valid_pull_request_linkage(pr_number, expected_head_ref):
        cause = (
            "InvalidPullRequestNumber"
            if isinstance(expected_head_ref, str)
            and SAFE_BRANCH_RE.fullmatch(expected_head_ref)
            else "InvalidExpectedHeadRef"
        )
        raise _evidence_error(cause)

    repository, _workflow = _configuration()
    path = f"/repos/{repository}/pulls/{pr_number}"
    pull = _github_request("GET", path)
    if not isinstance(pull, dict):
        raise _evidence_error("MalformedPullRequest")
    base = pull.get("base")
    head = pull.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise _evidence_error("MalformedPullRequestRefs")
    if pull.get("number") != pr_number:
        raise _evidence_error("PullRequestNumberMismatch")
    if _validated_repo_identity(base.get("repo")) != repository:
        raise _evidence_error("BaseRepositoryMismatch")
    if base.get("ref") != BASE_BRANCH:
        raise _evidence_error("BaseRefMismatch")
    if _validated_repo_identity(head.get("repo")) != repository:
        raise _evidence_error("HeadRepositoryMismatch")
    if head.get("ref") != expected_head_ref:
        raise _evidence_error("HeadRefMismatch")
    base_sha = base.get("sha")
    head_sha = head.get("sha")
    if not isinstance(base_sha, str) or not SHA_RE.fullmatch(base_sha):
        raise _evidence_error("InvalidBaseSha")
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        raise _evidence_error("InvalidHeadSha")
    state = pull.get("state")
    if state not in {"open", "closed"}:
        raise _evidence_error("InvalidPullRequestState")
    total_files = pull.get("changed_files")
    if isinstance(total_files, bool) or not isinstance(total_files, int) or total_files < 0:
        raise _evidence_error("InvalidChangedFileCount")

    files = []
    included_bytes = 0
    truncation_reasons = []
    missing_patch = False
    for page in range(1, MAX_REVIEW_EVIDENCE_PAGES + 1):
        if len(files) >= min(total_files, MAX_REVIEW_EVIDENCE_FILES):
            break
        query = urlencode({"per_page": REVIEW_EVIDENCE_PAGE_SIZE, "page": page})
        items = _github_request("GET", f"{path}/files?{query}")
        if not isinstance(items, list):
            raise _evidence_error("MalformedPullRequestFiles")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise _evidence_error("MalformedPullRequestFile")
            if len(files) >= MAX_REVIEW_EVIDENCE_FILES:
                truncation_reasons.append("file_limit")
                break
            remaining = MAX_REVIEW_EVIDENCE_BYTES - included_bytes
            patch, patch_bytes, patch_truncated = _bounded_patch(item.get("patch"), remaining)
            if patch is None:
                missing_patch = True
            files.append(
                {
                    "filename": item["filename"],
                    "status": str(item.get("status") or ""),
                    "additions": int(item.get("additions") or 0),
                    "deletions": int(item.get("deletions") or 0),
                    "changes": int(item.get("changes") or 0),
                    "patch": patch,
                    "patch_truncated": patch_truncated,
                }
            )
            included_bytes += patch_bytes
            if patch_truncated:
                truncation_reasons.append("byte_limit")
                break
        if "byte_limit" in truncation_reasons or len(items) < REVIEW_EVIDENCE_PAGE_SIZE:
            break

    if len(files) < total_files:
        if len(files) >= MAX_REVIEW_EVIDENCE_FILES:
            truncation_reasons.append("file_limit")
        elif "byte_limit" not in truncation_reasons:
            truncation_reasons.append("page_limit")
    if missing_patch:
        truncation_reasons.append("missing_patch")
    reasons = tuple(dict.fromkeys(truncation_reasons))
    evidence_body = {
        "repository": repository,
        "pr_number": pr_number,
        "state": state,
        "base_ref": base["ref"],
        "base_sha": base_sha,
        "head_ref": head["ref"],
        "head_sha": head_sha,
        "head_repository": repository,
        "changed_files": files,
        "truncated": bool(reasons),
        "truncation_reason": list(reasons),
        "included_file_count": len(files),
        "total_file_count": total_files,
        "included_bytes": included_bytes,
        "sufficient_for_automatic_acceptance": not reasons and len(files) == total_files,
    }
    digest_source = json.dumps(
        evidence_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    evidence_body["evidence_sha256"] = hashlib.sha256(digest_source).hexdigest()
    evidence_body["fetched_at"] = _now_iso()
    return PullRequestEvidence(evidence_body)


def _workflow_validation_state(run_id):
    repository, _workflow = _configuration()
    data = _github_request(
        "GET", f"/repos/{repository}/actions/runs/{int(run_id)}/jobs?per_page=100"
    ) or {}
    states = []
    for job in data.get("jobs") or []:
        name = str(job.get("name") or "")
        if name.startswith("outcome-"):
            states.append(name.removeprefix("outcome-"))
    allowed = {
        "passed",
        "failed",
        STATE_NO_CHANGES,
        STATE_SECURITY_BLOCKED,
        STATE_INFRASTRUCTURE_FAILED,
    }
    return states[0] if len(states) == 1 and states[0] in allowed else STATE_INFRASTRUCTURE_FAILED


def _safe_github_url(value, repository):
    prefix = f"https://github.com/{repository}/"
    value = str(value or "")
    return value if value.startswith(prefix) else ""


def _workflow_run_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        run_id = int(value)
        return run_id if run_id > 0 else None
    return None


def github_actions_run_url(run_id):
    """Build a run URL exclusively from trusted server configuration."""
    normalized_run_id = _workflow_run_id(run_id)
    if normalized_run_id is None:
        return ""
    try:
        repository = _configured_repository()
    except CodexConfigurationError:
        return ""
    return f"https://github.com/{repository}/actions/runs/{normalized_run_id}"


def _workflow_test_result(pr_body):
    match = re.search(r"<!--\s*codex-tests:\s*(passed|failed)\s*-->", pr_body or "")
    if not match:
        return "Статус автоматических проверок не удалось определить из Pull Request."
    return (
        "Автоматические проверки GitHub Actions прошли успешно."
        if match.group(1) == "passed"
        else "Автоматические проверки GitHub Actions завершились с ошибками."
    )


def _active_iteration(task):
    task_metadata = _metadata(task)
    try:
        iteration_id = int(task_metadata.get("active_codex_iteration_id"))
    except (TypeError, ValueError):
        iteration_id = None
    if iteration_id is not None:
        iteration = task.iterations.filter(
            pk=iteration_id,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            automation_metadata__purpose=PURPOSE,
        ).first()
        if iteration is not None:
            return iteration
    candidates = task.iterations.filter(
        executor_type=DevelopmentIteration.EXECUTOR_CODEX,
        automation_metadata__purpose=PURPOSE,
    ).order_by("-id")
    for iteration in candidates:
        metadata = _metadata(iteration)
        if not metadata.get("applied") and metadata.get("state") in ACTIVE_STATES:
            return iteration
    return None


def resolve_codex_iteration(task):
    return _active_iteration(task)


def _create_event(task, actor_id, message, *, action, iteration, old_status, extra=None):
    metadata = {
        "action": action,
        "old_status": old_status,
        "new_status": task.status,
        "iteration_id": iteration.pk,
        "iteration_number": iteration.iteration_number,
    }
    metadata.update(extra or {})
    DevelopmentTaskEvent.objects.create(
        task=task,
        event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
        message=message,
        actor_id=actor_id,
        metadata=metadata,
    )


def dispatch_codex(task_id, actor_id):
    if not is_configured():
        return CodexOperationResult("not_configured")
    _configuration()

    with transaction.atomic():
        task = DevelopmentTask.objects.select_for_update().get(pk=task_id)
        existing = _active_iteration(task)
        if existing is not None and not _metadata(existing).get("applied"):
            state = _metadata(existing).get("state", STATE_DISPATCHED)
            return CodexOperationResult(state, changed=False)
        if task.status != DevelopmentTask.STATUS_READY_FOR_CODEX:
            return CodexOperationResult("not_available")
        analysis = resolve_primary_analysis_iteration(task)
        if analysis is None or analysis.status != DevelopmentIteration.STATUS_ACCEPTED:
            return CodexOperationResult("not_available")
        task_metadata = _metadata(task)
        if "auto_complexity" not in task_metadata and "auto_selected_model" not in task_metadata:
            task_metadata = selection_metadata(task, analysis.response)
        try:
            selected_model = effective_model(
                task_metadata.get("model_selection_mode", "auto"),
                task_metadata.get("auto_selected_model"),
            )
        except ModelSelectionError:
            return CodexOperationResult("invalid_model")
        task_metadata["effective_model"] = selected_model
        try:
            prompt_build = _build_codex_prompt(task, analysis)
        except CodexPromptError:
            return CodexOperationResult("prompt_too_large")
        launch_token = uuid4().hex
        next_number = (task.iterations.aggregate(value=Max("iteration_number"))["value"] or 0) + 1
        branch_name = _branch_name(task, launch_token)
        iteration = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=next_number,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt=prompt_build.prompt,
            result_summary="Запрос на выполнение Codex подготавливается.",
            started_at=timezone.now(),
            automation_metadata={
                "purpose": PURPOSE,
                "provider": PROVIDER,
                "state": STATE_DISPATCHING,
                "launch_token": launch_token,
                "branch_name": branch_name,
                "launch_started_at": _now_iso(),
                "effective_model": selected_model,
                "prompt_bytes": prompt_build.prompt_bytes,
                "prompt_limit_bytes": prompt_build.prompt_limit_bytes,
                "prompt_truncated": prompt_build.truncated,
                "truncated_sections": list(prompt_build.truncated_sections),
            },
        )
        task_metadata.update(
            {
                AUTO_CYCLE_METADATA_KEY: True,
                "active_codex_iteration_id": iteration.pk,
                "codex_launch_token": launch_token,
                "codex_branch_name": branch_name,
                "codex_model": selected_model,
            }
        )
        task.automation_metadata = task_metadata
        task.current_activity = "Подготавливается передача задачи в GitHub Actions"
        task.save(update_fields=["automation_metadata", "current_activity", "updated_at"])

    payload = {
        "ref": BASE_BRANCH,
        "inputs": {
            "task_reference": task.reference,
            "launch_token": launch_token,
            "branch_name": branch_name,
            "codex_model": selected_model,
            "prompt_b64": base64.b64encode(prompt_build.prompt.encode("utf-8")).decode("ascii"),
            "pr_title_b64": base64.b64encode(
                _utf8_truncate(f"[{task.reference}] {task.title}", 240).encode("utf-8")
            ).decode("ascii"),
        },
    }
    try:
        run_external_io(_dispatch_workflow, payload)
    except Exception as exc:
        logger.warning(
            "Development Codex dispatch outcome unknown: task=%s iteration=%s error_type=%s",
            task_id,
            iteration.pk,
            type(exc).__name__,
        )
        with transaction.atomic():
            locked = DevelopmentIteration.objects.select_for_update().select_related("task").get(pk=iteration.pk)
            metadata = _metadata(locked)
            if metadata.get("launch_token") == launch_token:
                metadata.update({"state": STATE_DISPATCH_UNKNOWN, "dispatch_checked_at": _now_iso()})
                locked.automation_metadata = metadata
                locked.technical_errors = (
                    "Не удалось однозначно подтвердить запуск GitHub Actions. "
                    "Автоматический повтор заблокирован."
                )
                locked.save(update_fields=["automation_metadata", "technical_errors", "updated_at"])
                task = locked.task
                old_status = task.status
                if task.status == DevelopmentTask.STATUS_READY_FOR_CODEX:
                    task.status = DevelopmentTask.STATUS_BLOCKED
                    task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
                    task.current_activity = "Требуется ручная проверка запуска Codex"
                    task.blockers = locked.technical_errors
                    task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
                    _create_event(
                        task,
                        actor_id,
                        "Запуск Codex требует ручной проверки",
                        action="codex_dispatch_unknown",
                        iteration=locked,
                        old_status=old_status,
                    )
                else:
                    DevelopmentTaskEvent.objects.create(
                        task=task,
                        event_type=DevelopmentTaskEvent.TYPE_NOTE,
                        message="Исход запуска Codex неизвестен; состояние задачи было изменено вручную",
                        actor_id=actor_id,
                        metadata={
                            "action": "codex_dispatch_unknown_task_state_changed",
                            "iteration_id": locked.pk,
                            "iteration_number": locked.iteration_number,
                            "task_status": task.status,
                        },
                    )
        return CodexOperationResult(STATE_DISPATCH_UNKNOWN, changed=True)

    with transaction.atomic():
        locked = DevelopmentIteration.objects.select_for_update().select_related("task").get(pk=iteration.pk)
        metadata = _metadata(locked)
        if metadata.get("launch_token") != launch_token:
            raise RuntimeError("Codex dispatch ownership changed unexpectedly")
        metadata.update({"state": STATE_DISPATCHED, "dispatched_at": _now_iso()})
        locked.automation_metadata = metadata
        locked.result_summary = "Задача передана в GitHub Actions."
        locked.save(update_fields=["automation_metadata", "result_summary", "updated_at"])
        task = locked.task
        if task.status != DevelopmentTask.STATUS_READY_FOR_CODEX:
            DevelopmentTaskEvent.objects.create(
                task=task,
                event_type=DevelopmentTaskEvent.TYPE_NOTE,
                message="Codex запущен, но состояние задачи было изменено вручную",
                actor_id=actor_id,
                metadata={
                    "action": "codex_dispatched_task_state_changed",
                    "iteration_id": locked.pk,
                    "iteration_number": locked.iteration_number,
                    "task_status": task.status,
                },
            )
            return CodexOperationResult("task_state_changed", changed=True)
        old_status = task.status
        task.status = DevelopmentTask.STATUS_CODEX_WORKING
        task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
        task.current_activity = "Codex выполняет задачу в GitHub Actions"
        task.blockers = ""
        task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
        _create_event(
            task,
            actor_id,
            "Задача передана в Codex",
            action="codex_dispatched",
            iteration=locked,
            old_status=old_status,
        )
    return CodexOperationResult(STATE_DISPATCHED, changed=True)


def _corrective_dispatch_is_stale(iteration, metadata):
    started_at = parse_datetime(str(metadata.get("launch_started_at") or ""))
    if started_at is None:
        started_at = iteration.updated_at
    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at, timezone.get_current_timezone())
    grace_seconds = max(
        60,
        int(settings.GITHUB_DEVELOPMENT_TIMEOUT_SECONDS) * 2,
    )
    return timezone.now() - started_at >= timedelta(seconds=grace_seconds)


def _mark_corrective_dispatch_unknown(iteration_id, *, error_type):
    message = "Не удалось однозначно подтвердить запуск corrective Codex."
    with transaction.atomic():
        locked = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration_id)
        )
        metadata = _metadata(locked)
        if metadata.get("state") != STATE_DISPATCHING:
            return CodexOperationResult(
                metadata.get("state", STATE_DISPATCH_UNKNOWN), changed=False
            )
        metadata.update(
            {
                "state": STATE_DISPATCH_UNKNOWN,
                "dispatch_checked_at": _now_iso(),
                "dispatch_error_type": error_type,
            }
        )
        locked.automation_metadata = metadata
        locked.technical_errors = message
        locked.save(
            update_fields=["automation_metadata", "technical_errors", "updated_at"]
        )
        task = locked.task
        old_status = task.status
        task.status = DevelopmentTask.STATUS_BLOCKED
        task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
        task.current_activity = "Требуется ручная проверка запуска corrective Codex"
        task.blockers = message
        task.save(
            update_fields=[
                "status",
                "current_stage",
                "current_activity",
                "blockers",
                "updated_at",
            ]
        )
        _create_event(
            task,
            None,
            "Запуск corrective Codex требует проверки",
            action="corrective_codex_dispatch_unknown",
            iteration=locked,
            old_status=old_status,
            extra={"error_type": error_type},
        )
        notify_human_required(
            task,
            message,
            dedupe_suffix=f"corrective-dispatch-unknown:{locked.pk}",
        )
    return CodexOperationResult(STATE_DISPATCH_UNKNOWN, changed=True)


def _reconcile_corrective_dispatch(iteration_id):
    with transaction.atomic():
        iteration = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration_id)
        )
        metadata = _metadata(iteration)
        if metadata.get("state") != STATE_DISPATCHING:
            return CodexOperationResult(
                metadata.get("state", STATE_DISPATCHED), changed=False
            )
        if iteration.task.status != DevelopmentTask.STATUS_REVISION:
            return CodexOperationResult("task_state_changed", changed=False)
        if not _corrective_dispatch_is_stale(iteration, metadata):
            return CodexOperationResult(STATE_DISPATCHING, changed=False)
        launch_token = metadata.get("launch_token")
        task = iteration.task

    try:
        run = run_external_io(_find_matching_run, task, iteration)
    except Exception as exc:
        logger.warning(
            "Corrective Codex reconciliation failed: task=%s iteration=%s error_type=%s",
            task.pk,
            iteration.pk,
            type(exc).__name__,
        )
        return _mark_corrective_dispatch_unknown(
            iteration.pk, error_type=type(exc).__name__
        )
    run_id = _workflow_run_id(run.get("id")) if run else None
    if run_id is None or not _remember_workflow_run(
        iteration.pk, launch_token, run_id
    ):
        return _mark_corrective_dispatch_unknown(
            iteration.pk, error_type="WorkflowRunNotFound"
        )

    with transaction.atomic():
        locked = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration.pk)
        )
        metadata = _metadata(locked)
        if metadata.get("launch_token") != launch_token:
            return CodexOperationResult("ownership_changed", changed=False)
        if metadata.get("state") != STATE_DISPATCHING:
            return CodexOperationResult(
                metadata.get("state", STATE_DISPATCHED), changed=False
            )
        metadata.update(
            {
                "state": STATE_DISPATCHED,
                "dispatched_at": _now_iso(),
                "dispatch_reconciled": True,
            }
        )
        locked.automation_metadata = metadata
        locked.result_summary = "Corrective Codex найден в GitHub Actions."
        locked.save(
            update_fields=["automation_metadata", "result_summary", "updated_at"]
        )
        task = locked.task
        if task.status != DevelopmentTask.STATUS_REVISION:
            return CodexOperationResult("task_state_changed", changed=True)
        old_status = task.status
        corrective_number = metadata.get("corrective_number")
        task.status = DevelopmentTask.STATUS_CODEX_WORKING
        task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
        task.current_activity = f"Codex выполняет корректировку {corrective_number}"
        task.blockers = ""
        task.save(
            update_fields=[
                "status",
                "current_stage",
                "current_activity",
                "blockers",
                "updated_at",
            ]
        )
        _create_event(
            task,
            None,
            "Автоматический запуск corrective Codex восстановлен",
            action="corrective_codex_dispatch_reconciled",
            iteration=locked,
            old_status=old_status,
            extra={
                "review_id": metadata.get("corrective_review_id"),
                "corrective_number": corrective_number,
                "workflow_run_id": run_id,
            },
        )
    return CodexOperationResult(STATE_DISPATCHED, changed=True)


def dispatch_corrective_codex(task_id, review_id):
    """Launch or safely reconcile one corrective execution per AI Review."""
    if not is_configured():
        return CodexOperationResult("not_configured")
    _configuration()
    existing = DevelopmentIteration.objects.filter(
        task_id=task_id,
        executor_type=DevelopmentIteration.EXECUTOR_CODEX,
        automation_metadata__corrective_review_id=review_id,
    ).first()
    if existing is not None:
        state = _metadata(existing).get("state", STATE_DISPATCHED)
        if state == STATE_DISPATCHING:
            return _reconcile_corrective_dispatch(existing.pk)
        return CodexOperationResult(state, changed=False)
    return _dispatch_new_corrective_codex(task_id, review_id)


def _corrective_review_data(review):
    metadata = _metadata(review)
    if metadata.get("decision") == "corrective_required":
        instructions = metadata.get("corrective_instructions") or []
        fingerprint = metadata.get("fingerprint")
    elif (
        metadata.get("decision") == "human_required"
        and metadata.get("human_resolution") == "corrective"
    ):
        note = str(metadata.get("human_resolution_note") or "").strip()
        if not note:
            return None
        instructions = [note]
        fingerprint = metadata.get("human_resolution_fingerprint")
    else:
        return None
    if not instructions or not all(
        isinstance(item, str) and item.strip() for item in instructions
    ):
        return None
    return {"instructions": instructions, "fingerprint": fingerprint}


def _dispatch_new_corrective_codex(task_id, review_id):
    """Launch one bounded corrective execution; the review row is its idempotency key."""
    if not is_configured():
        return CodexOperationResult("not_configured")
    _configuration()
    with transaction.atomic():
        task = DevelopmentTask.objects.select_for_update().get(pk=task_id)
        if task.status != DevelopmentTask.STATUS_REVISION:
            return CodexOperationResult("not_available")
        review = task.iterations.filter(
            pk=review_id,
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            automation_metadata__purpose="ai_review",
        ).first()
        review_data = _corrective_review_data(review) if review is not None else None
        if review is None or review_data is None:
            return CodexOperationResult("not_available")
        existing = task.iterations.filter(
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            automation_metadata__corrective_review_id=review.pk,
        ).first()
        if existing:
            return CodexOperationResult(_metadata(existing).get("state", STATE_DISPATCHED))
        previous = task.iterations.filter(
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            automation_metadata__purpose=PURPOSE,
        )
        corrective_number = previous.filter(automation_metadata__corrective_number__gt=0).count() + 1
        if corrective_number > settings.DEVELOPMENT_MAX_CORRECTIVE_ITERATIONS:
            task.status = DevelopmentTask.STATUS_BLOCKED
            task.current_activity = "Достигнут лимит автоматических корректировок"
            task.blockers = "Требуется решение человека после достижения лимита корректировок."
            task.save(update_fields=["status", "current_activity", "blockers", "updated_at"])
            DevelopmentTaskEvent.objects.create(
                task=task, event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
                message="Достигнут лимит автоматических корректировок",
                metadata={"action": "corrective_limit_reached", "review_id": review.pk},
            )
            return CodexOperationResult("limit_reached", changed=True)
        fingerprint = review_data["fingerprint"]
        earlier_reviews = task.iterations.filter(
            executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
            automation_metadata__purpose="ai_review",
        ).exclude(pk=review.pk)
        repeated_fingerprint = bool(
            fingerprint
            and any(
                (data := _corrective_review_data(item))
                and data["fingerprint"] == fingerprint
                for item in earlier_reviews
            )
        )
        if repeated_fingerprint:
            task.status = DevelopmentTask.STATUS_BLOCKED
            task.current_activity = "Автоматическая корректировка остановлена"
            task.blockers = "AI Review повторил те же замечания; требуется решение человека."
            task.save(update_fields=["status", "current_activity", "blockers", "updated_at"])
            DevelopmentTaskEvent.objects.create(
                task=task, event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
                message="Обнаружен повторяющийся цикл замечаний",
                metadata={"action": "corrective_loop_detected", "review_id": review.pk, "fingerprint": fingerprint},
            )
            return CodexOperationResult("loop_detected", changed=True)
        task_meta = _metadata(task)
        selected_model = task_meta.get("effective_model") or task_meta.get("codex_model")
        if selected_model not in CODEX_MODELS:
            return CodexOperationResult("invalid_model")
        instructions = review_data["instructions"]
        previous_codex_id = _metadata(review).get("codex_iteration_id")
        prompt = "\n".join([
            "Выполни только необходимые исправления по результатам AI Review.",
            f"Задача: {task.reference} — {task.title}",
            "Замечания и критерии исправления:",
            *[f"- {item}" for item in instructions],
            f"Предыдущая Codex iteration: {previous_codex_id}",
            "Не выполняй deploy, merge, push или commit вручную. Не изменяй нерелевантную область.",
            "Добавь/обнови локальные тесты и кратко перечисли проверки.",
        ])
        if len(prompt.encode("utf-8")) > settings.GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES:
            return CodexOperationResult("prompt_too_large")
        launch_token = uuid4().hex
        number = (task.iterations.aggregate(value=Max("iteration_number"))["value"] or 0) + 1
        branch_name = _branch_name(task, launch_token)
        iteration = DevelopmentIteration.objects.create(
            task=task, iteration_number=number,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_WORKING, prompt=prompt,
            result_summary="Корректировка Codex подготавливается.", started_at=timezone.now(),
            automation_metadata={
                "purpose": PURPOSE, "provider": PROVIDER, "state": STATE_DISPATCHING,
                "launch_token": launch_token, "branch_name": branch_name,
                "launch_started_at": _now_iso(), "effective_model": selected_model,
                "corrective_number": corrective_number, "corrective_review_id": review.pk,
                "previous_codex_iteration_id": previous_codex_id,
                "prompt_bytes": len(prompt.encode("utf-8")),
            },
        )
        task_meta.update(
            {
                AUTO_CYCLE_METADATA_KEY: True,
                "active_codex_iteration_id": iteration.pk,
                "codex_launch_token": launch_token,
                "codex_branch_name": branch_name,
                "codex_model": selected_model,
            }
        )
        task.automation_metadata = task_meta
        task.current_activity = f"Запускается корректировка Codex {corrective_number}"
        task.save(update_fields=["automation_metadata", "current_activity", "updated_at"])

    payload = {"ref": BASE_BRANCH, "inputs": {
        "task_reference": task.reference, "launch_token": launch_token,
        "branch_name": branch_name, "codex_model": selected_model,
        "prompt_b64": base64.b64encode(prompt.encode()).decode("ascii"),
        "pr_title_b64": base64.b64encode(_utf8_truncate(f"[{task.reference}] corrective {corrective_number}: {task.title}", 240).encode()).decode("ascii"),
    }}
    try:
        run_external_io(_dispatch_workflow, payload)
    except Exception as exc:
        logger.warning("Corrective Codex dispatch outcome unknown: task=%s iteration=%s error_type=%s", task_id, iteration.pk, type(exc).__name__)
        return _mark_corrective_dispatch_unknown(
            iteration.pk, error_type=type(exc).__name__
        )
    with transaction.atomic():
        locked = DevelopmentIteration.objects.select_for_update().select_related("task").get(pk=iteration.pk)
        metadata = _metadata(locked)
        metadata.update({"state": STATE_DISPATCHED, "dispatched_at": _now_iso()})
        locked.automation_metadata = metadata
        locked.result_summary = "Corrective Codex передан в GitHub Actions."
        locked.save(update_fields=["automation_metadata", "result_summary", "updated_at"])
        task = locked.task
        old_status = task.status
        task.status = DevelopmentTask.STATUS_CODEX_WORKING
        task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
        task.current_activity = f"Codex выполняет корректировку {corrective_number}"
        task.blockers = ""
        task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
        _create_event(task, None, "Автоматическая корректировка передана в Codex", action="corrective_codex_dispatched", iteration=locked, old_status=old_status, extra={"review_id": review.pk, "corrective_number": corrective_number})
    return CodexOperationResult(STATE_DISPATCHED, changed=True)


def _find_matching_run(task, iteration):
    metadata = _metadata(iteration)
    expected_title = f"development-{task.reference}-{metadata.get('launch_token', '')}"
    matches = [
        run
        for run in (_list_workflow_runs().get("workflow_runs") or [])
        if run.get("display_title") == expected_title and run.get("head_branch") == BASE_BRANCH
    ]
    return matches[0] if len(matches) == 1 else None


def _remember_workflow_run(iteration_id, launch_token, run_id):
    normalized_run_id = _workflow_run_id(run_id)
    if normalized_run_id is None:
        return False
    with transaction.atomic():
        iteration = DevelopmentIteration.objects.select_for_update().get(pk=iteration_id)
        metadata = _metadata(iteration)
        if metadata.get("launch_token") != launch_token:
            return False
        existing_run_id = _workflow_run_id(metadata.get("workflow_run_id"))
        if existing_run_id is not None and existing_run_id != normalized_run_id:
            return False
        if (
            metadata.get("workflow_run_id") == normalized_run_id
            and "workflow_run_url" not in metadata
        ):
            return True
        metadata["workflow_run_id"] = normalized_run_id
        # Legacy API-derived URLs are intentionally never trusted by the UI.
        metadata.pop("workflow_run_url", None)
        iteration.automation_metadata = metadata
        iteration.save(update_fields=["automation_metadata", "updated_at"])
    return True


def _store_codex_usage(metadata, usage):
    record = codex_usage_record(usage)
    if record is None:
        raise GitHubRequestError(category="response", cause_type="InvalidUsageRecord")
    ai_usage = metadata.get("ai_usage")
    if ai_usage is None:
        metadata["ai_usage"] = {"stage": "codex", "status": "known", "calls": [record]}
        return
    if not isinstance(ai_usage, dict) or ai_usage.get("stage") != "codex":
        raise GitHubRequestError(category="response", cause_type="UsageHistoryMismatch")
    calls = ai_usage.get("calls")
    if not isinstance(calls, list):
        raise GitHubRequestError(category="response", cause_type="UsageHistoryMismatch")
    identity = (record["launch_token"], record["workflow_run_id"])
    matching = [
        call for call in calls
        if isinstance(call, dict)
        and (call.get("launch_token"), call.get("workflow_run_id")) == identity
    ]
    if matching:
        if len(matching) != 1 or matching[0] != record:
            raise GitHubRequestError(category="response", cause_type="UsageHistoryMismatch")
        return
    if calls:
        raise GitHubRequestError(category="response", cause_type="UsageExecutionMismatch")
    ai_usage["status"] = "known"
    ai_usage["calls"] = [record]
    metadata["ai_usage"] = ai_usage


def check_codex(task_id, actor_id):
    if not is_configured():
        return CodexOperationResult("not_configured")
    repository, _workflow = _configuration()
    with transaction.atomic():
        task = DevelopmentTask.objects.select_for_update().get(pk=task_id)
        iteration = _active_iteration(task)
        if iteration is None:
            return CodexOperationResult("not_available")
        metadata = _metadata(iteration)
        if metadata.get("applied"):
            return CodexOperationResult(metadata.get("state", STATE_COMPLETED), changed=False)
        if metadata.get("state") not in ACTIVE_STATES:
            return CodexOperationResult("not_available")
        if task.status not in RECOVERABLE_TASK_STATUSES:
            return CodexOperationResult("task_state_changed")
        iteration_id = iteration.pk
        launch_token = metadata.get("launch_token")
        branch_name = metadata.get("branch_name")

    try:
        run = run_external_io(_find_matching_run, task, iteration)
    except Exception as exc:
        logger.warning(
            "Development Codex check failed: task=%s iteration=%s error_type=%s",
            task_id,
            iteration_id,
            type(exc).__name__,
        )
        return CodexOperationResult("check_failed")
    if run is None:
        return CodexOperationResult("not_found")

    run_id = _workflow_run_id(run.get("id"))
    if run_id is None or not _remember_workflow_run(iteration_id, launch_token, run_id):
        return CodexOperationResult("not_found")
    run = dict(run)
    run["id"] = run_id

    run_status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    validation_state = ""
    remote_state = STATE_QUEUED if run_status == "queued" else STATE_IN_PROGRESS
    if run_status == "completed":
        try:
            validation_state = run_external_io(
                _workflow_validation_state, run["id"]
            )
        except Exception as exc:
            logger.warning(
                "Development Codex outcome lookup failed: task=%s iteration=%s error_type=%s",
                task_id,
                iteration_id,
                type(exc).__name__,
            )
            return CodexOperationResult("check_failed")
        if validation_state == "passed" and conclusion == "success":
            remote_state = STATE_COMPLETED
        elif validation_state == STATE_NO_CHANGES and conclusion == "success":
            remote_state = STATE_NO_CHANGES
        elif validation_state == "failed":
            remote_state = STATE_VALIDATION_FAILED
        elif validation_state == STATE_SECURITY_BLOCKED:
            remote_state = STATE_SECURITY_BLOCKED
        elif conclusion in {STATE_CANCELLED, STATE_TIMED_OUT}:
            remote_state = conclusion
        else:
            remote_state = STATE_INFRASTRUCTURE_FAILED

    pull_request = None
    changed_files = []
    codex_artifact = None
    if run_status == "completed":
        try:
            codex_artifact = run_external_io(
                _codex_artifact,
                run_id,
                task.reference,
                launch_token,
                branch_name,
                metadata.get("effective_model"),
                required=remote_state == STATE_NO_CHANGES,
            )
            expected_artifact_result = (
                STATE_NO_CHANGES if validation_state == STATE_NO_CHANGES
                else "changes" if validation_state in {"passed", "failed", STATE_SECURITY_BLOCKED}
                else None
            )
            if (
                codex_artifact
                and expected_artifact_result
                and codex_artifact["result"] != expected_artifact_result
            ):
                raise GitHubRequestError(
                    category="response", cause_type="ArtifactOutcomeMismatch"
                )
        except Exception as exc:
            logger.warning(
                "Development Codex artifact lookup failed: task=%s iteration=%s error_type=%s",
                task_id,
                iteration_id,
                type(exc).__name__,
            )
            return CodexOperationResult("check_failed")
    if remote_state in {STATE_COMPLETED, STATE_VALIDATION_FAILED}:
        try:
            pull_request = run_external_io(_find_pull_request, branch_name)
            if pull_request:
                changed_files = run_external_io(
                    _pull_request_files, pull_request["number"]
                )
        except Exception as exc:
            logger.warning(
                "Development Codex pull request lookup failed: task=%s iteration=%s error_type=%s",
                task_id,
                iteration_id,
                type(exc).__name__,
            )
            return CodexOperationResult("check_failed")

    with transaction.atomic():
        locked = DevelopmentIteration.objects.select_for_update().select_related("task").get(pk=iteration_id)
        task = locked.task
        metadata = _metadata(locked)
        if metadata.get("launch_token") != launch_token or metadata.get("applied"):
            return CodexOperationResult(metadata.get("state", "not_available"), changed=False)
        if task.status not in RECOVERABLE_TASK_STATUSES:
            return CodexOperationResult("task_state_changed")
        metadata.update(
            {
                "state": remote_state,
                "workflow_run_id": run_id,
                "checked_at": _now_iso(),
                "validation_state": validation_state,
            }
        )
        if codex_artifact:
            try:
                _store_codex_usage(metadata, codex_artifact["usage"])
            except GitHubRequestError:
                return CodexOperationResult("check_failed")
            metadata["artifact_id"] = codex_artifact["artifact_id"]
        locked.automation_metadata = metadata
        if remote_state in {STATE_QUEUED, STATE_IN_PROGRESS}:
            locked.save(update_fields=["automation_metadata", "updated_at"])
            if task.status != DevelopmentTask.STATUS_CODEX_WORKING:
                old_status = task.status
                task.status = DevelopmentTask.STATUS_CODEX_WORKING
                task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
                task.current_activity = "Codex выполняет задачу в GitHub Actions"
                task.blockers = ""
                task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
                _create_event(
                    task,
                    actor_id,
                    "Запуск Codex найден в GitHub Actions",
                    action="codex_run_found",
                    iteration=locked,
                    old_status=old_status,
                )
            return CodexOperationResult(remote_state, changed=True)

        now = timezone.now()
        old_status = task.status
        metadata.update({"applied": True, "completed_at": now.isoformat()})
        locked.completed_at = now
        if remote_state == STATE_NO_CHANGES:
            for key in ("pr_number", "pr_url", "pr_title"):
                metadata.pop(key, None)
            locked.automation_metadata = metadata
            locked.status = DevelopmentIteration.STATUS_ACCEPTED
            locked.result_summary = "Codex не предложил изменений; результат передан на review."
            locked.response = codex_artifact["summary"] or "Codex завершил анализ без изменений."
            locked.changed_files = ""
            locked.test_result = "Trusted artifact подтверждает отсутствие изменений."
            locked.technical_errors = ""
            locked.save(
                update_fields=[
                    "automation_metadata",
                    "status",
                    "result_summary",
                    "response",
                    "changed_files",
                    "test_result",
                    "technical_errors",
                    "completed_at",
                    "updated_at",
                ]
            )
            task.status = DevelopmentTask.STATUS_REVIEW
            task.current_stage = DevelopmentTask.STAGE_REVIEW
            task.current_activity = "Codex не предложил изменений; требуется проверка результата"
            task.blockers = ""
            task.save(
                update_fields=[
                    "status",
                    "current_stage",
                    "current_activity",
                    "blockers",
                    "updated_at",
                ]
            )
            _create_event(
                task,
                actor_id,
                "Codex завершил работу без изменений",
                action="codex_no_changes",
                iteration=locked,
                old_status=old_status,
            )
            return CodexOperationResult(STATE_NO_CHANGES, changed=True)
        if remote_state == STATE_COMPLETED and pull_request:
            pr_url = _safe_github_url(pull_request.get("html_url"), repository)
            metadata.update(
                {
                    "pr_number": pull_request.get("number"),
                    "pr_url": pr_url,
                    "pr_title": _compact(pull_request.get("title", ""), 255),
                }
            )
            locked.automation_metadata = metadata
            locked.status = DevelopmentIteration.STATUS_ACCEPTED
            locked.result_summary = "Codex завершил работу и создал Pull Request."
            locked.response = _compact(pull_request.get("body", ""), 4000)
            locked.changed_files = "\n".join(changed_files)
            locked.test_result = _workflow_test_result(pull_request.get("body", ""))
            locked.technical_errors = ""
            locked.save(
                update_fields=[
                    "automation_metadata",
                    "status",
                    "result_summary",
                    "response",
                    "changed_files",
                    "test_result",
                    "technical_errors",
                    "completed_at",
                    "updated_at",
                ]
            )
            task.status = DevelopmentTask.STATUS_REVIEW
            task.current_stage = DevelopmentTask.STAGE_REVIEW
            task.current_activity = "Изменения Codex готовы к review"
            task.blockers = ""
            task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
            _create_event(
                task,
                actor_id,
                "Codex создал Pull Request",
                action="codex_completed",
                iteration=locked,
                old_status=old_status,
                extra={"pr_number": pull_request.get("number")},
            )
            return CodexOperationResult(STATE_COMPLETED, changed=True)

        if remote_state == STATE_VALIDATION_FAILED and pull_request:
            pr_url = _safe_github_url(pull_request.get("html_url"), repository)
            message = "Codex создал изменения, но проверки не прошли."
            metadata.update(
                {
                    "pr_number": pull_request.get("number"),
                    "pr_url": pr_url,
                    "pr_title": _compact(pull_request.get("title", ""), 255),
                }
            )
            locked.automation_metadata = metadata
            locked.status = DevelopmentIteration.STATUS_FAILED
            locked.result_summary = message
            locked.response = _compact(pull_request.get("body", ""), 4000)
            locked.changed_files = "\n".join(changed_files)
            locked.test_result = _workflow_test_result(pull_request.get("body", ""))
            locked.technical_errors = message
            locked.save(
                update_fields=[
                    "automation_metadata",
                    "status",
                    "result_summary",
                    "response",
                    "changed_files",
                    "test_result",
                    "technical_errors",
                    "completed_at",
                    "updated_at",
                ]
            )
            task.status = DevelopmentTask.STATUS_BLOCKED
            task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
            task.current_activity = "Codex создал изменения, но проверки не прошли"
            task.blockers = message
            task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
            _create_event(
                task,
                actor_id,
                "Проверки изменений Codex не прошли",
                action="codex_validation_failed",
                iteration=locked,
                old_status=old_status,
                extra={"pr_number": pull_request.get("number")},
            )
            return CodexOperationResult(STATE_VALIDATION_FAILED, changed=True, message=message)

        if remote_state == STATE_SECURITY_BLOCKED:
            message = "Codex попытался изменить защищённые файлы; автоматическая публикация заблокирована."
        elif remote_state in {STATE_COMPLETED, STATE_VALIDATION_FAILED}:
            message = "GitHub Actions завершился без однозначно найденного Pull Request."
        else:
            message = "Выполнение Codex в GitHub Actions завершилось ошибкой."
        locked.automation_metadata = metadata
        locked.status = (
            DevelopmentIteration.STATUS_CANCELLED
            if remote_state == STATE_CANCELLED
            else DevelopmentIteration.STATUS_FAILED
        )
        locked.result_summary = "Выполнение Codex не завершено успешно."
        locked.test_result = "GitHub Actions не завершил workflow успешно."
        locked.technical_errors = message
        locked.save(
            update_fields=[
                "automation_metadata",
                "status",
                "result_summary",
                "test_result",
                "technical_errors",
                "completed_at",
                "updated_at",
            ]
        )
        task.status = DevelopmentTask.STATUS_BLOCKED
        task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
        task.current_activity = "Выполнение Codex требует проверки"
        task.blockers = message
        task.save(update_fields=["status", "current_stage", "current_activity", "blockers", "updated_at"])
        _create_event(
            task,
            actor_id,
            "Выполнение Codex требует проверки",
            action="codex_failed",
            iteration=locked,
            old_status=old_status,
            extra={"provider_state": remote_state},
        )
        notify_human_required(
            task,
            message,
            dedupe_suffix=f"codex-attention:{locked.pk}:{remote_state}",
        )
        return CodexOperationResult(remote_state, changed=True, message=message)
