from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import Client, Pool, WaterReading, Organization, PoolAccess, OrganizationAccess
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
