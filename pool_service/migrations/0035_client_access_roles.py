from django.db import migrations, models


def forwards(apps, schema_editor):
    ClientAccess = apps.get_model("pool_service", "ClientAccess")
    ClientInvite = apps.get_model("pool_service", "ClientInvite")
    ClientAccess.objects.filter(role="staff").update(role="editor")
    ClientInvite.objects.filter(role="staff").update(role="editor")


def backwards(apps, schema_editor):
    ClientAccess = apps.get_model("pool_service", "ClientAccess")
    ClientInvite = apps.get_model("pool_service", "ClientInvite")
    ClientAccess.objects.filter(role="editor").update(role="staff")
    ClientInvite.objects.filter(role="editor").update(role="staff")


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0034_organization_payment_request"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clientaccess",
            name="role",
            field=models.CharField(
                choices=[("viewer", "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440"), ("editor", "\u0420\u0435\u0434\u0430\u043a\u0442\u043e\u0440")],
                default="editor",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="clientinvite",
            name="role",
            field=models.CharField(
                choices=[("viewer", "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440"), ("editor", "\u0420\u0435\u0434\u0430\u043a\u0442\u043e\u0440")],
                default="editor",
                max_length=20,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
