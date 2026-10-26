"""Commercial schema with a limited validated ORM write contract."""
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models, router, transaction
from django.db.models import Q, F
from django.utils import timezone
from django.core.validators import RegexValidator


CANONICAL_KEY = RegexValidator(r"^[a-z0-9][a-z0-9_-]*\Z", "Use lowercase ASCII letters, digits, hyphens and underscores.")


class CommercialQuerySet(models.QuerySet):
    """All writes must pass instance validation under the parent row locks.

    Unsafe expression/batch writes are intentionally unavailable in this schema
    release. The PR04 service uses bounded instance writes against this contract.
    """
    def update(self, **kwargs):
        raise ValidationError("Use validated CRM instance.save().")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Bulk CRM writes are unavailable.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Bulk CRM writes are unavailable.")


class CommercialModel(models.Model):
    objects = CommercialQuerySet.as_manager()
    immutable_scope = ("organization_id",)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        organization_id = getattr(self, "organization_id", None)
        using = self._state.db or router.db_for_write(type(self), instance=self)
        for name in ("pipeline", "stage", "client", "lead", "deal", "command"):
            field = next((f for f in self._meta.fields if f.name == name), None)
            pk = getattr(self, field.attname, None) if field else None
            if not pk:
                continue
            try:
                parent = field.remote_field.model.objects.using(using).get(pk=pk)
            except ObjectDoesNotExist as exc:
                raise ValidationError({name: "CRM relation does not exist."}) from exc
            parent_org = parent.pipeline.organization_id if name == "stage" else parent.organization_id
            if organization_id is not None and parent_org != organization_id:
                raise ValidationError({name: "CRM relation must belong to the same organization."})
            if name == "stage" and parent.pipeline_id != self.pipeline_id:
                raise ValidationError({name: "Stage must belong to the selected pipeline."})
        # Assignment is account identity, not an HR record. Historical assignments
        # survive HR changes; fresh writes require current organization eligibility.
        for name in ("assignee", "manager"):
            user_id = getattr(self, f"{name}_id", None)
            if user_id:
                if self.pk and type(self).objects.using(using).filter(pk=self.pk, **{f"{name}_id": user_id}).exists():
                    continue  # retain the historical assignment, never a new grant
                from pool_service.services.crm_permissions import crm_staff_users
                from pool_service.models import Organization
                org = Organization.objects.using(using).get(pk=organization_id)
                if not crm_staff_users(org).using(using).filter(pk=user_id).exists():
                    raise ValidationError({name: "Assignee is unavailable in this organization."})

    def save(self, *args, **kwargs):
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            # Every supported commercial save joins the command/settings mutex.
            # Lock both scopes before refusing a reparent; never lock row -> org.
            from pool_service.models import Organization
            org_ids = set()
            candidates = [self]
            if self.pk:
                previous = type(self).objects.using(using).filter(pk=self.pk).first()
                if previous:
                    candidates.append(previous)
            for candidate in candidates:
                org_id = getattr(candidate, "organization_id", None)
                if isinstance(candidate, CrmStage) and candidate.pipeline_id:
                    org_id = CrmPipeline.objects.using(using).filter(pk=candidate.pipeline_id).values_list("organization_id", flat=True).first()
                if org_id:
                    org_ids.add(org_id)
            list(Organization.objects.using(using).select_for_update().filter(pk__in=org_ids).order_by("pk"))
            old = type(self).objects.using(using).select_for_update().filter(pk=self.pk).first() if self.pk else None
            if old:
                for key in self.immutable_scope:
                    if getattr(old, key) != getattr(self, key):
                        raise ValidationError("CRM scope keys are immutable.")
                # Validate the precise row that will be written, not stale values
                # omitted by update_fields; partial scope changes are forbidden.
                if kwargs.get("update_fields") is not None:
                    selected = set(kwargs["update_fields"])
                    for field in self._meta.concrete_fields:
                        if field.primary_key or field.name in selected or field.attname in selected:
                            continue
                        if getattr(old, field.attname) != getattr(self, field.attname):
                            raise ValidationError("Unsaved CRM fields differ from persisted values.")
            for field in self._meta.fields:
                if field.is_relation and getattr(self, field.attname, None):
                    field.remote_field.model.objects.using(using).select_for_update().filter(pk=getattr(self, field.attname)).exists()
            self.full_clean()
            return super().save(*args, **kwargs)


class CrmPipeline(CommercialModel):
    immutable_scope = ("organization_id", "code")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    code = models.SlugField(max_length=40, validators=[CANONICAL_KEY])
    name = models.CharField(max_length=160)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="crm_pipeline_org_code")]
        ordering = ["position", "pk"]


class StageQuerySet(CommercialQuerySet):
    def delete(self):
        raise ValidationError("Disable CRM stages to preserve history.")


