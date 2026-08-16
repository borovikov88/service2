from django.db import migrations, models
from django.db.models import Count


class RemoveConstraintIfExists(migrations.RemoveConstraint):
    """Remove migration state even when a backend skipped the physical constraint."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        with schema_editor.connection.cursor() as cursor:
            constraints = schema_editor.connection.introspection.get_constraints(
                cursor, model._meta.db_table
            )
        if self.name not in constraints:
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


def normalize_and_validate_identity_keys(apps, schema_editor):
    Employee = apps.get_model("pool_service", "Employee")
    Identity = apps.get_model("pool_service", "EmployeeOneCIdentity")

    identity_fields = ("onec_employee_id", "personnel_number", "source_identity_key")
    changed = []
    for identity in Identity.objects.only("pk", *identity_fields).iterator(chunk_size=500):
        dirty = False
        for field in identity_fields:
            value = getattr(identity, field)
            normalized = (str(value).strip() or None) if value is not None else None
            if value != normalized:
                setattr(identity, field, normalized)
                dirty = True
        if dirty:
            changed.append(identity)
    if changed:
        Identity.objects.bulk_update(changed, identity_fields, batch_size=500)

    checks = (
        (Employee, "user_id", "unique_employee_user_per_org"),
        (Identity, "onec_employee_id", "unique_onec_employee_id_per_org"),
        (Identity, "personnel_number", "unique_personnel_number_per_org"),
        (Identity, "source_identity_key", "unique_source_employee_per_org"),
    )
    for model, field, constraint_name in checks:
        duplicate = (
            model.objects.exclude(**{f"{field}__isnull": True})
            .values("organization_id", field)
            .annotate(row_count=Count("pk"))
            .filter(row_count__gt=1)
            .order_by("organization_id", field)
            .first()
        )
        if duplicate:
            raise RuntimeError(
                f"Cannot create {constraint_name}: duplicate non-NULL value "
                f"for organization_id={duplicate['organization_id']}."
            )


def restore_source_identity_empty_strings(apps, schema_editor):
    Identity = apps.get_model("pool_service", "EmployeeOneCIdentity")
    Identity.objects.filter(source_identity_key__isnull=True).update(source_identity_key="")


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0093_management_finance_foundation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="employeeonecidentity",
            name="source_identity_key",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.RunPython(
            normalize_and_validate_identity_keys,
            restore_source_identity_empty_strings,
        ),
        RemoveConstraintIfExists(
            model_name="employee",
            name="unique_employee_user_per_org",
        ),
        RemoveConstraintIfExists(
            model_name="employeeonecidentity",
            name="unique_onec_employee_id_per_org",
        ),
        RemoveConstraintIfExists(
            model_name="employeeonecidentity",
            name="unique_personnel_number_per_org",
        ),
        RemoveConstraintIfExists(
            model_name="employeeonecidentity",
            name="unique_source_employee_per_org",
        ),
        migrations.AddConstraint(
            model_name="employee",
            constraint=models.UniqueConstraint(
                fields=("organization", "user"),
                name="unique_employee_user_per_org",
            ),
        ),
        migrations.AddConstraint(
            model_name="employeeonecidentity",
            constraint=models.UniqueConstraint(
                fields=("organization", "onec_employee_id"),
                name="unique_onec_employee_id_per_org",
            ),
        ),
        migrations.AddConstraint(
            model_name="employeeonecidentity",
            constraint=models.UniqueConstraint(
                fields=("organization", "personnel_number"),
                name="unique_personnel_number_per_org",
            ),
        ),
        migrations.AddConstraint(
            model_name="employeeonecidentity",
            constraint=models.UniqueConstraint(
                fields=("organization", "source_identity_key"),
                name="unique_source_employee_per_org",
            ),
        ),
    ]
