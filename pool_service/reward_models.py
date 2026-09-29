from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class OneCAuthorIdentity(models.Model):
    STATUS_NEEDS_MAPPING = "needs_mapping"
    STATUS_MAPPED = "mapped"
    STATUS_TECHNICAL = "technical"
    STATUS_EXCLUDED = "excluded"
    STATUS_CHOICES = [
        (STATUS_NEEDS_MAPPING, "Требует сопоставления"),
        (STATUS_MAPPED, "Сопоставлен"),
        (STATUS_TECHNICAL, "Техническая учётная запись"),
        (STATUS_EXCLUDED, "Исключён"),
    ]

    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="onec_author_identities")
    onec_user_id = models.CharField(max_length=120)
    raw_name = models.CharField(max_length=500, blank=True)
    employee = models.ForeignKey("Employee", on_delete=models.PROTECT, null=True, blank=True, related_name="onec_author_identities")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_NEEDS_MAPPING)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_onec_authors")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "onec_user_id"], name="unique_onec_author_per_org")]
        indexes = [models.Index(fields=["organization", "status"], name="onec_author_org_status_idx")]

    def clean(self):
        super().clean()
        if self.employee_id and self.employee.organization_id != self.organization_id:
            raise ValidationError({"employee": "Сотрудник относится к другой организации."})


class OneCCustomerIdentity(models.Model):
    """Explicit, auditable 1C counterparty -> Service2 client link."""

    organization = models.ForeignKey(
        "Organization", on_delete=models.CASCADE, related_name="onec_customer_identities"
    )
    onec_customer_id = models.CharField(max_length=120)
    raw_name = models.CharField(max_length=500, blank=True)
    client = models.ForeignKey(
        "Client", on_delete=models.PROTECT, related_name="onec_customer_identities"
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_onec_customers",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "onec_customer_id"],
                name="unique_onec_customer_per_org",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "client"],
                name="onec_customer_org_client_idx",
            )
        ]

    def clean(self):
        super().clean()
        if self.client_id and self.client.organization_id != self.organization_id:
            raise ValidationError({"client": "Клиент относится к другой организации."})


class RewardCustomerManagerRule(models.Model):
    """Default client manager keyed by stable 1C counterparty GUID."""

    organization = models.ForeignKey(
        "Organization",
        on_delete=models.CASCADE,
        related_name="reward_customer_manager_rules",
    )
    onec_customer_id = models.CharField(max_length=120)
    raw_name = models.CharField(max_length=500, blank=True)
    employee = models.ForeignKey(
        "Employee",
        on_delete=models.PROTECT,
        related_name="reward_customer_manager_rules",
    )
    share = models.DecimalField(
        max_digits=7,
        decimal_places=6,
        default=Decimal("1.000000"),
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_reward_customer_manager_rules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["organization", "onec_customer_id", "effective_from"],
                name="reward_cust_mgr_lookup_idx",
            )
        ]

    def clean(self):
        super().clean()
        if self.share <= 0 or self.share > 1:
            raise ValidationError({"share": "Доля должна быть больше 0 и не больше 1."})
        if self.employee_id and self.employee.organization_id != self.organization_id:
            raise ValidationError({"employee": "Сотрудник относится к другой организации."})


class RewardOrderObjectLink(models.Model):
    """Explicit order -> Service2 object link used only for deterministic reward rules."""

    organization = models.ForeignKey(
        "Organization", on_delete=models.CASCADE, related_name="reward_order_object_links"
    )
    source_document_key = models.CharField(max_length=255)
    client = models.ForeignKey(
        "Client", on_delete=models.PROTECT, related_name="reward_order_object_links"
    )
    pool = models.ForeignKey(
        "Pool", on_delete=models.PROTECT, related_name="reward_order_object_links"
    )
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="linked_reward_order_objects",
    )
    linked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_document_key"],
                name="unique_reward_order_object_link",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "pool"],
                name="reward_order_pool_idx",
            )
        ]

    def clean(self):
        super().clean()
        if self.client_id and self.client.organization_id != self.organization_id:
            raise ValidationError({"client": "Клиент относится к другой организации."})
        if self.pool_id:
            if self.pool.organization_id != self.organization_id:
                raise ValidationError({"pool": "Объект относится к другой организации."})
            if self.client_id and self.pool.client_id != self.client_id:
                raise ValidationError({"pool": "Объект относится к другому клиенту."})


