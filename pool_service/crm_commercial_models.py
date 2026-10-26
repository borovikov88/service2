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
    immutable_scope = ("organization_id", "origin_lead_id")
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
    direction = models.CharField(max_length=24, blank=True, choices=[("", "Не уточнено"), ("construction", "Строительство"), ("service", "Обслуживание"), ("retail", "Розница"), ("tender", "Торги")])
    specialization = models.CharField(max_length=40, blank=True)
    location = models.CharField(max_length=255, blank=True)
    desired_at = models.DateTimeField(null=True, blank=True)
    next_task = models.ForeignKey("pool_service.ServiceTask", null=True, blank=True, on_delete=models.PROTECT, related_name="current_commercial_leads")
    origin_lead = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived_needs")
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="duplicate_leads")
    linked_deal = models.ForeignKey("pool_service.CrmDeal", null=True, blank=True, on_delete=models.PROTECT, related_name="resolved_leads")
    intake_reason = models.CharField(max_length=40, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    needs_review = models.BooleanField(default=False)
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


    def clean(self):
        super().clean()
        using = self._state.db or router.db_for_write(type(self), instance=self)
        if self.origin_lead_id:
            origin = type(self).objects.using(using).filter(pk=self.origin_lead_id).first()
            if not origin or origin.organization_id != self.organization_id or origin.pk == self.pk or origin.origin_lead_id:
                raise ValidationError("Origin must be a canonical source lead in this organization.")
        if self.duplicate_of_id:
            target = type(self).objects.using(using).filter(pk=self.duplicate_of_id).first()
            if not target or target.organization_id != self.organization_id or target.pk == self.pk:
                raise ValidationError("Duplicate target must be another lead in this organization.")
        if self.linked_deal_id:
            if not CrmDeal.objects.using(using).filter(pk=self.linked_deal_id, organization_id=self.organization_id).exists():
                raise ValidationError("Linked deal belongs to another organization.")
        if self.next_task_id:
            from pool_service.models import ServiceTask
            task = ServiceTask.objects.using(using).filter(pk=self.next_task_id).first()
            if (not task or task.organization_id != self.organization_id or not self.pk
                    or not CrmLeadTaskLink.objects.using(using).filter(lead_id=self.pk, task_id=task.pk, organization_id=self.organization_id).exists()):
                raise ValidationError("Lead next action requires its own validated task link.")


class CrmDeal(CommercialModel):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    pipeline = models.ForeignKey(CrmPipeline, on_delete=models.PROTECT, related_name="deals")
    stage = models.ForeignKey(CrmStage, on_delete=models.PROTECT, related_name="deals")
    client = models.ForeignKey("pool_service.Client", null=True, blank=True, on_delete=models.PROTECT)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=255)
    work_subject = models.CharField(max_length=2000, blank=True)
    object_kind = models.CharField(max_length=16, choices=[("none", "Не выбран"), ("pool", "Обслуживаемый объект"), ("client_object", "Объект клиента"), ("planned", "Планируемый объект")], default="none")
    pool = models.ForeignKey("pool_service.Pool", null=True, blank=True, on_delete=models.PROTECT, related_name="commercial_deals")
    client_object = models.ForeignKey("pool_service.ClientOwnedObject", null=True, blank=True, on_delete=models.PROTECT, related_name="commercial_deals")
    planned_object_name = models.CharField(max_length=255, blank=True)
    planned_object_address = models.CharField(max_length=500, blank=True)
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
            models.CheckConstraint(condition=(
                Q(object_kind="none", pool__isnull=True, client_object__isnull=True, planned_object_name="", planned_object_address="")
                | Q(object_kind="pool", pool__isnull=False, client_object__isnull=True, planned_object_name="", planned_object_address="")
                | Q(object_kind="client_object", pool__isnull=True, client_object__isnull=False, planned_object_name="", planned_object_address="")
                | (Q(object_kind="planned", pool__isnull=True, client_object__isnull=True) & ~Q(planned_object_name=""))
            ), name="crm_deal_object_shape"),
            models.CheckConstraint(condition=Q(outcome__in=["open", "won", "lost", "cancelled", "unknown"]), name="crm_deal_outcome"),
            models.CheckConstraint(condition=~Q(outcome="unknown") | Q(data_quality="legacy_review"), name="crm_deal_unknown_quality"),
            models.CheckConstraint(condition=Q(data_quality__in=["native", "legacy_review"]), name="crm_deal_quality"),
            models.CheckConstraint(condition=Q(fulfillment_kind__in=["unknown", "construction", "service", "retail", "mixed"]), name="crm_deal_fulfillment"),
            models.CheckConstraint(condition=(Q(amount__isnull=True, amount_basis="unknown") | (Q(amount__isnull=False, amount__gte=0, amount_basis__in=["indicative", "agreed"]) & ~Q(currency=""))), name="crm_deal_amount_shape"),
        ]
        indexes = [models.Index(fields=["organization", "pipeline", "is_archived"], name="crm_deal_board_idx")]


    def clean(self):
        super().clean()
        using = self._state.db or router.db_for_write(type(self), instance=self)
        if self.object_kind == "planned" and not self.planned_object_name.strip():
            raise ValidationError({"planned_object_name": "Planned object needs a name."})
        if self.pool_id:
            from pool_service.models import Pool
            if not self.client_id or not Pool.objects.using(using).filter(pk=self.pool_id,
                    organization_id=self.organization_id, client_id=self.client_id).exists():
                raise ValidationError({"pool": "Deal object must belong to this client and organization."})
        if self.client_object_id:
            from pool_service.client_crm_models import ClientOwnedObject
            if not self.client_id or not ClientOwnedObject.objects.using(using).filter(pk=self.client_object_id,
                    client_id=self.client_id, client__organization_id=self.organization_id).exists():
                raise ValidationError({"client_object": "Deal object must belong to this client and organization."})


