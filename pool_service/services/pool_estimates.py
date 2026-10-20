"""Deterministic v1 worksheet. Unknown prices and engineering data stay unknown."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_UP
from datetime import datetime
import re

from . import estimate_embedded as embedded
from . import estimate_works as works

from pool_service.services.permissions import organization_accesses_for_user

VARIANTS = {"economy": ("Эконом", "Aquaviva"), "standard": ("Стандарт", "Hayward"), "premium": ("Премиум", "IML")}
BLOCKS = {"construction": "Строительство чаши", "embedded": "Закладные элементы", "equipment": "Оборудование", "finish": "Отделка", "options": "Дополнительные работы"}
KINDS = {"other", "work", "pump", "filter", "heater", *embedded.ROLES}
FINISHES = {
    "film": ("Плёнка ПВХ для бассейна (+15% запас)", "Монтаж и сварка плёнки ПВХ"),
    "tile": ("Плитка для бассейна (без запаса)", "Облицовка бассейна плиткой"),
    "mosaic": ("Мозаика для бассейна (без запаса)", "Облицовка бассейна мозаикой"),
}
FINISH_CHANGED = "Заменён тип отделки"
CENT = Decimal("0.01")


class EstimateInputError(ValueError):
    pass


def allowed_organizations(user):
    if not user or not user.is_authenticated or not user.is_active:
        return []
    # Explicit organization membership even for a superuser: no implicit tenant switch.
    return [a.organization for a in organization_accesses_for_user(user) if a.role in {"owner", "admin", "manager"}]


def number(value, label, *, minimum="0", maximum="1000000000", optional=False, places=3):
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 40:
        raise EstimateInputError(f"{label}: требуется число.")
    try:
        result = Decimal(str(value).replace(",", "."))
        if not result.is_finite() or not Decimal(minimum) <= result <= Decimal(maximum):
            raise InvalidOperation
        return result.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise EstimateInputError(f"{label}: допустимо от {minimum} до {maximum}.") from None


def string(value, label, max_length=200, required=False):
    if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
        raise EstimateInputError(f"{label}: заполните текст до {max_length} символов.")
    return value.strip()


def boolean(value):
    if not isinstance(value, bool):
        raise EstimateInputError("Некорректный переключатель.")
    return value


def normalize_inputs(raw):
    if not isinstance(raw, dict):
        raise EstimateInputError("Заполните параметры бассейна.")
    data = {"title": string(raw.get("title", ""), "Название расчёта", required=True)}
    for key, label, low, high in [
        ("length", "Длина", ".1", "100"), ("width", "Ширина", ".1", "100"),
        ("depth_start", "Глубина в начале", ".1", "10"), ("depth_end", "Глубина в конце", ".1", "10"),
        ("turnover_hours", "Время оборота воды", ".5", "24"),
    ]:
        data[key] = str(number(raw.get(key), label, minimum=low, maximum=high))
    data["required_head"] = _optional(number(raw.get("required_head"), "Расчётный напор", optional=True, minimum=".1", maximum="100"))
    data["heating_power"] = _optional(number(raw.get("heating_power"), "Мощность подогрева", optional=True, minimum=".1", maximum="10000"))
    # Missing fields in old saved versions stay missing; never inject new assumptions.
    for key, label, low, high in (
        ("water_start", "Начальная температура воды", "0", "45"),
        ("water_target", "Нужная температура воды", "1", "45"),
        ("heating_hours", "Время прогрева", "1", "336"),
        ("heating_reserve", "Запас мощности", "0", "100"),
    ):
        data[key] = _optional(number(raw.get(key), label, optional=True, minimum=low, maximum=high))
    for key, default, label in (('skimmer_area', '25', 'Площадь на скиммер'), ('skimmer_flow', '5', 'Расход на скиммер'), ('inlet_flow', '3', 'Расход на форсунку')):
        data[key] = str(number(raw.get(key, default), label, minimum='.1', maximum='100'))
    data["finish"] = string(raw.get("finish", ""), "Отделка", 20)
    if data["finish"] not in {"", *FINISHES}:
        raise EstimateInputError("Выберите отделку: плёнка, плитка или мозаика.")
    for key, choices in {"location": {"indoor", "outdoor"}, "heating": {"none", "exchanger", "electric", "heatpump"}, "power": {"230", "400", "unknown"}}.items():
        if not isinstance(raw.get(key), str) or raw[key] not in choices:
            raise EstimateInputError(f"Выберите значение поля {key}.")
        data[key] = raw[key]
    data["address"] = string(raw.get("address", ""), "Место установки", 300)
    data["notes"] = string(raw.get("notes", ""), "Примечание", 1000)
    if data["heating"] != "none" and not data["heating_power"] and data["water_start"] is not None and data["water_target"] is not None and Decimal(data["water_target"]) <= Decimal(data["water_start"]):
        raise EstimateInputError("Для прогрева нужная температура должна быть выше начальной.")
    return data


def geometry(data):
    length, width, d1, d2, hours = (Decimal(data[k]) for k in ("length", "width", "depth_start", "depth_end", "turnover_hours"))
    average = (d1 + d2) / 2
    floor = (length * length + (d2 - d1) ** 2).sqrt() * width
    finish_area = floor + 2 * length * average + width * (d1 + d2)
    return {key: str(value.quantize(Decimal(".001"), rounding=ROUND_HALF_UP)) for key, value in {
        "volume": length * width * average, "surface": length * width,
        "perimeter": 2 * (length + width), "finish_area": finish_area,
        "finish_net": Decimal(embedded.ceil_quantity(finish_area)),
        "finish_stock": Decimal(embedded.ceil_quantity(finish_area * Decimal("1.15"))),
        "required_flow": length * width * average / hours,
    }.items()}


def _optional(value):
    return str(value) if value is not None else None


def heating_requirement(inputs):
    """First-heating energy, with an explicit user reserve; not a heat-loss model.

    P = 1.16 * V * delta-T / hours. Pahlen electric-heater sizing formula.
    Nominal heat-pump/exchanger output still needs checking at the site conditions.
    """
    if inputs["heating"] == "none":
        return {"mode": "none", "power": None}
    if inputs.get("heating_power"):
        return {"mode": "manual", "power": inputs["heating_power"]}
    keys = ("water_start", "water_target", "heating_hours", "heating_reserve")
    if any(inputs.get(k) is None for k in keys):
        return {"mode": "incomplete", "power": None}
    start, target, hours, reserve = (Decimal(inputs[k]) for k in keys)
    volume = Decimal(inputs["length"]) * Decimal(inputs["width"]) * (Decimal(inputs["depth_start"]) + Decimal(inputs["depth_end"])) / 2
    location_factor = Decimal("1.5") if inputs["location"] == "outdoor" else Decimal(1)
    power = Decimal("1.16") * volume * (target - start) / hours * (1 + reserve / 100) * location_factor
    return {"mode": "estimated", "power": str(power.quantize(Decimal(".001"), rounding=ROUND_UP))}


def price_summary(snapshot):
    """Conservative date for active, unchanged catalogue prices in all three CPs.

    Searching newer products must not relabel the prices of a saved estimate.
    Also usable on legacy snapshots without re-evaluating historical totals.
    """
    dates, manual = [], False
    for variant in snapshot["variants"].values():
        for block in variant["blocks"]:
            for row in block["rows"]:
                if block["excluded"] or row["excluded"] or row["existing"] or row["price"] is None or Decimal(row["price"]) <= 0:
                    continue
                origin = row.get("catalog")
                if not origin or Decimal(row["price"]) != Decimal(origin["price"]):
                    manual = True
                    continue
                dates.append(datetime.fromisoformat(origin["checked_at"]))
    return {"checked_at": min(dates).isoformat() if dates else None,
            "mixed": len({d.date() for d in dates}) > 1, "manual": manual}


def finish_rows(finish, metrics, variant="economy", include_works=True):
    names = FINISHES.get(finish, ("Материал отделки (без запаса)", "Работы по отделке"))
    rows = []
    for key, name in zip(('finish_material', 'finish_work'), names):
        basis = 'finish_stock' if finish == 'film' and key == 'finish_material' else 'finish_net'
        rows.append(make_row(key, name, 'м²', embedded.ceil_quantity(metrics[basis]), basis))
    if finish == 'film':
        rows.append(make_row('finish_geotextile', 'Геотекстиль под плёнку (+15% запас)', 'м²', embedded.ceil_quantity(metrics['finish_stock']), 'finish_stock'))
    for row in rows:
        row["finish_type"] = finish
    works.identify(rows[1])
    rows[1]["price"] = works.finish_rate(finish, variant) if include_works else None
    return rows + (works.finish_extras(finish, metrics, variant) if include_works else [])


def change_finish(snapshot, finish, replace=True):
    """Keep previous material/rate as reversible excluded rows, never reuse its price."""
    for variant_key, variant in snapshot["variants"].items():
        block = next(b for b in variant["blocks"] if b["id"] == "finish")
        for row in block["rows"] if replace else []:
            managed = row.get("finish_type") or re.fullmatch(r"finish_(?:material|work|geotextile)(?:_(?:film|tile|mosaic)?_\d+)?", row["id"])
            if managed and row.get("finish_type", "") != finish and not row["excluded"] and not row["existing"]:
                row.update(excluded=True, reason=FINISH_CHANGED)
            elif row.get("finish_type") == finish and row["excluded"] and row["reason"] == FINISH_CHANGED:
                row.update(excluded=False, reason="")
        for original in finish_rows(finish, geometry(snapshot["inputs"]), variant_key, include_works=bool(snapshot.get("works_catalog"))):
            base = original["id"]
            # A manually excluded row of this type remains excluded, without a duplicate.
            if any(r.get("finish_type") == finish and (r["id"] == base or r["id"].startswith(base + "_")) for r in block["rows"]):
                continue
            ids = {r["id"] for b in variant["blocks"] for r in b["rows"]}
            original["id"] = next((f"{base}_{finish}_{n}" for n in range(100) if f"{base}_{finish}_{n}" not in ids), "")
            if not original["id"] or len(block["rows"]) >= 100:
                raise EstimateInputError("В блоке отделки недостаточно места для новых позиций.")
            block["rows"].append(original)


def make_row(key, name, unit="компл.", quantity="1", basis="", kind="other", brand=""):
    return {"id": key, "name": name, "unit": unit, "quantity": quantity, "price": None,
            "basis": basis, "auto_quantity": bool(basis), "kind": kind, "brand": brand, "model": "",
            "excluded": False, "reason": "", "existing": False,
            "specs": {"flow": None, "head": None, "filter_flow": None, "backwash_flow": None}}


def generate(raw):
    inputs = normalize_inputs(raw)
    metrics = geometry(inputs)
    variants = {}
    for key, (_, brand) in VARIANTS.items():
        rowsets = {
            "construction": [make_row("bowl_material", "Материалы для чаши")],
            "embedded": [make_row("embedded_work", "Установка закладных элементов и обвязка", kind="work")],
            "equipment": [make_row("pump", "Фильтрационный насос", kind="pump", brand=brand), make_row("filter", "Фильтр", kind="filter", brand=brand),
                          make_row("treatment", "Обеззараживание: подобрать комплект"), make_row("piping", "Обвязка и автоматика"), make_row("equipment_work", "Монтаж и пусконаладка")],
            "finish": finish_rows(inputs["finish"], metrics, key),
            "options": [],
        }
        if inputs["heating"] != "none":
            rowsets["equipment"].insert(2, make_row("heater", "Подогрев: подобрать мощность и модель", kind="heater"))
        variants[key] = {"embedded_profile": embedded.DEFAULTS[key], "blocks": [{"id": bid, "title": title, "excluded": False, "reason": "", "rows": rowsets[bid]} for bid, title in BLOCKS.items()]}
    result = {"schema": 1, "inputs": inputs, "variants": variants, "geometry": metrics}
    works.add_works(result, fresh=True)
    embedded.ensure_rows(result)
    embedded.update_quantities(result)
    return evaluate(result)


def normalize_catalog(raw):
    if raw is None:
        return None
    from .estimate_catalog import safe_shop_url, safe_image_url
    if not isinstance(raw, dict) or not re.fullmatch(r"\d{1,12}", str(raw.get("id", ""))):
        raise EstimateInputError("Некорректная ссылка на товар каталога.")
    url = safe_shop_url(raw.get("url"))
    if not url or raw.get("selection") not in ("auto", "manual") or raw.get("power") not in ("230", "400", "unknown") or raw.get("heater_type") not in ("", "exchanger", "electric", "heatpump"):
        raise EstimateInputError("Некорректные данные товара магазина.")
    result = {"id": str(raw["id"]), "url": url, "selection": raw["selection"], "power": raw["power"], "heater_type": raw["heater_type"]}
    result["image"] = safe_image_url(raw.get("image", ""))
    options = raw.get("power_options", [raw["power"]] if raw["power"] != "unknown" else [])
    if not isinstance(options, list) or len(options) > 2 or any(p not in ("230", "400") for p in options) or len(set(options)) != len(options):
        raise EstimateInputError("Некорректные варианты питания товара.")
    result["power_options"] = options[:]
    result["titanium"] = boolean(raw.get("titanium", False))
    result['embedded_role'] = string(raw.get('embedded_role', ''), 'Тип закладной', 20)
    result['material'] = string(raw.get('material', ''), 'Материал закладной', 20)
    lining = raw.get('lining', [])
    if result['embedded_role'] not in {'', *embedded.ROLES} or result['material'] not in {'', 'plastic', 'steel304', 'steel316'} or not isinstance(lining, list) or len(lining) > 2 or any(x not in ('film', 'concrete') for x in lining):
        raise EstimateInputError('Некорректные характеристики закладной.')
    result['lining'] = lining[:]
    for field, limit in (("checked_at", 50), ("feed_date", 50), ("sku", 80)):
        result[field] = string(raw.get(field, ""), field, limit)
    try:
        if datetime.fromisoformat(result["checked_at"]).tzinfo is None:
            raise ValueError
    except ValueError:
        raise EstimateInputError("Некорректная дата получения цены магазина.") from None
    result["price"] = str(number(raw.get("price"), "Цена магазина", minimum=".01", places=2))
    for field in ("nominal_flow", "heating_power"):
        result[field] = _optional(number(raw.get(field), "Характеристика магазина", minimum=".001", maximum="100000", optional=True))
    for field in ("available", "missing"):
        result[field] = boolean(raw.get(field, False))
    return result


def normalize_snapshot(raw):
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise EstimateInputError("Неизвестный формат расчёта.")
    result = {"schema": 1, "inputs": normalize_inputs(raw.get("inputs")), "variants": {}, "works_catalog": 1 if raw.get("works_catalog") == 1 else 0}
    variants = raw.get("variants")
    if not isinstance(variants, dict) or set(variants) != set(VARIANTS):
        raise EstimateInputError("В расчёте должны быть три варианта.")
    for key in VARIANTS:
        variant = variants[key]
        blocks = variant.get("blocks") if isinstance(variant, dict) else None
        if not isinstance(blocks, list) or len(blocks) != len(BLOCKS):
            raise EstimateInputError("Некорректный состав блоков.")
        block_ids, row_ids, cleaned = set(), set(), []
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("id"), str) or block["id"] not in BLOCKS or block["id"] in block_ids:
                raise EstimateInputError("Некорректный блок.")
            block_ids.add(block["id"])
            rows = block.get("rows")
            if not isinstance(rows, list) or len(rows) > 100:
                raise EstimateInputError("В блоке допускается до 100 строк.")
            clean = {"id": block["id"], "title": BLOCKS[block["id"]], "excluded": boolean(block.get("excluded")), "reason": string(block.get("reason", ""), "Причина исключения", 300), "rows": []}
            for row in rows:
                if not isinstance(row, dict):
                    raise EstimateInputError("Некорректная строка.")
                rid = row.get("id")
                if not isinstance(rid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", rid) or rid in row_ids:
                    raise EstimateInputError("Строки должны иметь уникальные идентификаторы.")
                row_ids.add(rid)
                if not isinstance(row.get("kind"), str) or row["kind"] not in KINDS or not isinstance(row.get("basis", ""), str) or row.get("basis", "") not in {"", "finish_area", "finish_net", "finish_stock", "surface", "perimeter", "volume", "embedded_work", *embedded.ROLES}:
                    raise EstimateInputError("Некорректный тип строки.")
                item = {"id": rid, "kind": row["kind"], "basis": row.get("basis", "")}
                item['embedded_role'] = string(row.get('embedded_role', ''), 'Тип закладной', 20)
                item['embedded_profile'] = string(row.get('embedded_profile', ''), 'Комплектация закладной', 30)
                if item['embedded_role'] not in {'', *embedded.ROLES} or item['embedded_profile'] not in {'', *embedded.PROFILES}:
                    raise EstimateInputError('Некорректная комплектация закладной.')
                item["finish_type"] = string(row.get("finish_type", ""), "Тип отделки", 20)
                if item["finish_type"] not in {"", *FINISHES}:
                    raise EstimateInputError("Некорректный тип отделки строки.")
                for field, limit in (("name", 200), ("unit", 20), ("brand", 80), ("model", 120), ("reason", 300)):
                    item[field] = string(row.get(field, ""), field, limit, required=field in {"name", "unit"})
                for field in ("excluded", "existing", "auto_quantity", "selection_locked"):
                    item[field] = boolean(row.get(field, False))
                item["work_code"] = string(row.get("work_code", ""), "Код работы", 80)
                works.identify(item)
                item["quantity"] = _optional(number(row.get("quantity"), "Количество", minimum=".001", maximum="1000000", optional=item["kind"] == "work"))
                item["price"] = _optional(number(row.get("price"), "Цена", optional=True, places=2))
                specs = row.get("specs", {})
                if not isinstance(specs, dict):
                    raise EstimateInputError("Некорректные характеристики.")
                item["specs"] = {field: _optional(number(specs.get(field), "Характеристика оборудования", minimum=".001", maximum="100000", optional=True)) for field in ("flow", "head", "filter_flow", "backwash_flow")}
                item["catalog"] = normalize_catalog(row.get("catalog"))
                clean["rows"].append(item)
            cleaned.append(clean)
        profile = string(variant.get('embedded_profile', embedded.DEFAULTS[key]), 'Комплектация закладных', 30)
        if profile not in embedded.PROFILES:
            raise EstimateInputError('Выберите комплектацию закладных.')
        result["variants"][key] = {"blocks": cleaned, 'embedded_profile': profile}
    return result


def evaluate(raw):
    """Recompute derived fields on the server; never trust client totals/warnings."""
    from .estimate_compatibility import annotate
    result = normalize_snapshot(raw)
    result["geometry"] = geometry(result["inputs"])
    result["heating"] = heating_requirement(result["inputs"])
    embedded.update_quantities(result)
    result["prices"] = price_summary(result)
    for key, variant in result["variants"].items():
        total, missing, models, missing_quantities = Decimal(0), 0, 0, 0
        physical = []
        for block in variant["blocks"]:
            subtotal = Decimal(0)
            for row in block["rows"]:
                included = not block["excluded"] and not row["excluded"]
                if included or row["existing"]:
                    physical.append(row)
                charged = included and not row["existing"]
                amount = (Decimal(row["quantity"]) * Decimal(row["price"])).quantize(CENT, rounding=ROUND_HALF_UP) if charged and row["price"] is not None and row["quantity"] is not None else None
                row["amount"] = _optional(amount)
                if charged and row["price"] is None:
                    missing += 1
                if charged and row["quantity"] is None:
                    missing_quantities += 1
                if charged and row["kind"] not in {"other", "work"} and not row["model"]:
                    models += 1
                if amount is not None:
                    subtotal += amount
            block["subtotal"] = str(subtotal.quantize(CENT))
            total += subtotal
        warnings = []
        embedded_block = next(b for b in variant['blocks'] if b['id'] == 'embedded')
        if not embedded_block['excluded']:
            unresolved = [embedded.ROLES[r['embedded_role']] for r in embedded_block['rows'] if not r['excluded'] and not r['existing'] and r.get('embedded_role') and not r['model']]
            if unresolved:
                warnings.append('Закладные: не найдены подходящие товары для ' + embedded.PROFILES[variant['embedded_profile']] + ' и выбранной отделки: ' + ', '.join(unresolved) + '. Количества рассчитаны, цены требуют уточнения.')
            warnings.append('Количество закладных предварительное: по площади зеркала и расходу фильтрации. Проверьте пропускную способность конкретных моделей и размещение форсунок. Два донных слива не заменяют проверку безопасной схемы всасывания и расстояния между ними.')
        variant['embedded_counts'] = embedded.counts(result['inputs'], result['geometry'], variant)
        for row in physical:
            origin = row.get("catalog")
            if not origin:
                continue
            label = row["name"]
            if row.get('embedded_role'):
                if not embedded.matches({**origin, 'brand': row['brand']}, variant['embedded_profile'], result['inputs']['finish']):
                    warnings.append(f'{label}: материал, отделка или доступность не соответствуют выбранному комплекту закладных либо не подтверждены. Сохранена прежняя позиция; проверьте замену.')
            if row["kind"] == "heater" and origin["selection"] == "auto":
                if key == "economy" and row["brand"] != "Aquaviva":
                    warnings.append(f"{label}: в экономе используется альтернатива Aquaviva. Приоритет Aquaviva действует среди недорогих моделей с минимальной достаточной мощностью; выбранный товар можно заменить вручную.")
                if key == "premium" and not origin["titanium"]:
                    warnings.append(f"{label}: для премиума титан не подтверждён названием товара. Материал уточните перед заказом; модель можно заменить вручную.")
            if origin["missing"]:
                warnings.append(f"{label}: товар исчез из выгрузки магазина; сохранённая цена требует подтверждения.")
            elif not origin["available"]:
                warnings.append(f"{label}: магазин не подтверждает доступность к заказу.")
            if row["kind"] in {"pump", "heater"} and origin["heater_type"] != "exchanger":
                options = origin["power_options"] if row["kind"] == "heater" else ([origin["power"]] if origin["power"] != "unknown" else [])
                if result["inputs"]["power"] == "unknown" or not options:
                    warnings.append(f"{label}: подтвердите напряжение питания.")
                elif result["inputs"]["power"] not in options:
                    warnings.append(f"{label}: питание не соответствует выбранному для объекта.")
            if row["kind"] == "pump" and origin["nominal_flow"] and Decimal(origin["nominal_flow"]) < Decimal(result["geometry"]["required_flow"]):
                warnings.append(f"{label}: производительность из каталога ниже требуемого расхода.")
            if row["kind"] == "heater" and origin["heater_type"] != result["inputs"]["heating"]:
                warnings.append(f"{label}: тип подогрева не соответствует параметрам бассейна.")
            if row["kind"] == "heater" and result["heating"]["power"] and origin["heating_power"] and Decimal(origin["heating_power"]) < Decimal(result["heating"]["power"]):
                warnings.append(f"{label}: мощность ниже заданной для подогрева.")
        if missing_quantities:
            warnings.append(f"Не указаны объёмы работ: {missing_quantities}. Заполните их по проекту; эти строки не включены в сумму.")
        if missing:
            warnings.append(f"Не указаны цены: {missing}. Показана только сумма заполненных строк.")
        if models:
            warnings.append(f"Не выбраны модели оборудования: {models}.")
        pumps, filters = ([r for r in physical if r["kind"] == kind] for kind in ("pump", "filter"))
        equipment_in_scope = any(not b["excluded"] and b["id"] == "equipment" for b in variant["blocks"])
        if equipment_in_scope and (not pumps or not filters):
            warnings.append("Для проверки фильтрации нужны насос и фильтр: добавьте их либо отметьте существующее оборудование.")
        if len(pumps) == len(filters) == 1 and all(Decimal(r["quantity"]) == 1 for r in pumps + filters):
            pump, filt = pumps[0]["specs"], filters[0]["specs"]
            head = result["inputs"]["required_head"]
            if not all((pump["flow"], pump["head"], filt["filter_flow"], head)) or Decimal(pump["head"] or 0) != Decimal(head or 0):
                warnings.append("Совместимость не подтверждена: укажите расход насоса при расчётном напоре и допустимый расход фильтра. Максимальный расход из названия модели не подходит.")
                nominal = (pumps[0].get("catalog") or {}).get("nominal_flow")
                if nominal and filt["filter_flow"] and Decimal(nominal) > Decimal(filt["filter_flow"]):
                    warnings.append("Предварительно по каталогу: производительность насоса выше допустимого расхода фильтра. Рассмотрите больший фильтр и подтвердите рабочую точку насоса.")
            else:
                if Decimal(pump["flow"]) > Decimal(filt["filter_flow"]):
                    warnings.append("Расход насоса выше допустимого для фильтра. Подберите больший фильтр либо пересчитайте режим насоса.")
                if Decimal(pump["flow"]) < Decimal(result["geometry"]["required_flow"]):
                    warnings.append("Расход насоса ниже требуемого для заданного времени оборота воды.")
            warnings.append("Обратную промывку, трубопроводы и рабочие режимы должен проверить инженер по кривым оборудования; эти проверки пока не автоматизированы.")
        elif pumps or filters:
            warnings.append("Несколько насосов/фильтров или неполный комплект: требуется расчёт гидравлической схемы.")
        if result["inputs"]["heating"] != "none":
            if equipment_in_scope and not any(r["kind"] == "heater" and r["model"] for r in physical):
                warnings.append("Нагреватель не подобран: в каталоге не найдена доступная модель с подтверждёнными мощностью, типом и питанием. Проверьте параметры либо выберите модель через каталог.")
            if result["heating"]["mode"] == "incomplete":
                warnings.append("Для подбора нагревателя задайте мощность либо температуры, время прогрева и запас.")
            elif result["heating"]["mode"] == "estimated":
                warnings.append("Мощность подогрева рассчитана предварительно по времени прогрева с заданным запасом. Теплопотери, особенно у открытого бассейна, рассчитываются отдельно; запас их не заменяет.")
            if result["inputs"]["heating"] == "exchanger":
                warnings.append("Для теплообменника подтвердите мощность при фактической температуре и расходе теплоносителя, а также мощность котла.")
            elif result["inputs"]["heating"] == "heatpump":
                warnings.append("Для теплового насоса подтвердите тепловую мощность при расчётной температуре воздуха и воды; номинальная мощность может быть выше фактической.")
            else:
                warnings.append("Для электронагревателя подтвердите выделенную электрическую мощность и минимальный расход воды.")
        if any(r["kind"] == "heater" for r in physical) and result["inputs"]["heating"] == "none":
            warnings.append("Выбран режим без подогрева, но нагреватель остался в составе. Исключите его, если он не нужен.")
        if result["inputs"]["finish"] and any(b["id"] == "finish" and not b["excluded"] for b in variant["blocks"]):
            warnings.append("Отделка: плёнка и геотекстиль с запасом 15%, работы без запаса; расчётные количества округляются вверх до целого м². Подготовка основания и расходники уточняются отдельно.")
        variant.update(total=str(total.quantize(CENT)), missing_prices=missing, missing_quantities=missing_quantities, missing_models=models, warnings=warnings, title=VARIANTS[key][0], preferred_brand="Дорогие модели каталога" if key == "premium" else VARIANTS[key][1])
        annotate(result, variant)
    return result


def recalculate(raw, inputs):
    """Keep exclusions and manual edits; update only explicitly automatic quantities."""
    result = normalize_snapshot(raw)
    old_finish = result["inputs"]["finish"]
    result["inputs"] = normalize_inputs(inputs)
    # Also adds geotextile when recalculating a legacy film estimate.
    change_finish(result, result["inputs"]["finish"], replace=old_finish != result["inputs"]["finish"])
    metrics = geometry(result["inputs"])
    for variant in result["variants"].values():
        for block in variant["blocks"]:
            for row in block["rows"]:
                if row["kind"] == "heater" and not row["existing"]:
                    if result["inputs"]["heating"] == "none" and not row["excluded"]:
                        row.update(excluded=True, reason="Подогрев отключён")
                    elif result["inputs"]["heating"] != "none" and row["excluded"] and row["reason"] == "Подогрев отключён":
                        row.update(excluded=False, reason="")
                if row['auto_quantity'] and row['basis'] in metrics and not row['existing']:
                    if row.get('finish_type') == 'film' and row['id'].startswith(('finish_material', 'finish_geotextile')):
                        row['basis'] = 'finish_stock'
                    elif row['id'].startswith(('finish_material', 'finish_work')):
                        row['basis'] = 'finish_net'
                    row['quantity'] = embedded.ceil_quantity(metrics[row['basis']])
    result['geometry'] = metrics
    embedded.ensure_rows(result)
    embedded.update_quantities(result)
    return evaluate(result)
