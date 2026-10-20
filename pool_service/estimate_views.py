import json
from datetime import datetime
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from pool_service.services.estimate_embedded import change_profile
from pool_service.services.estimate_work_rates import apply_defaults, confirm_price
from pool_service.estimate_models import PoolEstimate, PoolEstimateVersion
from pool_service.services.pool_estimates import (
    EstimateInputError, allowed_organizations, evaluate, generate, geometry, normalize_inputs, price_summary, recalculate, string, VARIANTS,
)
from pool_service.services.estimate_catalog import (
    CatalogUnavailable, apply_product, auto_select, catalog_status, get_catalog,
    refresh_prices, search_catalog,
)
from decimal import Decimal


def _organizations(user):
    return {str(org.pk): org for org in allowed_organizations(user)}


@login_required
@never_cache
@require_http_methods(["GET"])
def estimate_page(request):
    organizations = _organizations(request.user)
    if not organizations:
        return render(request, "403.html", status=403)
    return render(request, "pool_service/estimates/calculator.html", {
        "active_tab": "estimates", "estimate_organizations": organizations.values(),
    })


def _positive_int(value, allow_zero=False):
    if type(value) is not int or value < (0 if allow_zero else 1) or value > 2147483647:
        raise EstimateInputError("Некорректный номер версии.")
    return value


def _load(organization, identifier):
    try:
        uid = UUID(str(identifier))
    except (ValueError, TypeError, AttributeError):
        raise EstimateInputError("Некорректный номер расчёта.") from None
    return get_object_or_404(PoolEstimate, pk=uid, organization=organization)


def _version_data(estimate, version):
    return {"id": str(estimate.pk), "revision": estimate.revision, "loaded_version": version.number,
            "snapshot": {**version.snapshot, "prices": price_summary(version.snapshot)}, "versions": list(estimate.versions.values("number", "note", "created_at"))}