class RewardSchemeVersion(models.Model):
    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="reward_scheme_versions")
    name = models.CharField(max_length=120, default="Тестовая схема №1")
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_test = models.BooleanField(default=True)
    documentation_retail_fixed = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("50.00"))
    retail_check_rate = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("0.010000"))
    documentation_document_fixed = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("200.00"))
    sale_rate = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("0.100000"))
    project_rate = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("0.050000"))
    work_rate = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("0.400000"))
    client_manager_rate = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("0.000000"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_reward_schemes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["organization_id", "effective_from", "version"]
        constraints = [models.UniqueConstraint(fields=["organization", "name", "version"], name="unique_reward_scheme_version")]
        permissions = [
            ("view_employee_rewards", "Can view employee reward summary"),
            ("manage_employee_reward_participation", "Can manage employee reward participation"),
            ("manage_employee_reward_rules", "Can manage employee reward rules"),
            ("close_employee_reward_period", "Can close employee reward period"),
        ]


class RewardParticipantTemplate(models.Model):
    ROLE_CLIENT_MANAGER = "client_manager"
    ROLE_SALE = "sale"
    ROLE_DOCUMENTATION = "documentation"
    ROLE_PROJECT = "project"
    ROLE_WORK = "work"
    ROLE_CHOICES = [
        (ROLE_CLIENT_MANAGER, "Менеджер клиента"),
        (ROLE_SALE, "Продажа"),
        (ROLE_DOCUMENTATION, "Оформление"),
        (ROLE_PROJECT, "Проект / расчёт"),
        (ROLE_WORK, "Выполнение работ"),
    ]

    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="reward_participant_templates")
    client = models.ForeignKey("Client", on_delete=models.CASCADE, null=True, blank=True, related_name="reward_participant_templates")
    pool = models.ForeignKey("Pool", on_delete=models.CASCADE, null=True, blank=True, related_name="reward_participant_templates")
    employee = models.ForeignKey("Employee", on_delete=models.PROTECT, null=True, blank=True, related_name="reward_participant_templates")
    role = models.CharField(max_length=24, choices=ROLE_CHOICES)
    share = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("1.000000"))
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_company_client = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_reward_templates")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "role", "effective_from"], name="reward_tpl_org_role_idx")]

    def clean(self):
        super().clean()
        if self.share < 0 or self.share > 1:
            raise ValidationError({"share": "Доля должна быть от 0 до 1."})
        if self.is_company_client:
            if self.role != self.ROLE_CLIENT_MANAGER:
                raise ValidationError({"is_company_client": "Общий клиент компании допустим только для роли менеджера клиента."})
            if self.employee_id:
                raise ValidationError({"employee": "Для общего клиента компании конкретный менеджер не назначается."})
        elif not self.employee_id:
            raise ValidationError({"employee": "Для шаблона нужно выбрать сотрудника."})
        if self.employee_id and self.employee.organization_id != self.organization_id:
            raise ValidationError({"employee": "Сотрудник относится к другой организации."})
        if self.pool_id and self.client_id and self.pool.client_id != self.client_id:
            raise ValidationError({"pool": "Объект относится к другому клиенту."})


