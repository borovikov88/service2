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
    OBJECT_TYPE_POOL = "pool"
    OBJECT_TYPE_WATER = "water"
    OBJECT_TYPE_CHOICES = [
        (OBJECT_TYPE_POOL, "Бассейн"),
        (OBJECT_TYPE_WATER, "Водоподготовка"),
    ]
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
    WATER_SYSTEM_SOFTENING = "softening"
    WATER_SYSTEM_IRON_REMOVAL = "iron_removal"
    WATER_SYSTEM_REVERSE_OSMOSIS = "reverse_osmosis"
    WATER_SYSTEM_UV = "uv"
    WATER_SYSTEM_COMPLEX = "complex"
    WATER_SYSTEM_CHOICES = [
        (WATER_SYSTEM_SOFTENING, "Умягчение"),
        (WATER_SYSTEM_IRON_REMOVAL, "Обезжелезивание"),
        (WATER_SYSTEM_REVERSE_OSMOSIS, "Обратный осмос"),
        (WATER_SYSTEM_UV, "УФ обработка"),
        (WATER_SYSTEM_COMPLEX, "Комплексная"),
    ]
    WATER_SOURCE_WELL = "well"
    WATER_SOURCE_CITY = "city"
    WATER_SOURCE_TANK = "tank"
    WATER_SOURCE_CHOICES = [
        (WATER_SOURCE_WELL, "Скважина"),
        (WATER_SOURCE_CITY, "Центральная"),
        (WATER_SOURCE_TANK, "Резервуар"),
    ]
    WATER_CAPACITY_M3_HOUR = "m3_hour"
    WATER_CAPACITY_M3_DAY = "m3_day"
    WATER_CAPACITY_UNIT_CHOICES = [
        (WATER_CAPACITY_M3_HOUR, "м³/час"),
        (WATER_CAPACITY_M3_DAY, "м³/сутки"),
    ]
    WATER_OPERATION_CONTINUOUS = "continuous"
    WATER_OPERATION_SCHEDULED = "scheduled"
    WATER_OPERATION_MODE_CHOICES = [
        (WATER_OPERATION_CONTINUOUS, "Постоянно"),
        (WATER_OPERATION_SCHEDULED, "По расписанию"),
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
        (SERVICE_FREQ_TWICE_MONTHLY, "\u0420\u0430\u0437 \u0432 2 \u043d\u0435\u0434\u0435\u043b\u0438"),
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
    object_type = models.CharField(max_length=20, choices=OBJECT_TYPE_CHOICES, default=OBJECT_TYPE_POOL)
    description = CKEditor5Field(blank=True, null=True, verbose_name="Описание объекта")
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
    water_system_type = models.CharField(max_length=30, choices=WATER_SYSTEM_CHOICES, null=True, blank=True)
    water_source = models.CharField(max_length=30, choices=WATER_SOURCE_CHOICES, null=True, blank=True)
    water_capacity_value = models.FloatField(null=True, blank=True)
    water_capacity_unit = models.CharField(max_length=20, choices=WATER_CAPACITY_UNIT_CHOICES, null=True, blank=True)
    water_control_parameters = models.TextField(null=True, blank=True)
    water_equipment = models.TextField(null=True, blank=True)
    water_operation_mode = models.CharField(max_length=20, choices=WATER_OPERATION_MODE_CHOICES, null=True, blank=True)
    water_contact_name = models.CharField(max_length=120, null=True, blank=True)
    water_contact_phone = models.CharField(max_length=30, null=True, blank=True)
    water_access_notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        org_name = self.organization.name if self.organization else "без организации"
        label = self.get_object_type_display() if hasattr(self, "get_object_type_display") else "Объект"
        return f"{label}: {self.address} ({org_name})"


class ServiceVisitPlan(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="visit_plans")
    week_start = models.DateField()
    planned_date = models.DateField()
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_visit_plans",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("pool", "week_start")

    def __str__(self):
        return f"{self.pool} {self.week_start}"


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
    roles = models.JSONField(default=list, blank=True)
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


class CrmItem(models.Model):
    DIRECTION_SERVICE = "service"
    DIRECTION_PROJECT = "project"
    DIRECTION_SALES = "sales"
    DIRECTION_TENDER = "tender"
    DIRECTION_CHOICES = [
        (DIRECTION_SERVICE, "service"),
        (DIRECTION_PROJECT, "project"),
        (DIRECTION_SALES, "sales"),
        (DIRECTION_TENDER, "tender"),
    ]

    STAGE_SERVICE_NEW = "srv_new"
    STAGE_SERVICE_IN_PROGRESS = "srv_in_progress"
    STAGE_SERVICE_DONE = "srv_done"
    STAGE_SERVICE_BLOCKED = "srv_blocked"
    STAGE_PROJECT_IDEA = "prj_idea"
    STAGE_PROJECT_DESIGN = "prj_design"
    STAGE_PROJECT_BUILD = "prj_build"
    STAGE_PROJECT_COMMISSION = "prj_commission"
    STAGE_PROJECT_DONE = "prj_done"
    STAGE_PROJECT_HOLD = "prj_hold"
    STAGE_SALES_LEAD = "sale_lead"
    STAGE_SALES_QUALIFY = "sale_qualify"
    STAGE_SALES_OFFER = "sale_offer"
    STAGE_SALES_CONTRACT = "sale_contract"
    STAGE_SALES_WON = "sale_won"
    STAGE_SALES_LOST = "sale_lost"
    STAGE_TENDER_PREPARE = "tend_prepare"
    STAGE_TENDER_SUBMITTED = "tend_submitted"
    STAGE_TENDER_SHORTLIST = "tend_shortlist"
    STAGE_TENDER_WON = "tend_won"
    STAGE_TENDER_LOST = "tend_lost"
    STAGE_TENDER_CANCEL = "tend_cancel"
    URGENCY_LOW = "low"
    URGENCY_REQUIRED = "required"
    URGENCY_CRITICAL = "critical"
    STAGE_CHOICES = [
        (STAGE_SERVICE_NEW, "service_new"),
        (STAGE_SERVICE_IN_PROGRESS, "service_in_progress"),
        (STAGE_SERVICE_DONE, "service_done"),
        (STAGE_SERVICE_BLOCKED, "service_blocked"),
        (STAGE_PROJECT_IDEA, "project_idea"),
        (STAGE_PROJECT_DESIGN, "project_design"),
        (STAGE_PROJECT_BUILD, "project_build"),
        (STAGE_PROJECT_COMMISSION, "project_commission"),
        (STAGE_PROJECT_DONE, "project_done"),
        (STAGE_PROJECT_HOLD, "project_hold"),
        (STAGE_SALES_LEAD, "sales_lead"),
        (STAGE_SALES_QUALIFY, "sales_qualify"),
        (STAGE_SALES_OFFER, "sales_offer"),
        (STAGE_SALES_CONTRACT, "sales_contract"),
        (STAGE_SALES_WON, "sales_won"),
        (STAGE_SALES_LOST, "sales_lost"),
        (STAGE_TENDER_PREPARE, "tender_prepare"),
        (STAGE_TENDER_SUBMITTED, "tender_submitted"),
        (STAGE_TENDER_SHORTLIST, "tender_shortlist"),
        (STAGE_TENDER_WON, "tender_won"),
        (STAGE_TENDER_LOST, "tender_lost"),
        (STAGE_TENDER_CANCEL, "tender_cancel"),
    ]
    URGENCY_CHOICES = [
        (URGENCY_LOW, "\u041d\u0435\u0441\u0440\u043e\u0447\u043d\u043e"),
        (URGENCY_REQUIRED, "\u0422\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f"),
        (URGENCY_CRITICAL, "\u041a\u0440\u0438\u0442\u0438\u0447\u043d\u043e"),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="crm_items",
    )
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES)
    title = models.CharField(max_length=255)
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_items",
    )
    pool = models.ForeignKey(
        Pool,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_items",
    )
    stage = models.CharField(max_length=40, choices=STAGE_CHOICES, default=STAGE_SERVICE_NEW)
    urgency = models.CharField(max_length=20, choices=URGENCY_CHOICES, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True)
    service_works = models.TextField(blank=True)
    equipment_replacement = models.TextField(blank=True)
    photo_url = models.URLField(blank=True)
    photo = models.ImageField(upload_to="crm_issues/", blank=True, null=True)
    responsible = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_responsible_items",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_created_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["organization", "direction"], name="crm_org_dir_idx"),
        ]

    def __str__(self):
        return f"{self.title} ({self.direction})"


