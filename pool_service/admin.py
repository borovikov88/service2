from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from .models import Client, Pool, WaterReading, Organization, PoolAccess, OrganizationAccess
from django.utils.html import format_html

# Если модель WaterReading уже зарегистрирована, отрегестрируем её
try:
    admin.site.unregister(WaterReading)
except admin.sites.NotRegistered:
    pass

class PoolAccessInline(admin.TabularInline):  # Или StackedInline для другого стиля
    model = PoolAccess
    extra = 1  # Количество пустых форм для добавления новых доступов
    
class OrganizationAccessInline(admin.TabularInline):
    model = OrganizationAccess
    extra = 1

class WaterReadingResource(resources.ModelResource):
    class Meta:
        model = WaterReading
        # Здесь можно указать поля, если нужно:
        # fields = ('id', 'pool', 'date', 'ph', 'chlorine', 'temperature', ...)

@admin.register(WaterReading)
class WaterReadingAdmin(ImportExportModelAdmin):
    resource_class = WaterReadingResource
    
@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name",)
    inlines = [OrganizationAccessInline]

@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = ('client', 'address', 'organization')
    inlines = [PoolAccessInline]  # Добавляем вложенную админку для доступов

    def formatted_description(self, obj):
        if obj.description:  # Проверяем, есть ли описание
            return format_html(obj.description)
        return "-"  # Если пусто, показываем прочерк
    formatted_description.short_description = "Описание бассейна"
    list_filter = ("organization",)
    search_fields = ('address', 'description')  # Добавляем поиск по описанию

#@admin.register(PoolAccess)
#class PoolAccessAdmin(admin.ModelAdmin):
#    list_display = ("user", "pool", "role")
#    list_filter = ("pool", "role")
#    search_fields = ("user__username", "pool__address")

#@admin.register(OrganizationAccess)
#class OrganizationAccessAdmin(admin.ModelAdmin):
#    list_display = ("user", "organization", "role")
#    list_filter = ("organization", "role")
#    search_fields = ("user__username", "organization__name")

admin.site.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('name', 'last_name', 'address')

