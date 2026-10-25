"""Manual provenance links to existing CRM cards. No provider writes or finances."""
from datetime import timedelta
import hashlib
import json
import re
import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_models import ChannelConnection, Conversation, PhoneCall
from .communication_services import organization_access, conversation_capability
from .crm_source_models import CrmSourceLink
from .models import CrmItem, DataAuditLog, Organization, ServiceTask
from .services.call_privacy import is_private_call, task_source_is_private
from .services.crm_permissions import crm_items_for_user, crm_directions
from .services.crm_locking import lock_crm_graph
from .services.permissions import company_has_access

ACCOUNT = "446312402"
LIMIT = 50


class LinkConflict(ValueError):
    pass


def scope(actor, connection_id=4):
    connection = ChannelConnection.objects.select_related("channel__organization").filter(pk=connection_id).first()
    if (connection is None or connection.pk != 4 or str(connection.external_id) != ACCOUNT
            or connection.channel.kind != "avito" or not connection.is_active or not connection.channel.is_active):
        raise PermissionDenied
    organization = connection.channel.organization
    if (actor is None or not actor.is_authenticated or not actor.is_active
            or organization_access(actor, organization) is None
            or not crm_directions(actor, organization) or not company_has_access(organization)):
        raise PermissionDenied
    return connection


def source_for(actor, connection, kind, identifier, *, edit=False, lock=False):
    organization = connection.channel.organization
    if edit and actor.is_superuser:
        raise PermissionDenied  # Existing CRM business writes exclude the technical superuser.
    if kind == "conversation":
        try:
            parsed = uuid.UUID(str(identifier))
        except (ValueError, TypeError, AttributeError):
            raise PermissionDenied from None
        query = Conversation.objects.select_related("connection__channel")
        if lock:
            query = query.select_for_update()
        source = query.filter(uuid=parsed, organization=organization, connection=connection,
            connection__channel__organization=organization).first()
        if source is not None and CrmSourceLink.objects.filter(conversation=source).exclude(organization=organization, avito_connection=connection).exists():
            raise PermissionDenied
        if source is None or not conversation_capability(actor, "can_view_conversations", organization):
            raise PermissionDenied
        if edit and (not conversation_capability(actor, "can_take_conversation", organization)
                or source.assignee_id not in (None, actor.pk)
                and not conversation_capability(actor, "can_assign_conversation", organization)):
            raise PermissionDenied
        return source
    if kind != "call" or not re.fullmatch(r"[1-9][0-9]{0,17}", str(identifier)):
        raise PermissionDenied
    query = PhoneCall.objects.select_related("connection")
    if lock:
        query = query.select_for_update()
    source = query.filter(pk=int(identifier), organization=organization,
        source_kind=PhoneCall.SOURCE_TELEPHONY).exclude(direction=PhoneCall.DIRECTION_INTERNAL).first()
    all_calls = conversation_capability(actor, "can_view_all_calls", organization)
    own_calls = conversation_capability(actor, "can_view_own_calls", organization)
    if (source is None or is_private_call(source)
            or source.connection is None or source.connection.organization_id != organization.pk
            or not (all_calls or own_calls and actor.pk in (source.employee_id, source.peer_employee_id))):
        raise PermissionDenied
    if CrmSourceLink.objects.filter(phone_call=source).exclude(organization=organization, avito_connection=connection).exists():
        raise PermissionDenied
    return source


def requests_for(actor, connection):
    organization = connection.channel.organization
    return crm_items_for_user(actor, organization).exclude(
        is_archived=True, archived_reason=CrmItem.ARCHIVE_REASON_DELETED).filter(
        Q(client__isnull=True) | Q(client__organization=organization)).filter(
        Q(pool__isnull=True) | (Q(pool__client__organization=organization)
            & (Q(pool__organization=organization) | Q(pool__organization__isnull=True))))


