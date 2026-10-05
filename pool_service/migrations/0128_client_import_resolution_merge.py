from django.conf import settings
from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone
import django.db.models.deletion


OWNER_DECISIONS = {
    # Юридические лица
    "НФ-017647": "legal",   # Добрая Банька
    "НФ-017726": "legal",   # Новая Волна
    "НФ-017754": "legal",   # Бирюзовая катунь
    "НФ-018043": "legal",   # Атлантика
    "НФ-018046": "legal",   # ВТБ24
    "НФ-018098": "legal",   # Рикки-Тикки
    "НФ-018403": "legal",   # Бонифаций
    "НФ-018417": "legal",   # Спа Комсомольский 40
    "НФ-018459": "legal",   # Сантехника Алтай
    "НФ-018471": "legal",   # Водный Мир
    "НФ-018483": "legal",   # Лесотель
    "НФ-018487": "legal",   # ООО Термариум Актру
    "НФ-018505": "legal",   # Акрил
    "НФ-018630": "legal",   # Аэрофлот
    # Физические лица
    "НФ-018031": "private", # Андреев Иван
    "НФ-018047": "private", # Анопко Александр Михайлович
    "НФ-018664": "private", # Ирина (подарок для дочери)
    "НФ-018675": "private", # Воронько Павел
    # ИП
    "НФ-018251": "ip",      # ИП Ямщиков Алексей Владимирович
    # Не импортировать
    "НФ-017913": "skip",    # Расчеты по карте
    "НФ-018042": "skip",    # Наше предприятие
    "НФ-018241": "skip",    # Алиэкспресс
    "НФ-018452": "skip",    # Интернет-магазин
    "НФ-018476": "skip",    # Прочие
}


def apply_owner_decisions(apps, schema_editor):
    Candidate = apps.get_model("pool_service", "ClientImportCandidate")
    ImportRun = apps.get_model("pool_service", "ClientImportRun")
    now = timezone.now()
    touched_org_ids = set()

    for source_code, resolution in OWNER_DECISIONS.items():
        status = "skipped" if resolution == "skip" else "ready"
        reason = (
            "Не импортировать — решение владельца"
            if resolution == "skip"
            else "Тип подтверждён владельцем"
        )
        qs = Candidate.objects.filter(source_code=source_code, applied_at__isnull=True)
        touched_org_ids.update(qs.values_list("organization_id", flat=True))
        qs.update(
            resolution=resolution,
            resolution_note="Решение владельца от 05.10.2026",
            resolved_at=now,
            status=status,
            reason=reason,
        )

    for organization_id in touched_org_ids:
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


def reverse_owner_decisions(apps, schema_editor):
    Candidate = apps.get_model("pool_service", "ClientImportCandidate")
    for source_code in OWNER_DECISIONS:
        Candidate.objects.filter(
            source_code=source_code,
            applied_at__isnull=True,
            resolution_note="Решение владельца от 05.10.2026",
        ).update(
            resolution="auto",
            resolution_note="",
            resolved_by=None,
            resolved_at=None,
            status="review",
            reason="Требуется повторная проверка после отката решения",
        )


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
