from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0149_avito_daily_statistics"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(name="CrmSourceLink", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("call_origin", models.CharField(choices=[("unknown", "Источник звонка неизвестен"), ("manual", "Авито: подтверждено менеджером")], default="unknown", max_length=16)),
        ("order_guid", models.UUIDField(blank=True, null=True)),
        ("order_evidence", models.JSONField(blank=True, default=dict, editable=False)),
        ("avito_item_id", models.CharField(blank=True, max_length=64)),
        ("ad_source", models.CharField(choices=[("unknown", "Источник неизвестен"), ("provider", "Контекст Авито"), ("manual", "Подтверждено менеджером")], default="unknown", max_length=16)),
        ("quality", models.CharField(choices=[("unknown", "Не оценено"), ("target", "Целевое"), ("non_target", "Нецелевое")], default="unknown", max_length=16)),
        ("reason", models.CharField(blank=True, max_length=500)),
        ("evidence", models.JSONField(blank=True, default=dict, editable=False)),
        ("confirmed_at", models.DateTimeField(blank=True, null=True)),
        ("revision", models.PositiveIntegerField(default=0)),
        ("last_operation", models.UUIDField(blank=True, editable=False, null=True)),
        ("last_operation_hash", models.CharField(blank=True, editable=False, max_length=64)),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("updated_at", models.DateTimeField(auto_now=True)),
        ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization")),
        ("avito_connection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="pool_service.channelconnection")),
        ("conversation", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="crm_source_link", to="pool_service.conversation")),
        ("phone_call", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="crm_source_link", to="pool_service.phonecall")),
        ("crm_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="source_links", to="pool_service.crmitem")),
        ("next_task", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="crm_source_followups", to="pool_service.servicetask")),
        ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_crm_sources", to=settings.AUTH_USER_MODEL)),
    ], options={
        "indexes": [models.Index(fields=["organization", "avito_connection"], name="crm_source_scope_idx")],
        "constraints": [models.CheckConstraint(condition=(models.Q(conversation__isnull=False, phone_call__isnull=True) | models.Q(conversation__isnull=True, phone_call__isnull=False)), name="crm_source_exactly_one")],
    })]
