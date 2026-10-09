import os
import uuid

from django.contrib.auth.models import User
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.contrib.auth.hashers import check_password, make_password

from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.storage import private_media_storage


class CommunicationChannel(models.Model):
    KIND_AVITO = "avito"
    KIND_WEBSITE = "website"
    KIND_MEGAFON = "megafon"
    KIND_CHOICES = [(KIND_AVITO, "Авито"), (KIND_WEBSITE, "Сайт"), (KIND_MEGAFON, "Мегафон")]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="communication_channels")
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("organization", "kind", "name")
        permissions = [
            ("can_view_conversations", "Can view conversations"),
            ("can_reply_conversations", "Can reply to conversations"),
            ("can_take_conversation", "Can take a conversation"),
            ("can_assign_conversation", "Can assign a conversation"),
            ("can_view_all_calls", "Can view all calls"),
            ("can_view_own_calls", "Can view own calls"),
            ("can_listen_calls", "Can listen to call recordings"),
            ("can_manage_channels", "Can manage communication channels"),
        ]

    def __str__(self):
        return self.name


class CommunicationAccess(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="communication_accesses")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="communication_accesses")
    can_view_conversations = models.BooleanField(default=False)
    can_reply_conversations = models.BooleanField(default=False)
    can_take_conversation = models.BooleanField(default=False)
    can_assign_conversation = models.BooleanField(default=False)
    can_view_all_calls = models.BooleanField(default=False)
    can_view_own_calls = models.BooleanField(default=False)
    can_listen_calls = models.BooleanField(default=False)
    can_manage_channels = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="comm_access_org_user_uniq")]


def _communication_access_defaults(role):
    fields = {
        "can_view_conversations": False,
        "can_reply_conversations": False,
        "can_take_conversation": False,
        "can_assign_conversation": False,
        "can_view_all_calls": False,
        "can_view_own_calls": False,
        "can_listen_calls": False,
        "can_manage_channels": False,
    }
    if role in {"owner", "admin"}:
        return {name: True for name in fields}
    if role == "manager":
        fields.update(
            can_view_conversations=True,
            can_reply_conversations=True,
            can_take_conversation=True,
            can_view_own_calls=True,
            can_listen_calls=True,
        )
    return fields


def _effective_communication_role(organization, user):
    roles = set(OrganizationAccess.objects.filter(
        organization=organization,
        user=user,
    ).values_list("role", flat=True))
    if roles & {"owner", "admin"}:
        return "owner"
    if "manager" in roles:
        return "manager"
    return next(iter(roles), None)


@receiver(pre_save, sender=OrganizationAccess)
def remember_previous_communication_role(sender, instance, **_kwargs):
    if not instance.pk:
        instance._communication_previous_role = None
        return
    instance._communication_previous_role = OrganizationAccess.objects.filter(
        pk=instance.pk
    ).values_list("role", flat=True).first()


@receiver(post_save, sender=OrganizationAccess)
def create_communication_access(sender, instance, created, **_kwargs):
    effective_role = _effective_communication_role(instance.organization, instance.user)
    if effective_role is None:
        return
    defaults = _communication_access_defaults(effective_role)
    access, access_created = CommunicationAccess.objects.get_or_create(
        organization=instance.organization,
        user=instance.user,
        defaults=defaults,
    )
    role_changed = (
        created
        or getattr(instance, "_communication_previous_role", None) != instance.role
    )
    if not access_created and role_changed:
        # Role transitions recompute inherited capabilities so an owner/admin
        # downgrade cannot retain elevated channel/call/assignment rights.
        CommunicationAccess.objects.filter(pk=access.pk).update(**defaults)


@receiver(post_delete, sender=OrganizationAccess)
def recompute_communication_access_after_role_delete(sender, instance, **_kwargs):
    effective_role = _effective_communication_role(instance.organization, instance.user)
    access = CommunicationAccess.objects.filter(
        organization=instance.organization,
        user=instance.user,
    )
    if effective_role is None:
        access.delete()
        return
    access.update(**_communication_access_defaults(effective_role))


