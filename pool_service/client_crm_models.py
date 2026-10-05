from django.conf import settings
from django.db import models


class ClientCRMProfile(models.Model):
    LEGAL_FORM_NONE = ""
    LEGAL_FORM_ENTITY = "entity"
    LEGAL_FORM_IP = "ip"
    LEGAL_FORM_CHOICES = [
        (LEGAL_FORM_NONE, "Не указано"),
        (LEGAL_FORM_ENTITY, "Юридическое лицо"),
        (LEGAL_FORM_IP, "ИП"),
    ]

    SOURCE_MANUAL = "manual"
    SOURCE_ONEC = "onec"
    SOURCE_ONEC_IP = "onec_ip"
    SOURCE_PHONEBOOK = "phonebook"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Вручную"),
        (SOURCE_ONEC, "1С"),
        (SOURCE_ONEC_IP, "1С / ИП"),
        (SOURCE_PHONEBOOK, "Телефонная книга"),
    ]

    client = models.OneToOneField(
        "pool_service.Client",
        on_delete=models.CASCADE,
        related_name="crm_profile",
    )
    legal_form = models.CharField(
        max_length=16,
        choices=LEGAL_FORM_CHOICES,
        blank=True,
        default=LEGAL_FORM_NONE,
    )
    middle_name = models.CharField(max_length=120, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    legal_name = models.CharField(max_length=500, blank=True)
    kpp = models.CharField(max_length=20, blank=True)
    ogrn = models.CharField(max_length=20, blank=True)
    onec_ref = models.CharField(max_length=36, null=True, blank=True, unique=True)
    onec_code = models.CharField(max_length=64, blank=True)
    onec_name = models.CharField(max_length=500, blank=True)
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
    )
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_responsible_clients",
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_managed_clients",
    )
    notes = models.TextField(blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "CRM-профиль клиента"
        verbose_name_plural = "CRM-профили клиентов"

    def __str__(self):
        return str(self.client)


class ClientContact(models.Model):
    KIND_PHONE = "phone"
    KIND_EMAIL = "email"
    KIND_CHOICES = [
        (KIND_PHONE, "Телефон"),
        (KIND_EMAIL, "Email"),
    ]

    client = models.ForeignKey(
        "pool_service.Client",
        on_delete=models.CASCADE,
        related_name="crm_contacts",
    )
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    value = models.CharField(max_length=255)
    match_value = models.CharField(max_length=255, blank=True, db_index=True)
    label = models.CharField(max_length=120, blank=True)
    is_primary = models.BooleanField(default=False)
    sources = models.JSONField(default=list, blank=True)
    source_reference = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "-is_primary", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "kind", "value"],
                name="crm_contact_client_kind_value_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "kind"],
                name="crm_contact_client_kind_idx",
            ),
        ]

    def __str__(self):
        return f"{self.client}: {self.value}"


class ClientCompanyLink(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_ONEC_IP = "onec_ip"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Вручную"),
        (SOURCE_ONEC_IP, "1С / ИП"),
    ]

    company = models.ForeignKey(
        "pool_service.Client",
        on_delete=models.CASCADE,
        related_name="person_links",
    )
    person = models.ForeignKey(
        "pool_service.Client",
        on_delete=models.CASCADE,
        related_name="company_links",
    )
    position = models.CharField(max_length=160, blank=True)
    roles = models.JSONField(default=list, blank=True)
    is_primary = models.BooleanField(default=False)
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
    )
    source_reference = models.CharField(max_length=120, blank=True)
    automatic = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "person"],
                name="crm_company_person_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["company", "source"],
                name="crm_company_link_source_idx",
            ),
            models.Index(
                fields=["person"],
                name="crm_person_link_idx",
            ),
        ]

    def __str__(self):
        return f"{self.person} -> {self.company}"


class ClientImportCandidate(models.Model):
    KIND_LEGAL = "legal"
    KIND_IP = "ip"
    KIND_PRIVATE = "private"
    KIND_CHOICES = [
        (KIND_LEGAL, "Юридическое лицо"),
        (KIND_IP, "ИП"),
        (KIND_PRIVATE, "Физическое лицо"),
    ]

    STATUS_READY = "ready"
    STATUS_REVIEW = "review"
    STATUS_DUPLICATE = "duplicate"
    STATUS_INVALID = "invalid"
    STATUS_IMPORTED = "imported"
    STATUS_CHOICES = [
        (STATUS_READY, "Готов к импорту"),
        (STATUS_REVIEW, "Нужно проверить"),
        (STATUS_DUPLICATE, "Возможный дубль"),
        (STATUS_INVALID, "Некорректные данные"),
        (STATUS_IMPORTED, "Импортирован"),
    ]

    organization = models.ForeignKey(
        "pool_service.Organization",
        on_delete=models.CASCADE,
        related_name="client_import_candidates",
    )
    source_ref = models.CharField(max_length=36)
    source_code = models.CharField(max_length=64, blank=True)
    source_kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    name = models.CharField(max_length=500)
    legal_name = models.CharField(max_length=500, blank=True)
    inn = models.CharField(max_length=20, blank=True)
    kpp = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=80, blank=True)
    email = models.CharField(max_length=255, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_REVIEW,
    )
    reason = models.CharField(max_length=500, blank=True)
    matched_client = models.ForeignKey(
        "pool_service.Client",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_candidates",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_ref"],
                name="crm_import_org_ref_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status"],
                name="crm_import_org_status_idx",
            ),
            models.Index(
                fields=["organization", "source_kind"],
                name="crm_import_org_kind_idx",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"
