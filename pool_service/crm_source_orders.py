"""Observed planning-source order references, never payment or causal revenue."""
import hashlib
import json
import uuid

from django.core.exceptions import PermissionDenied

from .client_crm_models import ClientCRMProfile
from .finance_imports.money_forecast import _active_snapshot
from .money_models import OneCMoneyForecastRow, OneCMoneyForecastSnapshot
from .services.finance import can_access_finance_overview

LABEL = "Наблюдаемый документ заказа 1С; связь подтверждена вручную"


def allowed(actor, organization):
    return bool(actor is not None and actor.is_authenticated and actor.is_active and can_access_finance_overview(actor, organization))


def _uuid(value):
    try:
        result = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return result if result.int else None


def observed(actor, organization, item, order_guid, *, lock=False):
    if not allowed(actor, organization):
        raise PermissionDenied
    guid = _uuid(order_guid)
    if guid is None or item is None or item.organization_id != organization.pk or not item.client_id:
        return None
    profile = ClientCRMProfile.objects.filter(client_id=item.client_id,
        client__organization=organization, source=ClientCRMProfile.SOURCE_ONEC,
        merged_into__isnull=True, last_synced_at__isnull=False).first()
    party = _uuid(profile.onec_ref) if profile else None
    snapshot = _active_snapshot(organization)
    if party is None or snapshot is None:
        return None
    if lock:
        snapshot = OneCMoneyForecastSnapshot.objects.select_for_update().filter(pk=snapshot.pk, organization=organization, is_active=True).first()
        if snapshot is None:
            return None
    rows = list(OneCMoneyForecastRow.objects.filter(snapshot=snapshot, direction="receipt",
        item_kind__in=("order_schedule", "order_due"), order_guid=guid).order_by("pk")[:101])
    if (not rows or len(rows) > 100
            or any(_uuid(row.counterparty_guid) != party for row in rows)):
        return None
    shapes = {(row.order_number, row.order_date.isoformat() if row.order_date else None,
               row.order_state) for row in rows}
    if len(shapes) != 1:
        return None
    current = _active_snapshot(organization)
    if current is None or current.pk != snapshot.pk:
        return None
    number, date, state = next(iter(shapes))
    # Schedules dedupe to one observed document. No forecast monetary fields enter this projection.
    business = {"order_guid": str(guid), "counterparty_guid": str(party),
        "number": number, "date": date, "state": state}
    digest = hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()
    return {"guid": guid, "number": number, "date": date, "state": state,
        "evidence": {"schema_version": 1, "source": "active_money_forecast_planning_snapshot",
            "snapshot_id": snapshot.pk, "source_at": snapshot.source_at.isoformat(),
            "fetched_at": snapshot.fetched_at.isoformat(), "business_sha256": digest,
            "order_guid_sha256": hashlib.sha256(str(guid).encode()).hexdigest(),
            "profile_id": profile.pk, "profile_synced_at": profile.last_synced_at.isoformat(),
            "manual_confirmation": True, "payment_confirmed": False}}


def candidates(actor, organization, item):
    if not allowed(actor, organization):
        return None
    if item is None or not item.client_id:
        return []
    profile = ClientCRMProfile.objects.filter(client_id=item.client_id,
        client__organization=organization, source=ClientCRMProfile.SOURCE_ONEC,
        merged_into__isnull=True, last_synced_at__isnull=False).first()
    party = _uuid(profile.onec_ref) if profile else None
    snapshot = _active_snapshot(organization)
    if party is None or snapshot is None:
        return []
    guids = list(OneCMoneyForecastRow.objects.filter(snapshot=snapshot, direction="receipt",
        item_kind__in=("order_schedule", "order_due"), counterparty_guid=party,
        order_guid__isnull=False).order_by("order_guid").values_list("order_guid", flat=True).distinct()[:51])
    return [result for guid in guids[:50] if (result := observed(actor, organization, item, guid)) is not None]


def state(actor, organization, link, item):
    if not allowed(actor, organization):
        return {"state": "restricted", "label": "Финансовая ссылка недоступна при текущих правах."}
    if link is None or link.order_guid is None:
        return {"state": "unknown", "label": "Документ заказа не связан; оплата и выручка неизвестны."}
    current = observed(actor, organization, item, link.order_guid)
    stored = link.order_evidence if isinstance(link.order_evidence, dict) else {}
    if (current is None or stored.get("source") != "active_money_forecast_planning_snapshot"
            or stored.get("business_sha256") != current["evidence"]["business_sha256"]):
        return {"state": "unknown", "label": "Ссылка сохранена исторически; текущий источник требует сверки. Оплата неизвестна."}
    return {"state": "observed", "label": LABEL, "guid": current["guid"],
        "number": current["number"], "date": current["date"], "order_state": current["state"],
        "source_at": current["evidence"]["source_at"], "fetched_at": current["evidence"]["fetched_at"],
        "payment_confirmed": False}
