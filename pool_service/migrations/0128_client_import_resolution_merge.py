from django.conf import settings
from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone
import django.db.models.deletion


OWNER_DECISIONS = {
    "НФ-017647": "legal",
    "НФ-017726": "legal",
    "НФ-017754": "legal",
    "НФ-018043": "legal",
    "НФ-018046": "legal",
    "НФ-018098": "legal",
    "НФ-018403": "legal",
    "НФ-018417": "legal",
    "НФ-018459": "legal",
    "НФ-018471": "legal",
    "НФ-018483": "legal",
    "НФ-018487": "legal",
    "НФ-018505": "legal",
    "НФ-018630": "legal",
    "НФ-018031": "private",
    "НФ-018047": "private",
    "НФ-018664": "private",
    "НФ-018675": "private",
    "НФ-018251": "ip",
    "НФ-017913": "skip",
    "НФ-018042": "skip",
    "НФ-018241": "skip",
    "НФ-018452": "skip",
    "НФ-018476": "skip",
}

BACKUP_KEY = "_migration_0128_resolution_backup"


def _refresh_run_counts(Candidate, ImportRun, organization_ids):
    for organization_id in organization_ids:
        counts = {
            item["status"]: item["count"]
            for item in Candidate.objects.filter(
                organization_id=organization_id
            ).values("status").annotate(count=Count("id"))
        }
        latest = (
            ImportRun.objects.filter(
                organization_id=organization_id,
                status="success",
            )
            .order_by("-requested_at", "-id")
            .first()
        )
        if latest:
            latest.ready_count = counts.get("ready", 0)
            latest.review_count = counts.get("review", 0)
            latest.duplicate_count = counts.get("duplicate", 0)
            latest.invalid_count = counts.get("invalid", 0) + counts.get("skipped", 0)
            latest.imported_count = counts.get("imported", 0)
            latest.save(
                update_fields=[
                    "ready_count",
                    "review_count",
                    "duplicate_count",
                    "invalid_count",
                    "imported_count",
                    "updated_at",
                ]
            )


def apply_owner_decisions(apps, schema_editor):
    Candidate = apps.get_model("pool_service", "ClientImportCandidate")
    ImportRun = apps.get_model("pool_service", "ClientImportRun")
    now = timezone.now()
    touched_org_ids = set()

    for source_code, resolution in OWNER_DECISIONS.items():
        candidates = Candidate.objects.filter(
            source_code=source_code,
            applied_at__isnull=True,
        )
        for candidate in candidates.iterator():
            touched_org_ids.add(candidate.organization_id)
            payload = dict(candidate.payload or {})
            if BACKUP_KEY not in payload:
                payload[BACKUP_KEY] = {
                    "status": candidate.status,
                    "reason": candidate.reason,
                    "matched_client_id": candidate.matched_client_id,
                }

            candidate.payload = payload
            candidate.resolution = resolution
            candidate.resolution_note = "Подтверждено владельцем 05.10.2026"
            candidate.resolved_at = now
            candidate.resolved_by_id = None
            if resolution == "skip":
                candidate.status = "skipped"
                candidate.reason = "Не импортировать — решение владельца"
            else:
                candidate.status = "ready"
                candidate.reason = "Тип подтверждён владельцем"
                candidate.matched_client_id = None
            candidate.save(
                update_fields=[
                    "payload",
                    "resolution",
                    "resolution_note",
                    "resolved_at",
                    "resolved_by",
                    "status",
                    "reason",
                    "matched_client",
                    "updated_at",
                ]
            )

    _refresh_run_counts(Candidate, ImportRun, touched_org_ids)


def reverse_owner_decisions(apps, schema_editor):
    Candidate = apps.get_model("pool_service", "ClientImportCandidate")
    ImportRun = apps.get_model("pool_service", "ClientImportRun")
    touched_org_ids = set()

    for source_code in OWNER_DECISIONS:
        candidates = Candidate.objects.filter(
            source_code=source_code,
            applied_at__isnull=True,
            resolution_note="Подтверждено владельцем 05.10.2026",
        )
        for candidate in candidates.iterator():
            payload = dict(candidate.payload or {})
            backup = payload.pop(BACKUP_KEY, None)
            if not backup:
                continue
            touched_org_ids.add(candidate.organization_id)
            candidate.payload = payload
            candidate.resolution = "auto"
            candidate.resolution_note = ""
            candidate.resolved_by_id = None
            candidate.resolved_at = None
            candidate.status = backup.get("status") or "review"
            candidate.reason = backup.get("reason") or ""
            candidate.matched_client_id = backup.get("matched_client_id")
            candidate.save(
                update_fields=[
                    "payload",
                    "resolution",
                    "resolution_note",
                    "resolved_by",
                    "resolved_at",
                    "status",
                    "reason",
                    "matched_client",
                    "updated_at",
                ]
            )

    _refresh_run_counts(Candidate, ImportRun, touched_org_ids)


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0127_client_import_run"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="clientcrmprofile",
            name="merged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientcrmprofile",
            name="merged_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="merged_client_profiles",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="clientcrmprofile",
            name="merged_into",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="merged_legacy_profiles",
                to="pool_service.client",
            ),
        ),
        migrations.AlterField(
            model_name="clientimportrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "В очереди"),
                    ("running", "Загружается"),
                    ("applying", "Импортируются клиенты"),
                    ("success", "Готово"),
                    ("failed", "Ошибка"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="clientimportcandidate",
            name="status",
            field=models.CharField(
                choices=[
                    ("ready", "Готов к импорту"),
                    ("review", "Нужно проверить"),
                    ("duplicate", "Возможный дубль"),
                    ("invalid", "Некорректные данные"),
                    ("skipped", "Не импортировать"),
                    ("imported", "Импортирован"),
                ],
                default="review",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="clientimportcandidate",
            name="resolution",
            field=models.CharField(
                choices=[
                    ("auto", "По данным 1С"),
                    ("legal", "Юридическое лицо"),
                    ("private", "Физическое лицо"),
                    ("ip", "ИП"),
                    ("skip", "Не импортировать"),
                ],
                default="auto",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="clientimportcandidate",
            name="resolution_note",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="clientimportcandidate",
            name="resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clientimportcandidate",
            name="resolved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resolved_client_import_candidates",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(apply_owner_decisions, reverse_owner_decisions),
    ]
