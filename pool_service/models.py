import uuid

from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django_ckeditor_5.fields import CKEditor5Field
from django.utils import timezone


class Organization(models.Model):
    PLAN_PERSONAL_FREE = "PERSONAL_FREE"
    PLAN_COMPANY_TRIAL = "COMPANY_TRIAL"
    PLAN_COMPANY_PAID = "COMPANY_PAID"
    PLAN_CHOICES = [
        (PLAN_PERSONAL_FREE, "PERSONAL_FREE"),
        (PLAN_COMPANY_TRIAL, "COMPANY_TRIAL"),
        (PLAN_COMPANY_PAID, "COMPANY_PAID"),
    ]

    name = models.CharField(max_length=255, unique=True)
    inn = models.CharField(max_length=20, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_CHOICES, default=PLAN_COMPANY_TRIAL)
    trial_started_at = models.DateTimeField(blank=True, null=True)
    paid_until = models.DateTimeField(blank=True, null=True)
    notify_limits = models.BooleanField(default=True)
    notify_missed_visits = models.BooleanField(default=True)
    notify_pool_staff_daily = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class OrganizationWaterNorms(models.Model):
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="water_norms",
    )
    ph_min = models.FloatField(null=True, blank=True)
    ph_max = models.FloatField(null=True, blank=True)
    cl_free_min = models.FloatField(null=True, blank=True)
    cl_free_max = models.FloatField(null=True, blank=True)
    cl_total_min = models.FloatField(null=True, blank=True)
    cl_total_max = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"Norms for {self.organization.name}"


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    timezone = models.CharField(
        max_length=50,
        default="Europe/Moscow",
        help_text="Часовой пояс пользователя, например, Europe/Moscow",
    )
    email_confirmed_at = models.DateTimeField(null=True, blank=True)
    phone_confirmed_at = models.DateTimeField(null=True, blank=True)
    phone_verification_required = models.BooleanField(default=False)
    phone_verification_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    phone_verification_attempts = models.PositiveSmallIntegerField(default=0)
    phone_verification_started_at = models.DateTimeField(null=True, blank=True)
    phone_verification_expires_at = models.DateTimeField(null=True, blank=True)
    phone_verification_check_id = models.CharField(max_length=50, blank=True)
    phone_verification_call_phone = models.CharField(max_length=20, blank=True)
    phone_sms_code_hash = models.CharField(max_length=128, blank=True)
    phone_sms_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.username}'s profile"


class Client(models.Model):
    CLIENT_TYPE_CHOICES = [
        ("private", "Частный клиент"),
        ("legal", "Юридическое лицо"),
    ]

    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="client_profile")
    client_type = models.CharField(max_length=20, choices=CLIENT_TYPE_CHOICES, default="private")
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    name = models.CharField(max_length=255, verbose_name="Имя/название")
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    inn = models.CharField(max_length=20, blank=True, null=True)
    contact_position = models.CharField(max_length=120, blank=True, null=True)
    organization = models.ForeignKey(Organization, on_delete=models.SET_NULL, null=True, blank=True, related_name="clients")

    def __str__(self):
        return self.name


class Pool(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    SHAPE_CHOICES = [
        ("rect", "Прямоугольный"),
        ("round", "Круглый"),
        ("oval", "Овальный"),
        ("free", "Произвольная форма"),
    ]
    TYPE_CHOICES = [
        ("overflow", "Переливной"),
        ("skimmer", "Скиммерный"),
    ]
    SERVICE_FREQ_WEEKLY = "weekly"
    SERVICE_FREQ_TWICE_MONTHLY = "twice_monthly"
    SERVICE_FREQ_MONTHLY = "monthly"
    SERVICE_FREQ_BIMONTHLY = "bimonthly"
    SERVICE_FREQ_QUARTERLY = "quarterly"
    SERVICE_FREQ_TWICE_YEARLY = "twice_yearly"
    SERVICE_FREQ_YEARLY = "yearly"
    SERVICE_FREQUENCY_CHOICES = [
        (SERVICE_FREQ_WEEKLY, "\u0420\u0430\u0437 \u0432 \u043d\u0435\u0434\u0435\u043b\u044e"),
        (SERVICE_FREQ_TWICE_MONTHLY, "\u0414\u0432\u0430 \u0440\u0430\u0437\u0430 \u0432 \u043c\u0435\u0441\u044f\u0446"),
        (SERVICE_FREQ_MONTHLY, "\u0420\u0430\u0437 \u0432 \u043c\u0435\u0441\u044f\u0446"),
        (SERVICE_FREQ_BIMONTHLY, "\u0420\u0430\u0437 \u0432 2 \u043c\u0435\u0441\u044f\u0446\u0430"),
        (SERVICE_FREQ_QUARTERLY, "\u0420\u0430\u0437 \u0432 \u043a\u0432\u0430\u0440\u0442\u0430\u043b"),
        (SERVICE_FREQ_TWICE_YEARLY, "\u0414\u0432\u0430 \u0440\u0430\u0437\u0430 \u0432 \u0433\u043e\u0434"),
        (SERVICE_FREQ_YEARLY, "\u0420\u0430\u0437 \u0432 \u0433\u043e\u0434"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    address = models.CharField(max_length=255)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="pools",
        null=True,
        blank=True,
    )
    description = CKEditor5Field(blank=True, null=True, verbose_name="Описание бассейна")
    shape = models.CharField(max_length=20, choices=SHAPE_CHOICES, default="rect")
    pool_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="skimmer")
    length = models.FloatField(null=True, blank=True)
    width = models.FloatField(null=True, blank=True)
    diameter = models.FloatField(null=True, blank=True)
    variable_depth = models.BooleanField(default=False)
    depth = models.FloatField(null=True, blank=True)
    depth_min = models.FloatField(null=True, blank=True)
    depth_max = models.FloatField(null=True, blank=True)
    overflow_volume = models.FloatField(null=True, blank=True)
    surface_area = models.FloatField(null=True, blank=True)
    volume = models.FloatField(null=True, blank=True)
    dosing_station = models.BooleanField(default=False)
    service_frequency = models.CharField(max_length=20, choices=SERVICE_FREQUENCY_CHOICES, null=True, blank=True)
    service_interval_days = models.PositiveSmallIntegerField(null=True, blank=True)
    service_suspended = models.BooleanField(default=False)
    daily_readings_required = models.BooleanField(default=False)

    def __str__(self):
        org_name = self.organization.name if self.organization else "без организации"
        return f"Бассейн: {self.address} ({org_name})"


class PoolAccess(models.Model):
    ROLE_CHOICES = [
        ("viewer", "Просмотр"),
        ("editor", "Редактор"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="accesses")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.pool.address} ({self.role})"


class OrganizationAccess(models.Model):
    ROLE_CHOICES = [
        ("owner", "Владелец"),
        ("manager", "Менеджер"),
        ("service", "Сервис"),
        ("admin", "Администратор"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="Пользователь")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="accesses", verbose_name="Организация")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name="Роль")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(role="owner"),
                name="unique_owner_per_org",
            )
        ]

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.organization.name} ({self.role})"