def item_for(actor, connection, item_id):
    if item_id is None:
        return None
    item = requests_for(actor, connection).select_related("client", "pool__client").filter(pk=item_id).first()
    organization_id = connection.channel.organization_id
    if item is None:
        raise PermissionDenied
    if (item.client_id and item.client.organization_id != organization_id
            or item.pool_id and (item.pool.organization_id not in (None, organization_id)
                or item.pool.client.organization_id != organization_id)):
        raise PermissionDenied
    return item


def task_allowed(actor, task, item, organization_id):
    if task is None:
        return True
    if item is None or task.crm_item_id != item.pk:
        return False
    from .views import _task_can_view
    seen = set()
    current = task
    while current is not None:
        if (current.pk in seen or len(seen) >= 20 or current.organization_id != organization_id
                or current.is_archived or not _task_can_view(current, actor)
                or task_source_is_private(current)
                or current.client_id and current.client.organization_id != organization_id
                or current.pool_id and (current.pool.organization_id not in (None, organization_id)
                    or current.pool.client.organization_id != organization_id)):
            return False
        payload = current.payload_json if isinstance(current.payload_json, dict) else {}
        if "source_call_id" in payload:
            raw = payload["source_call_id"]
            if type(raw) is bool or not re.fullmatch(r"[1-9][0-9]{0,17}", str(raw)):
                return False
            call = PhoneCall.objects.select_related("connection").filter(pk=int(raw), organization_id=organization_id).first()
            if (call is None or call.direction == PhoneCall.DIRECTION_INTERNAL
                    or call.source_kind == PhoneCall.SOURCE_TELEPHONY
                    and (call.connection is None or call.connection.organization_id != organization_id)):
                return False
            from types import SimpleNamespace
            from django.http import Http404
            from .communication_views import _authorize_call_access
            try:
                _authorize_call_access(SimpleNamespace(user=actor), call)
            except (PermissionDenied, Http404):
                return False
        seen.add(current.pk)
        current = current.parent_task
    return True


def source_key(kind, source):
    return {"conversation_id" if kind == "conversation" else "phone_call_id": source.pk}


def link_for(connection, kind, source):
    return CrmSourceLink.objects.filter(organization_id=connection.channel.organization_id,
        avito_connection=connection, **source_key(kind, source)).first()


def owned_ads(connection, now=None):
    from .avito_crm_coverage import owned_ad_observations
    return owned_ad_observations(connection, now or timezone.now())


def ad_provenance(connection, kind, source, mode, manual_id, existing, now):
    if mode == "preserve":
        if existing is None:
            mode = "auto"
        else:
            return existing.avito_item_id, existing.ad_source, dict(existing.evidence) if isinstance(existing.evidence, dict) else {}
    known = owned_ads(connection, now)
    evidence = {"schema_version": 1, "connection_id": connection.pk,
        "account_id": ACCOUNT, "organization_id": connection.channel.organization_id,
        "source_kind": kind, "source_id": source.pk, "confirmed_method": "manager",
        "confirmed_at": now.isoformat()}
    if mode == "manual":
        if manual_id not in known:
            raise ValueError("Принадлежность выбранного объявления аккаунту сейчас не подтверждена.")
        evidence.update(ad_method="manager_selection", ownership_checked_at=known[manual_id].isoformat())
        return manual_id, "manual", evidence
    if mode == "auto" and kind == "conversation":
        context = source.provider_context if isinstance(source.provider_context, dict) else {}
        item = context.get("item") if isinstance(context.get("item"), dict) else {}
        identifier = str(item.get("id", ""))
        synced = source.context_synced_at
        if identifier in known and synced is not None and timezone.is_aware(synced) and synced <= now:
            evidence.update(ad_method="stored_provider_context", context_synced_at=synced.isoformat(),
                ownership_checked_at=known[identifier].isoformat())
            return identifier, "provider", evidence
    if mode not in {"auto", "unknown"}:
        raise ValueError("Некорректный способ подтверждения источника.")
    evidence["ad_method"] = "unknown"
    return "", "unknown", evidence


