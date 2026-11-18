"""Bounded stage commands using the existing per-card transaction boundary.

The board already supplies the form's projection. Writes still lock and validate
each current card; this adapter avoids N HTTP requests and N full detail reads.
"""
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST

from . import crm_lead_views, crm_live_views
from .services.crm_commercial_commands import (
    CommercialConflict, close_commercial_deal_negative, execute_commercial_command,
)
from .services.crm_lead_commands import execute_lead_command
from .services.permissions import company_has_access

MAX_COMMANDS = 25
MAX_BODY_BYTES = 131072


def _commands(request, kind):
    if request.content_type != "application/json":
        raise ValidationError("Ожидается запрос JSON.")
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES or len(request.body) > MAX_BODY_BYTES:
            raise ValidationError("Слишком большой запрос.")
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Некорректный запрос JSON.") from exc
    if type(body) is not dict or set(body) != {"commands"}:
        raise ValidationError("Некорректный состав запроса.")
    commands = body["commands"]
    if type(commands) is not list or not 1 <= len(commands) <= MAX_COMMANDS:
        raise ValidationError("За один запрос можно сохранить от 1 до 25 карточек.")
    fields = {"operation_key", "command_type", "subject_id", "expected_revision", "changes"}
    allowed = {"lead.disposition"} if kind == "lead" else {"deal.transition", "deal.close_negative"}
    subjects, keys = set(), set()
    # Reject malformed envelopes before the first write. Business conflicts and
    # denials remain individual results, with the same command keys on retries.
    for command in commands:
        if (type(command) is not dict or set(command) != fields
                or type(command["command_type"]) is not str or command["command_type"] not in allowed
                or type(command["changes"]) is not dict
                or type(command["subject_id"]) is not int or not 0 < command["subject_id"] <= 9223372036854775807
                or type(command["expected_revision"]) is not int or command["expected_revision"] < 0
                or type(command["operation_key"]) is not str or len(command["operation_key"]) > 100):
            raise ValidationError("Некорректная команда изменения стадии.")
        if command["subject_id"] in subjects or command["operation_key"] in keys:
            raise ValidationError("Карточки и операции в одном запросе не должны повторяться.")
        subjects.add(command["subject_id"])
        keys.add(command["operation_key"])
        if command["command_type"] == "deal.close_negative" and set(command["changes"]) != {"outcome", "reason", "expected_task_version"}:
            raise ValidationError("Укажите исход, причину и текущую версию следующего шага.")
    return commands


def _error(status, code, message):
    return {"status": status, "code": code, "error": message}


@crm_live_views._api
@require_POST
def command(request, kind):
    adapter = crm_lead_views if kind == "lead" else crm_live_views
    organization = adapter._organization(request)
    if not company_has_access(organization):
        raise PermissionDenied
    commands = _commands(request, kind)
    results = []
    for payload in commands:
        try:
            if kind == "lead":
                result = execute_lead_command(actor=request.user, organization=organization, **payload)
            elif payload["command_type"] == "deal.close_negative":
                result = close_commercial_deal_negative(actor=request.user, organization=organization,
                    **{key: payload[key] for key in ("operation_key", "subject_id", "expected_revision")},
                    **payload["changes"])
            else:
                result = execute_commercial_command(actor=request.user, organization=organization, **payload)
            row = {"status": 200, "result": result}
        except PermissionDenied:
            row = _error(403, "forbidden", "Доступ к этому действию сейчас отсутствует.")
        except Http404:
            row = _error(404, "not_found", "Карточка недоступна.")
        except CommercialConflict:
            row = _error(409, "conflict", "Карточка или задача уже изменена. Обновите её и проверьте данные.")
        except ValidationError as exc:
            message = next((text for text in exc.messages if any("а" <= char.lower() <= "я" for char in text)),
                "Проверьте поля, дату следующего действия и допустимую стадию.")
            row = _error(400, "invalid", message[:500])
        results.append({"operation_key": payload["operation_key"], **row})

    # As in single-card commands, a committed result must not disclose a card
    # after read access is lost. Recheck the whole successful set once; no call
    # media, identity projection, finance data or task content is serialized.
    successful = [row["result"][kind + "_id"] for row in results if row["status"] == 200]
    readable = set()
    if successful:
        try:
            organization = adapter._organization(request)
            queryset = (adapter._queryset if kind == "lead" else adapter._deal_queryset)(request.user, organization)
            readable = set(queryset.filter(pk__in=successful).values_list("pk", flat=True))
        except PermissionDenied:
            pass
    for row in results:
        if row["status"] == 200:
            result = row["result"]
            if result[kind + "_id"] not in readable:
                row.pop("result")
                row.update(_error(503, "verify_required", "Результат сохранения сейчас недоступен. Проверьте ту же операцию."))
            else:
                row[kind] = {"id": result[kind + "_id"], "revision": result["revision"]}
    return JsonResponse({"results": results})