class RewardParticipation(models.Model):
    ROLE_CLIENT_MANAGER = RewardParticipantTemplate.ROLE_CLIENT_MANAGER
    ROLE_SALE = RewardParticipantTemplate.ROLE_SALE
    ROLE_DOCUMENTATION = RewardParticipantTemplate.ROLE_DOCUMENTATION
    ROLE_PROJECT = RewardParticipantTemplate.ROLE_PROJECT
    ROLE_WORK = RewardParticipantTemplate.ROLE_WORK
    ROLE_CHOICES = RewardParticipantTemplate.ROLE_CHOICES

    STATUS_REQUIRED = "required"
    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_NOT_APPLICABLE = "not_applicable"
    STATUS_CHOICES = [
        (STATUS_REQUIRED, "Требуется назначение"),
        (STATUS_PENDING, "Назначено, ожидает подтверждения"),
        (STATUS_CONFIRMED, "Подтверждено"),
        (STATUS_NOT_APPLICABLE, "Не применяется"),
    ]
    SOURCE_TEMPLATE = "template"
    SOURCE_ORDER = "order"
    SOURCE_TASK = "task"
    SOURCE_ONEC_AUTHOR = "onec_author"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_TEMPLATE, "Шаблон"),
        (SOURCE_ORDER, "Заказ"),
        (SOURCE_TASK, "Задача / наряд"),
        (SOURCE_ONEC_AUTHOR, "Автор документа 1С"),
        (SOURCE_MANUAL, "Вручную"),
    ]

    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="reward_participations")
    employee = models.ForeignKey("Employee", on_delete=models.PROTECT, null=True, blank=True, related_name="reward_participations")
    author_identity = models.ForeignKey(OneCAuthorIdentity, on_delete=models.PROTECT, null=True, blank=True, related_name="reward_participations")
    role = models.CharField(max_length=24, choices=ROLE_CHOICES)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    share = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("1.000000"))
    period_month = models.DateField()
    scope_key = models.CharField(max_length=255)
    source_document_key = models.CharField(max_length=255)
    source_document_type = models.CharField(max_length=120, blank=True)
    source_document_guid = models.CharField(max_length=120, blank=True)
    source_document_number = models.CharField(max_length=120, blank=True)
    source_document_date = models.DateField(null=True, blank=True)
    scope_line_identities = models.JSONField(default=list, blank=True)
    customer_name = models.CharField(max_length=500, blank=True)
    object_label = models.CharField(max_length=500, blank=True)
    basis = models.CharField(max_length=500, blank=True)
    assignment_source = models.CharField(max_length=24, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_reward_participations")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_reward_participations")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["organization", "period_month", "role"], name="reward_part_org_month_role_idx"),
            models.Index(fields=["organization", "source_document_key"], name="reward_part_doc_idx"),
        ]

    def clean(self):
        super().clean()
        if self.share < 0 or self.share > 1:
            raise ValidationError({"share": "Доля должна быть от 0 до 1."})
        if self.employee_id and self.employee.organization_id != self.organization_id:
            raise ValidationError({"employee": "Сотрудник относится к другой организации."})


class RewardParticipationChange(models.Model):
    participation = models.ForeignKey(RewardParticipation, on_delete=models.CASCADE, related_name="changes")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reward_participation_changes")
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class RewardMonthClose(models.Model):
    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="reward_month_closes")
    period_month = models.DateField()
    scheme_version = models.ForeignKey(RewardSchemeVersion, on_delete=models.PROTECT, related_name="month_closes")
    snapshot = models.JSONField(default=dict)
    source_hash = models.CharField(max_length=64)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="closed_reward_months")
    closed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "period_month"], name="unique_reward_close_org_month")]


class RewardAdjustment(models.Model):
    STATUS_PROPOSED = "proposed"
    STATUS_CONFIRMED = "confirmed"
    STATUS_CHOICES = [(STATUS_PROPOSED, "Предложена"), (STATUS_CONFIRMED, "Подтверждена")]

    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="reward_adjustments")
    employee = models.ForeignKey("Employee", on_delete=models.PROTECT, related_name="reward_adjustments")
    period_month = models.DateField()
    original_period_month = models.DateField()
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PROPOSED)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_reward_adjustments")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_reward_adjustments")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "period_month", "status"], name="reward_adj_org_month_idx")]
