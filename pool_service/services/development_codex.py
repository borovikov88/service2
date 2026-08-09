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

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
)
from pool_service.services.development_ai import resolve_primary_analysis_iteration
from pool_service.services.development_model_selection import (
    ModelSelectionError,
    effective_model,
    selection_metadata,
)


logger = logging.getLogger(__name__)

PROVIDER = "github_actions"
PURPOSE = "codex_execution"
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
    "manifest.json",
    "pr-title.txt",
}
MAX_NO_CHANGES_ARCHIVE_BYTES = 500_000
MAX_NO_CHANGES_CONTENT_BYTES = 250_000
MAX_CODEX_SUMMARY_BYTES = 100_000
MAX_PR_TITLE_BYTES = 255


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


def build_codex_prompt(task, analysis_iteration):
    analysis = (analysis_iteration.response or analysis_iteration.result_summary or "").strip()
    if not analysis:
        raise CodexPromptError("Primary AI analysis has no usable result")
    prompt = "\n".join(
        [
            "Выполни задачу разработки в текущем Django-репозитории.",
            "",
            f"Reference: {task.reference}",
            f"Название: {task.title}",
            f"Приоритет: {task.get_priority_display()}",
            "",
            "Исходная задача:",
            task.description,
            "",
            "Бизнес-цель:",
            task.business_goal or "Не указана.",
            "",
            "Definition of Done:",
            task.definition_of_done or "Не указано.",
            "",
            "Результат первичного AI-анализа:",
            analysis,
            "",
            "Текущий технический контекст:",
            f"Уже выполнено: {task.completed_work or 'Не указано.'}",
            f"Текущая активность: {task.current_activity or 'Не указана.'}",
            f"Blockers: {task.blockers or 'Нет.'}",
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
    byte_count = len(prompt.encode("utf-8"))
    if byte_count > settings.GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES:
        raise CodexPromptError(
            f"Codex prompt exceeds the configured {settings.GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES}-byte limit"
        )
    return prompt


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
            return _read_limited_response(response, MAX_NO_CHANGES_ARCHIVE_BYTES)
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


def _no_changes_summary(run_id, task_reference, launch_token, branch_name):
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
    if len(artifacts) != 1:
        raise GitHubRequestError(category="response", cause_type="ArtifactIdentityMismatch")
    artifact = artifacts[0]
    if int(artifact.get("size_in_bytes") or 0) > MAX_NO_CHANGES_ARCHIVE_BYTES:
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
                or info.file_size > MAX_NO_CHANGES_CONTENT_BYTES
                for info in infos
            ):
                raise GitHubRequestError(category="response", cause_type="UnsafeArtifactEntry")
            if sum(info.file_size for info in infos) > MAX_NO_CHANGES_CONTENT_BYTES:
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
        "result",
        "patch_sha256",
        "patch_size",
        "final_sha256",
        "final_size",
        "title_sha256",
        "title_size",
    }
    correlation_ok = (
        set(manifest) == expected_keys
        and manifest.get("task_reference") == task_reference
        and manifest.get("launch_token") == launch_token
        and manifest.get("branch_name") == branch_name
        and manifest.get("result") == STATE_NO_CHANGES
    )
    if not correlation_ok:
        raise GitHubRequestError(category="response", cause_type="ArtifactCorrelationMismatch")
    patch = files["codex.patch"]
    final = files["codex-final.txt"]
    title = files["pr-title.txt"]
    if (
        patch
        or len(final) > MAX_CODEX_SUMMARY_BYTES
        or len(title) > MAX_PR_TITLE_BYTES
        or not _artifact_digest_matches(manifest, "patch", patch)
        or not _artifact_digest_matches(manifest, "final", final)
        or not _artifact_digest_matches(manifest, "title", title)
    ):
        raise GitHubRequestError(category="response", cause_type="ArtifactDigestMismatch")
    try:
        summary = final.decode("utf-8")
        title_text = title.decode("utf-8")
    except UnicodeError as exc:
        raise GitHubRequestError(category="response", cause_type="ArtifactEncodingError") from exc
    if not title_text.strip() or "\x00" in title_text:
        raise GitHubRequestError(category="response", cause_type="ArtifactTitleInvalid")
    return _compact(summary, 4000), artifact["id"]


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
    launch_token = uuid4().hex

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
        prompt = build_codex_prompt(task, analysis)
        next_number = (task.iterations.aggregate(value=Max("iteration_number"))["value"] or 0) + 1
        branch_name = _branch_name(task, launch_token)
        iteration = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=next_number,
            executor_type=DevelopmentIteration.EXECUTOR_CODEX,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt=prompt,
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
            },
        )
        task_metadata.update(
            {
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
            "prompt_b64": base64.b64encode(prompt.encode("utf-8")).decode("ascii"),
            "pr_title_b64": base64.b64encode(
                _utf8_truncate(f"[{task.reference}] {task.title}", 240).encode("utf-8")
            ).decode("ascii"),
        },
    }
    try:
        _dispatch_workflow(payload)
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
        run = _find_matching_run(task, iteration)
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
            validation_state = _workflow_validation_state(run["id"])
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
    no_changes_summary = ""
    no_changes_artifact_id = None
    if remote_state in {STATE_COMPLETED, STATE_VALIDATION_FAILED}:
        try:
            pull_request = _find_pull_request(branch_name)
            if pull_request:
                changed_files = _pull_request_files(pull_request["number"])
        except Exception as exc:
            logger.warning(
                "Development Codex pull request lookup failed: task=%s iteration=%s error_type=%s",
                task_id,
                iteration_id,
                type(exc).__name__,
            )
            return CodexOperationResult("check_failed")
    elif remote_state == STATE_NO_CHANGES:
        try:
            no_changes_summary, no_changes_artifact_id = _no_changes_summary(
                run["id"], task.reference, launch_token, branch_name
            )
        except Exception as exc:
            logger.warning(
                "Development Codex no-changes artifact lookup failed: "
                "task=%s iteration=%s error_type=%s",
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
            metadata["artifact_id"] = no_changes_artifact_id
            locked.automation_metadata = metadata
            locked.status = DevelopmentIteration.STATUS_ACCEPTED
            locked.result_summary = "Codex не предложил изменений; результат передан на review."
            locked.response = no_changes_summary or "Codex завершил анализ без изменений."
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
        return CodexOperationResult(remote_state, changed=True, message=message)