class CrmDealTaskLink(CommercialModel):
    """Typed provenance; the existing task remains the source of its content."""
    immutable_scope = ("organization_id", "deal_id", "task_id", "created_by_id")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    deal = models.ForeignKey(CrmDeal, on_delete=models.PROTECT, related_name="task_links")
    task = models.OneToOneField("pool_service.ServiceTask", on_delete=models.PROTECT,
                               related_name="commercial_deal_link")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = "objects"

    def clean(self):
        super().clean()
        if not self.task_id or not self.deal_id:
            return
        from pool_service.models import ServiceTask
        using = self._state.db or router.db_for_write(type(self), instance=self)
        task = ServiceTask.objects.using(using).filter(pk=self.task_id).first()
        deal = CrmDeal.objects.using(using).filter(pk=self.deal_id).first()
        if (not task or not deal or task.organization_id != self.organization_id
                or task.client_id != deal.client_id or task.crm_item_id
                or task.water_reading_id or task.pool_id
                or task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP):
            raise ValidationError("The task must belong to this deal's client and organization, without a legacy CRM link.")
        if CrmLeadTaskLink.objects.using(using).filter(task_id=self.task_id).exists():
            raise ValidationError("A deal task cannot also be a lead task.")


class CrmDealNextAction(CommercialModel):
    """One primary pointer, with completed/replaced task links retained."""
    immutable_scope = ("organization_id", "deal_id")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    deal = models.OneToOneField(CrmDeal, on_delete=models.PROTECT, related_name="next_action")
    task_link = models.OneToOneField(CrmDealTaskLink, on_delete=models.PROTECT,
                                    related_name="primary_for")

    class Meta:
        base_manager_name = "objects"

    def clean(self):
        super().clean()
        if not self.task_link_id:
            return
        using = self._state.db or router.db_for_write(type(self), instance=self)
        link = CrmDealTaskLink.objects.using(using).filter(pk=self.task_link_id).first()
        if not link or link.organization_id != self.organization_id or link.deal_id != self.deal_id:
            raise ValidationError("The primary task link must belong to this deal.")
        link.full_clean()


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
        if not isinstance(self.result, dict) or not self.result.keys() <= {"lead_id", "deal_id", "event_id", "revision", "task_id", "deal_ids", "previous_lead_id"}:
            raise ValidationError("Command result accepts only structural CRM identifiers.")
        if "deal_ids" in self.result and (type(self.result["deal_ids"]) is not list
                or not 1 <= len(self.result["deal_ids"]) <= 10
                or any(type(value) is not int or value <= 0 for value in self.result["deal_ids"])
                or len(set(self.result["deal_ids"])) != len(self.result["deal_ids"])):
            raise ValidationError("Deal results require a bounded unique list of identifiers.")
        if any(type(value) is not int or value < 0 for key, value in self.result.items() if key != "deal_ids"):
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
        allowed = {"work_subject_changed", "planned_object_changed", "object_kind", "pool_id", "client_object_id", "specialization_changed", "fulfillment_kind", "amount", "currency", "amount_basis", "expected_decision_at", "outcome", "closed_at", "stage_id", "pipeline_id", "manager_id", "assignee_id", "revision", "is_archived", "status", "title_changed", "need_changed", "next_action_changed"}
        if not isinstance(self.safe_diff, dict) or not self.safe_diff.keys() <= allowed:
            raise ValidationError("Event diff accepts only structural CRM fields.")
        for field, change in self.safe_diff.items():
            if not isinstance(change, dict) or set(change) != {"before", "after"}:
                raise ValidationError("Event change requires before and after.")
            enums = {
                "object_kind": {"none", "pool", "client_object", "planned"},
                "fulfillment_kind": {"unknown", "construction", "service", "retail", "mixed"},
                "amount_basis": {"unknown", "indicative", "agreed"},
                "outcome": {"open", "won", "lost", "cancelled", "unknown"},
            }
            if field in {"pool_id", "client_object_id"}:
                if any(value is not None and (type(value) is not int or value <= 0) for value in change.values()):
                    raise ValidationError("Object history values must be positive IDs or NULL.")
                continue
            if field in enums:
                if any(type(value) is not str or value not in enums[field] for value in change.values()):
                    raise ValidationError("Event value must be a known CRM enum.")
                continue
            if field in {"amount", "currency", "expected_decision_at", "closed_at"}:
                import re
                from django.utils.dateparse import parse_datetime
                for value in change.values():
                    if field == "currency":
                        valid = type(value) is str and (value == "" or re.fullmatch(r"[A-Z]{3}", value))
                    elif value is None:
                        valid = True
                    elif field == "amount":
                        valid = type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,15})\.[0-9]{2}", value)
                    else:
                        try:
                            valid = type(value) is str and parse_datetime(value) is not None and timezone.is_aware(parse_datetime(value))
                        except ValueError:
                            valid = False
                    if not valid:
                        raise ValidationError("Event value must be canonical commercial data.")
                continue
            if field == "status":
                if any(type(value) is not str or value not in {"new", "working", "qualifying", "waiting", "deferred", "closed"} for value in change.values()):
                    raise ValidationError("Event status must be a known lead state.")
                continue
            if field in {"title_changed", "need_changed", "next_action_changed", "work_subject_changed", "planned_object_changed", "specialization_changed"} and any(type(value) is not bool for value in change.values()):
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
    resolve_contact_identity = models.BooleanField(default=False, editable=False)
    view_team_deals = models.BooleanField(default=False)
    view_leads = models.BooleanField(default=False, editable=False)
    create_own_leads = models.BooleanField(default=False, editable=False)
    take_leads = models.BooleanField(default=False, editable=False)
    qualify_own_leads = models.BooleanField(default=False, editable=False)
    edit_own_leads = models.BooleanField(default=False, editable=False)
    transition_own_leads = models.BooleanField(default=False, editable=False)
    edit_own_deals = models.BooleanField(default=False, editable=False)
    transition_own_deals = models.BooleanField(default=False, editable=False)
    create_own_deals = models.BooleanField(default=False, editable=False)
    manage_own_next_action = models.BooleanField(default=False, editable=False)

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["organization", "user"], name="crm_access_org_user")]


