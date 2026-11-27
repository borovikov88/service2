"""Conversation metadata is a binding; only a signed-in browser confirms identity."""
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from pool_service.models import Organization
from pool_service.onec_draft_models import OneCChatIdentity
from pool_service.reward_models import OneCAuthorIdentity
from pool_service.operations_mcp_auth import ONEC_DRAFTS, revalidate_authenticated_request, is_enabled
from . import onec_draft_requests as drafts
from .onec_write_connection import WriteError, binding_key, guid, integration_config, reference_filter
from .onec_write_validation import _link_target

SALT = "onec-chat-confirm-v1"


def enabled():
    return is_enabled(ONEC_DRAFTS) and bool(getattr(settings, "ONEC_MCP_CHAT_IDENTITY_ENABLED", False))


def mapped_author(user_id, organization_id):
    rows = list(OneCAuthorIdentity.objects.filter(
        organization_id=organization_id, employee__organization_id=organization_id,
        employee__user_id=user_id, employee__is_active=True,
        status=OneCAuthorIdentity.STATUS_MAPPED, confirmed_at__isnull=False,
        confirmed_by__isnull=False,
    ).exclude(employee__employment_status="dismissed").select_related("employee")[:2])
    if len(rows) != 1:
        raise WriteError("Для сотрудника нужна одна подтверждённая привязка к автору 1С в Ровике.")
    return guid(rows[0].onec_user_id), rows[0].raw_name or rows[0].employee.display_name


def _lock_organization():
    if not Organization.objects.select_for_update().filter(pk=drafts.target_organization()).exists():
        raise drafts.DraftUnavailable


def _current(authenticated):
    if not enabled() or not authenticated:
        raise drafts.DraftUnavailable
    # One order across chats and grants: org -> OAuth -> staff -> chat/request.
    _lock_organization()
    try:
        return revalidate_authenticated_request(authenticated, ONEC_DRAFTS)
    except PermissionError:
        raise drafts.DraftUnavailable from None


def binding(authenticated, metadata):
    current = _current(authenticated)
    if not isinstance(metadata, dict):
        raise WriteError("ChatGPT не передал идентификатор беседы. Запись из этого клиента недоступна.")
    values = [metadata.get(k) for k in ("openai/session", "openai/subject")]
    if any(not isinstance(v, str) or not v or len(v) > 512 or v.strip() != v
           or any(ord(c) < 32 for c in v) for v in values):
        raise WriteError("ChatGPT не передал идентификатор беседы. Запись из этого клиента недоступна.")
    data = json.dumps([current.grant.pk, drafts.target_organization(), *values], separators=(",", ":"))
    return salted_hmac(SALT + "-binding", data, algorithm="sha256").hexdigest()


def confirmation_token(row):
    return signing.Signer(salt=SALT).sign(f"{row.pk}:{row.challenge_nonce}")


def from_token(token):
    try:
        pk, nonce = signing.Signer(salt=SALT).unsign(token).split(":")
        return OneCChatIdentity.objects.select_related("user", "grant__client__principal", "grant__authorized_by").get(
            pk=uuid.UUID(pk), challenge_nonce=uuid.UUID(nonce))
    except (signing.BadSignature, ValueError, OneCChatIdentity.DoesNotExist):
        raise drafts.DraftUnavailable from None


def _valid_grant(row):
    from pool_service.operations_mcp_auth import _grant_is_valid, resource_url
    # Use the same policy as bearer authentication, including current client/user scopes.
    return _grant_is_valid(row.grant, now=timezone.now(), client=row.grant.client,
                           resource=resource_url(ONEC_DRAFTS), policy=ONEC_DRAFTS)


@dataclass(frozen=True)
class ChatActor:
    authenticated: object
    identity_id: uuid.UUID
    binding_hash: str