class ChannelConnection(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    channel = models.ForeignKey(CommunicationChannel, on_delete=models.CASCADE, related_name="connections")
    name = models.CharField(max_length=120)
    external_id = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    settings = models.JSONField(default=dict, blank=True, help_text="Non-secret connection metadata only")
    api_token_hash = models.CharField(max_length=128, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("channel", "external_id")

    def set_api_token(self, token):
        self.api_token_hash = make_password(token)

    def check_api_token(self, token):
        return bool(self.api_token_hash and token and check_password(token, self.api_token_hash))


class AvitoCredential(models.Model):
    """Encrypted credentials and short-lived access token for one Avito account."""

    connection = models.OneToOneField(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="avito_credential",
    )
    client_id_encrypted = models.TextField(editable=False)
    client_secret_encrypted = models.TextField(editable=False)
    access_token_encrypted = models.TextField(blank=True, editable=False)
    access_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.connection_id and self.connection.channel.kind != CommunicationChannel.KIND_AVITO:
            raise ValidationError("Avito credentials require an Avito connection.")


class AvitoApiThrottle(models.Model):
    """Shared per-account/method provider budget, independent of org snapshots."""

    key = models.CharField(max_length=64, unique=True)
    next_allowed_at = models.DateTimeField(default=timezone.now)


class AvitoSchedulerHeartbeat(models.Model):
    """Global supervisor evidence shared across isolated hosting identities."""

    key = models.CharField(max_length=32, primary_key=True)
    run_id = models.UUIDField()
    state = models.CharField(max_length=16)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    exit_code = models.IntegerField(null=True)


class AvitoStatusMonitor(models.Model):
    """Explicit owner subscription and durable worker lease, disabled by default."""

    connection = models.OneToOneField(ChannelConnection, on_delete=models.CASCADE, related_name="status_monitor")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    recipient = models.ForeignKey(User, on_delete=models.CASCADE)
    account_id = models.CharField(max_length=255)
    enabled = models.BooleanField(default=False)
    generation = models.UUIDField(default=uuid.uuid4)
    baseline_at = models.DateTimeField(null=True, blank=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    failure_count = models.PositiveIntegerField(default=0)
    last_item_count = models.PositiveIntegerField(default=0)
    last_page_count = models.PositiveIntegerField(default=0)
    next_due_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["enabled", "next_due_at"], name="avito_monitor_due_idx")]


class AvitoListingStatus(models.Model):
    monitor = models.ForeignKey(AvitoStatusMonitor, on_delete=models.CASCADE, related_name="listings")
    item_id = models.CharField(max_length=32)
    status = models.CharField(max_length=16)
    sequence = models.PositiveIntegerField(default=0)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["monitor", "item_id"], name="avito_monitor_item_uniq")]


class Conversation(models.Model):
    STATUS_NEW = "new"
    STATUS_ACTIVE = "active"
    STATUS_WAITING = "waiting"
    STATUS_DONE = "done"
    STATUS_SPAM = "spam"
    STATUS_CHOICES = [(STATUS_NEW, "Новое"), (STATUS_ACTIVE, "В работе"), (STATUS_WAITING, "Ожидаем клиента"), (STATUS_DONE, "Завершено"), (STATUS_SPAM, "Спам / Нецелевое")]
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="conversations")
    connection = models.ForeignKey(ChannelConnection, on_delete=models.PROTECT, related_name="conversations")
    external_id = models.CharField(max_length=255)
    participant_name = models.CharField(max_length=255)
    participant_phone = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_NEW)
    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_conversations")
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("connection", "external_id")
        ordering = ["-last_message_at", "-created_at"]

    def clean(self):
        if self.connection_id and self.organization_id and self.connection.channel.organization_id != self.organization_id:
            raise ValidationError("Conversation connection belongs to another organization.")
        if self.assignee_id and not self.assignee.organizationaccess_set.filter(organization_id=self.organization_id).exists():
            raise ValidationError("Conversation assignee must belong to the organization.")


class ConversationMessage(models.Model):
    DIRECTION_IN = "in"
    DIRECTION_OUT = "out"
    DIRECTION_INTERNAL = "internal"
    DELIVERY_RECEIVED = "received"
    DELIVERY_PENDING = "pending"
    DELIVERY_SENDING = "sending"
    DELIVERY_DELIVERED = "delivered"
    DELIVERY_FAILED = "failed"
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    external_id = models.CharField(max_length=255, blank=True, null=True)
    direction = models.CharField(max_length=3, choices=[(DIRECTION_IN, "Входящее"), (DIRECTION_OUT, "Исходящее")])
    body = models.TextField(blank=True)
    sender_name = models.CharField(max_length=255, blank=True)
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversation_messages")
    delivery_status = models.CharField(max_length=16, choices=[(DELIVERY_RECEIVED, "Получено"), (DELIVERY_PENDING, "Ожидает отправки"), (DELIVERY_SENDING, "Отправляется"), (DELIVERY_DELIVERED, "Доставлено"), (DELIVERY_FAILED, "Ошибка")], default=DELIVERY_RECEIVED)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_error = models.CharField(max_length=500, blank=True)
    delivery_attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "external_id"],
                name="comm_message_external_uniq",
            )
        ]