class LeadEvidenceQuerySet(CommercialQuerySet):
    def delete(self):
        raise ValidationError("Lead provenance cannot be deleted.")


class LeadEvidenceModel(CommercialModel):
    objects = LeadEvidenceQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "objects"

    def delete(self, *args, **kwargs):
        raise ValidationError("Lead provenance cannot be deleted.")


class CrmLeadTaskLink(LeadEvidenceModel):
    """Retained lead task provenance; tasks use their existing privacy/lifecycle."""
    immutable_scope = ("organization_id", "lead_id", "task_id", "created_by_id")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    lead = models.ForeignKey(CrmLead, on_delete=models.PROTECT, related_name="task_links")
    task = models.OneToOneField("pool_service.ServiceTask", on_delete=models.PROTECT, related_name="commercial_lead_link")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        from pool_service.models import ServiceTask
        using = self._state.db or router.db_for_write(type(self), instance=self)
        task = ServiceTask.objects.using(using).filter(pk=self.task_id).first()
        if (not task or task.organization_id != self.organization_id or task.crm_item_id
                or task.water_reading_id or task.pool_id or task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP):
            raise ValidationError("Lead tasks must be independent CRM follow-ups in this organization.")
        if CrmDealTaskLink.objects.using(using).filter(task_id=self.task_id).exists():
            raise ValidationError("A lead task cannot be a deal task.")


