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

    def __str__(self):
        return self.name


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
        ("staff", "Сотрудник клиента"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="staff_accesses")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="staff")
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
    role = models.CharField(max_length=20, choices=ClientAccess.ROLE_CHOICES, default="staff")
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
