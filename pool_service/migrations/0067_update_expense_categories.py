from django.db import migrations


TARGET_CATEGORIES = [
    "Материалы",
    "Работы",
    "Транспорт",
    "Инструмент",
    "Прочее",
]
LEGACY_CATEGORIES = ["Доставка", "Вода"]


def apply_categories(apps, schema_editor):
    Organization = apps.get_model("pool_service", "Organization")
    ExpenseCategory = apps.get_model("pool_service", "ExpenseCategory")
    for organization in Organization.objects.all():
        for sort_order, name in enumerate(TARGET_CATEGORIES, start=1):
            category, _ = ExpenseCategory.objects.get_or_create(
                organization=organization,
                name=name,
                defaults={"sort_order": sort_order, "is_active": True},
            )
            updates = {}
            if category.sort_order != sort_order:
                updates["sort_order"] = sort_order
            if not category.is_active:
                updates["is_active"] = True
            if updates:
                ExpenseCategory.objects.filter(pk=category.pk).update(**updates)
        ExpenseCategory.objects.filter(
            organization=organization,
            name__in=LEGACY_CATEGORIES,
            is_active=True,
        ).update(is_active=False)


def reverse_categories(apps, schema_editor):
    Organization = apps.get_model("pool_service", "Organization")
    ExpenseCategory = apps.get_model("pool_service", "ExpenseCategory")
    for organization in Organization.objects.all():
        for name in LEGACY_CATEGORIES:
            ExpenseCategory.objects.get_or_create(
                organization=organization,
                name=name,
                defaults={"sort_order": 0, "is_active": True},
            )
        ExpenseCategory.objects.filter(
            organization=organization,
            name__in=LEGACY_CATEGORIES,
        ).update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0066_accountabletransactionchange_and_more"),
    ]

    operations = [
        migrations.RunPython(apply_categories, reverse_categories),
    ]
