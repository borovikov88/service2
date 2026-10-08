"""Owner-only draft settings and free metadata preview; no dispatch side effects."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import BooleanField, Case, Value, When
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess
from pool_service.communication_models import PhoneCall, TelephonyEmployeeIdentity
from pool_service.call_processing_models import (
    CallProcessingBudget,
    CallProcessingRule,
    CallPrivateNumber,
    CallProcessingRuleAudit,
)
from pool_service.services.call_processing_policy import (
    MANUAL, MODES, MAX_NUMBERS, CallFacts, Rule, decide_call, parse_numbers, phone_key,
)

STAFF_ROLES = frozenset({"owner", "admin", "manager", "service", "installer"})
PREVIEW_LIMIT = 500
REASONS = {
    "personal": "Личное — исключено",
    "manual_upload": "Загруженный файл — только вручную",
    "direction_unknown": "Не определено направление",
    "mapping_required": "Требуется сопоставление сотрудника и линии",
    "no_conversation": "Нет состоявшегося разговора",
    "number_unknown": "Номер собеседника не определён",
    "privacy_mapping_required": "Нельзя надёжно проверить личные исключения",
    "manual": "Только вручную — правило не включено",
    "invalid_rule": "Правило требует исправления",
    "activation_required": "Правило ещё не активировано",
    "historical": "Звонок старше даты включения",
    "not_allowed": "Номер не в разрешающем списке",
    "already_ready": "Готовая расшифровка — повтор не нужен",
    "already_processing": "Обработка уже выполняется",
    "saved_transcript": "Сохранённый текст — только продолжение анализа",
    "audio_pending": "Подходит, но нужно дождаться аудиофайла",
    "rules_match": "Подходит для автоматической расшифровки",
}


def settings_allowed(user, organization):
    return bool(
        user and user.is_authenticated and user.is_active and not user.is_superuser
        and organization and OrganizationAccess.objects.filter(
            organization=organization, user=user, role="owner",
        ).exists()
    )


def _lock_owner(user, organization):
    if not settings_allowed(user, organization):
        raise PermissionDenied
    # All preference mutations serialize at organization -> user -> role.
    Organization.objects.select_for_update().get(pk=organization.pk)
    actor = User.objects.select_for_update().filter(pk=user.pk).first()
    roles = set(OrganizationAccess.objects.select_for_update().filter(
        organization=organization, user_id=user.pk,
    ).order_by("pk").values_list("role", flat=True))
    if not actor or not actor.is_active or actor.is_superuser or "owner" not in roles:
        raise PermissionDenied
    return actor


def _audit(organization, actor, target, action, details):
    CallProcessingRuleAudit.objects.create(
        organization=organization, actor=actor, target_user_id=target,
        action=action, details=details,
    )


@transaction.atomic
def save_rule(*, user, organization, employee_id, mode, include_staff, numbers_text, expected_revision):
    actor = _lock_owner(user, organization)
    if mode not in MODES or not isinstance(include_staff, bool):
        raise ValidationError("Некорректный режим обработки.")
    employee = User.objects.select_for_update().filter(pk=employee_id, is_active=True).first()
    roles = set(OrganizationAccess.objects.select_for_update().filter(
        organization=organization, user_id=employee_id,
    ).order_by("pk").values_list("role", flat=True))
    if not employee or not roles.intersection(STAFF_ROLES):
        raise PermissionDenied
    if "owner" in roles and employee_id != actor.pk:
        raise PermissionDenied
    try:
        numbers = list(parse_numbers(numbers_text))
    except ValueError as exc:
        raise ValidationError("Укажите до 500 корректных номеров, каждый с новой строки.") from exc
    rule = CallProcessingRule.objects.select_for_update().filter(
        organization=organization, employee=employee,
    ).first()
    revision = rule.revision if rule else 0
    if isinstance(expected_revision, bool) or expected_revision != revision:
        raise ValidationError("Правило уже изменено. Обновите страницу перед сохранением.")
    if rule is None:
        rule = CallProcessingRule(organization=organization, employee=employee)
    rule.mode = mode
    rule.include_staff = include_staff
    rule.work_numbers = numbers
    rule.revision = revision + 1
    rule.changed_by = actor
    # This release prepares preferences only. No activation, queue creation or
    # historical backfill is performed by saving a rule.
    rule.effective_from = None
    rule.full_clean()
    rule.save()
    _audit(organization, actor, employee.pk, "rule_saved", {
        "revision": rule.revision, "mode": mode,
        "include_staff": include_staff, "number_count": len(numbers),
    })
    return rule


@transaction.atomic
def save_budget(*, user, organization, monthly_limit_usd, expected_revision):
    actor = _lock_owner(user, organization)
    budget = CallProcessingBudget.objects.select_for_update().filter(
        organization=organization,
    ).first()
    revision = budget.revision if budget else 0
    if isinstance(expected_revision, bool) or expected_revision != revision:
        raise ValidationError("Лимит уже изменён. Обновите страницу перед сохранением.")
    if monthly_limit_usd is not None and monthly_limit_usd <= 0:
        raise ValidationError("Лимит должен быть больше нуля или оставлен пустым.")
    if budget is None:
        budget = CallProcessingBudget(organization=organization)
    budget.monthly_limit_usd = monthly_limit_usd
    budget.revision = revision + 1
    budget.changed_by = actor
    budget.full_clean()
    budget.save()
    _audit(
        organization,
        actor,
        actor.pk,
        "budget_saved",
        {
            "revision": budget.revision,
            "monthly_limit_usd": (
                str(budget.monthly_limit_usd)
                if budget.monthly_limit_usd is not None
                else None
            ),
        },
    )
    return budget


@transaction.atomic
def add_private_numbers(*, user, organization, label, numbers_text):
    actor = _lock_owner(user, organization)
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 120:
        raise ValidationError("Укажите название личного контакта, до 120 символов.")
    try:
        numbers = parse_numbers(numbers_text)
    except ValueError as exc:
        raise ValidationError("Проверьте номера личного контакта.") from exc
    if not numbers:
        raise ValidationError("Добавьте хотя бы один номер.")
    existing = set(CallPrivateNumber.objects.filter(
        organization=organization, owner=actor,
    ).values_list("phone_key", flat=True))
    if len(existing | set(numbers)) > MAX_NUMBERS:
        raise ValidationError("В личном списке допускается не более 500 номеров.")
    changed = 0
    for key in numbers:
        row, created = CallPrivateNumber.objects.get_or_create(
            organization=organization, owner=actor, phone_key=key,
            defaults={"label": label.strip()},
        )
        if not created and row.label != label.strip():
            row.label = label.strip()
            row.save(update_fields=["label"])
            changed += 1
        elif created:
            changed += 1
    if changed:
        _audit(organization, actor, actor.pk, "private_numbers_saved", {"changed_count": changed})
    return changed


@transaction.atomic
def remove_private_number(*, user, organization, number_id):
    actor = _lock_owner(user, organization)
    row = CallPrivateNumber.objects.select_for_update().filter(
        pk=number_id, organization=organization, owner=actor,
    ).first()
    if row is None:
        raise PermissionDenied
    row.delete()
    _audit(organization, actor, actor.pk, "private_number_removed", {"changed_count": 1})


def _identity_rows(organization, active_ids):
    return list(TelephonyEmployeeIdentity.objects.filter(
        organization=organization, connection__organization=organization,
        connection__is_active=True, is_active=True, requires_manual_confirmation=False,
        status__in=[TelephonyEmployeeIdentity.STATUS_AUTO_MATCHED, TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED],
        employee__organization=organization, employee__user_id__in=active_ids,
    ).values("connection_id", "extension", "external_user", "employee__user_id"))


def preview_rules(*, user, organization, now=None):
    """Inspect <=500 recent call metadata rows. Never fetch transcript/audio text.

