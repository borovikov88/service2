from django.db import migrations


INACTIVE_ONEC_EMPLOYEE_IDS = {
    "4ae19340-f7ee-11ef-9ef8-fa163e1420a5",  # Алексеев Иван Алексеевич
    "51e9332c-2a62-11f0-8180-fa163e1420a5",  # Алена (техничка)
    "6f11b6fc-ca47-11ef-9fc3-fa163e1420a5",  # Гайдуков Иван Сергеевич
    "99792896-c4d2-11ef-9fc3-fa163e1420a5",  # Золотарева Елена Владимировна
    "4c285c90-c4d4-11ef-9fc3-fa163e1420a5",  # Микулич Кирилл Владимирович
    "e2014ef0-d95d-11ef-807a-fa163e1420a5",  # Надежкина Наталья Николаевна
    "a3d5898c-d8c4-11ef-807a-fa163e1420a5",  # Холудеев Георгий Павлович
    "c759a89a-8a1f-11f0-9d3c-fa163e1420a5",  # Щербаков Сергей Олегович
    "42ffba0c-d95e-11ef-807a-fa163e1420a5",  # Юдин Владислав Евгеньевич
}


def deactivate_confirmed_inactive_employees(apps, schema_editor):
    Employee = apps.get_model("pool_service", "Employee")
    EmployeeOneCIdentity = apps.get_model("pool_service", "EmployeeOneCIdentity")

    identities = EmployeeOneCIdentity.objects.filter(
        onec_employee_id__in=INACTIVE_ONEC_EMPLOYEE_IDS,
        employee_id__isnull=False,
    )
    employee_ids = set(identities.values_list("employee_id", flat=True))
    if employee_ids:
        Employee.objects.filter(pk__in=employee_ids).update(
            is_active=False,
            employment_status="dismissed",
        )
    identities.update(source_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0128_client_import_resolution_merge"),
    ]

    operations = [
        migrations.RunPython(
            deactivate_confirmed_inactive_employees,
            migrations.RunPython.noop,
        ),
    ]
