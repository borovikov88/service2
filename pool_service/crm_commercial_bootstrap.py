"""Proposed section 7 settings. Existing names/order/activation are never reset."""
from django.db import transaction
from pool_service.crm_commercial_models import CrmPipeline, CrmStage
from pool_service.models import Organization

PROPOSED_PIPELINES = (
    ("construction", "Строительство и монтаж", (
        ("need", "Потребность уточнена"), ("technical", "Собираем технические данные"),
        ("proposal", "Готовим решение и расчёт"), ("sent", "Предложение отправлено"),
        ("agreement", "Согласование"), ("order", "Оформление заказа и старта"),
        ("execution", "Исполнение"), ("reconciliation", "Завершение и расчёты"),
        ("completed", "Успешно завершено"))),
    ("service", "Обслуживание", (
        ("request", "Запрос уточнён"), ("assessment", "Оценка объекта"),
        ("proposal", "Предложение и согласование"), ("contract", "Договор заключён"),
        ("active", "Действующее обслуживание"), ("reconciliation", "Завершение и сверка"),
        ("completed", "Успешно завершено"))),
    ("retail", "Розничная продажа", (
        ("need", "Потребность"), ("selection", "Подбор и наличие"),
        ("proposal", "Предложение клиенту"), ("order", "Подтверждение заказа"),
        ("fulfillment", "Выполнение заказа и расчёты"), ("completed", "Успешно завершено"))),
    ("tender", "Торги", (
        ("review", "Проверяем закупку"), ("assessment", "Оцениваем условия и экономику"),
        ("preparation", "Готовим заявку"), ("submitted", "Заявка подана"),
        ("result", "Ожидаем результат"), ("contract", "Победа и оформление контракта"),
        ("execution", "Контракт в исполнении"), ("completed", "Завершено"))),
)


@transaction.atomic
def bootstrap_commercial_settings(organization_id):
    # Serialize concurrent runs within this organization on both supported DBs.
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    created_pipelines = created_stages = 0
    for position, (code, name, stages) in enumerate(PROPOSED_PIPELINES):
        pipeline, created = CrmPipeline.objects.get_or_create(
            organization=organization, code=code,
            defaults={"name": name, "position": position})
        created_pipelines += int(created)
        for stage_position, (stage_code, stage_name) in enumerate(stages):
            _, created = CrmStage.objects.get_or_create(
                pipeline=pipeline, code=stage_code,
                defaults={"name": stage_name, "position": stage_position})
            created_stages += int(created)
    return created_pipelines, created_stages