def _audit_stamp(raw):
    try:
        stamp = parse_datetime(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if stamp is not None and timezone.is_aware(stamp) else None


def _audit_value(link):
    evidence = link.order_evidence if isinstance(link.order_evidence, dict) else {}
    source_evidence = link.evidence if isinstance(link.evidence, dict) else {}
    stamp = evidence.get("manual_confirmed_at")
    try:
        stamp = parse_datetime(stamp) if isinstance(stamp, str) else None
    except (TypeError, ValueError, OverflowError):
        stamp = None
    stamp = stamp.isoformat() if stamp is not None and timezone.is_aware(stamp) else None
    return {"crm_item_id": link.crm_item_id, "next_task_id": link.next_task_id,
        "ad_source": link.ad_source, "avito_item_id": link.avito_item_id,
        "quality": link.quality, "call_origin": link.call_origin,
        "order_guid_sha256": hashlib.sha256(str(link.order_guid).encode()).hexdigest() if link.order_guid else "",
        "reason_sha256": hashlib.sha256(link.reason.encode()).hexdigest(),
        "order_evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        "source_evidence_sha256": hashlib.sha256(json.dumps(source_evidence, sort_keys=True).encode()).hexdigest(),
        "ad_confirmed_by_id": source_evidence.get("ad_confirmed_by_id") if type(source_evidence.get("ad_confirmed_by_id")) is int else None,
        "ad_confirmed_at": _audit_stamp(source_evidence.get("ad_confirmed_at")).isoformat() if _audit_stamp(source_evidence.get("ad_confirmed_at")) else None,
        "order_snapshot_id": evidence.get("snapshot_id") if type(evidence.get("snapshot_id")) is int else None,
        "order_confirmed_by_id": evidence.get("manual_confirmed_by_id") if type(evidence.get("manual_confirmed_by_id")) is int else None,
        "order_confirmed_at": stamp,
        "revision": link.revision}


def save_link(actor, connection_id, kind, identifier, *, item_id=None, task_id=None,
              quality="unknown", reason="", ad_mode="preserve", manual_ad_id="",
              call_origin="preserve", order_action="preserve", order_guid="",
              expected_revision=0, operation=None):
    from . import crm_source_orders as orders
    if (quality not in dict(CrmSourceLink.QUALITY_CHOICES)
            or ad_mode not in {"preserve", "auto", "manual", "unknown"}
            or call_origin not in {"preserve", "unknown", "manual"}
            or order_action not in {"preserve", "set", "clear"}):
        raise ValueError("Некорректная оценка или источник.")
    reason = reason.strip()
    if len(reason) > 500 or type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Некорректная версия или слишком длинная причина.")
    try:
        operation = uuid.UUID(str(operation))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Некорректный ключ операции.") from None
    for value in (item_id, task_id):
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("Некорректная ссылка.")
    payload = {"actor": actor.pk, "connection": connection_id, "item": item_id,
        "task": task_id, "quality": quality, "reason": reason, "ad_mode": ad_mode,
        "manual_ad_id": manual_ad_id, "call_origin": call_origin, "order_action": order_action,
        "order_guid": str(order_guid or ""), "kind": kind, "source": str(identifier)}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with transaction.atomic():
        current = scope(actor, connection_id)
        def binding(value):
            return (value.pk, value.channel_id, value.channel.organization_id, str(value.external_id),
                value.is_active, value.channel.is_active, value.channel.kind)
        expected_binding = binding(current)
        # Match communication_enrichment: connection -> organization -> source.
        locked = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=current.pk)
        if binding(locked) != expected_binding:
            raise PermissionDenied
        Organization.objects.select_for_update().get(pk=locked.channel.organization_id)
        current = scope(actor, connection_id)
        if binding(current) != expected_binding:
            raise PermissionDenied
        source = source_for(actor, current, kind, identifier, edit=True, lock=True)
        if item_id is not None:
            lock_crm_graph([item_id], extra_task_ids=[task_id] if task_id else [])
        item = item_for(actor, current, item_id)
        task = ServiceTask.objects.select_related("parent_task").filter(pk=task_id).first() if task_id else None
        if task_id and (task is None or not task_allowed(actor, task, item, current.channel.organization_id)):
            raise PermissionDenied
        link = CrmSourceLink.objects.select_for_update().filter(**source_key(kind, source)).first()
        if link and (link.organization_id != current.channel.organization_id or link.avito_connection_id != current.pk):
            raise PermissionDenied
        if link and link.crm_item_id:
            item_for(actor, current, link.crm_item_id)
        if link and link.next_task_id and task_id is None and link.crm_item_id == item_id:
            if not task_allowed(actor, link.next_task, item, current.channel.organization_id):
                task = link.next_task
        can_finance = orders.allowed(actor, current.channel.organization)
        if (order_action != "preserve" and not can_finance
                or link and link.order_guid and link.crm_item_id != item_id and not can_finance):
            raise PermissionDenied
        observed_order = None
        if order_action == "set":
            observed_order = orders.observed(actor, current.channel.organization, item, order_guid, lock=True)
            if observed_order is None:
                raise ValueError("Документ заказа не подтверждён текущим разрешённым источником.")
        elif order_action == "preserve" and order_guid:
            raise ValueError("Для изменения финансовой ссылки выберите явное действие.")
        if link and link.last_operation == operation:
            if link.last_operation_hash != fingerprint:
                raise LinkConflict("Этот ключ уже использован для другого изменения.")
            return link, False
        if (link.revision if link else 0) != expected_revision:
            raise LinkConflict("Связь уже изменена. Обновите страницу перед сохранением.")
        now = timezone.now()
        identifier_ad, ad_source, evidence = ad_provenance(
            current, kind, source, ad_mode, manual_ad_id, link, now)
        origin = "unknown" if kind == "conversation" else (
            link.call_origin if call_origin == "preserve" and link else
            "unknown" if call_origin == "preserve" else call_origin)
        if kind == "call" and ad_source == "manual" and origin != "manual":
            raise ValueError("Сначала явно подтвердите происхождение звонка с Авито.")
        if (ad_mode != "preserve" or link is None) and identifier_ad:
            evidence.update(ad_confirmed_by_id=actor.pk, ad_confirmed_at=now.isoformat())
        evidence.update(schema_version=1, connection_id=current.pk, account_id=ACCOUNT,
            organization_id=current.channel.organization_id, source_kind=kind, source_id=source.pk)
        if kind == "call" and call_origin == "manual":
            evidence["call_origin_ack"] = {"method": "manager_ack", "actor_id": actor.pk,
                "confirmed_at": now.isoformat()}
        elif kind == "call" and origin == "manual" and link:
            evidence["call_origin_ack"] = dict((link.evidence or {}).get("call_origin_ack") or {})
        elif kind == "call":
            evidence.pop("call_origin_ack", None)
        if link is None:
            link = CrmSourceLink(organization=current.channel.organization,
                avito_connection=current, **source_key(kind, source))
        before = _audit_value(link)
        link.crm_item, link.next_task = item, task
        link.quality, link.reason, link.call_origin = quality, reason, origin
        link.avito_item_id, link.ad_source, link.evidence = identifier_ad, ad_source, evidence
        if order_action == "clear":
            link.order_guid, link.order_evidence = None, {}
        elif order_action == "set":
            link.order_guid = observed_order["guid"]
            link.order_evidence = dict(observed_order["evidence"])
            link.order_evidence.update(manual_confirmation=True, manual_confirmed_by_id=actor.pk,
                manual_confirmed_at=now.isoformat())
        elif can_finance and link.order_guid and link.crm_item_id != before["crm_item_id"]:
            raise ValueError("При смене заявки подтвердите или снимите ссылку на документ заказа.")
        link.confirmed_by, link.confirmed_at = actor, now
        link.revision += 1
        link.last_operation, link.last_operation_hash = operation, fingerprint
        link.full_clean()
        link.save()
        after = _audit_value(link)
        DataAuditLog.objects.create(entity_type="crm_source_link", entity_id=str(link.pk),
            organization=current.channel.organization, actor=actor,
            action=DataAuditLog.ACTION_CREATE if before["revision"] == 0 else DataAuditLog.ACTION_UPDATE,
            before=before, after=after,
            changed_fields=[key for key in after if before.get(key) != after[key]])
        return link, True

