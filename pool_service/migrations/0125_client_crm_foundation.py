# Generated manually for the CRM client import foundation.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0124_callanalysis_processing"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ClientCRMProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("legal_form", models.CharField(blank=True, choices=[("", "Не указано"), ("entity", "Юридическое лицо"), ("ip", "ИП")], default="", max_length=16)),
                ("middle_name", models.CharField(blank=True, max_length=120)),
                ("birth_date", models.DateField(blank=True, null=True)),
                ("legal_name", models.CharField(blank=True, max_length=500)),
                ("kpp", models.CharField(blank=True, max_length=20)),
                ("ogrn", models.CharField(blank=True, max_length=20)),
                ("onec_ref", models.CharField(blank=True, max_length=36, null=True, unique=True)),
                ("onec_code", models.CharField(blank=True, max_length=64)),
                ("onec_name", models.CharField(blank=True, max_length=500)),
                ("source", models.CharField(choices=[("manual", "Вручную"), ("onec", "1С"), ("onec_ip", "1С / ИП"), ("phonebook", "Телефонная книга")], default="manual", max_length=20)),
                ("notes", models.TextField(blank=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="crm_profile", to="pool_service.client")),
                ("manager", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="crm_managed_clients", to=settings.AUTH_USER_MODEL)),
                ("responsible", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="crm_responsible_clients", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "CRM-профиль клиента", "verbose_name_plural": "CRM-профили клиентов"},
        ),
        migrations.CreateModel(
            name="ClientContact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("phone", "Телефон"), ("email", "Email")], max_length=12)),
                ("value", models.CharField(max_length=255)),
                ("match_value", models.CharField(blank=True, db_index=True, max_length=255)),
                ("label", models.CharField(blank=True, max_length=120)),
                ("is_primary", models.BooleanField(default=False)),
                ("sources", models.JSONField(blank=True, default=list)),
                ("source_reference", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="crm_contacts", to="pool_service.client")),
            ],
            options={"ordering": ["kind", "-is_primary", "id"]},
        ),
        migrations.CreateModel(
            name="ClientCompanyLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.CharField(blank=True, max_length=160)),
                ("roles", models.JSONField(blank=True, default=list)),
                ("is_primary", models.BooleanField(default=False)),
                ("source", models.CharField(choices=[("manual", "Вручную"), ("onec_ip", "1С / ИП")], default="manual", max_length=20)),
                ("source_reference", models.CharField(blank=True, max_length=120)),
                ("automatic", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="person_links", to="pool_service.client")),
                ("person", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="company_links", to="pool_service.client")),
            ],
        ),
        migrations.CreateModel(
            name="ClientImportCandidate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_ref", models.CharField(max_length=36)),
                ("source_code", models.CharField(blank=True, max_length=64)),
                ("source_kind", models.CharField(choices=[("legal", "Юридическое лицо"), ("ip", "ИП"), ("private", "Физическое лицо")], max_length=16)),
                ("name", models.CharField(max_length=500)),
                ("legal_name", models.CharField(blank=True, max_length=500)),
                ("inn", models.CharField(blank=True, max_length=20)),
                ("kpp", models.CharField(blank=True, max_length=20)),
                ("phone", models.CharField(blank=True, max_length=80)),
                ("email", models.CharField(blank=True, max_length=255)),
                ("birth_date", models.DateField(blank=True, null=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("ready", "Готов к импорту"), ("review", "Нужно проверить"), ("duplicate", "Возможный дубль"), ("invalid", "Некорректные данные"), ("imported", "Импортирован")], default="review", max_length=16)),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("matched_client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="import_candidates", to="pool_service.client")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="client_import_candidates", to="pool_service.organization")),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddConstraint(
            model_name="clientcontact",
            constraint=models.UniqueConstraint(fields=("client", "kind", "value"), name="crm_contact_client_kind_value_uniq"),
        ),
        migrations.AddIndex(
            model_name="clientcontact",
            index=models.Index(fields=["client", "kind"], name="crm_contact_client_kind_idx"),
        ),
        migrations.AddConstraint(
            model_name="clientcompanylink",
            constraint=models.UniqueConstraint(fields=("company", "person"), name="crm_company_person_uniq"),
        ),
        migrations.AddIndex(
            model_name="clientcompanylink",
            index=models.Index(fields=["company", "source"], name="crm_company_link_source_idx"),
        ),
        migrations.AddIndex(
            model_name="clientcompanylink",
            index=models.Index(fields=["person"], name="crm_person_link_idx"),
        ),
        migrations.AddConstraint(
            model_name="clientimportcandidate",
            constraint=models.UniqueConstraint(fields=("organization", "source_ref"), name="crm_import_org_ref_uniq"),
        ),
        migrations.AddIndex(
            model_name="clientimportcandidate",
            index=models.Index(fields=["organization", "status"], name="crm_import_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="clientimportcandidate",
            index=models.Index(fields=["organization", "source_kind"], name="crm_import_org_kind_idx"),
        ),
    ]