class CrmStage(CommercialModel):
    objects = StageQuerySet.as_manager()

    def delete(self, *args, **kwargs):
        raise ValidationError("Disable CRM stages to preserve history.")

    immutable_scope = ("pipeline_id", "code")
    pipeline = models.ForeignKey(CrmPipeline, on_delete=models.PROTECT, related_name="stages")
    code = models.SlugField(max_length=40, validators=[CANONICAL_KEY])
    name = models.CharField(max_length=160)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    # PR04 uses an explicit code registry; no executable JSON rules.

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["pipeline", "code"], name="crm_stage_pipeline_code")]
        ordering = ["position", "pk"]


class CrmLead(CommercialModel):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=[("client", "Клиент"), ("tender", "Тендер")], default="client")
    client = models.ForeignKey("pool_service.Client", null=True, blank=True, on_delete=models.PROTECT)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=255)
    need = models.TextField(blank=True)
    acquisition_source = models.CharField(max_length=80, blank=True)
    first_contact_channel = models.CharField(max_length=40, blank=True)
    first_occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16, choices=[("new", "Новый"), ("working", "В работе"), ("qualifying", "Уточнение"), ("waiting", "Ожидание"), ("deferred", "Отложен"), ("closed", "Разбор завершён")], default="new")
    result = models.CharField(max_length=16, choices=[("", "Не определён"), ("qualified", "Квалифицирован в сделки"), ("linked", "Привязан к текущей работе"), ("non_target", "Нецелевой"), ("spam", "Спам"), ("duplicate", "Дубль"), ("no_deal", "Закрыт без сделки")], blank=True, default="")
    reason = models.CharField(max_length=500, blank=True)
    follow_up_at = models.DateTimeField(null=True, blank=True)
    budget_kind = models.CharField(max_length=8, choices=[("unknown", "Неизвестен"), ("exact", "Точный"), ("range", "Диапазон")], default="unknown")
    budget_min = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    budget_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    revision = models.PositiveIntegerField(default=0)
    is_archived = models.BooleanField(default=False)

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=["client", "tender"]), name="crm_lead_kind"),
            models.CheckConstraint(condition=Q(status__in=["new", "working", "qualifying", "waiting", "deferred", "closed"]), name="crm_lead_status"),
            models.CheckConstraint(condition=Q(result__in=["", "qualified", "linked", "non_target", "spam", "duplicate", "no_deal"]), name="crm_lead_result"),
            models.CheckConstraint(condition=(Q(status="closed") & ~Q(result="")) | (~Q(status="closed") & Q(result="")), name="crm_lead_result_shape"),
            models.CheckConstraint(condition=~Q(result="no_deal") | ~Q(reason=""), name="crm_lead_no_deal_reason"),
            models.CheckConstraint(condition=(~Q(status__in=["waiting", "deferred"]) | (~Q(reason="") & Q(follow_up_at__isnull=False))), name="crm_lead_follow_up"),
            models.CheckConstraint(condition=(Q(budget_kind="unknown", budget_min__isnull=True, budget_max__isnull=True) | (Q(budget_kind="exact", budget_min__isnull=False, budget_max__isnull=False, budget_min=F("budget_max")) & ~Q(currency="")) | (Q(budget_kind="range", budget_min__isnull=False, budget_max__isnull=False, budget_max__gte=F("budget_min")) & ~Q(currency=""))), name="crm_lead_budget_shape"),
            models.CheckConstraint(condition=(Q(budget_min__isnull=True) | Q(budget_min__gte=0)) & (Q(budget_max__isnull=True) | Q(budget_max__gte=0)), name="crm_lead_budget_nonnegative"),
        ]
        indexes = [models.Index(fields=["organization", "status", "is_archived"], name="crm_lead_queue_idx")]


class CrmDeal(CommercialModel):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    pipeline = models.ForeignKey(CrmPipeline, on_delete=models.PROTECT, related_name="deals")
    stage = models.ForeignKey(CrmStage, on_delete=models.PROTECT, related_name="deals")
    client = models.ForeignKey("pool_service.Client", null=True, blank=True, on_delete=models.PROTECT)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=255)
    specialization = models.CharField(max_length=40, blank=True)
    fulfillment_kind = models.CharField(max_length=24, choices=[("unknown", "Неизвестен"), ("construction", "Строительство"), ("service", "Обслуживание"), ("retail", "Поставка"), ("mixed", "Смешанный")], default="unknown")
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    amount_basis = models.CharField(max_length=24, choices=[("unknown", "Неизвестно"), ("indicative", "Ориентир"), ("agreed", "Согласовано")], default="unknown")
    outcome = models.CharField(max_length=12, choices=[("open", "Открыта"), ("won", "Успех"), ("lost", "Проигрыш"), ("cancelled", "Отменена"), ("unknown", "Legacy: не определено")], default="open")
    data_quality = models.CharField(max_length=16, choices=[("native", "Новая"), ("legacy_review", "Legacy: сверка")], default="native")
    close_reason = models.CharField(max_length=500, blank=True)
    expected_decision_at = models.DateTimeField(null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    handed_over_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    is_archived = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(outcome__in=["open", "won", "lost", "cancelled", "unknown"]), name="crm_deal_outcome"),
            models.CheckConstraint(condition=~Q(outcome="unknown") | Q(data_quality="legacy_review"), name="crm_deal_unknown_quality"),
            models.CheckConstraint(condition=Q(data_quality__in=["native", "legacy_review"]), name="crm_deal_quality"),
            models.CheckConstraint(condition=Q(fulfillment_kind__in=["unknown", "construction", "service", "retail", "mixed"]), name="crm_deal_fulfillment"),
            models.CheckConstraint(condition=(Q(amount__isnull=True, amount_basis="unknown") | (Q(amount__isnull=False, amount__gte=0, amount_basis__in=["indicative", "agreed"]) & ~Q(currency=""))), name="crm_deal_amount_shape"),
        ]
        indexes = [models.Index(fields=["organization", "pipeline", "is_archived"], name="crm_deal_board_idx")]