class ClientAccess(models.Model):
    ROLE_CHOICES = [
        ("viewer", "Просмотр"),
        ("editor", "Редактор"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="staff_accesses")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="editor")
    phone = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "client")

    def __str__(self):
        return f"{self.user.get_full_name()} - {self.client.name} ({self.role})"


class ClientInvite(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="invites")
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_client_invites",
    )
    accepted_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_client_invites",
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=20, choices=ClientAccess.ROLE_CHOICES, default="editor")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.email} ({self.client.name})"

    def is_expired(self, now=None):
        now = now or timezone.now()
        return self.expires_at and self.expires_at <= now


class OrganizationInvite(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="invites")
    invited_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_org_invites",
    )
    accepted_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_org_invites",
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=20, choices=OrganizationAccess.ROLE_CHOICES, default="service")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.email} ({self.organization.name})"

    def is_expired(self, now=None):
        now = now or timezone.now()
        return self.expires_at and self.expires_at <= now


class OrganizationPaymentRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "pending"),
        (STATUS_APPROVED, "approved"),
        (STATUS_REJECTED, "rejected"),
        (STATUS_CANCELED, "canceled"),
    ]
    PERIOD_CHOICES = [
        (1, "1"),
        (3, "3"),
        (6, "6"),
        (12, "12"),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="payment_requests",
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_requests",
    )
    months = models.PositiveSmallIntegerField(choices=PERIOD_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_requests_decided",
    )
    paid_until_before = models.DateTimeField(null=True, blank=True)
    paid_until_after = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    def __str__(self):
        return f"{self.organization.name} ({self.months}m, {self.status})"


class Notification(models.Model):
    LEVEL_CHOICES = [
        ("info", "info"),
        ("warning", "warning"),
        ("critical", "critical"),
    ]
    KIND_CHOICES = [
        ("limits", "limits"),
        ("missed_visit", "missed_visit"),
        ("daily_missing", "daily_missing"),
        ("new_company", "new_company"),
        ("new_personal", "new_personal"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, null=True, blank=True)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, null=True, blank=True)
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, null=True, blank=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    level = models.CharField(max_length=16, choices=LEVEL_CHOICES, default="info")
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    action_url = models.CharField(max_length=400, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    is_read = models.BooleanField(default=False)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "is_read"], name="notif_user_read_idx"),
            models.Index(fields=["user", "is_resolved"], name="notif_user_resolved_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="unique_notification_dedupe",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.user_id})"


class PushSubscription(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.CharField(max_length=512, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Push subscription {self.user_id}"


class WaterReading(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="waterreading")
    date = models.DateTimeField()
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    temperature = models.FloatField(null=True, blank=True)
    ph = models.FloatField(null=True, blank=True)
    cl_free = models.FloatField(null=True, blank=True)
    cl_total = models.FloatField(null=True, blank=True)
    ph_dosing_station = models.FloatField(null=True, blank=True)
    cl_free_dosing_station = models.FloatField(null=True, blank=True)
    cl_total_dosing_station = models.FloatField(null=True, blank=True)
    redox_dosing_station = models.FloatField(null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    required_materials = models.TextField(null=True, blank=True)
    performed_works = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.pool.address} - {self.date.strftime('%d.%m.%Y %H:%M')}"