class CrmItemPhoto(models.Model):
    item = models.ForeignKey(CrmItem, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="crm_issues/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"CrmItemPhoto {self.item_id}"


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
        ("task_assignment", "task_assignment"),
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
    consumables_replaced = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.pool.address} - {self.date.strftime('%d.%m.%Y %H:%M')}"


class ServiceTask(models.Model):
    VISIBILITY_PUBLIC = "public"
    VISIBILITY_PRIVATE = "private"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, "Общая"),
        (VISIBILITY_PRIVATE, "Личная"),
    ]

    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Низкий"),
        (PRIORITY_NORMAL, "Обычный"),
        (PRIORITY_HIGH, "Высокий"),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="service_tasks",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    visibility = models.CharField(max_length=16, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC)
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_service_tasks",
    )
    responsibles = models.ManyToManyField(User, related_name="service_tasks")
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_service_tasks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["organization", "start_date"], name="task_org_start_idx"),
            models.Index(fields=["organization", "end_date"], name="task_org_end_idx"),
        ]

    def __str__(self):
        return f"{self.title} ({self.organization.name})"

    @property
    def is_completed(self):
        return bool(self.completed_at)

    def get_end_date(self):
        return self.end_date or self.start_date


class ServiceTaskChange(models.Model):
    ACTION_CREATED = "created"
    ACTION_UPDATED = "updated"
    ACTION_COMPLETED = "completed"
    ACTION_REOPENED = "reopened"
    ACTION_MOVED = "moved"
    ACTION_CHOICES = [
        (ACTION_CREATED, "Создание"),
        (ACTION_UPDATED, "Изменение"),
        (ACTION_COMPLETED, "Выполнено"),
        (ACTION_REOPENED, "Возобновлено"),
        (ACTION_MOVED, "Перенос"),
    ]

    task = models.ForeignKey(ServiceTask, on_delete=models.CASCADE, related_name="changes")
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    field_name = models.CharField(max_length=80, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["task", "created_at"], name="task_change_idx"),
        ]

    def __str__(self):
        return f"{self.task_id} {self.action}"
