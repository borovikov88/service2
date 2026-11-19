"""Operational links to documents saved from 1C, separate from settlement evidence."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.crm_commercial_models import CANONICAL_KEY, CrmCommand, CrmEvent
from pool_service.crm_order_models import CrmDealDocumentLink, CrmDealOrderLink, CrmOneCDocument, CrmOneCOrder, CrmOrderSyncState
from pool_service.models import DataAuditLog, Organization
from .crm_commercial_access import can_view_team_deals
from .crm_commercial_commands import CommercialConflict, _authorize, _owned_deal, _positive_id
from .crm_order_finance import (
    OrderRefreshUnavailable, _confirmed_customers, _hash, _in_current_source, _money, _object_scope, _order_scope,
    _order_lookup_scope, _writable, current_order_source, refresh_order_candidates,
)

KINDS = {"order": "Заказ", **dict(CrmOneCDocument.KINDS)}
PAGE_SIZE = 20


def _can_read(actor, organization, deal):
    return (deal.organization_id == organization.pk and can_view_team_deals(actor, organization)
        and (deal.manager_id == actor.pk or organization.accesses.filter(user=actor, role="owner").exists()))


def _valid_orders(organization, source, customers):
    config, source_id = source
    return CrmOneCOrder.objects.filter(organization=organization, source_id=source_id,
        external_organization__in=config.organization_guids, customer_guid__in=customers,
        order_valid=True, cancelled=False, missing=False)


def _valid_documents(organization, source, customers):
    config, source_id = source
    return CrmOneCDocument.objects.filter(organization=organization, source_id=source_id,
        external_organization__in=config.organization_guids, customer_guid__in=customers, valid=True)


def _record_scope(row):
    guid = row.order_guid if isinstance(row, CrmOneCOrder) else row.guid
    return _hash([row.organization_id, row.source_id, row.external_organization, guid, row.customer_guid,
        row.currency_guid, row.currency, _money(row.amount), row.order_guid])


def _row_data(row, kind, deal, *, automatic=False, anchor=False, link_id=None):
    stamp = row.order_date if kind == "order" else row.document_date
    comparable = row.currency and row.currency == deal.currency and deal.amount is not None and deal.amount == row.amount
    state = "Проведён по данным 1С"
    if kind == "order":
        if row.cancelled:
            state = "Отменён"
        elif row.missing or row.lookup_failed:
            state = "Сохранённая версия"
        elif not row.order_valid:
            state = "Требуется сверка"
    return {"id": row.pk, "kind": kind, "label": KINDS[kind], "number": row.number, "date": stamp.isoformat(),
        "amount": _money(row.amount), "currency": row.currency, "customer": deal.client.name,
        "source_at": row.fetched_at.isoformat(), "automatic": automatic, "anchor": anchor, "link_id": link_id,
        "basis": "Явная связь с заказом" if automatic else
            "Совпадают контрагент и сумма" if comparable else "Подтверждённый контрагент",
        "state": state}


def _matching_parent(document, orders):
    return orders.filter(source_id=document.source_id, external_organization=document.external_organization,
        order_guid=document.order_guid, customer_guid=document.customer_guid, currency_guid=document.currency_guid)


def _automatic(deal, source, customers, orders):
    parents = orders.filter(currency_guid__gt="")
    documents = _valid_documents(deal.organization, source, customers).filter(
        order_guid__gt="", currency_guid__gt="").filter(Exists(parents.filter(
            source_id=OuterRef("source_id"), external_organization=OuterRef("external_organization"),
            order_guid=OuterRef("order_guid"), customer_guid=OuterRef("customer_guid"),
            currency_guid=OuterRef("currency_guid"))))
    return documents.exclude(Exists(CrmDealDocumentLink.objects.filter(document_id=OuterRef("pk")).filter(
        Q(deal=deal) | Q(suppressed=False))))


def _parents(deal, source, customers):
    orders = _valid_orders(deal.organization, source, customers)
    scoped = orders.filter(Q(document_claim__deal=deal, document_claim__client_id=deal.client_id, document_claim__organization_id=deal.organization_id)
        | Q(deal_link__deal=deal, deal_link__client_id=deal.client_id, deal_link__organization_id=deal.organization_id))
    valid = []
    for order in scoped.select_related("document_claim", "deal_link"):
        claim = getattr(order, "document_claim", None)
        anchor = getattr(order, "deal_link", None)
        if (claim and claim.deal_id == deal.pk and claim.client_id == deal.client_id
                and claim.organization_id == deal.organization_id and claim.scope_fingerprint == _record_scope(order)):
            valid.append(order.pk)
        elif (anchor and anchor.deal_id == deal.pk and anchor.client_id == deal.client_id
                and anchor.organization_id == deal.organization_id and not anchor.requires_reconciliation
                and anchor.scope_fingerprint == _order_scope(order) and anchor.object_scope == _object_scope(deal)):
            valid.append(order.pk)
    return orders.filter(pk__in=valid)


def document_projection(actor, organization, deal):
    if not _can_read(actor, organization, deal):
        return {"visible": False}
    writable = _writable(actor, organization, deal)
    result = {"visible": True, "can_edit": writable, "items": [], "review": False}
    source = current_order_source(organization.pk)
    customers = _confirmed_customers(organization, deal.client_id) if source else set()
    links = list(CrmDealDocumentLink.objects.filter(organization=organization, deal=deal, suppressed=False)
        .select_related("order", "document").order_by("pk"))
    anchor = CrmDealOrderLink.objects.filter(organization=organization, deal=deal).select_related("order").first()
    order_ids = set()
    if anchor:
        order = anchor.order
        if (anchor.client_id == deal.client_id and order.organization_id == organization.pk
                and order.customer_guid in customers and _in_current_source(order, source)):
            order_ids.add(order.pk)
            result["items"].append(_row_data(order, "order", deal, anchor=True))
        else:
            result["review"] = True
    if not source:
        result.update(reason="Источник 1С недоступен.", can_search=False)
    elif not customers:
        result.update(reason="У клиента пока нет подтверждённой связи с 1С.", can_search=False)
    else:
        result.update(reason="", can_search=writable)
    for link in links:
        row = link.order or link.document
        kind = "order" if link.order_id else row.kind
        if link.order_id and link.order_id in order_ids:
            continue
        if (link.client_id != deal.client_id or row.organization_id != organization.pk or row.customer_guid not in customers
                or not _in_current_source(row, source) or link.scope_fingerprint != _record_scope(row)
                or (row.missing or row.cancelled or not row.order_valid if link.order_id else not row.valid)):
            result["review"] = True
            if writable:
                result["items"].append({"kind": kind, "label": KINDS[kind], "link_id": link.pk, "unverified": True})
            continue
        result["items"].append(_row_data(row, kind, deal, link_id=link.pk))
    if customers:
        autos = _automatic(deal, source, customers, _parents(deal, source, customers))
        result["items"].extend(_row_data(row, row.kind, deal, automatic=True)
            for row in autos.order_by("-document_date", "-pk")[:100])
    return result


def document_candidates(*, actor, organization, deal, kind, query="", amount="", day="", page=1, exact=False):
    if not _can_read(actor, organization, deal) or not _writable(actor, organization, deal):
        raise PermissionDenied
    if kind not in KINDS or not 1 <= page <= 100:
        raise ValidationError("Выберите тип документа и допустимую страницу.")
    source = current_order_source(organization.pk)
    customers = _confirmed_customers(organization, deal.client_id) if source else set()
    if not customers:
        return {"items": [], "has_more": False, "reason": "Для поиска нужны источник 1С и подтверждённый контрагент."}
    if kind == "order":
        rows = _valid_orders(organization, source, customers).filter(
            deal_link__isnull=True, document_claim__isnull=True)
        date_field = "order_date"
    else:
        occupied = CrmDealDocumentLink.objects.filter(document_id=OuterRef("pk"), suppressed=False)
        rows = _valid_documents(organization, source, customers).filter(kind=kind).exclude(Exists(occupied))
        reserved = _valid_orders(organization, source, customers).filter(
            external_organization=OuterRef("external_organization"), order_guid=OuterRef("order_guid"),
            customer_guid=OuterRef("customer_guid"), currency_guid=OuterRef("currency_guid")).filter(
                Q(document_claim__isnull=False) | Q(deal_link__isnull=False))
        rows = rows.exclude(Exists(reserved.exclude(Q(deal_link__deal=deal) | Q(document_claim__deal=deal))))
        date_field = "document_date"
    if query:
        if exact:
            rows = rows.filter(number=query)
        elif query.casefold() not in deal.client.name.casefold():
            rows = rows.filter(number__icontains=query[:100])
    if amount:
        try:
            amount = Decimal(amount.strip().replace(" ", "").replace(",", "."))
            if not amount.is_finite() or amount < 0 or amount >= Decimal("1e16") or amount.as_tuple().exponent < -2:
                raise InvalidOperation
        except InvalidOperation:
            raise ValidationError("Укажите сумму документа.") from None
        rows = rows.filter(amount=amount)
    if day:
        try:
            value = date.fromisoformat(day)
            zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
            start = datetime.combine(value, datetime.min.time(), zone)
            end = start + timedelta(days=1)
        except (ValueError, OverflowError):
            raise ValidationError("Укажите дату документа.") from None
        rows = rows.filter(**{date_field + "__gte": start, date_field + "__lt": end})
    offset = (page - 1) * PAGE_SIZE
    saved = list(rows.order_by("-" + date_field, "-pk")[offset:offset + PAGE_SIZE + 1])
    state = CrmOrderSyncState.objects.filter(organization=organization, source_id=source[1]).first() if kind == "order" else None
    return {"items": [_row_data(row, kind, deal) for row in saved[:PAGE_SIZE]], "has_more": len(saved) > PAGE_SIZE,
        "last_failed": bool(state and state.failed or any(getattr(row, "lookup_failed", False) for row in saved)),
        "reason": "" if saved else "В сохранённых данных совпадений нет. Можно проверить полный номер в 1С."}


def observe_realizations(order):
    """Keep document headers from the existing verified order reader when available."""
    for row in order.documents:
        if row.get("kind") != "realization" or not row.get("document_amount") or not row.get("posted") or row.get("deleted"):
            continue
        try:
            guid = str(UUID(row["ref"]))
            stamp = parse_datetime(row["date"])
            amount = Decimal(row["document_amount"])
            if not stamp or timezone.is_naive(stamp) or not amount.is_finite() or not 0 < amount < Decimal("1e16"):
                continue
        except (ValueError, TypeError, KeyError, InvalidOperation):
            continue
        scope = dict(organization_id=order.organization_id, source_id=order.source_id, kind="realization",
            external_organization=order.external_organization, guid=guid)
        older = CrmOneCDocument.objects.filter(**scope).first()
        if older and older.fetched_at >= order.fetched_at:
            continue
        CrmOneCDocument.objects.update_or_create(**scope, defaults=dict(customer_guid=order.customer_guid,
            order_guid=row.get("order_guid") or "", currency_guid=order.currency_guid, currency=order.currency,
            number=row["number"], document_date=stamp, amount=amount, valid=True, fetched_at=order.fetched_at))


def refresh_documents(*, actor, organization, deal_id, kind, query):
    from pool_service.finance_imports.odata_crm_orders import read_crm_documents
    if type(kind) is not str or kind not in KINDS or type(query) is not str or not 1 <= len(query.strip()) <= 80 or any(
            ord(char) < 32 or ord(char) == 127 for char in query):
        raise ValidationError("Выберите тип и введите полный номер документа.")
    query = query.strip()
    if kind == "order":
        refresh_order_candidates(actor=actor, organization=organization, deal_id=deal_id, query=query, operational=True)
    else:
        with transaction.atomic():
            user, organization, deal, customers = _order_lookup_scope(actor, organization, deal_id, operational=True)
            pinned = (deal.client_id, _object_scope(deal), customers)
            source = current_order_source(organization.pk)
            if not source:
                raise OrderRefreshUnavailable
        config, source_id = source
        started = timezone.now()
        try:
            rows = read_crm_documents(config=config, kind=kind, number=query, customers=customers, now=started)
        except Exception as exc:
            raise OrderRefreshUnavailable from exc
        with transaction.atomic():
            user, organization, deal, customers = _order_lookup_scope(actor, organization, deal_id, operational=True)
            current = current_order_source(organization.pk)
            if (not current or current[1] != source_id or set(current[0].organization_guids) != set(config.organization_guids)
                    or pinned != (deal.client_id, _object_scope(deal), customers)):
                raise CommercialConflict("Источник или данные сделки изменились. Повторите проверку.")
            for (external_organization, guid), values in rows.items():
                scope = dict(organization=organization, source_id=source_id, external_organization=external_organization,
                    kind=kind, guid=guid)
                if not CrmOneCDocument.objects.filter(**scope, fetched_at__gte=started).exists():
                    CrmOneCDocument.objects.update_or_create(**scope, defaults={**values, "fetched_at": started})
            previous = CrmOneCDocument.objects.filter(organization=organization, source_id=source_id, kind=kind,
                external_organization__in=config.organization_guids, customer_guid__in=customers, number=query,
                fetched_at__lt=started)
            previous.update(valid=False)
    from .crm_commercial_access import deals_for_user
    deal = deals_for_user(actor, organization).select_related("client").filter(pk=deal_id).first()
    if not deal:
        raise PermissionDenied
    return {**document_candidates(actor=actor, organization=organization, deal=deal, kind=kind, query=query, exact=True),
        "checked_at": timezone.now().isoformat(), "links": document_projection(actor, organization, deal)}


def _document_to_bind(deal, source, customers, kind, record_id):
    if not source:
        raise PermissionDenied
    orders = _valid_orders(deal.organization, source, customers)
    if kind == "order":
        row = orders.filter(pk=record_id).first()
        occupied = row and (CrmDealOrderLink.objects.filter(order=row).exists()
            or CrmDealDocumentLink.objects.filter(order=row).exclude(deal=deal).exists())
        foreign_children = row and CrmDealDocumentLink.objects.filter(suppressed=False, document__valid=True,
            document__organization_id=deal.organization_id, document__source_id=row.source_id,
            document__external_organization=row.external_organization, document__order_guid=row.order_guid,
            document__customer_guid=row.customer_guid, document__currency_guid=row.currency_guid).exclude(deal=deal).exists()
        occupied = occupied or foreign_children
    else:
        row = _valid_documents(deal.organization, source, customers).filter(kind=kind, pk=record_id).first()
        occupied = row and (row.deal_claims.filter(suppressed=False).exclude(deal=deal).exists()
            or _matching_parent(row, orders).filter(Q(document_claim__isnull=False) | Q(deal_link__isnull=False))
                .exclude(Q(document_claim__deal=deal) | Q(deal_link__deal=deal)).exists())
    if not row or occupied:
        raise CommercialConflict("Документ недоступен для связи. Обновите список и проверьте существующие связи.")
    return row


def execute_document_command(*, actor, organization, operation_key, subject_id, expected_revision, changes):
    _positive_id(subject_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("Некорректная версия сделки.")
    if type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError("Некорректный ключ операции.")
    CANONICAL_KEY(operation_key)
    if type(changes) is not dict or not changes.keys() <= {"kind", "record_id", "remove_link_id", "exclude_id", "reason"}:
        raise ValidationError("Выберите документ или связь.")
    kind, record_id = changes.get("kind"), changes.get("record_id")
    remove_id, exclude_id = changes.get("remove_link_id"), changes.get("exclude_id")
    if (record_id is None) != (kind is None) or kind is not None and (type(kind) is not str or kind not in KINDS):
        raise ValidationError("Выберите тип и документ.")
    if not (record_id or remove_id or exclude_id) or remove_id and exclude_id:
        raise ValidationError("Выберите документ или связь.")
    for value in (record_id, remove_id, exclude_id):
        if value is not None:
            _positive_id(value)
    reason = changes.get("reason", "")
    if type(reason) is not str or len(reason.strip()) > 500 or (remove_id or exclude_id) and not reason.strip():
        raise ValidationError("Укажите причину снятия или замены связи (до 500 символов).")
    command_type = "deal.documents"
    digest = _hash([command_type, actor.pk, organization.pk, subject_id, expected_revision, changes])
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        user = _authorize("default", actor.pk, organization, "edit_own_deals")
        deal = _owned_deal("default", user.pk, organization.pk, subject_id)
        if not _writable(user, organization, deal):
            raise PermissionDenied
        previous = CrmCommand.objects.filter(organization=organization, operation_key=operation_key).first()
        if previous:
            if previous.actor_id != user.pk or previous.command_type != command_type or previous.payload_hash != digest:
                raise CommercialConflict("Ключ уже использован для другой операции.")
            return dict(previous.result)
        if expected_revision != deal.revision:
            raise CommercialConflict("Карточка изменилась. Откройте её заново.")
        source = current_order_source(organization.pk)
        customers = _confirmed_customers(organization, deal.client_id, lock=True) if source else set()
        new = _document_to_bind(deal, source, customers, kind, record_id) if record_id else None
        old = None
        if remove_id:
            link = CrmDealDocumentLink.objects.filter(organization=organization, deal=deal, pk=remove_id, suppressed=False).first()
            if not link:
                raise CommercialConflict("Связь уже изменилась. Откройте карточку заново.")
            old = ["order", link.order_id] if link.order_id else [link.document.kind, link.document_id]
            if link.document_id:
                link.suppressed = True
                link.confirmed_by = user
                link.confirmed_at = timezone.now()
                link.save(update_fields=["suppressed", "confirmed_by", "confirmed_at"])
            else:
                link.delete()
        if exclude_id:
            if not customers:
                raise CommercialConflict("Связь уже изменилась.")
            excluded = _automatic(deal, source, customers, _parents(deal, source, customers)).filter(pk=exclude_id).first()
            if not excluded:
                raise CommercialConflict("Связь уже изменилась.")
            old = [excluded.kind, excluded.pk]
            CrmDealDocumentLink.objects.create(deal=deal, organization=organization, document=excluded, client_id=deal.client_id,
                suppressed=True, confirmed_by=user, scope_fingerprint=_record_scope(excluded))
        if new:
            relation = {"order": new} if kind == "order" else {"document": new}
            CrmDealDocumentLink.objects.update_or_create(deal=deal, **relation, defaults=dict(organization=organization,
                client_id=deal.client_id, suppressed=False, confirmed_by=user, confirmed_at=timezone.now(),
                scope_fingerprint=_record_scope(new)))
        before = deal.revision
        deal.revision += 1
        deal.save(update_fields=["revision"])
        command = CrmCommand.objects.create(organization=organization, operation_key=operation_key,
            command_type=command_type, payload_hash=digest, actor=user)
        event = CrmEvent.objects.create(organization=organization, deal=deal, command=command, actor=user,
            event_type=command_type, reason=reason.strip(), safe_diff={"revision": {"before": before, "after": deal.revision}})
        DataAuditLog.objects.create(organization=organization, client_id=deal.client_id, actor=user,
            entity_type="CrmDealDocumentLink", entity_id=str(deal.pk), action="update" if old and new else "create" if new else "delete",
            before={"kind": old[0], "id": old[1]} if old else {},
            after={"event_id": event.pk, **({"kind": kind, "id": record_id} if new else {})},
            changed_fields=["document", "event_id"],)
        command.result = {"deal_id": deal.pk, "event_id": event.pk, "revision": deal.revision}
        command.save(update_fields=["result"])
        return dict(command.result)
