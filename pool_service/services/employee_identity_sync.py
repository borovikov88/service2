import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.communication_models import (
    ChannelConnection,
    CommunicationChannel,
    PhoneCall,
    TelephonyEmployeeIdentity,
)
from pool_service.communication_secrets import CommunicationSecretError, decrypt_secret
from pool_service.finance_imports.employee_matching import (
    confirm_employee_identity,
    normalize_onec_name,
    resolve_employee_identity,
)
from pool_service.finance_imports.odata_profit import (
    ODataPreviewError,
    ZERO_GUID,
    normalize_guid,
    read_odata_pages,
    validate_config,
)
from pool_service.finance_imports.odata_profit_drafts import (
    config_from_settings,
    is_odata_target_organization,
)
from pool_service.models import (
    DataAuditLog,
    Employee,
    EmployeeOneCIdentity,
    OrganizationAccess,
)


ONEC_EMPLOYEE_ENTITY = "Catalog_Сотрудники"
ONEC_EMPLOYEE_FIELDS = (
    "Ref_Key",
    "Code",
    "Description",
    "DeletionMark",
    "ВАрхиве",
    "Недействителен",
    "ГоловнаяОрганизация_Key",
)
MAX_MEGAFON_ACCOUNTS = 500
MAX_MEGAFON_RESPONSE_BYTES = 512 * 1024