def visible_link(actor, connection, kind, source):
    link = link_for(connection, kind, source)
    if link is None:
        return None
    if link.crm_item_id:
        try:
            item_for(actor, connection, link.crm_item_id)
        except PermissionDenied:
            return None
    return link


def _evidence_valid(link, connection, kind, source):
    data = link.evidence if isinstance(link.evidence, dict) else {}
    valid = (data.get("connection_id") == connection.pk and data.get("account_id") == ACCOUNT
        and data.get("organization_id") == connection.channel.organization_id
        and data.get("source_kind") == kind and data.get("source_id") == source.pk)
    if link.ad_source in {"provider", "manual"}:
        stamp = _audit_stamp(data.get("ad_confirmed_at"))
        valid = (valid and type(data.get("ad_confirmed_by_id")) is int and data["ad_confirmed_by_id"] > 0
            and stamp is not None and stamp <= timezone.now()
            and data.get("ad_method") == ("manager_selection" if link.ad_source == "manual" else "stored_provider_context"))
    if kind == "call" and link.call_origin == "manual":
        ack = data.get("call_origin_ack")
        stamp = _audit_stamp(ack.get("confirmed_at")) if isinstance(ack, dict) else None
        valid = (valid and isinstance(ack, dict) and ack.get("method") == "manager_ack"
            and type(ack.get("actor_id")) is int and ack["actor_id"] > 0
            and stamp is not None and stamp <= timezone.now())
    return bool(valid)


