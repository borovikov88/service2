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
    class Meta:
        model = WaterReading

# WaterReading admin
@admin.register(WaterReading)
class WaterReadingAdmin(ImportExportModelAdmin):
    resource_class = WaterReadingResource

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
