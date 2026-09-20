from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0106_payroll_plan_snapshot"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="employee",
            options={
                "ordering": ["display_name", "id"],
                "permissions": [
                    ("view_employee_hr", "Can view employee HR records"),
                ],
            },
        ),
    ]