All personal rules are used internally for classification, but another owner's
numbers/labels never leave this function. Private calls are omitted altogether.
This simulates future rules against history; it does not request processing.
"""
    if not settings_allowed(user, organization):
        raise PermissionDenied
    now = now or timezone.now()
    active = list(User.objects.filter(
        is_active=True, organizationaccess__organization=organization,
        organizationaccess__role__in=STAFF_ROLES,
    ).distinct())
    users = {item.pk: item for item in active}
    rules = {row.employee_id: Rule(
        mode=row.mode, include_staff=row.include_staff,
        work_numbers=frozenset(row.work_numbers if isinstance(row.work_numbers, list) else []),
        effective_from=row.effective_from,
    ) for row in CallProcessingRule.objects.filter(organization=organization, employee_id__in=users)}
    private = defaultdict(set)
    for owner_id, key in CallPrivateNumber.objects.filter(organization=organization).values_list("owner_id", "phone_key"):
        private[owner_id].add(key)
    private = {uid: frozenset(keys) for uid, keys in private.items()}
    references = defaultdict(set)
    user_numbers = defaultdict(set)
    for item in _identity_rows(organization, users):
        uid = item["employee__user_id"]
        for kind, value in (("extension", item["extension"]), ("user", item["external_user"])):
            if value:
                references[(item["connection_id"], kind, value)].add(uid)
                key = phone_key(value)
                if key:
                    user_numbers[uid].add(key)
    for uid, account in users.items():
        key = phone_key(account.username)
        if key:
            user_numbers[uid].add(key)

    def verified(row, prefix=""):
        uid = row[prefix + "employee_id"]
        if uid not in users:
            return False
        matches = set()
        for kind, field in (("extension", "provider_extension"), ("user", "provider_user")):
            value = row[prefix + field]
            if value:
                matches.update(references.get((row["connection_id"], kind, value), set()))
        return matches == {uid}

    queryset = PhoneCall.objects.filter(
        organization=organization, source_kind=PhoneCall.SOURCE_TELEPHONY,
        started_at__gte=now - timedelta(days=7), started_at__lte=now,
    ).annotate(saved_text=Case(
        When(analysis__transcript__gt="", then=Value(True)),
        default=Value(False), output_field=BooleanField(),
    )).order_by("-started_at", "-pk")
    calls = list(queryset.values(
        "id", "connection_id", "employee_id", "peer_employee_id",
        "provider_extension", "provider_user", "peer_provider_extension", "peer_provider_user",
        "phone_number", "direction", "started_at", "duration_seconds", "result",
        "recording_status", "recording_file", "analysis__status", "saved_text",
    )[:PREVIEW_LIMIT + 1])
    rows, selected_seconds, selected_count, private_count = [], 0, 0, 0
    for row in calls[:PREVIEW_LIMIT]:
        participants = tuple(uid for uid in (row["employee_id"], row["peer_employee_id"]) if uid)
        external_keys = set()
        key = phone_key(row["phone_number"])
        if key:
            external_keys.add(key)
        counterpart_pairs = []
        keys = set(external_keys)
        if row["direction"] == PhoneCall.DIRECTION_INTERNAL:
            keys.clear()
            for uid in participants:
                peer_keys = set()
                for peer_uid in participants:
                    if peer_uid != uid:
                        peer_keys.update(user_numbers.get(peer_uid, set()))
                counterpart_pairs.append((uid, frozenset(peer_keys)))
                keys.update(peer_keys)
        else:
            counterpart_pairs = [
                (uid, frozenset(external_keys)) for uid in participants
            ]
        is_verified = verified(row) and (
            verified(row, "peer_") if row["direction"] == PhoneCall.DIRECTION_INTERNAL or row["peer_employee_id"] else True
        )
        facts = CallFacts(
            participants=participants, counterpart_numbers=frozenset(keys),
            counterpart_numbers_by_participant=tuple(counterpart_pairs),
            started_at=row["started_at"], verified=is_verified,
            direction=row["direction"], answered=row["result"] == PhoneCall.RESULT_ANSWERED,
            duration_seconds=row["duration_seconds"],
            audio_ready=row["recording_status"] == PhoneCall.RECORDING_STORED and bool(row["recording_file"]),
            analysis_status=row["analysis__status"] or "", saved_transcript=row["saved_text"],
        )
        decision = decide_call(facts, rules, private, simulation=True)
        if decision.reason == "personal":
            # Count only exclusions that match the viewing owner's actual
            # counterpart. Never use the aggregate internal-call number set:
            # it can include the viewer's own number and leak that a peer's
            # private rule hid the row.
            viewer_counterparts = dict(
                facts.counterpart_numbers_by_participant
            ).get(user.pk, frozenset())
            if (
                user.pk in participants
                and private.get(user.pk, frozenset()) & viewer_counterparts
            ):
                private_count += 1
            continue
        if decision.selected:
            selected_count += 1
            selected_seconds += row["duration_seconds"]
        rows.append({
            "id": row["id"], "started_at": row["started_at"], "phone": row["phone_number"],
            "employees": ", ".join((users[uid].get_full_name() or users[uid].username) for uid in dict.fromkeys(participants) if uid in users) or "Не сопоставлен",
            "duration_seconds": row["duration_seconds"], "selected": decision.selected,
            "action": decision.action, "reason": REASONS[decision.reason],
        })
    return {
        "rows": rows, "selected_count": selected_count,
        "selected_minutes": (Decimal(selected_seconds) / Decimal(60)).quantize(Decimal("0.01")),
        "own_private_count": private_count, "limited": len(calls) > PREVIEW_LIMIT,
        "limit": PREVIEW_LIMIT, "from_date": now - timedelta(days=7), "to_date": now,
    }
