"""Personal approval and MCP entry points for unposted writes to the existing Fresh base."""
import json
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from pool_service import onec_diagnostic_mcp_views as transport
from pool_service.operations_mcp_auth import (
    ONEC_DRAFTS, OperationsMcpOAuthError, authenticate_bearer_header, is_enabled, protected_resource_metadata_url,
)
from pool_service.services import onec_draft_requests as drafts
from pool_service.services import onec_write_requests as writes
from pool_service.services.onec_write_connection import WriteError
from pool_service.services.onec_draft_intent import InvalidIntent
from pool_service.services import onec_chat_identity as chat_identity
from pool_service.onec_chat_views import WIDGET_URI


def _obj(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _text(length):
    return {"type": "string", "minLength": 1, "maxLength": length}


KIND_TO_TOOL = {"document": "prepare_document", "nomenclature": "request_nomenclature"}
TICKET = {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}
NUMBER = {"type": "string", "pattern": "^[0-9]{1,12}([.,][0-9]{1,6})?$"}
LINE = _obj({"item": _text(200), "article": _text(100), "quantity": NUMBER, "price": NUMBER, "unit": _text(100)},
            ["item", "quantity"])
CREATE_TOOLS = {"document": "create_1c_document", "nomenclature": "create_1c_nomenclature"}
DESCRIPTIONS = {
    "document": "Подготовить непроведённый документ по тексту или счёту. Уточнить неоднозначные позиции, единицы, суммы и НДС. Для заказа доступны lines либо точные реквизиты fields и tables из схемы 1С.",
    "nomenclature": "Только по явному поручению создать отсутствующие по артикулу карточки. Передать одну карточку или items из 1–10 карточек для личного подтверждения. Не вызывать автоматически из заказа.",
}
CARD_FIELDS = {"name": _text(200), "unit": _text(100), "article": _text(200), "packaging": _text(200), "group": _text(200)}
CARD = _obj(CARD_FIELDS, ["name", "unit", "article"])
TYPED_FIELDS = {"type": "object", "maxProperties": 80, "additionalProperties": {"type": ["string", "boolean", "integer"]}}
SCHEMAS = {
    "document": _obj({"ticket": TICKET, "document_type": _text(100), "organization": _text(160),
                      "counterparty": _text(160), "source_number": _text(160), "source_date": _text(160),
                      "lines": {"type": "array", "maxItems": 100, "items": LINE}, "details": _text(500),
                      "fields": TYPED_FIELDS, "tables": {"type": "object", "maxProperties": 10,
                      "additionalProperties": {"type": "array", "maxItems": 100, "items": TYPED_FIELDS}}},
                     ["ticket", "document_type"]),
    "nomenclature": {**_obj({"ticket": TICKET, **CARD_FIELDS,
                             "items": {"type": "array", "minItems": 1, "maxItems": 10, "items": CARD}}, ["ticket"]),
                     "oneOf": [{"required": ["name", "unit", "article"]}, {"required": ["items"]}]},
}


def _review_url(request, name, **kwargs):
    return request.build_absolute_uri(reverse(name, kwargs=kwargs))


def _connected_tools():
    document = {**SCHEMAS["document"], "properties": dict(SCHEMAS["document"]["properties"]), "required": ["document_type"]}
    document["properties"].pop("ticket")
    cards = {**SCHEMAS["nomenclature"], "properties": dict(SCHEMAS["nomenclature"]["properties"]), "required": []}
    cards["properties"].pop("ticket")
    confirm = _obj({"request_id": {"type": "string", "format": "uuid"}}, ["request_id"])
    if chat_identity.enabled():
        confirm["properties"]["confirmation_digest"] = _text(64)
        confirm["required"].append("confirmation_digest")
    definitions = (
        ("create_1c_document", "Создать непроведённый документ с пустым автором и проверить фактический ответ 1С. Уточнить неоднозначные позиции перед записью. Если 1С заполнила автора, сообщить об этом; не обещать автоматическое назначение первого сотрудника.", document, True, True),
        ("request_nomenclature", DESCRIPTIONS["nomenclature"], cards, False, False),
        ("create_1c_nomenclature", "Записать подтверждённые сотрудником карточки. Использовать request_id из результата подготовки.", confirm, True, True),
    )
    tools = [{"name": name, "description": description, "inputSchema": schema,
             "securitySchemes": [{"type": "oauth2", "scopes": [ONEC_DRAFTS.scope]}],
             "annotations": {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": idempotent, "openWorldHint": external}}
            for name, description, schema, idempotent, external in definitions]
    if chat_identity.enabled():
        tools[0]["description"] = "Создать непроведённый документ с автором, подтверждённым для текущего чата. Уточните состав в чате перед записью. Автор из текста не принимается."
        tools[1]["description"] = "Подготовить явно запрошенные новые карточки и вернуть точный состав для согласования в ChatGPT. Покажите результат сотруднику. Переход на сайт не нужен."
        tools[2]["description"] = "Создать карточки только после явного согласования в этом чате. Передайте request_id и confirmation_digest неизменными из подготовки."
        tools.insert(0, {"name": "get_chat_identity", "description": "Начните с этого инструмента в каждом новом чате. Возвращает кнопку подтверждения сотрудника или уже подтверждённое имя. Подтверждение не создаёт документы.",
            "inputSchema": _obj({}, []), "securitySchemes": [{"type": "oauth2", "scopes": [ONEC_DRAFTS.scope]}],
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            "_meta": {"openai/outputTemplate": WIDGET_URI, "openai/widgetAccessible": True}})
    for tool in tools:
        tool.setdefault("_meta", {})["securitySchemes"] = tool["securitySchemes"]
    return tools


def _auth_challenge(*, error):
    return (f'Bearer resource_metadata="{protected_resource_metadata_url(ONEC_DRAFTS)}", '
            f'scope="{ONEC_DRAFTS.scope}", error="{error}", '
            'error_description="Connect Rovik to access 1C drafts"')


def _connected_call(request, tool, arguments, authenticated, metadata=None):
    if not authenticated:
        return {**_result("Подключите Ровик для работы с черновиками 1С.", error=True),
                "_meta": {"mcp/www_authenticate": [_auth_challenge(error="insufficient_scope")]}}
    try:
        if chat_identity.enabled():
            identity, actor = chat_identity.begin(authenticated, metadata)
            if not actor:
                url = _review_url(request, "onec_chat_confirm", token=chat_identity.confirmation_token(identity))
                return {**_result("Подтвердите личность для этого чата кнопкой приложения. После возвращения продолжим согласование здесь."),
                    "structuredContent": {"status": "identity_required"},
                    "_meta": {"confirmation_url": url, "openai/outputTemplate": WIDGET_URI}}
            authenticated = actor
            if tool == "get_chat_identity" and not arguments:
                _key, name = chat_identity.mapped_author(identity.user_id, identity.organization_id)
                return {**_result("Сотрудник подтверждён: " + name),
                        "structuredContent": {"status": "confirmed", "employee_name": name}}
        if tool == "create_1c_document":
            outcome = writes.create_document(authenticated, arguments)
        elif tool == "request_nomenclature":
            if chat_identity.enabled():
                outcome = writes.prepare_chat_cards(authenticated, arguments)
            else:
                row_id = writes.prepare_cards(authenticated, arguments)
                url = _review_url(request, "onec_drafts_detail", pk=row_id)
                outcome = {"request_id": str(row_id), "review_url": url}
        elif (tool == "create_1c_nomenclature" and chat_identity.enabled()
                and set(arguments) == {"request_id", "confirmation_digest"}):
            outcome = writes.create_chat_cards(authenticated, arguments["request_id"], arguments["confirmation_digest"])
        elif tool == "create_1c_nomenclature" and not chat_identity.enabled() and set(arguments) == {"request_id"}:
            outcome = writes.create_cards(authenticated, arguments["request_id"])
        else:
            return _result("Неизвестный инструмент или неверные аргументы.", error=True)
        if chat_identity.enabled():
            chat_identity.validate(authenticated)
    except (drafts.DraftUnavailable, WriteError, InvalidIntent) as exc:
        return _result(str(exc) or "Запрос недоступен для этого сотрудника.", error=True)
    failed = any(item["status"] in {"unconfirmed", "author_not_empty", "author_mismatch"} for item in outcome.get("results", []))
    return {**_result(json.dumps(outcome, ensure_ascii=False), error=failed), "structuredContent": outcome}


def _tools(request):
    start = _review_url(request, "onec_drafts_home")
    tools = []
    for kind, name in KIND_TO_TOOL.items():
        for tool, description, schema, external in (
            (name, f"{DESCRIPTIONS[kind]} Личный код из {start}", SCHEMAS[kind], False),
            (CREATE_TOOLS[kind], "Записать в рабочую 1С по ранее подтверждённому личному коду. "
             "Сообщать только подтверждённые ссылки и номера. При неполном результате перечислить строки.",
             _obj({"ticket": TICKET}, ["ticket"]), True),
        ):
            tools.append({"name": tool, "description": description, "inputSchema": schema,
                          "securitySchemes": [{"type": "noauth"}], "annotations": {"readOnlyHint": False,
                          "destructiveHint": False, "idempotentHint": True, "openWorldHint": external}})
    return tools


def _result(text, *, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


@transaction.non_atomic_requests
@csrf_exempt
@never_cache
def onec_drafts_mcp(request):
    connected = is_enabled(ONEC_DRAFTS)
    authenticated = None
    version = transport._protocol_version(request)
    if not drafts.target_organization():
        raise Http404
    origin = request.headers.get("Origin")
    if origin and origin not in {"https://chatgpt.com", f"https://{request.get_host()}"}:
        return transport._mcp_empty(status=403, protocol_version=version)
    if request.method != "POST":
        response = transport._mcp_empty(status=204 if request.method == "OPTIONS" else 405, protocol_version=version)
        response["Allow"] = "POST, OPTIONS"
        return response
    if connected and request.headers.get("Authorization"):
        try:
            authenticated = authenticate_bearer_header(request.headers["Authorization"], ONEC_DRAFTS)
        except OperationsMcpOAuthError:
            response = transport._mcp_empty(status=401, protocol_version=version)
            response["WWW-Authenticate"] = _auth_challenge(error="invalid_token")
            return response
    body, invalid = transport._require_json_mcp_request(request, protocol_version=version)
    if invalid:
        return invalid
    request_id, method, params = body.get("id"), body["method"], body.get("params", {})
    if "id" not in body and method.startswith("notifications/"):
        return transport._mcp_empty(status=202, protocol_version=version)
    def reply(result):
        return transport._mcp_response(transport._jsonrpc_result(request_id, result), protocol_version=version)
    def error(code, message):
        return transport._mcp_response(transport._jsonrpc_error(request_id, code, message), protocol_version=version)
    if type(request_id) not in (str, int) or not isinstance(params, dict):
        return error(-32600, "Invalid request")
    if method == "initialize":
        if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("clientInfo"), dict) or not isinstance(params.get("capabilities"), dict):
            return error(-32602, "Invalid initialize params")
        version = params["protocolVersion"] if params["protocolVersion"] in transport.SUPPORTED_PROTOCOL_VERSIONS else transport.MCP_PROTOCOL_VERSION
        if connected:
            if chat_identity.enabled():
                return reply({"protocolVersion": version, "serverInfo": {"name": "rovik-onec-drafts", "version": "4.0"},
                    "capabilities": {"tools": {}, "resources": {}},
                    "instructions": "Call get_chat_identity at the start of every chat and after returning from identity confirmation. Identity is bound to this conversation, never infer it from names in messages. Show the identity button; do not use a browser or tools to confirm on the employee's behalf. Once confirmed, discuss all documents and catalog items in ChatGPT. create_1c_document creates unposted documents with the verified employee author. For missing items explicitly requested by the employee, call request_nomenclature, show the exact proposed cards in chat, then after explicit approval call create_1c_nomenclature with the returned request_id and confirmation_digest. Never post, invent missing fields, or change the author from chat text. Report actual readback results."})
            return reply({"protocolVersion": version, "serverInfo": {"name": "rovik-onec-drafts", "version": "3.0"},
                          "capabilities": {"tools": {}},
                          "instructions": "Create unposted documents with create_1c_document. Report the observed author_empty and author_ref_key; author_not_empty means a document exists but its author is already populated. Never claim that the first staff save or post sets the author: native 1C behavior requires separate verification. Never post documents or automatically create missing items. For explicitly requested catalog items use request_nomenclature, show the returned review_url, then call create_1c_nomenclature with the returned request_id after approval."})
        return reply({"protocolVersion": version, "serverInfo": {"name": "rovik-onec-drafts", "version": "2.0"},
                      "capabilities": {"tools": {}},
                      "instructions": "Use the employee’s ticket, prepare the request and show the private review link. After private approval, call create_1c_document or create_1c_nomenclature with the same ticket. Only report confirmed results. Never post documents or automatically create missing items."})
    if method == "ping":
        return reply({})
    if method in {"resources/list", "resources/read"} and chat_identity.enabled():
        if method == "resources/list":
            return reply({"resources": [{"uri": WIDGET_URI, "name": "Подтверждение сотрудника", "mimeType": "text/html+skybridge"}]})
        if params.get("uri") != WIDGET_URI:
            return error(-32602, "Unknown resource")
        return reply({"contents": [{"uri": WIDGET_URI, "mimeType": "text/html+skybridge",
            "text": render_to_string("onec_drafts/chat_identity_widget.html"),
            "_meta": {"openai/widgetDescription": "Одна кнопка подтверждения личности для текущего чата.",
                "openai/widgetCSP": {"connect_domains": [], "resource_domains": [],
                    "redirect_domains": [request.build_absolute_uri("/").rstrip("/")]}}}]})
    if method == "tools/list":
        return reply({"tools": _connected_tools() if connected else _tools(request)})
    if method != "tools/call":
        return error(-32601, "Unknown method")
    args = params.get("arguments")
    tool = params.get("name")
    if not isinstance(args, dict):
        return error(-32602, "Invalid arguments")
    if connected:
        return reply(_connected_call(request, tool, args, authenticated, params.get("_meta")))
    kind = next((key for key, name in CREATE_TOOLS.items() if name == tool), None)
    if kind:
        if set(args) != {"ticket"}:
            return error(-32602, "Only the approved personal ticket is accepted")
        try:
            outcome = writes.execute(args["ticket"], kind)
        except (drafts.DraftUnavailable, WriteError) as exc:
            return reply(_result(str(exc) or "Требуется действующий личный код и подтверждение его владельца.", error=True))
        failed = any(row["status"] == "unconfirmed" for row in outcome["results"])
        return reply(_result(json.dumps(outcome, ensure_ascii=False), error=failed))
    kind = next((key for key, name in KIND_TO_TOOL.items() if name == tool), None)
    if not kind:
        return error(-32602, "Unknown tool")
    try:
        row_id = drafts.submit(args.get("ticket"), kind, {k: v for k, v in args.items() if k != "ticket"})
    except drafts.DraftUnavailable:
        return reply(_result("Личный код недействителен для этого запроса.", error=True))
    except InvalidIntent as exc:
        return reply(_result(str(exc), error=True))
    url = _review_url(request, "onec_drafts_detail", pk=row_id)
    return reply(_result(f"Предложение готово. Открыть в личном Ровике: {url}. После личного подтверждения вызовите инструмент создания с этим же кодом."))


@login_required
@never_cache
@csrf_protect
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def drafts_home(request):
    try:
        rows = drafts.personal_requests(request.user.pk)
        row, ticket, message = None, None, ""
        if request.method == "POST" and not is_enabled(ONEC_DRAFTS):
            try:
                row, ticket = drafts.issue(request.user.pk, request.POST.get("kind"))
            except drafts.DraftLimit:
                message = "Достигнут часовой лимит запросов. Существующие предложения доступны ниже."
    except drafts.DraftUnavailable:
        raise Http404
    return render(request, "onec_drafts/home.html",
                  {"rows": rows[:30], "connected": is_enabled(ONEC_DRAFTS), "chat_identity": chat_identity.enabled(),
                   "ticket": ticket, "issued": row, "message": message})


@login_required
@never_cache
@csrf_protect
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def draft_detail(request, pk):
    try:
        rows = drafts.personal_requests(request.user.pk)
    except drafts.DraftUnavailable:
        raise Http404
    row = get_object_or_404(rows, pk=pk)
    connected = is_enabled(ONEC_DRAFTS)
    history_only = bool(row.chat_identity_id) or (row.token_hash is None and row.kind == "document") or (connected != (row.token_hash is None))
    message, renewed = "", ""
    choices = dict(row.selections)
    if request.method == "POST" and not history_only:
        choices = {key.removeprefix("line_"): value for key, value in request.POST.items()
                   if key.startswith("line_") and key[5:].isdigit() and value and len(value) == 36}
        try:
            if request.POST.get("action") == "renew":
                renewed = drafts.renew_approved(request.user.pk, pk)
                row.refresh_from_db()
                if connected:
                    message = "Срок подтверждения продлён. Вернитесь в чат."
            elif request.POST.get("action") != "preview":
                writes.approve(request.user.pk, pk, request.POST.get("digest"), request.POST.get("plan", ""), choices)
                row.refresh_from_db()
        except drafts.DraftUnavailable:
            raise Http404
        except WriteError as exc:
            message = str(exc)
    preview = {}
    if row.intent and not row.approved_at and not history_only:
        try:
            preview = writes.review_plan(row, choices)
        except WriteError as exc:
            message = message or str(exc)
    options = []
    for position, candidates in preview.get("candidates", {}).items():
        options.append({"position": position, "choices": [{"key": opt["Ref_Key"], "label": opt["Description"],
                        "article": opt["Артикул"], "selected": choices.get(position) == opt["Ref_Key"]} for opt in candidates]})
    return render(request, "onec_drafts/detail.html", {"row": row, "history_only": history_only, "connected": connected, "message": message or preview.get("message"), "renewed": renewed,
                  "digest": writes.review_digest(row), "plan": preview, "options": options,
                  "batch_items": (row.intent or {}).get("items", []),
                  "fields_json": json.dumps(row.intent.get("fields", {}), ensure_ascii=False, indent=2) if row.intent else "",
                  "tables_json": json.dumps(row.intent.get("tables", {}), ensure_ascii=False, indent=2) if row.intent else ""})
