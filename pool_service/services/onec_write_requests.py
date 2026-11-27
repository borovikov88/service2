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
    binding_key, employee_config, fingerprint, guid, reference_filter, text_filter,
)
from .onec_write_documents import document_payload, entity_for


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
    writer = FreshWriter(employee_config(row.user_id, row.organization_id))
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
    if not row or not row.intent or _intent_digest(row.intent) != expected_digest:
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
    if (not row.approved_at or row.expires_at <= timezone.now() or row.approved_digest != review_digest(row)
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


def _result(entity, remote, status):
    number = remote.get("Code" if entity == ITEMS else "Number")
    if not isinstance(number, str) or not number.strip():
        raise TransportFailure("1С ещё не подтвердила код или номер.")
    return {"status": status, "ref_key": guid(remote.get("Ref_Key")), "code" if entity == ITEMS else "number": number,
            **({"posted": remote["Posted"]} if entity != ITEMS else {})}


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
def _claim(row, token, key):
    drafts._lock_staff(row.user_id, row.organization_id)
    _authorize(row)
    previous = OneCWriteFence.objects.select_for_update().filter(pk=token).first()
    if previous:
        return previous, False
    now = timezone.now()
    count = OneCWriteFence.objects.filter(request__user_id=row.user_id, request__organization_id=row.organization_id,
                                          reserved_at__gt=now - timedelta(hours=1)).count()
    if count >= getattr(settings, "ONEC_MCP_WRITE_HOURLY_LIMIT", 10):
        raise WriteError("Достигнут часовой лимит записи объектов в 1С для сотрудника.")
    fence, new = OneCWriteFence.objects.get_or_create(pk=token, defaults={"request": row, "remote_key": key})
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


def _check_existing_document(writer, entity, key, body, source):
    expected = _canonical_body(body, source, matching_existing=True)
    expected.pop("Posted", None)
    remote = writer.active(entity, expected.keys(), reference_filter("Ref_Key", key))
    types = writer.entities[entity].field_types
    if not remote or not all(_same_value(writer, types[k], remote.get(k), v) for k, v in expected.items()):
        raise WriteError("Документ по этим реквизитам уже существует с другим содержимым. Сверьте строки в 1С.")


def _verify(writer, entity, fence, body, row, identity, status):
    if fence.mismatch:
        raise WriteError("При предыдущей проверке 1С вернула другие реквизиты. Проверьте запись в базе.")
    fields = _canonical_body(body, row.intent)
    fields.pop("Posted", None)
    number = "Code" if entity == ITEMS else "Number"
    selected = list(fields) + [number] + ([] if entity == ITEMS else ["Posted"])
    remote = writer.active(entity, selected, reference_filter("Ref_Key", str(fence.remote_key)))
    if not remote:
        # A provider may assign its own UUID and lose the POST response. Search the business key under the same rights.
        found = _locate(writer, entity, identity)
        if found:
            remote = writer.active(entity, selected, reference_filter("Ref_Key", found["Ref_Key"]))
    if not remote:
        raise TransportFailure("Результат записи пока не подтверждён. Повторная отправка заблокирована.")
    if (not all(_same_value(writer, writer.entities[entity].field_types[key], remote.get(key), value) for key, value in fields.items())
            or (entity != ITEMS and remote["Posted"] is not False and not fence.verified_at)):
        OneCWriteFence.objects.filter(pk=fence.pk).update(mismatch=True)
        raise WriteError("Результат отличается от подтверждённых реквизитов или режима записи. Проверьте объект в 1С.")
    receipt = _result(entity, remote, status)
    if not fence.verified_at:
        if not OneCWriteFence.objects.filter(pk=fence.pk, mismatch=False).update(verified_at=timezone.now(), remote_key=receipt["ref_key"]):
            raise WriteError("По этому объекту 1С обнаружено несоответствие. Проверьте результат.")
    return receipt


def _run_one(row, writer, source, position):
    initial_key = str(uuid.uuid4())
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
    else:
        entity = entity_for(writer, source["document_type"])
        writer.choices = row.selections
        body = document_payload(writer, entity, source, initial_key, row.submitted_at)
        if _plan_token(writer, body, source) != row.approved_plan:
            raise WriteError("Состав или реквизиты из 1С изменились после подтверждения. Требуется сверка.")
        identity = _identity(row, entity, body)
    token = _fence_key(writer, entity, identity)
    prior_claim = OneCWriteFence.objects.filter(pk=token).first()
    if not prior_claim:
        previous = _locate(writer, entity, identity)
        if previous and not OneCWriteFence.objects.filter(pk=token).exists():
            if entity != ITEMS:
                _check_existing_document(writer, entity, previous["Ref_Key"], body, source)
            return _result(entity, previous, "exists")
    fence, fresh = _claim(row, token, initial_key)
    if not fresh:
        if fence.request_id != row.pk:
            if not fence.verified_at or fence.mismatch:
                raise WriteError("Для этой позиции есть непроверенная попытка. Завершите сверку исходного запроса.")
            visible = _locate(writer, entity, identity, str(fence.remote_key))
            if not visible:
                raise WriteError("Ранее записанный объект недоступен. Проверьте его в 1С.")
            if entity != ITEMS:
                _check_existing_document(writer, entity, visible["Ref_Key"], body, source)
            return _result(entity, visible, "exists")
        return _verify(writer, entity, fence, body, row, identity, "verified")
    try:
        returned = writer.create(entity, body)
    except AccessDenied:
        OneCWriteFence.objects.filter(pk=fence.pk, remote_key=initial_key).delete()
        raise
    if returned and returned != initial_key:
        OneCWriteFence.objects.filter(pk=fence.pk).update(remote_key=returned)
        fence.remote_key = returned
    return _verify(writer, entity, fence, body, row, identity, "created")


def execute(ticket, kind):
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise drafts.DraftUnavailable
    row = OneCDraftRequest.objects.filter(token_hash=drafts._digest(ticket), organization_id=drafts.target_organization(),
                                          kind=kind).first()
    if row is None:
        raise drafts.DraftUnavailable
    writer = FreshWriter(_authorize(row))
    items = row.intent.get("items", [row.intent])
    results = []
    for i, item in enumerate(items):
        try:
            config = _authorize(row)
            # Also observe a freshly rotated password on a long-running batch.
            if config != writer.config:
                writer = FreshWriter(config)
            result = _run_one(row, writer, item, i)
        except (WriteError, drafts.DraftUnavailable) as exc:
            result = {"status": "unconfirmed", "message": str(exc) or "Запрос больше не разрешён."}
            results.append({"position": i + 1, **result})
            break
        results.append({"position": i + 1, **result})
    return {"results": results, "unattempted": len(items) - len(results)}
