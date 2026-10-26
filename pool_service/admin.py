from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import Client, Pool, WaterReading, Organization, PoolAccess, OrganizationAccess
from .communication_models import (
    CommunicationAccess,
    ChannelConnection,
    CommunicationChannel,
    Conversation,
    PhoneCall,
    TelephonyConnection,
)
from django.utils.html import format_html

# Inline classes
class PoolAccessInline(admin.TabularInline):
    model = PoolAccess
    extra = 1

class OrganizationAccessInline(admin.TabularInline):
    model = OrganizationAccess
    extra = 1

# Import/export for WaterReading
class WaterReadingResource(resources.ModelResource):
    def __init__(self, *args, source_actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_actor = source_actor
        self.source_org_ids = None
        self.source_preflight_success = False
        self.prelocked_reading_ids = set()

    def import_data(self, dataset, dry_run=False, raise_errors=False, use_transactions=None,
                    collect_failed_rows=False, rollback_on_validation_errors=False, **kwargs):
        # This resource alone requires all-or-nothing source batches, including
        # validation errors and dry-run preview. Other importers are unchanged.
        self.source_preflight_success = False
        self.prelocked_reading_ids = set()
        self.source_org_ids = None
        return super().import_data(dataset, dry_run=dry_run, raise_errors=raise_errors,
            use_transactions=True, collect_failed_rows=collect_failed_rows,
            rollback_on_validation_errors=True, **kwargs)

    def before_import(self, dataset, **kwargs):
        # Existing privileged batch writer: acquire every possible org mutex
        # in one sorted set before any per-row task/source locks are held.
        from django.db import transaction
        if not transaction.get_connection().in_atomic_block:
            return
        # Use the same Field/widget decoder as ModelInstanceLoader. Numeric
        # aliases such as "12.0" must address the same prelocked Reading as 12.
        reading_ids, pool_ids = set(), set()
        for row in dataset.dict:
            id_field, pool_field = self.fields['id'], self.fields['pool']
            if id_field.column_name in row:
                reading_id = id_field.clean(row)
                if reading_id is not None:
                    reading_ids.add(reading_id)
            if pool_field.column_name in row:
                pool = pool_field.clean(row)
                if pool is not None:
                    pool_ids.add(getattr(pool, 'pk', pool))
        pool_ids.update(WaterReading.objects.filter(pk__in=reading_ids).values_list('pool_id', flat=True))
        self.source_org_ids = set(Pool.objects.filter(pk__in=pool_ids).values_list('organization_id', flat=True)) - {None}
        list(Organization.objects.select_for_update().filter(pk__in=self.source_org_ids).order_by('pk'))
        from pool_service.services.crm_object_needs import lock_source_graphs
        graph = lock_source_graphs(reading_ids)
        self.source_graph = {reading_id: [(task.pk, task.crm_item_id) for task in tasks] for reading_id, tasks in graph.items()}
        locked_pools = list(Pool.objects.select_for_update().filter(pk__in=pool_ids).order_by('pk'))
        if any(pool.organization_id and pool.organization_id not in self.source_org_ids for pool in locked_pools):
            from django.core.exceptions import ValidationError
            raise ValidationError('Source organization changed before import locks.')
        list(Client.objects.select_for_update().filter(pk__in={pool.client_id for pool in locked_pools}).order_by('pk'))
        locked_readings = list(WaterReading.objects.select_for_update().filter(pk__in=reading_ids).order_by('pk'))
        if any(reading.pool_id not in pool_ids for reading in locked_readings):
            from django.core.exceptions import ValidationError
            raise ValidationError('Source pool changed before import locks.')
        # Supported source writers share the org mutex. The Reading locks also
        # block a later FK insertion; a pre-existing extra candidate fails closed.
        current = {}
        from .models import ServiceTask
        for reading_id, task_id, item_id in ServiceTask.objects.filter(water_reading_id__in=reading_ids, task_type='supply_request').order_by('pk').values_list('water_reading_id', 'pk', 'crm_item_id'):
            current.setdefault(reading_id, []).append((task_id, item_id))
        if current != self.source_graph:
            from django.core.exceptions import ValidationError
            raise ValidationError('Source candidate graph changed before import locks.')
        self.prelocked_reading_ids = {reading.pk for reading in locked_readings}
        self.source_preflight_success = True

    def _require_source_preflight(self):
        if not self.source_preflight_success:
            from django.core.exceptions import ValidationError
            raise ValidationError('Source import preflight failed; no row may be saved.')

    def before_import_row(self, row, **kwargs):
        # django-import-export records a before_import error but still loops
        # rows when raise_errors=False. Stop before parsing/saving any row.
        self._require_source_preflight()
        id_field = self.fields['id']
        reading_id = id_field.clean(row) if id_field.column_name in row else None
        self._require_prelocked_reading(reading_id)
        return super().before_import_row(row, **kwargs)

    def _require_prelocked_reading(self, reading_id):
        if (reading_id is not None and reading_id not in self.prelocked_reading_ids
                and WaterReading.objects.filter(pk=reading_id).exists()):
            from django.core.exceptions import ValidationError
            raise ValidationError('Existing Reading was not prelocked; retry the whole batch.')

    def before_save_instance(self, instance, row, **kwargs):
        self._require_source_preflight()
        self._require_prelocked_reading(instance.pk)
        # Do not acquire an unplanned org lock out of sorted order if a pool
        # was reparented after identifying the batch. Reject and roll back.
        pool_org = Pool.objects.filter(pk=instance.pool_id).values_list('organization_id', flat=True).first()
        old_org = WaterReading.objects.filter(pk=instance.pk).values_list('pool__organization_id', flat=True).first() if instance.pk else None
        if self.source_org_ids is not None and any(org_id and org_id not in self.source_org_ids for org_id in (pool_org, old_org)):
            from django.core.exceptions import ValidationError
            raise ValidationError('Source organization changed during import; retry the whole batch.')
        instance._source_actor_id = self.source_actor.pk if self.source_actor else None

    class Meta:
        model = WaterReading

# WaterReading admin
@admin.register(WaterReading)
class WaterReadingAdmin(ImportExportModelAdmin):
    resource_class = WaterReadingResource

    def get_import_resource_kwargs(self, request, **kwargs):
        return {**super().get_import_resource_kwargs(request, **kwargs), 'source_actor': request.user}

    def save_model(self, request, obj, form, change):
        obj._source_actor_id = request.user.pk
        super().save_model(request, obj, form, change)

# Organization admin
@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name",)
    inlines = [OrganizationAccessInline]

# Pool admin
@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = ('client', 'address', 'organization')
    inlines = [PoolAccessInline]
    list_filter = ("organization",)
    search_fields = ('address', 'description')

    def formatted_description(self, obj):
        if obj.description:
            return format_html(obj.description)
        return "-"
    formatted_description.short_description = "Описание объекта"

# Client admin
@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('name',)


class CommunicationSuperuserAdmin(admin.ModelAdmin):
    """Provider and cross-organization communication data is superuser-only."""

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


admin.site.register(CommunicationChannel, CommunicationSuperuserAdmin)
admin.site.register(ChannelConnection, CommunicationSuperuserAdmin)
admin.site.register(CommunicationAccess, CommunicationSuperuserAdmin)
admin.site.register(Conversation, CommunicationSuperuserAdmin)
admin.site.register(TelephonyConnection, CommunicationSuperuserAdmin)
admin.site.register(PhoneCall, CommunicationSuperuserAdmin)
# Credentials are provisioned through the dedicated command and are not exposed
# in Django admin, where encrypted values could otherwise be overwritten.
