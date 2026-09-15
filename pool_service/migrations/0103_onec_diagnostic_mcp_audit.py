from django.db import migrations


TABLE = "pool_service_onecdiagnosticmcpauditevent"


def create_audit_table(apps, schema_editor):
    q = schema_editor.quote_name
    vendor = schema_editor.connection.vendor
    if vendor == "mysql":
        id_column = f"{q('id')} bigint AUTO_INCREMENT NOT NULL PRIMARY KEY"
        text_type = "longtext"
        datetime_type = "datetime(6)"
    elif vendor == "postgresql":
        id_column = f"{q('id')} bigserial NOT NULL PRIMARY KEY"
        text_type = "text"
        datetime_type = "timestamp with time zone"
    else:
        id_column = f"{q('id')} integer NOT NULL PRIMARY KEY AUTOINCREMENT"
        text_type = "text"
        datetime_type = "datetime"

    schema_editor.execute(
        f"CREATE TABLE {q(TABLE)} ("
        f"{id_column}, "
        f"{q('principal_id')} bigint NULL, "
        f"{q('grant_id')} bigint NULL, "
        f"{q('authorized_by_id')} bigint NULL, "
        f"{q('target_organization_id')} bigint NULL, "
        f"{q('tool_name')} varchar(100) NOT NULL, "
        f"{q('entity_set')} varchar(300) NOT NULL DEFAULT '', "
        f"{q('selected_fields')} {text_type} NOT NULL, "
        f"{q('result')} varchar(16) NOT NULL, "
        f"{q('duration_ms')} integer NOT NULL DEFAULT 0, "
        f"{q('response_bytes')} integer NOT NULL DEFAULT 0, "
        f"{q('created_at')} {datetime_type} NOT NULL"
        ")"
    )
    schema_editor.execute(
        f"CREATE INDEX {q('onec_diag_audit_principal_idx')} "
        f"ON {q(TABLE)} ({q('principal_id')}, {q('created_at')})"
    )
    schema_editor.execute(
        f"CREATE INDEX {q('onec_diag_audit_tool_idx')} "
        f"ON {q(TABLE)} ({q('tool_name')}, {q('created_at')})"
    )


def drop_audit_table(apps, schema_editor):
    schema_editor.execute(f"DROP TABLE {schema_editor.quote_name(TABLE)}")


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0102_finance_position_snapshot"),
    ]

    operations = [
        migrations.RunPython(create_audit_table, drop_audit_table),
    ]