class MessageAttachment(models.Model):
    message = models.ForeignKey(ConversationMessage, on_delete=models.CASCADE, related_name="attachments")
    original = models.FileField(upload_to="communications/original/%Y/%m/", storage=private_media_storage)
    optimized = models.FileField(upload_to="communications/optimized/%Y/%m/", storage=private_media_storage, blank=True)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    original_size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ConversationAssignment(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="assignment_history")
    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversation_assignments")
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="conversation_assignment_changes")
    created_at = models.DateTimeField(auto_now_add=True)


class ConversationReadState(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="read_states")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="conversation_read_states")
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("conversation", "user")


class WebsiteRequest(models.Model):
    conversation = models.OneToOneField(Conversation, on_delete=models.CASCADE, related_name="website_request")
    service = models.CharField(max_length=255, blank=True)
    delivery = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=500, blank=True)
    comment = models.TextField(blank=True)
    submitted_at = models.DateTimeField()


class TelephonyConnection(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="telephony_connections")
    name = models.CharField(max_length=120, default="Мегафон")
    external_id = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    recording_allowed_hosts = models.JSONField(default=list, blank=True, help_text="HTTPS hostnames allowed for recording redirects")

    class Meta:
        unique_together = ("organization", "external_id")

    def clean(self):
        if not isinstance(self.recording_allowed_hosts, list) or any(
            not isinstance(item, str)
            or not item.strip()
            or ":" in item
            or "/" in item
            or "@" in item
            for item in self.recording_allowed_hosts
        ):
            raise ValidationError("Recording allowed hosts must be a list of hostnames without scheme or port.")