class CrmLeadSource(LeadEvidenceModel):
    immutable_scope = ("organization_id", "inbox_id", "lead_id")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    inbox = models.OneToOneField("pool_service.CrmInboxEvent", on_delete=models.PROTECT, related_name="lead_source")
    lead = models.ForeignKey(CrmLead, on_delete=models.PROTECT, related_name="sources")
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        from pool_service.crm_inbox_models import CrmInboxEvent
        using = self._state.db or router.db_for_write(type(self), instance=self)
        if not CrmInboxEvent.objects.using(using).filter(pk=self.inbox_id, organization_id=self.organization_id).exists():
            raise ValidationError("Lead inbox evidence belongs to another organization.")


class CrmLeadThread(LeadEvidenceModel):
    immutable_scope = ("organization_id", "context_key")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    context_key = models.CharField(max_length=64, validators=[RegexValidator(r"^[0-9a-f]{64}\Z", "Expected scoped context hash.")])
    lead = models.ForeignKey(CrmLead, on_delete=models.PROTECT, related_name="threads")

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["organization", "context_key"], name="crm_lead_thread_context")]


class CrmLeadDealLink(LeadEvidenceModel):
    immutable_scope = ("organization_id", "lead_id", "deal_id", "command_id", "kind")
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    lead = models.ForeignKey(CrmLead, on_delete=models.PROTECT, related_name="deal_links")
    deal = models.ForeignKey(CrmDeal, on_delete=models.PROTECT, related_name="lead_links")
    command = models.ForeignKey(CrmCommand, on_delete=models.PROTECT, related_name="lead_deal_links")
    kind = models.CharField(max_length=16, choices=[("qualification", "Квалификация"), ("additional", "Дополнительная"), ("existing", "Существующая")])
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        base_manager_name = "objects"
        constraints = [models.UniqueConstraint(fields=["lead", "deal"], name="crm_lead_deal_once")]


class CrmIntakeActivation(CommercialModel):
    """Independent new-intake cutoff; never reuses the commercial pilot marker."""
    immutable_scope = ("organization_id", "starts_at")
    organization = models.OneToOneField("pool_service.Organization", on_delete=models.PROTECT, related_name="crm_intake_activation")
    starts_at = models.DateTimeField(default=timezone.now)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=40, blank=True)