class CrmCommand(CommercialModel):
    immutable_scope = ("organization_id", "operation_key", "command_type", "payload_hash", "actor_id")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    operation_key = models.CharField(max_length=100, validators=[CANONICAL_KEY])
    command_type = models.CharField(max_length=80)
    payload_hash = models.CharField(max_length=64, validators=[RegexValidator(r"^[0-9a-f]{64}\Z", "Expected a SHA-256 content hash.")])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["organization", "operation_key"], name="crm_command_org_operation")]


    def clean(self):
        super().clean()
        if not isinstance(self.result, dict) or not self.result.keys() <= {"lead_id", "deal_id", "event_id", "revision"}:
            raise ValidationError("Command result accepts only structural CRM identifiers.")
        if any(type(value) is not int or value < 0 for value in self.result.values()):
            raise ValidationError("Command result values must be nonnegative integer identifiers.")


class EventQuerySet(CommercialQuerySet):
    def delete(self):
        raise ValidationError("CRM events are immutable.")

    def update(self, **kwargs):
        raise ValidationError("CRM events are immutable.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("CRM events are immutable.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Bulk CRM events are unavailable.")


class CrmEvent(CommercialModel):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    lead = models.ForeignKey(CrmLead, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    deal = models.ForeignKey(CrmDeal, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    command = models.ForeignKey(CrmCommand, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField(max_length=80)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    occurred_at = models.DateTimeField(default=timezone.now)
    recorded_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=500, blank=True)
    # Structural values only. Free text, names, messages, transcripts and payloads
    # stay in their authorized source. Explicit user reasons are bounded above.
    safe_diff = models.JSONField(default=dict, blank=True)
    objects = EventQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        constraints = [models.CheckConstraint(condition=Q(lead__isnull=False, deal__isnull=True) | Q(lead__isnull=True, deal__isnull=False), name="crm_event_one_subject")]
        indexes = [models.Index(fields=["organization", "recorded_at"], name="crm_event_timeline_idx")]

    def clean(self):
        super().clean()
        allowed = {"stage_id", "pipeline_id", "manager_id", "assignee_id", "revision", "is_archived", "status", "title_changed", "need_changed"}
        if not isinstance(self.safe_diff, dict) or not self.safe_diff.keys() <= allowed:
            raise ValidationError("Event diff accepts only structural CRM fields.")
        for field, change in self.safe_diff.items():
            if not isinstance(change, dict) or set(change) != {"before", "after"}:
                raise ValidationError("Event change requires before and after.")
            if field == "status":
                if any(type(value) is not str or value not in {"new", "working", "qualifying", "waiting", "deferred", "closed"} for value in change.values()):
                    raise ValidationError("Event status must be a known lead state.")
                continue
            if field in {"title_changed", "need_changed"} and any(type(value) is not bool for value in change.values()):
                raise ValidationError("Content markers must be booleans.")
            if any(value is not None and type(value) not in (int, bool) for value in change.values()):
                raise ValidationError("Event values must be IDs, revisions or booleans.")

    def delete(self, *args, **kwargs):
        raise ValidationError("CRM events are immutable.")

    def save(self, *args, **kwargs):
        if not self._state.adding or (self.pk and type(self).objects.filter(pk=self.pk).exists()):
            raise ValidationError("CRM events are immutable.")
        return super().save(*args, **kwargs)


class CrmAccess(CommercialModel):
    immutable_scope = ("organization_id", "user_id")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    view_team_deals = models.BooleanField(default=False)
    edit_own_leads = models.BooleanField(default=False, editable=False)
    transition_own_leads = models.BooleanField(default=False, editable=False)
    edit_own_deals = models.BooleanField(default=False, editable=False)
    transition_own_deals = models.BooleanField(default=False, editable=False)

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="crm_access_org_user")]