class TelephonyEmployeeIdentity(models.Model):
    STATUS_AUTO_MATCHED = "auto_matched"
    STATUS_MANUALLY_MATCHED = "manually_matched"
    STATUS_NEEDS_MAPPING = "needs_mapping"
    STATUS_EXCLUDED = "excluded"
    STATUS_CHOICES = [
        (STATUS_AUTO_MATCHED, "Сопоставлен автоматически"),
        (STATUS_MANUALLY_MATCHED, "Сопоставлен вручную"),
        (STATUS_NEEDS_MAPPING, "Требует сопоставления"),
        (STATUS_EXCLUDED, "Исключён"),
    ]
    MATCH_EXACT_NAME = "exact_name"
    MATCH_EXTENSION = "extension"
    MATCH_MANUAL = "manual"
    MATCH_NONE = "none"
    MATCH_CHOICES = [
        (MATCH_EXACT_NAME, "Точное ФИО"),
        (MATCH_EXTENSION, "Внутренний номер"),
        (MATCH_MANUAL, "Вручную"),
        (MATCH_NONE, "Нет сопоставления"),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="telephony_employee_identities",
    )
    connection = models.ForeignKey(
        TelephonyConnection,
        on_delete=models.CASCADE,
        related_name="employee_identities",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="telephony_identities",
    )
    raw_name = models.CharField(max_length=500, blank=True)
    normalized_name = models.CharField(max_length=500, blank=True, db_index=True)
    extension = models.CharField(max_length=64)
    external_user = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    requires_manual_confirmation = models.BooleanField(default=False)
    reassignment_detected_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_NEEDS_MAPPING,
    )
    match_method = models.CharField(
        max_length=24,
        choices=MATCH_CHOICES,
        default=MATCH_NONE,
    )
    confirmed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_telephony_employee_identities",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["raw_name", "extension", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "extension"],
                name="telephony_employee_connection_ext_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status"],
                name="tel_emp_org_status_idx",
            ),
            models.Index(
                fields=["connection", "external_user"],
                name="tel_emp_conn_user_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if self.connection_id and self.connection.organization_id != self.organization_id:
            raise ValidationError("Telephony identity belongs to another organization.")
        if self.employee_id and self.employee.organization_id != self.organization_id:
            raise ValidationError("Employee belongs to another organization.")


class PhoneCall(models.Model):
    SOURCE_TELEPHONY = "telephony"
    SOURCE_UPLOADED = "uploaded"
    SOURCE_CHOICES = [
        (SOURCE_TELEPHONY, "Телефония"),
        (SOURCE_UPLOADED, "Загруженный аудиофайл"),
    ]

    DIRECTION_IN = "in"
    DIRECTION_OUT = "out"
    DIRECTION_INTERNAL = "internal"
    RESULT_ANSWERED = "answered"
    RESULT_MISSED = "missed"
    RECORDING_NONE = "none"
    RECORDING_PENDING = "pending"
    RECORDING_DOWNLOADING = "downloading"
    RECORDING_STORED = "stored"
    RECORDING_FAILED = "failed"
    RECORDING_STATUS_CHOICES = [
        (RECORDING_NONE, "Нет записи"),
        (RECORDING_PENDING, "Ожидает сохранения"),
        (RECORDING_DOWNLOADING, "Сохраняется"),
        (RECORDING_STORED, "Сохранена"),
        (RECORDING_FAILED, "Ошибка сохранения"),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="phone_calls")
    source_kind = models.CharField(
        max_length=16,
        choices=SOURCE_CHOICES,
        default=SOURCE_TELEPHONY,
        db_index=True,
    )
    connection = models.ForeignKey(
        TelephonyConnection,
        on_delete=models.PROTECT,
        related_name="calls",
        null=True,
        blank=True,
    )
    external_id = models.CharField(max_length=255)
    employee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="phone_calls")
    employee_profile = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="phone_calls",
    )
    peer_employee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="peer_phone_calls",
    )
    peer_employee_profile = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="peer_phone_calls",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="phone_calls",
    )
    provider_user = models.CharField(max_length=255, blank=True)
    provider_extension = models.CharField(max_length=64, blank=True)
    peer_provider_user = models.CharField(max_length=255, blank=True)
    peer_provider_extension = models.CharField(max_length=64, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    phone_number = models.CharField(max_length=40)
    direction = models.CharField(
        max_length=8,
        choices=[
            (DIRECTION_IN, "Входящий"),
            (DIRECTION_OUT, "Исходящий"),
            (DIRECTION_INTERNAL, "Внутренний"),
        ],
    )
    started_at = models.DateTimeField(db_index=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=20, choices=[(RESULT_ANSWERED, "Отвечен"), (RESULT_MISSED, "Пропущен")])
    recording_ref = models.CharField(max_length=500, blank=True)
    recording_file = models.FileField(
        upload_to="communications/call_recordings/%Y/%m/%d/",
        storage=private_media_storage,
        blank=True,
    )
    recording_status = models.CharField(
        max_length=16,
        choices=RECORDING_STATUS_CHOICES,
        default=RECORDING_NONE,
    )
    recording_error = models.CharField(max_length=500, blank=True)
    recording_attempts = models.PositiveSmallIntegerField(default=0)
    recording_last_attempt_at = models.DateTimeField(null=True, blank=True)
    recording_downloaded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("connection", "external_id")
        ordering = ["-started_at"]

    @property
    def recording_filename(self):
        return os.path.basename(self.recording_file.name or "")

    def clean(self):
        if self.source_kind == self.SOURCE_TELEPHONY and not self.connection_id:
            raise ValidationError("Telephony call requires a telephony connection.")
        if self.source_kind == self.SOURCE_UPLOADED and self.connection_id:
            raise ValidationError("Uploaded audio must not use a telephony connection.")
        if self.connection_id and self.organization_id and self.connection.organization_id != self.organization_id:
            raise ValidationError("Telephony connection belongs to another organization.")
        if self.employee_id and not self.employee.organizationaccess_set.filter(organization_id=self.organization_id).exists():
            raise ValidationError("Call employee must belong to the organization.")
        if (
            self.employee_profile_id
            and self.employee_profile.organization_id != self.organization_id
        ):
            raise ValidationError("Call employee profile must belong to the organization.")
        if (
            self.peer_employee_id
            and not self.peer_employee.organizationaccess_set.filter(
                organization_id=self.organization_id
            ).exists()
        ):
            raise ValidationError("Peer call employee must belong to the organization.")
        if (
            self.peer_employee_profile_id
            and self.peer_employee_profile.organization_id != self.organization_id
        ):
            raise ValidationError("Peer call employee profile must belong to the organization.")
        if (
            self.client_id
            and self.client.organization_id != self.organization_id
        ):
            raise ValidationError("Call client must belong to the organization.")


class CallAnalysis(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает расшифровки"),
        (STATUS_PROCESSING, "Обрабатывается"),
        (STATUS_READY, "Готово"),
        (STATUS_FAILED, "Ошибка"),
    ]

    call = models.OneToOneField(PhoneCall, on_delete=models.CASCADE, related_name="analysis")
    transcript = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    facts = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error = models.CharField(max_length=500, blank=True)
    transcription_model = models.CharField(max_length=80, blank=True)
    analysis_model = models.CharField(max_length=80, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_token = models.CharField(max_length=36, blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    requested_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