def coverage(actor, connection):
    """Dialogs are one source each; unconfirmed company calls are candidates only."""
    from . import crm_source_orders as orders
    try:
        current = scope(actor, connection.pk)
    except PermissionDenied:
        return None
    if current.channel.organization_id != connection.channel.organization_id:
        return None
    connection = current
    result = {"dialogs": 0, "calls": 0, "linked_dialogs": 0, "linked_calls": 0,
        "unknown_requests": 0, "distinct_requests": 0, "confirmed_ad_sources": 0,
        "ad_source_unknown": 0, "quality_target": 0, "quality_non_target": 0,
        "candidate_calls_unknown": 0, "candidate_calls_linked": 0,
        "observed_order_references": None, "order_attribution_ambiguous": None,
        "order_links": None, "payment_links": None, "revenue": None, "complete": True}
    ids, known, sources = set(), owned_ads(connection), []
    if conversation_capability(actor, "can_view_conversations", connection.channel.organization):
        dialogs = list(Conversation.objects.filter(connection=connection,
            organization_id=connection.channel.organization_id,
            connection__channel__organization_id=connection.channel.organization_id).order_by("pk")[:501])
        result["complete"] = len(dialogs) <= 500
        mapping = {row.conversation_id: row for row in CrmSourceLink.objects.filter(
            organization_id=connection.channel.organization_id, avito_connection=connection,
            conversation_id__in=[source.pk for source in dialogs[:500]]).select_related("crm_item")}
        for candidate in dialogs[:500]:
            try:
                source = source_for(actor, connection, "conversation", candidate.uuid)
            except PermissionDenied:
                continue
            sources.append(("conversation", source, mapping.get(source.pk)))
    call_links = list(CrmSourceLink.objects.filter(organization_id=connection.channel.organization_id,
        avito_connection=connection, phone_call__isnull=False).select_related("phone_call", "crm_item").order_by("pk")[:501])
    result["complete"] = result["complete"] and len(call_links) <= 500
    for link in call_links[:500]:
        try:
            source = source_for(actor, connection, "call", link.phone_call_id)
        except PermissionDenied:
            continue
        sources.append(("call", source, link))
    finance = orders.allowed(actor, connection.channel.organization)
    order_sources = {}
    for kind, source, link in sources:
        if link and not _evidence_valid(link, connection, kind, source):
            link = None
        if link and link.crm_item_id:
            try:
                item = item_for(actor, connection, link.crm_item_id)
            except PermissionDenied:
                link, item = None, None
        else:
            item = None
        if kind == "call" and (link is None or link.call_origin != "manual"):
            result["candidate_calls_unknown"] += 1
            result["candidate_calls_linked"] += bool(item)
            continue
        bucket = "dialogs" if kind == "conversation" else "calls"
        result[bucket] += 1
        if item:
            ids.add(item.pk)
            result["linked_" + bucket] += 1
        else:
            result["unknown_requests"] += 1
        ad_confirmed = bool(link and link.ad_source in {"provider", "manual"} and link.avito_item_id in known)
        if ad_confirmed and link.ad_source == "provider":
            context = source.provider_context if isinstance(source.provider_context, dict) else {}
            provider_item = context.get("item") if isinstance(context.get("item"), dict) else {}
            ad_confirmed = str(provider_item.get("id", "")) == link.avito_item_id
        result["confirmed_ad_sources" if ad_confirmed else "ad_source_unknown"] += 1
        if link:
            result["quality_target"] += link.quality == "target"
            result["quality_non_target"] += link.quality == "non_target"
            if finance and item and link.order_guid:
                state = orders.state(actor, connection.channel.organization, link, item)
                if state["state"] == "observed":
                    order_sources.setdefault(str(link.order_guid), set()).add(link.avito_item_id if ad_confirmed else "")
    result["distinct_requests"] = len(ids)
    if finance:
        result["observed_order_references"] = len(order_sources)
        result["order_attribution_ambiguous"] = sum(len(ads) != 1 or "" in ads for ads in order_sources.values())
    return result


