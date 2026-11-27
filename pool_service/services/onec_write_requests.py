"""A private human approval authorizes one immutable intent. All writes go through Fresh."""
import json
import re
import uuid
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pool_service.onec_draft_models import OneCDraftRequest, OneCWriteFence

from . import onec_draft_requests as drafts
from .onec_write_connection import (
    ITEMS, UNITS, AccessDenied, FreshWriter, TransportFailure, WriteError, article_key,
    binding_key, employee_config, fingerprint, guid, integration_config, reference_filter, text_filter,
)
from .onec_write_documents import AUTHOR, EMPTY_GUID, _scope_org, document_payload, entity_for
from .onec_draft_intent import document_intent, nomenclature_intent
from pool_service.operations_mcp_auth import ONEC_DRAFTS, is_enabled, revalidate_authenticated_request


def _intent_digest(intent):
    return fingerprint(json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _canonical_body(body, intent, *, matching_existing=False):
    data = {key: value for key, value in body.items() if key != "Ref_Key"}
    if matching_existing and "Date" not in intent.get("fields", {}):
        data.pop("Date", None)
    return data


def _plan_token(writer, body, intent):
    labels = sorted([entity, key, label] for (entity, key), label in writer.resolved.items())
    return _intent_digest([_canonical_body(body, intent), labels])


def review_plan(row, choices):
    if not row.intent:
        return {"digest": "", "message": ""}
    writer = FreshWriter(integration_config() if row.chat_identity_id else employee_config(row.user_id, row.organization_id))
    if row.kind == "nomenclature":
        cards = []
        for source in row.intent.get("items", [row.intent]):
            if not source.get("article"):
                raise WriteError("Для записи карточки укажите артикул.")
            current = writer.item(source["article"])
            if current:
                cards.append({"article": source["article"], "existing": guid(current["Ref_Key"]), "code": current["Code"]})
                continue
            body = _item_payload(writer, source, str(uuid.uuid4()))
            cards.append({"article": source["article"], "unit": source["unit"], "unit_ref": body["ЕдиницаИзмерения_Key"],
                          "group": source.get("group", ""), "group_ref": body.get("Parent_Key", ""),
                          "body_digest": _intent_digest(_canonical_body(body, source))})
        return {"digest": _intent_digest(cards), "cards": cards, "message": ""}
    writer.choices = choices
    result = {"digest": "", "labels": [], "candidates": {}, "message": ""}
    try:
        entity = entity_for(writer, row.intent["document_type"])
        body = document_payload(writer, entity, row.intent, str(uuid.uuid4()), row.submitted_at)
        result["document_date"] = body["Date"]
        result["digest"] = _plan_token(writer, body, row.intent)
    except WriteError as exc:
        result["message"] = str(exc)
    result["lines"] = writer.reviewed_lines
    result["candidates"] = writer.candidates
    result["labels"] = [{"entity": entity, "key": key, "label": label}
                        for (entity, key), label in sorted(writer.resolved.items())]
    return result


def approve(user_id, request_id, expected_digest, expected_plan="", choices=None):
    org_id = drafts.target_organization()
    choices = choices or {}
    rows = drafts.personal_requests(user_id)
    row = rows.filter(pk=request_id, expires_at__gt=timezone.now()).first()
    if (not row or row.chat_identity_id or not row.intent or (row.kind == "document" and (row.token_hash is None or is_enabled(ONEC_DRAFTS)))
            or _intent_digest(row.intent) != expected_digest):
        raise drafts.DraftUnavailable
    config = employee_config(user_id, org_id)
    binding = binding_key(config)
    plan = review_plan(row, choices)
    if plan["message"]:
        raise WriteError(plan["message"])
    if not expected_plan or expected_plan != plan["digest"]:
        raise WriteError("Реквизиты изменились. Проверьте сведения из 1С ещё раз.")
    with transaction.atomic():
        drafts._lock_staff(user_id, org_id)
        locked = OneCDraftRequest.objects.select_for_update().get(pk=row.pk, user_id=user_id, organization_id=org_id)
        if locked.expires_at <= timezone.now() or review_digest(locked) != expected_digest:
            raise drafts.DraftUnavailable
        if locked.approved_at:
            if (locked.approved_binding != binding or locked.approved_digest != expected_digest
                    or locked.approved_plan != plan["digest"] or locked.selections != choices):
                raise drafts.DraftUnavailable
            return
        locked.approved_binding = binding
        locked.approved_digest = expected_digest
        locked.approved_plan = plan["digest"]
        locked.card_plan = plan.get("cards", [])
        locked.selections = choices
        locked.approved_at = timezone.now()
        locked.save(update_fields=["approved_binding", "approved_digest", "approved_plan", "card_plan", "selections", "approved_at"])


def review_digest(row):
    return _intent_digest(row.intent) if row.intent else ""


def _authorize(row):
    if row.chat_identity_id:
        if (not row.approved_at or row.expires_at <= timezone.now() or row.kind != "nomenclature"
                or row.approved_digest != review_digest(row) or _intent_digest(row.card_plan) != row.approved_plan):
            raise drafts.DraftUnavailable
        config = integration_config()
        if row.approved_binding != binding_key(config):
            raise drafts.DraftUnavailable
        return config
    if ((row.kind == "document" and is_enabled(ONEC_DRAFTS)) or not row.approved_at or row.expires_at <= timezone.now() or row.approved_digest != review_digest(row)
            or row.organization_id != drafts.target_organization()
            or not drafts.is_staff_member(row.user_id, row.organization_id)):
        raise drafts.DraftUnavailable
    config = employee_config(row.user_id, row.organization_id)
    if row.kind == "nomenclature" and _intent_digest(row.card_plan) != row.approved_plan:
        raise drafts.DraftUnavailable
    if row.approved_binding != binding_key(config):
        raise drafts.DraftUnavailable
    return config


def _locate(writer, entity, identity, key=None):
    if entity == ITEMS:
        card = writer.item(identity["article"])
        if card and key and guid(card["Ref_Key"]) != guid(key):
            raise WriteError("Артикул принадлежит другой ссылке. Проверьте карточки в 1С.")
        return card
    if key:
        where = reference_filter("Ref_Key", key)
    elif identity.get("invoice"):
        where = " and ".join((
            reference_filter("Организация_Key", identity["org"]), reference_filter("Контрагент_Key", identity["supplier"]),
            text_filter("НомерВходящегоДокумента", identity["invoice"]),
            "ДатаВходящегоДокумента eq datetime'" + identity["date"] + "'",
        ))
    else:
        return None
    return writer.active(entity, ["Number", "Posted"], where)


def _identity(row, entity, body):
    if entity == ITEMS:
        return {"article": body["Артикул"]}
    if (body.get("НомерВходящегоДокумента") and body.get("ДатаВходящегоДокумента")
            and body.get("Организация_Key") and body.get("Контрагент_Key")):
        return {"invoice": body["НомерВходящегоДокумента"], "date": body["ДатаВходящегоДокумента"],
                "org": guid(body["Организация_Key"]), "supplier": guid(body["Контрагент_Key"])}
    return {"contents": _intent_digest(_canonical_body(body, row.intent, matching_existing=True))}


def _observed_author(remote):
    empty = remote[AUTHOR] == EMPTY_GUID
    return {"author_empty": empty, "author_ref_key": None if empty else guid(remote[AUTHOR])}


def _result(entity, remote, status, *, include_author=False):
    number = remote.get("Code" if entity == ITEMS else "Number")
    if not isinstance(number, str) or not number.strip():
        raise TransportFailure("1С ещё не подтвердила код или номер.")
    return {"status": status, "ref_key": guid(remote.get("Ref_Key")), "code" if entity == ITEMS else "number": number,
            **({"posted": remote["Posted"]} if entity != ITEMS else {}),
            **(_observed_author(remote) if include_author else {})}


def _item_payload(writer, item, key):
    if not item.get("article"):
        raise WriteError("Для создания карточки укажите артикул.")
    unit = writer.active(UNITS, [], text_filter("Description", item["unit"]))
    if not unit:
        raise WriteError("Не удалось однозначно найти единицу измерения в 1С.")
    name = item["name"]
    if item.get("packaging") and item["packaging"].casefold() not in name.casefold():
        name += " (" + item["packaging"] + ")"
    limits = writer.text_limits(ITEMS)
    if len(item["article"]) > limits.get("Артикул", len(item["article"])) or len(name) > limits.get("НаименованиеПолное", len(name)):
        raise WriteError("Артикул или полное название длиннее допустимого в 1С.")
    body = {"Ref_Key": key, "Артикул": item["article"], "Description": name[:limits.get("Description")], "НаименованиеПолное": name,
            "ТипНоменклатуры": "Запас", "ЕдиницаИзмерения_Key": guid(unit["Ref_Key"]), "IsFolder": False}
    if item.get("group"):
        group = writer.active(ITEMS, [], text_filter("Description", item["group"]) + " and IsFolder eq true", folder=True)
        if not group:
            raise WriteError("Группа не найдена в справочнике номенклатуры.")
        body["Parent_Key"] = guid(group["Ref_Key"])
    writer.schema(ITEMS, body)
    return body


def _fence_key(writer, entity, identity):
    normalized = dict(identity)
    for field in ("article", "invoice"):
        if field in normalized:
            normalized[field] = article_key(normalized[field])
    return _intent_digest([writer.config.base_url, entity, normalized])


@transaction.atomic
def _claim(row, token, key, authenticated=None, config=None, *, existing=False):
    if row.token_hash is None:
        _check_actor(authenticated, row)
    drafts._lock_staff(row.user_id, row.organization_id)
    if row.token_hash is None:
        current = integration_config() if row.kind == "document" else _authorize(row)
        if current != config:
            raise drafts.DraftUnavailable
    else:
        _authorize(row)
    previous = OneCWriteFence.objects.select_for_update().filter(pk=token).first()
    if previous:
        return previous, False
    now = timezone.now()
    count = OneCWriteFence.objects.filter(request__user_id=row.user_id, request__organization_id=row.organization_id, is_existing=False,
                                          reserved_at__gt=now - timedelta(hours=1)).count()
    if not existing and count >= getattr(settings, "ONEC_MCP_WRITE_HOURLY_LIMIT", 10):
        raise WriteError("Достигнут часовой лимит записи объектов в 1С для сотрудника.")
    fence, new = OneCWriteFence.objects.get_or_create(pk=token, defaults={"request": row, "remote_key": key,
        "is_existing": existing, "verified_at": now if existing else None})
    return fence, new


def _same_value(writer, dtype, actual, expected):
    from .onec_write_documents import complex_fields
    if dtype.startswith("Collection("):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        columns = complex_fields(writer, dtype)
        return all(isinstance(a, dict) and all(k in a and _same_value(writer, columns[k], a[k], v) for k, v in e.items())
                   for a, e in zip(actual, expected))
    if dtype == "Edm.Boolean":
        return actual is expected
    if dtype in {"Edm.Decimal", "Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte", "Edm.Single", "Edm.Double"}:
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except (ValueError, ArithmeticError):
            return False
    if dtype == "Edm.Guid":
        try:
            return guid(actual) == guid(expected)
        except WriteError:
            return False
    if dtype in {"Edm.DateTime", "Edm.DateTimeOffset"}:
        from datetime import datetime
        try:
            return datetime.fromisoformat(actual) == datetime.fromisoformat(expected)
        except (TypeError, ValueError):
            return False
    return actual == expected


def _check_existing_document(writer, entity, key, body, source, *, include_author=False):
    expected = _canonical_body(body, source, matching_existing=True)
    expected.pop("Posted", None)
    selected = list(expected) + ["Number", "Posted"] + ([AUTHOR] if include_author else [])
    remote = writer.active(entity, selected, reference_filter("Ref_Key", key))
    types = writer.entities[entity].field_types
    if not remote or not all(_same_value(writer, types[k], remote.get(k), v) for k, v in expected.items()):
        raise WriteError("Документ по этим реквизитам уже существует с другим содержимым. Сверьте строки в 1С.")
    return remote


def _verify(writer, entity, fence, body, row, identity, status):
    if fence.mismatch:
        raise WriteError("При предыдущей проверке 1С вернула другие реквизиты. Проверьте запись в базе.")
    fields = _canonical_body(body, row.intent)
    fields.pop("Posted", None)
    expected_author = str(row.confirmed_author) if row.chat_identity_id else None
    if expected_author:
        fields.pop(AUTHOR, None)
    number = "Code" if entity == ITEMS else "Number"
    selected = list(fields) + [number] + ([] if entity == ITEMS else ["Posted"])
    direct_document = row.token_hash is None and row.kind == "document"
    if direct_document:
        selected += [AUTHOR]
    remote = writer.active(entity, selected, reference_filter("Ref_Key", str(fence.remote_key)))
    if not remote and not direct_document:
        # A provider may assign its own UUID and lose the POST response. Search the business key under the same rights.
        found = _locate(writer, entity, identity)
        if found:
            remote = writer.active(entity, selected, reference_filter("Ref_Key", found["Ref_Key"]))
    if not remote:
        raise TransportFailure("Результат записи пока не подтверждён. Повторная отправка заблокирована.")
    if direct_document:
        if "Организация_Key" in fields:
            _scope_org(writer, remote["Организация_Key"])
        if remote[AUTHOR] != EMPTY_GUID:
            guid(remote[AUTHOR])
        if fence.verified_at:
            status = "exists" if fence.is_existing else "verified"
    # No native handler is installed. A populated author cannot prove who saved
    # the document, and cannot waive first-read content/Posted verification.
    compare_contents = not direct_document or not fence.verified_at
    if compare_contents and (
        not all(_same_value(writer, writer.entities[entity].field_types[key], remote.get(key), value)
                for key, value in fields.items())
        or (entity != ITEMS and remote["Posted"] is not False and not fence.verified_at)
    ):
        pending = OneCWriteFence.objects.filter(pk=fence.pk)
        if direct_document:
            pending = pending.filter(verified_at__isnull=True)
        if pending.update(mismatch=True):
            raise WriteError("Результат отличается от подтверждённых реквизитов или режима записи. Проверьте объект в 1С.")
        # A concurrent request may have completed initial verification while
        # this GET was in flight. Native edits now require identity-only readback.
        fence.refresh_from_db()
        if not direct_document or fence.mismatch or not fence.verified_at:
            raise WriteError("По этому объекту 1С требуется сверка результата.")
        compare_contents = False
        status = "exists" if fence.is_existing else "verified"
    receipt = _result(entity, remote, status, include_author=direct_document)
    if direct_document:
        receipt["verification"] = "creation_fields" if compare_contents else "document_identity"
    if not fence.verified_at:
        OneCWriteFence.objects.filter(pk=fence.pk, mismatch=False, verified_at__isnull=True).update(
            verified_at=timezone.now(), remote_key=receipt["ref_key"],
            author_was_empty=receipt.get("author_empty"),
            author_matches=(remote.get(AUTHOR) == expected_author) if expected_author else None)
        fence.refresh_from_db()
        if fence.mismatch or not fence.verified_at:
            raise WriteError("По этому объекту 1С обнаружено несоответствие. Проверьте результат.")
    if direct_document:
        receipt["initial_author_empty"] = fence.author_was_empty
        if expected_author:
            receipt["expected_author_ref_key"] = expected_author
            receipt["initial_author_matches"] = fence.author_matches
            if fence.author_matches is False:
                receipt["status"] = "author_mismatch"
                receipt["message"] = "Документ создан, но 1С не сохранила подтверждённого автора. Повторное создание заблокировано; проверьте документ по номеру."
        elif fence.author_was_empty is False:
            receipt["status"] = "author_not_empty"
            receipt["message"] = "Документ записан, но при первой проверке поле «Автор» уже было заполнено в 1С. Повторно создавать его не нужно. Правило первого сотрудника не подтверждено."
    return receipt


def _draft_schema(writer, entity):
    schema = writer.entities.get(entity)
    if not schema or schema.field_types.get(AUTHOR) != "Edm.Guid":
        raise WriteError("Для этого вида документа не опубликовано поле «Автор» типа ссылка.")


def _document_snapshot(row, writer, authenticated):
    snapshot = row.document_snapshot
    if snapshot is None:
        entity = entity_for(writer, row.intent["document_type"])
        _draft_schema(writer, entity)
        body = document_payload(writer, entity, row.intent, str(uuid.uuid4()), row.submitted_at)
        if row.chat_identity_id:
            body[AUTHOR] = str(row.confirmed_author)
        with transaction.atomic():
            _check_actor(authenticated, row)
            locked = OneCDraftRequest.objects.select_for_update().get(pk=row.pk)
            if locked.document_snapshot is None:
                locked.document_snapshot = {"entity": entity, "body": body}
                locked.save(update_fields=["document_snapshot"])
            snapshot = locked.document_snapshot
    entity, body = snapshot["entity"], snapshot["body"]
    _draft_schema(writer, entity)
    writer.schema(entity, body)
    if row.chat_identity_id:
        from .onec_chat_identity import verify_author
        if body.get(AUTHOR) != str(row.confirmed_author):
            raise drafts.DraftUnavailable
        verify_author(writer, entity, str(row.confirmed_author))
    if "Организация_Key" in body:
        _scope_org(writer, body["Организация_Key"])
    return entity, body


def _run_one(row, writer, source, position, authenticated=None):
    initial_key = str(uuid.uuid4())
    direct_document = row.kind == "document" and row.token_hash is None
    if row.kind == "nomenclature":
        if not source.get("article"):
            raise WriteError("Укажите артикул новой карточки.")
        entity, identity = ITEMS, {"article": source["article"]}
        approved = row.card_plan[position]
        if approved.get("existing"):
            fence = OneCWriteFence.objects.filter(pk=_fence_key(writer, entity, identity)).first()
            if fence and (fence.mismatch or not fence.verified_at):
                raise WriteError("По этому артикулу есть непроверенная запись. Сверьте результат исходного запроса.")
            existing = writer.item(source["article"])
            if not existing or guid(existing["Ref_Key"]) != approved["existing"]:
                raise WriteError("Карточка, найденная до подтверждения, изменилась или недоступна.")
            return _result(ITEMS, existing, "exists")
        body = _item_payload(writer, source, initial_key)
        if _intent_digest(_canonical_body(body, source)) != approved.get("body_digest"):
            raise WriteError("Единица измерения, группа или реквизиты карточки изменились после подтверждения.")
    elif row.token_hash is None:
        entity, body = _document_snapshot(row, writer, authenticated)
        initial_key = body["Ref_Key"]
        identity = _identity(row, entity, body)
    else:
        entity = entity_for(writer, source["document_type"])
        writer.choices = row.selections
        body = document_payload(writer, entity, source, initial_key, row.submitted_at)
        if _plan_token(writer, body, source) != row.approved_plan:
            raise WriteError("Состав или реквизиты из 1С изменились после подтверждения. Требуется сверка.")
        identity = _identity(row, entity, body)
    token = _fence_key(writer, entity, identity)
    fence = None
    prior_claim = OneCWriteFence.objects.filter(pk=token).first()
    if not prior_claim:
        previous = _locate(writer, entity, identity)
        if previous and not OneCWriteFence.objects.filter(pk=token).exists():
            if entity != ITEMS:
                previous = _check_existing_document(writer, entity, previous["Ref_Key"], body, source,
                                                    include_author=direct_document)
            receipt = _result(entity, previous, "exists", include_author=direct_document)
            if not direct_document:
                return receipt
            fence, fresh = _claim(row, token, previous["Ref_Key"], authenticated, writer.config, existing=True)
            if fresh:
                receipt["initial_author_empty"] = None
                return receipt
    if fence is None:
        fence, fresh = _claim(row, token, initial_key, authenticated, writer.config)
    if not fresh:
        if fence.request_id != row.pk:
            if not fence.verified_at or fence.mismatch:
                raise WriteError("Для этой позиции есть непроверенная попытка. Завершите сверку исходного запроса.")
            visible = _locate(writer, entity, identity, str(fence.remote_key))
            if not visible:
                raise WriteError("Ранее записанный объект недоступен. Проверьте его в 1С.")
            if entity != ITEMS:
                visible = _check_existing_document(writer, entity, visible["Ref_Key"], body, source,
                                                   include_author=direct_document)
            return _result(entity, visible, "exists", include_author=direct_document)
        return _verify(writer, entity, fence, body, row, identity, "verified")
    try:
        payload = ({**body, AUTHOR: str(row.confirmed_author) if row.chat_identity_id else EMPTY_GUID}
                   if row.kind == "document" and row.token_hash is None else body)
        if row.chat_identity_id:
            _check_actor(authenticated, row)
        returned = writer.create(entity, payload)
    except AccessDenied:
        OneCWriteFence.objects.filter(pk=fence.pk, remote_key=initial_key).delete()
        raise
    if returned and returned != initial_key:
        OneCWriteFence.objects.filter(pk=fence.pk).update(remote_key=returned)
        fence.remote_key = returned
    return _verify(writer, entity, fence, body, row, identity, "created")


def execute(ticket, kind):
    if is_enabled(ONEC_DRAFTS) or not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise drafts.DraftUnavailable
    row = OneCDraftRequest.objects.filter(token_hash=drafts._digest(ticket), organization_id=drafts.target_organization(),
                                          kind=kind).first()
    if row is None:
        raise drafts.DraftUnavailable
    return _execute_approved(row)


def _execute_approved(row, authenticated=None):
    if row.chat_identity_id:
        _check_actor(authenticated, row)
    writer = FreshWriter(_authorize(row))
    items = row.intent.get("items", [row.intent])
    results = []
    for i, item in enumerate(items):
        try:
            if authenticated:
                with transaction.atomic():
                    _check_actor(authenticated, row)
            config = _authorize(row)
            # Also observe a freshly rotated password on a long-running batch.
            if config != writer.config:
                writer = FreshWriter(config)
            result = _run_one(row, writer, item, i, authenticated)
        except (WriteError, drafts.DraftUnavailable) as exc:
            result = {"status": "unconfirmed", "message": str(exc) or "Запрос больше не разрешён."}
            results.append({"position": i + 1, **result})
            break
        results.append({"position": i + 1, **result})
    return {"results": results, "unattempted": len(items) - len(results)}


def _check_actor(authenticated, row=None):
    from .onec_chat_identity import ChatActor, enabled, validate
    if isinstance(authenticated, ChatActor):
        identity = validate(authenticated, row)
        return identity.user_id, identity.organization_id
    if enabled():
        raise drafts.DraftUnavailable
    if not authenticated or not is_enabled(ONEC_DRAFTS):
        raise drafts.DraftUnavailable
    try:
        current = revalidate_authenticated_request(authenticated, ONEC_DRAFTS)
    except PermissionError:
        raise drafts.DraftUnavailable from None
    actor = current.grant.authorized_by_id
    org = drafts.target_organization()
    if row is not None and (row.user_id != actor or row.organization_id != org or row.token_hash is not None):
        raise drafts.DraftUnavailable
    return actor, org


@transaction.atomic
def _new_request(authenticated, intent, config=None):
    actor, org = _check_actor(authenticated)
    drafts._lock_staff(actor, org)
    now = timezone.now()
    rows = OneCDraftRequest.objects.filter(organization_id=org, user_id=actor)
    from .onec_chat_identity import ChatActor, validate
    identity = validate(authenticated) if isinstance(authenticated, ChatActor) else None
    pk = uuid.uuid4()
    if intent["kind"] == "document":
        # Derived from an authenticated initiator and normalized contents. Never an access credential.
        pk = uuid.uuid5(uuid.NAMESPACE_URL, _intent_digest(["onec-mcp-draft-v1", org, actor, config.base_url, intent,
            *([str(identity.pk)] if identity else [])]))
        previous = rows.filter(pk=pk, kind="document", token_hash__isnull=True).first()
        if previous:
            return previous
    if rows.filter(created_at__gt=now-timedelta(hours=1)).count() >= getattr(settings, "ONEC_DRAFTS_HOURLY_LIMIT", 10):
        raise WriteError("Достигнут часовой лимит новых запросов в 1С.")
    return rows.create(pk=pk, organization_id=org, user_id=actor, kind=intent["kind"], token_hash=None,
                       intent=intent, submitted_at=now, expires_at=now+timedelta(minutes=15),
                       chat_identity=identity, confirmed_author=identity.author_ref if identity else None)


def create_document(authenticated, arguments):
    intent = document_intent(arguments)
    config = integration_config()
    row = _new_request(authenticated, intent, config)
    writer = FreshWriter(config)
    result = _run_one(row, writer, row.intent, 0, authenticated)
    return {"results": [{"position": 1, **result}], "unattempted": 0}


def prepare_cards(authenticated, arguments):
    return _new_request(authenticated, nomenclature_intent(arguments)).pk


def prepare_chat_cards(authenticated, arguments):
    row = _new_request(authenticated, nomenclature_intent(arguments))
    plan = review_plan(row, {})
    with transaction.atomic():
        _check_actor(authenticated, row)
        row.card_plan = plan["cards"]
        row.approved_plan = plan["digest"]
        row.approved_binding = binding_key(integration_config())
        row.save(update_fields=["card_plan", "approved_plan", "approved_binding"])
    return {"request_id": str(row.pk), "confirmation_digest": _intent_digest([review_digest(row), plan["digest"]]),
            "items": row.intent.get("items", [row.intent]), "resolved": plan["cards"],
            "message": "Покажите эти карточки сотруднику в чате и создавайте только после его явного согласия."}


def create_chat_cards(authenticated, request_id, confirmation_digest):
    try:
        pk = uuid.UUID(request_id) if isinstance(request_id, str) else None
    except ValueError:
        pk = None
    with transaction.atomic():
        actor, org = _check_actor(authenticated)
        row = OneCDraftRequest.objects.select_for_update().filter(pk=pk, user_id=actor, organization_id=org,
            kind="nomenclature", chat_identity_id=authenticated.identity_id, token_hash__isnull=True).first()
        if (not row or not row.approved_plan or row.expires_at <= timezone.now()
                or confirmation_digest != _intent_digest([review_digest(row), row.approved_plan])):
            raise drafts.DraftUnavailable
        _check_actor(authenticated, row)
        if not row.approved_at:
            row.approved_at = timezone.now()
            row.approved_digest = review_digest(row)
            row.save(update_fields=["approved_at", "approved_digest"])
    return _execute_approved(row, authenticated)


def create_cards(authenticated, request_id):
    try:
        pk = uuid.UUID(request_id) if isinstance(request_id, str) else None
    except ValueError:
        pk = None
    with transaction.atomic():
        actor, org = _check_actor(authenticated)
        row = OneCDraftRequest.objects.filter(pk=pk, user_id=actor, organization_id=org, kind="nomenclature",
                                              token_hash__isnull=True).first()
        if not row:
            raise drafts.DraftUnavailable
    return _execute_approved(row, authenticated)