@login_required
@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def estimate_api(request):
    try:
        if request.method == "POST":
            if request.content_type != "application/json" or len(request.body) > 1500000:
                return JsonResponse({"error": "Нужен JSON размером не более 1,5 МБ."}, status=400)
            try:
                data = json.loads(request.body)
            except (ValueError, UnicodeDecodeError):
                raise EstimateInputError("Некорректный JSON.") from None
            if not isinstance(data, dict):
                raise EstimateInputError("Некорректный запрос.")
        else:
            data = request.GET
        organizations = _organizations(request.user)
        organization = organizations.get(str(data.get("organization", "")))
        if organization is None:
            return JsonResponse({"error": "Нет доступа к расчётам этой организации."}, status=403)
        if request.method == "GET":
            if data.get("id"):
                estimate = _load(organization, data["id"])
                try:
                    number = int(data.get("version") or estimate.revision)
                except (ValueError, TypeError):
                    raise EstimateInputError("Некорректный номер версии.") from None
                version = get_object_or_404(estimate.versions, number=number)
                return JsonResponse(_version_data(estimate, version))
            query = string(data.get("q", ""), "Поиск", 200)
            projects = PoolEstimate.objects.filter(organization=organization, title__icontains=query)
            return JsonResponse({"projects": list(projects.values("id", "title", "revision", "updated_at")[:50]), "limit": 50})
        action = data.get("action")
        if action == "generate":
            snapshot = apply_defaults(generate(data.get("inputs")), organization)
            try:
                catalog = get_catalog()
                snapshot, count = auto_select(snapshot, catalog)
                return JsonResponse({"snapshot": snapshot, "catalog_status": catalog_status(catalog), "selected_count": count})
            except CatalogUnavailable as exc:
                return JsonResponse({"snapshot": snapshot, "catalog_error": str(exc)})
        if action == 'catalog_images':
            ids = data.get('ids')
            if not isinstance(ids, list) or len(ids) > 1500 or any(not isinstance(i, str) or not i.isdigit() or len(i) > 12 for i in ids):
                raise EstimateInputError('Некорректный список товаров.')
            products = get_catalog()['products']
            return JsonResponse({'images': {i: products[i].get('image', '') for i in set(ids) if i in products}})
        if action == "catalog_search":
            inputs = normalize_inputs(data.get("inputs"))
            return JsonResponse(search_catalog(get_catalog(), data.get("q", ""), data.get("kind", ""), data.get("brand", ""), inputs, Decimal(geometry(inputs)["required_flow"])))
        if action == 'compatibility_auto':
            from .services.estimate_compatibility import repair
            variant = string(data.get('variant'), 'Вариант', 20)
            bid = string(data.get('block'), 'Блок', 30)
            rid = string(data.get('row'), 'Строка', 64)
            snapshot = evaluate(data.get('snapshot'))
            if variant not in VARIANTS:
                raise EstimateInputError('Выберите смету.')
            row = next((r for b in snapshot['variants'][variant]['blocks'] if b['id'] == bid for r in b['rows'] if r['id'] == rid), None)
            if not row or not (row.get('compatibility') or {}).get('can_auto'):
                raise EstimateInputError('Для этой позиции автоматическое исправление недоступно.')
            catalog = get_catalog() if row['compatibility']['kind'] == 'filter' else None
            response = {'snapshot': repair(snapshot, variant, bid, rid, catalog)}
            if catalog:
                response['catalog_status'] = catalog_status(catalog)
            return JsonResponse(response)
        if action in ("catalog_auto", "catalog_apply", "catalog_prices"):
            snapshot = evaluate(data.get("snapshot"))
            catalog = get_catalog()
            if action == "catalog_auto":
                snapshot, count = auto_select(snapshot, catalog)
                return JsonResponse({"snapshot": snapshot, "selected_count": count, "catalog_status": catalog_status(catalog)})
            variant = string(data.get("variant"), "Вариант", 20)
            if variant not in VARIANTS:
                raise EstimateInputError("Выберите смету.")
            if action == "catalog_prices":
                snapshot, updated, skipped = refresh_prices(snapshot, catalog, variant)
                return JsonResponse({"snapshot": snapshot, "updated": updated, "skipped": skipped, "catalog_status": catalog_status(catalog)})
            product_id = string(data.get("product_id"), "Товар", 12)
            product = catalog["products"].get(product_id)
            if not product:
                raise EstimateInputError("Товар отсутствует в текущей выгрузке магазина.")
            bid = string(data.get("block"), "Блок", 30)
            rid = string(data.get("row"), "Строка", 64)
            row = next((r for b in snapshot["variants"][variant]["blocks"] if b["id"] == bid for r in b["rows"] if r["id"] == rid), None)
            if row is None:
                raise EstimateInputError("Сначала добавьте строку в смету.")
            apply_product(row, product, catalog)
            return JsonResponse({"snapshot": evaluate(snapshot), "catalog_status": catalog_status(catalog)})
        if action == "recalculate":
            previous_rows = {(k, r["id"]) for k, v in evaluate(data.get("snapshot"))["variants"].items() for b in v["blocks"] for r in b["rows"]}
            snapshot = apply_defaults(recalculate(data.get("snapshot"), data.get("inputs")), organization, only_new=previous_rows)
            try:
                catalog = get_catalog()
                snapshot, count = auto_select(snapshot, catalog)
                return JsonResponse({"snapshot": snapshot, "catalog_status": catalog_status(catalog), "selected_count": count})
            except CatalogUnavailable as exc:
                return JsonResponse({"snapshot": snapshot, "catalog_error": str(exc)})
        if action == 'embedded_profile':
            variant = string(data.get('variant'), 'Вариант', 20)
            snapshot = change_profile(data.get('snapshot'), variant, data.get('profile'))
            try:
                catalog = get_catalog()
                snapshot, count = auto_select(snapshot, catalog, only_variant=variant)
                return JsonResponse({'snapshot': snapshot, 'selected_count': count, 'catalog_status': catalog_status(catalog)})
            except CatalogUnavailable as exc:
                return JsonResponse({'snapshot': snapshot, 'catalog_error': str(exc)})
        if action == 'add_works':
            from .services.estimate_works import add_works, fill_missing_prices
            from .services.pool_estimates import change_finish
            snapshot = evaluate(data.get('snapshot'))
            priced_rows = {(k, r["id"]) for k, v in snapshot["variants"].items() for b in v["blocks"] for r in b["rows"] if r['price'] is not None or r['existing']}
            add_works(snapshot)
            change_finish(snapshot, snapshot['inputs']['finish'], replace=False)
            filled = fill_missing_prices(snapshot)
            return JsonResponse({'snapshot': apply_defaults(snapshot, organization, only_new=priced_rows), 'filled_prices': filled})
        if action == 'work_price':
            return JsonResponse(confirm_price(data, organization, request.user))
        if action == "validate":
            return JsonResponse({"snapshot": evaluate(data.get("snapshot"))})
        if action != "save":
            raise EstimateInputError("Неизвестное действие.")
        snapshot = evaluate(data.get("snapshot"))
        note = string(data.get("note", ""), "Комментарий версии", 300)
        expected = _positive_int(data.get("expected_revision"), allow_zero=True)
        with transaction.atomic():
            if data.get("id"):
                loaded = _load(organization, data["id"])
                estimate = PoolEstimate.objects.select_for_update().get(pk=loaded.pk, organization=organization)
                if estimate.revision != expected:
                    return JsonResponse({"error": "Расчёт изменён в другой вкладке. Сохраните свои правки отдельным расчётом или откройте последнюю версию.", "conflict": True}, status=409)
            else:
                if expected != 0:
                    raise EstimateInputError("Новый расчёт должен начинаться с версии 0.")
                estimate = PoolEstimate.objects.create(organization=organization, title=snapshot["inputs"]["title"], created_by=request.user)
            estimate.revision += 1
            estimate.title = snapshot["inputs"]["title"]
            estimate.updated_at = timezone.now()
            estimate.save(update_fields=["revision", "title", "updated_at"])
            version = PoolEstimateVersion.objects.create(estimate=estimate, number=estimate.revision, snapshot=snapshot, note=note, created_by=request.user)
        return JsonResponse(_version_data(estimate, version), status=201)
    except EstimateInputError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@login_required
@never_cache
@require_http_methods(["GET"])
def estimate_print(request, estimate_id, number):
    organizations = _organizations(request.user)
    estimate = get_object_or_404(PoolEstimate, pk=estimate_id, organization_id__in=organizations)
    version = get_object_or_404(estimate.versions, number=number)
    prices = price_summary(version.snapshot)
    return render(request, "pool_service/estimates/print.html", {"estimate": estimate, "version": version,
        "prices_date": datetime.fromisoformat(prices["checked_at"]) if prices["checked_at"] else None,
        "prices_mixed": prices["mixed"], "prices_manual": prices["manual"]})