@transaction.atomic
def validate(actor, request_row=None):
    if not isinstance(actor, ChatActor):
        raise drafts.DraftUnavailable
    current = _current(actor.authenticated)
    row = OneCChatIdentity.objects.select_related("user").filter(
        pk=actor.identity_id, grant_id=current.grant.pk, binding_hash=actor.binding_hash,
        organization_id=drafts.target_organization(), confirmed_at__isnull=False, revoked_at__isnull=True,
    ).first()
    if not row or not drafts.is_staff_member(row.user_id, row.organization_id):
        raise drafts.DraftUnavailable
    author, _ = mapped_author(row.user_id, row.organization_id)
    if str(row.author_ref) != author or row.connection_binding != binding_key(integration_config()):
        raise WriteError("Привязка автора или подключение 1С изменились. Нужен новый чат с подтверждением личности.")
    if request_row is not None and (request_row.chat_identity_id != row.pk or request_row.user_id != row.user_id
                                   or str(request_row.confirmed_author) != author):
        raise drafts.DraftUnavailable
    return row


@transaction.atomic
def begin(authenticated, metadata):
    key = binding(authenticated, metadata)
    current = _current(authenticated)
    now = timezone.now()
    # Serialize creation and bound pending challenges for this connection.
    type(current.grant).objects.select_for_update().get(pk=current.grant.pk)
    row = OneCChatIdentity.objects.select_for_update().filter(binding_hash=key).first()
    if row is None:
        if OneCChatIdentity.objects.filter(grant=current.grant, created_at__gt=now-timedelta(hours=1)).count() >= 30:
            raise WriteError("Слишком много новых чатов. Повторите позже.")
        row = OneCChatIdentity.objects.create(binding_hash=key, grant=current.grant,
            organization_id=drafts.target_organization(), challenge_expires_at=now+timedelta(minutes=10))
    if row.revoked_at:
        raise drafts.DraftUnavailable
    if row.confirmed_at:
        actor = ChatActor(authenticated, row.pk, key)
        validate(actor)
        return row, actor
    if row.challenge_expires_at <= now:
        row.challenge_nonce = uuid.uuid4()
        row.challenge_expires_at = now+timedelta(minutes=10)
        row.save(update_fields=["challenge_nonce", "challenge_expires_at"])
    return row, None


@transaction.atomic
def confirm(token, user):
    candidate = from_token(token)
    _lock_organization()
    # Match every new chat path, including grants owned by different employees.
    candidate.grant = type(candidate.grant).objects.select_for_update().select_related(
        "client__principal", "principal", "authorized_by").get(pk=candidate.grant_id)
    drafts._lock_staff(user.pk, candidate.organization_id)
    row = OneCChatIdentity.objects.select_for_update().get(pk=candidate.pk)
    if (not enabled() or row.revoked_at or row.challenge_nonce != candidate.challenge_nonce
            or row.organization_id != drafts.target_organization() or not _valid_grant(candidate)):
        raise drafts.DraftUnavailable
    if row.confirmed_at:
        if row.user_id != user.pk:
            raise drafts.DraftUnavailable
        return row
    if row.challenge_expires_at <= timezone.now():
        raise WriteError("Ссылка истекла. Вернитесь в чат и запросите подтверждение снова.")
    author, _ = mapped_author(user.pk, row.organization_id)
    row.user = user
    row.author_ref = author
    row.connection_binding = binding_key(integration_config())
    row.confirmed_at = timezone.now()
    row.save(update_fields=["user", "author_ref", "connection_binding", "confirmed_at"])
    return row


def verify_author(writer, entity, author):
    target = _link_target(writer, writer.entities[entity].entity_type, "Автор_Key")
    if target != "Catalog_Пользователи":
        raise WriteError("Связь поля «Автор» со справочником пользователей 1С не подтверждена.")
    schema = writer.schema(target, ["Description", "Недействителен", "Служебный"])
    if any(schema.field_types.get(k) != "Edm.Boolean" for k in ("Недействителен", "Служебный")):
        raise WriteError("Не удалось проверить состояние пользователя 1С.")
    remote = writer.active(target, ["Description", "Недействителен", "Служебный"], reference_filter("Ref_Key", author))
    if not remote or remote.get("Недействителен") is not False or remote.get("Служебный") is not False:
        raise WriteError("Автор 1С недоступен, недействителен или является служебной учётной записью.")