def setup_evidence():
    """Metadata-only post-deploy check; never creates associations."""
    from django.db import connection as database
    if CrmSourceLink._meta.db_table not in database.introspection.table_names():
        return {"state": "blocked", "code": "crm_source_table_unavailable"}
    connection = ChannelConnection.objects.select_related("channel").filter(pk=4).first()
    if connection is None or str(connection.external_id) != ACCOUNT:
        return {"state": "blocked", "code": "crm_source_scope_invalid"}
    try:
        from .avito_services_workplace_setup import _actor
        actor = _actor(connection)
        scope(actor, 4)
        counts = coverage(actor, connection)
        if not isinstance(counts, dict):
            return {"state": "blocked", "code": "crm_source_scope_invalid"}
    except (PermissionDenied, ValueError):
        return {"state": "blocked", "code": "crm_source_actor_unavailable"}
    return {"schema_version": 1, "state": "ready", "connection_id": 4, "account_id": ACCOUNT,
        "visible_dialogs": counts["dialogs"], "confirmed_request_sources": counts["linked_dialogs"] + counts["linked_calls"],
        "distinct_requests": counts["distinct_requests"], "assessed_calls_with_unknown_origin": counts["candidate_calls_unknown"],
        "table_ready": True, "manager_route": "/avito/4/attribution/",
        "automatic_request_creation": False, "customer_post_performed": False,
        "financial_amounts_logged": False}

def navigation(actor, organization):
    """A discoverable manager entry without granting channel-management rights."""
    try:
        connection = scope(actor, 4)
    except PermissionDenied:
        return ""
    if connection.channel.organization_id != organization.pk:
        return ""
    if not any(conversation_capability(actor, name, organization) for name in
               ("can_view_conversations", "can_view_own_calls", "can_view_all_calls")):
        return ""
    from django.urls import reverse
    return reverse("avito_crm_attribution", args=[4])