class EmployeeIdentitySyncError(ValidationError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _employee_display_name(employee):
    return employee.display_name or " ".join(
        value
        for value in (
            employee.last_name,
            employee.first_name,
            employee.middle_name,
        )
        if value
    )


def _employee_candidates(organization, raw_name):
    normalized = normalize_onec_name(raw_name)
    return [
        employee
        for employee in Employee.objects.filter(
            organization=organization,
            is_active=True,
        ).select_related("user").order_by("id")
        if normalize_onec_name(_employee_display_name(employee)) == normalized
    ]


def _name_tokens(value):
    return {
        token
        for token in normalize_onec_name(value)
        .replace("(", " ")
        .replace(")", " ")
        .split()
        if token
    }


def _service_user_candidates(organization, employee):
    normalized = normalize_onec_name(_employee_display_name(employee))
    employee_tokens = _name_tokens(_employee_display_name(employee))
    candidates = []
    accesses = (
        OrganizationAccess.objects.filter(
            organization=organization,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("user_id")
    )
    seen = set()
    for access in accesses:
        user = access.user
        if user.pk in seen:
            continue
        seen.add(user.pk)
        name_values = [
            user.get_full_name(),
            " ".join(
                value
                for value in (user.last_name, user.first_name)
                if value
            ),
        ]
        username_match = (
            bool(user.username)
            and normalize_onec_name(user.username) == normalized
        )
        name_match = any(
            len(_name_tokens(value)) >= 2
            and _name_tokens(value).issubset(employee_tokens)
            for value in name_values
            if value
        )
        if username_match or name_match:
            candidates.append(user)
    return candidates


def auto_link_service2_user(employee, *, actor=None):
    if employee.user_id:
        return False
    candidates = _service_user_candidates(employee.organization, employee)
    if len(candidates) != 1:
        return False
    user = candidates[0]
    if Employee.objects.filter(
        organization=employee.organization,
        user=user,
    ).exclude(pk=employee.pk).exists():
        return False
    before = {"user_id": None}
    employee.user = user
    employee.save(update_fields=["user", "updated_at"])
    DataAuditLog.objects.create(
        entity_type="Employee",
        entity_id=str(employee.pk),
        action=DataAuditLog.ACTION_UPDATE,
        organization=employee.organization,
        actor=actor,
        before=before,
        after={"user_id": user.pk},
        changed_fields=["user_id"],
    )
    return True


def map_employee_service2_user(employee, user, actor):
    if not OrganizationAccess.objects.filter(
        organization=employee.organization,
        user=user,
        user__is_active=True,
    ).exists():
        raise EmployeeIdentitySyncError(
            "Пользователь Service2 не относится к этой организации."
        )
    conflict = Employee.objects.filter(
        organization=employee.organization,
        user=user,
    ).exclude(pk=employee.pk).first()
    if conflict:
        raise EmployeeIdentitySyncError(
            f"Этот аккаунт Service2 уже связан с сотрудником «{conflict.display_name}»."
        )
    with transaction.atomic():
        locked = Employee.objects.select_for_update().get(pk=employee.pk)
        before = {"user_id": locked.user_id}
        locked.user = user
        locked.save(update_fields=["user", "updated_at"])
        DataAuditLog.objects.create(
            entity_type="Employee",
            entity_id=str(locked.pk),
            action=DataAuditLog.ACTION_UPDATE,
            organization=locked.organization,
            actor=actor,
            before=before,
            after={"user_id": user.pk},
            changed_fields=["user_id"] if before["user_id"] != user.pk else [],
        )
    employee.refresh_from_db()
    backfill_employee_calls(employee)
    return locked


def _build_onec_employee_url(config):
    config = validate_config(config)
    organizations = list(config.organization_guids)
    if ZERO_GUID not in organizations:
        organizations.append(ZERO_GUID)
    organization_filter = " or ".join(
        f"ГоловнаяОрганизация_Key eq guid'{guid}'"
        for guid in organizations
    )
    filters = f"DeletionMark eq false and ({organization_filter})"
    query = "&".join(
        (
            f"$select={quote(','.join(ONEC_EMPLOYEE_FIELDS))}",
            f"$filter={quote(filters)}",
        )
    )
    return config, (
        f"{config.base_url}{quote(ONEC_EMPLOYEE_ENTITY, safe='')}?{query}"
    )


def sync_onec_employee_identities(organization, actor=None):
    if not is_odata_target_organization(organization):
        raise EmployeeIdentitySyncError(
            "Синхронизация 1С не настроена для этой организации."
        )
    try:
        config, initial_url = _build_onec_employee_url(config_from_settings())
    except (ODataPreviewError, AttributeError, TypeError, ValueError) as exc:
        raise EmployeeIdentitySyncError(
            "Не удалось подготовить подключение к справочнику сотрудников 1С."
        ) from exc

    now = timezone.now()
    seen_ids = set()
    synced = 0
    active = 0
    inactive = 0
    auto_linked_users = 0

    try:
        pages = read_odata_pages(config, initial_url)
        for rows, _page_number in pages:
            for row in rows:
                if not isinstance(row, dict):
                    raise ODataPreviewError("Employee catalog row must be an object")
                if row.get("DeletionMark") is not False:
                    continue
                raw_name = row.get("Description")
                if not isinstance(raw_name, str) or not raw_name.strip() or len(raw_name) > 500:
                    raise ODataPreviewError("Employee catalog name is invalid")
                code = row.get("Code")
                if code is None:
                    code = ""
                if not isinstance(code, str) or len(code) > 120:
                    raise ODataPreviewError("Employee catalog code is invalid")
                archived = row.get("ВАрхиве")
                invalid = row.get("Недействителен")
                if type(archived) is not bool or type(invalid) is not bool:
                    raise ODataPreviewError("Employee catalog activity flag is invalid")
                onec_id = normalize_guid(
                    row.get("Ref_Key"),
                    field="Catalog_Сотрудники.Ref_Key",
                )
                source_active = not archived and not invalid
                identity = resolve_employee_identity(
                    organization,
                    raw_name.strip(),
                    onec_employee_id=onec_id,
                    personnel_number=code.strip() or None,
                )
                identity.raw_name = raw_name.strip()
                identity.normalized_name = normalize_onec_name(raw_name)
                identity.source_active = source_active
                identity.last_seen_at = now
                identity.save(
                    update_fields=[
                        "raw_name",
                        "normalized_name",
                        "source_active",
                        "last_seen_at",
                        "updated_at",
                    ]
                )
                seen_ids.add(identity.pk)
                synced += 1
                if source_active:
                    active += 1
                    if identity.employee_id and auto_link_service2_user(
                        identity.employee,
                        actor=actor,
                    ):
                        auto_linked_users += 1
                else:
                    inactive += 1
    except (ODataPreviewError, HTTPError, URLError) as exc:
        raise EmployeeIdentitySyncError(
            "Не удалось получить справочник сотрудников из 1С."
        ) from exc

    EmployeeOneCIdentity.objects.filter(
        organization=organization,
    ).exclude(pk__in=seen_ids).update(source_active=False)

    return {
        "synced": synced,
        "active": active,
        "inactive": inactive,
        "auto_linked_users": auto_linked_users,
        "synced_at": now,
    }


def _provider_connection(telephony):
    return (
        ChannelConnection.objects.filter(
            channel__organization=telephony.organization,
            channel__kind=CommunicationChannel.KIND_MEGAFON,
            external_id=telephony.external_id,
        )
        .select_related("channel")
        .first()
    )


def _megafon_api_credentials(telephony):
    provider = _provider_connection(telephony)
    if provider is None:
        raise EmployeeIdentitySyncError("Для линии не найдено подключение МегаФона.")
    values = dict(provider.settings or {})
    endpoint = str(values.get("megafon_api_base_url") or "").strip()
    encrypted_key = str(values.get("megafon_api_key_encrypted") or "").strip()
    if not endpoint or not encrypted_key:
        raise EmployeeIdentitySyncError(
            "Сначала сохраните адрес АТС и ключ авторизации МегаФона."
        )
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError as exc:
        raise EmployeeIdentitySyncError("Некорректный адрес АТС МегаФона.") from exc
    hostname = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not hostname
        or hostname != "megapbx.ru" and not hostname.endswith(".megapbx.ru")
        or port not in (None, 443)
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise EmployeeIdentitySyncError("Адрес АТС МегаФона не прошёл проверку.")
    try:
        token = decrypt_secret(encrypted_key)
    except CommunicationSecretError as exc:
        raise EmployeeIdentitySyncError(
            "Не удалось прочитать сохранённый ключ АТС."
        ) from exc
    return provider, endpoint, token


def _read_megafon_accounts(telephony):
    provider, endpoint, token = _megafon_api_credentials(telephony)
    payload = json.dumps(
        {"cmd": "accounts", "token": token},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Service2-MegaFon-Employee-Sync/1.0",
        },
        method="POST",
    )
    timeout = float(
        getattr(settings, "COMMUNICATION_MEGAFON_API_TIMEOUT_SECONDS", 10)
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_MEGAFON_RESPONSE_BYTES + 1)
            if response.status != 200:
                raise EmployeeIdentitySyncError(
                    f"МегаФон вернул HTTP {response.status}."
                )
    except HTTPError as exc:
        raise EmployeeIdentitySyncError(
            f"МегаФон вернул HTTP {exc.code} при синхронизации сотрудников."
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise EmployeeIdentitySyncError(
            "МегаФон не ответил на запрос списка сотрудников."
        ) from exc
    if len(raw) > MAX_MEGAFON_RESPONSE_BYTES:
        raise EmployeeIdentitySyncError("Список сотрудников МегаФона слишком большой.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EmployeeIdentitySyncError(
            "МегаФон вернул некорректный список сотрудников."
        ) from exc
    if not isinstance(data, list) or len(data) > MAX_MEGAFON_ACCOUNTS:
        raise EmployeeIdentitySyncError(
            "МегаФон вернул неожиданный формат списка сотрудников."
        )
    accounts = []
    seen_extensions = set()
    for item in data:
        if not isinstance(item, dict):
            raise EmployeeIdentitySyncError(
                "МегаФон вернул некорректную запись сотрудника."
            )
        raw_name = item.get("name")
        extension = item.get("ext")
        if not isinstance(raw_name, str) or len(raw_name) > 500:
            raise EmployeeIdentitySyncError(
                "МегаФон вернул некорректное имя сотрудника."
            )
        if isinstance(extension, int) and not isinstance(extension, bool):
            extension = str(extension)
        if not isinstance(extension, str):
            raise EmployeeIdentitySyncError(
                "МегаФон вернул некорректный внутренний номер."
            )
        extension = extension.strip()
        raw_name = raw_name.strip()
        if not extension or len(extension) > 64:
            raise EmployeeIdentitySyncError(
                "МегаФон вернул некорректный внутренний номер."
            )
        if extension in seen_extensions:
            raise EmployeeIdentitySyncError(
                "МегаФон вернул повторяющийся внутренний номер."
            )
        seen_extensions.add(extension)
        accounts.append({"name": raw_name, "ext": extension})
    return provider, accounts


def apply_telephony_identity_to_calls(identity):
    if not identity.employee_id or identity.requires_manual_confirmation:
        return 0
    employee = identity.employee
    matches = PhoneCall.objects.filter(
        organization=identity.organization,
        connection=identity.connection,
        employee_profile__isnull=True,
    )
    selector = Q(provider_extension=identity.extension)
    if identity.external_user:
        selector |= Q(provider_user=identity.external_user)
    matches = matches.filter(selector)
    return matches.update(
        employee_profile_id=employee.pk,
        employee_id=employee.user_id,
    )


def backfill_employee_calls(employee):
    total = 0
    for identity in employee.telephony_identities.filter(
        is_active=True,
        requires_manual_confirmation=False,
    ):
        total += apply_telephony_identity_to_calls(identity)
    return total


def _auto_match_telephony_identity(identity, actor=None):
    if identity.requires_manual_confirmation:
        return identity
    if identity.status == TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED:
        return identity
    candidates = _employee_candidates(identity.organization, identity.raw_name)
    if len(candidates) == 1:
        employee = candidates[0]
        identity.employee = employee
        identity.status = TelephonyEmployeeIdentity.STATUS_AUTO_MATCHED
        identity.match_method = TelephonyEmployeeIdentity.MATCH_EXACT_NAME
        if auto_link_service2_user(employee, actor=actor):
            employee.refresh_from_db()
    else:
        identity.employee = None
        identity.status = TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING
        identity.match_method = TelephonyEmployeeIdentity.MATCH_NONE
    identity.save(
        update_fields=[
            "employee",
            "status",
            "match_method",
            "updated_at",
        ]
    )
    if identity.employee_id:
        apply_telephony_identity_to_calls(identity)
    return identity


def sync_megafon_employee_identities(telephony, actor=None):
    provider, accounts = _read_megafon_accounts(telephony)
    now = timezone.now()
    seen = []
    auto_matched = 0
    needs_mapping = 0
    for account in accounts:
        normalized_name = normalize_onec_name(account["name"])
        identity, created = TelephonyEmployeeIdentity.objects.get_or_create(
            connection=telephony,
            extension=account["ext"],
            defaults={
                "organization": telephony.organization,
                "raw_name": account["name"],
                "normalized_name": normalized_name,
                "last_seen_at": now,
            },
        )
        previous_normalized_name = identity.normalized_name
        was_inactive = not identity.is_active
        was_pending_revalidation = identity.requires_manual_confirmation
        source_name_changed = (
            not created
            and bool(previous_normalized_name)
            and previous_normalized_name != normalized_name
        )
        reassigned_extension = source_name_changed and (
            was_inactive or was_pending_revalidation
        )

        identity.raw_name = account["name"]
        identity.normalized_name = normalized_name
        identity.is_active = True
        identity.last_seen_at = now
        update_fields = [
            "raw_name",
            "normalized_name",
            "is_active",
            "last_seen_at",
            "updated_at",
        ]

        if reassigned_extension:
            identity.employee = None
            identity.external_user = ""
            identity.status = TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING
            identity.match_method = TelephonyEmployeeIdentity.MATCH_NONE
            identity.confirmed_by = None
            identity.confirmed_at = None
            identity.requires_manual_confirmation = True
            update_fields.extend(
                [
                    "employee",
                    "external_user",
                    "status",
                    "match_method",
                    "confirmed_by",
                    "confirmed_at",
                    "requires_manual_confirmation",
                ]
            )
        elif was_pending_revalidation and not source_name_changed:
            # The accounts API confirmed the same holder name that existed
            # before the extension disappeared, so the prior mapping is safe.
            identity.requires_manual_confirmation = False
            update_fields.append("requires_manual_confirmation")

        identity.save(update_fields=update_fields)
        identity = _auto_match_telephony_identity(identity, actor=actor)
        if identity.employee_id and not identity.requires_manual_confirmation:
            apply_telephony_identity_to_calls(identity)

        seen.append(identity.pk)
        if identity.employee_id and not identity.requires_manual_confirmation:
            auto_matched += 1
        else:
            needs_mapping += 1

    TelephonyEmployeeIdentity.objects.filter(
        organization=telephony.organization,
        connection=telephony,
    ).exclude(pk__in=seen).update(is_active=False)

    settings_data = dict(provider.settings or {})
    settings_data["megafon_accounts_synced_at"] = now.isoformat()
    settings_data["megafon_accounts_count"] = len(accounts)
    settings_data["megafon_accounts_unmapped"] = needs_mapping
    provider.settings = settings_data
    provider.save(update_fields=["settings"])

    return {
        "synced": len(accounts),
        "auto_matched": auto_matched,
        "needs_mapping": needs_mapping,
        "synced_at": now,
    }


def map_telephony_identity(identity, employee, actor):
    if identity.organization_id != employee.organization_id:
        raise EmployeeIdentitySyncError(
            "Сотрудник и телефония относятся к разным организациям."
        )
    with transaction.atomic():
        locked = TelephonyEmployeeIdentity.objects.select_for_update().get(
            pk=identity.pk
        )
        before = {
            "employee_id": locked.employee_id,
            "requires_manual_confirmation": locked.requires_manual_confirmation,
            "status": locked.status,
            "match_method": locked.match_method,
        }
        locked.employee = employee
        locked.requires_manual_confirmation = False
        locked.status = TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED
        locked.match_method = TelephonyEmployeeIdentity.MATCH_MANUAL
        locked.confirmed_by = actor
        locked.confirmed_at = timezone.now()
        locked.full_clean()
        locked.save()
        DataAuditLog.objects.create(
            entity_type="TelephonyEmployeeIdentity",
            entity_id=str(locked.pk),
            action=DataAuditLog.ACTION_UPDATE,
            organization=locked.organization,
            actor=actor,
            before=before,
            after={
                "employee_id": locked.employee_id,
                "requires_manual_confirmation": locked.requires_manual_confirmation,
                "status": locked.status,
                "match_method": locked.match_method,
            },
            changed_fields=[
                key
                for key in before
                if before[key]
                != {
                    "employee_id": locked.employee_id,
                    "requires_manual_confirmation": locked.requires_manual_confirmation,
                    "status": locked.status,
                    "match_method": locked.match_method,
                }[key]
            ],
        )
    auto_link_service2_user(employee, actor=actor)
    employee.refresh_from_db()
    apply_telephony_identity_to_calls(locked)
    return locked


def resolve_call_employee(organization, telephony, extension="", external_user=""):
    extension = (extension or "").strip()
    external_user = (external_user or "").strip()
    identity = None

    if extension:
        raw_name = external_user or extension
        identity, created = TelephonyEmployeeIdentity.objects.get_or_create(
            connection=telephony,
            extension=extension,
            defaults={
                "organization": organization,
                "raw_name": raw_name,
                "normalized_name": normalize_onec_name(raw_name),
                "external_user": external_user,
                "is_active": True,
                "status": TelephonyEmployeeIdentity.STATUS_NEEDS_MAPPING,
                "match_method": TelephonyEmployeeIdentity.MATCH_NONE,
                "last_seen_at": timezone.now(),
            },
        )
        if created:
            identity = _auto_match_telephony_identity(identity)
        elif not identity.is_active:
            # A webhook proves the extension exists again, but it does not prove
            # the previous holder still owns it. Preserve the old historical
            # mapping, but do not use it for new calls until accounts confirms
            # the same holder or an operator maps the extension manually.
            identity.is_active = True
            identity.requires_manual_confirmation = True
            identity.external_user = external_user
            identity.last_seen_at = timezone.now()
            identity.save(
                update_fields=[
                    "is_active",
                    "requires_manual_confirmation",
                    "external_user",
                    "last_seen_at",
                    "updated_at",
                ]
            )
        else:
            updates = []
            if external_user and external_user != identity.external_user:
                identity.external_user = external_user
                updates.append("external_user")
            identity.last_seen_at = timezone.now()
            updates.append("last_seen_at")
            identity.save(update_fields=[*updates, "updated_at"])
    elif external_user:
        identity = TelephonyEmployeeIdentity.objects.filter(
            organization=organization,
            connection=telephony,
            external_user=external_user,
            is_active=True,
        ).select_related("employee__user").first()

    if (
        identity
        and identity.employee_id
        and not identity.requires_manual_confirmation
    ):
        employee = identity.employee
        return employee, employee.user
    return None, None


def sync_all_employee_identities(organization, actor=None):
    onec = sync_onec_employee_identities(organization, actor=actor)
    telephony_results = []
    for telephony in organization.telephony_connections.filter(is_active=True):
        base_result = {
            "connection_id": telephony.pk,
            "name": telephony.name,
            "synced": 0,
            "auto_matched": 0,
            "needs_mapping": 0,
            "error": "",
        }
        try:
            result = sync_megafon_employee_identities(telephony, actor=actor)
        except EmployeeIdentitySyncError as exc:
            base_result["error"] = "; ".join(exc.messages)
        else:
            base_result.update(result)
        telephony_results.append(base_result)
    for employee in Employee.objects.filter(organization=organization, is_active=True):
        auto_link_service2_user(employee, actor=actor)
    return {"onec": onec, "telephony": telephony_results}
