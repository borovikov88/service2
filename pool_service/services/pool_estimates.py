"""Deterministic v1 worksheet. Unknown prices and engineering data stay unknown."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from pool_service.services.permissions import organization_accesses_for_user

VARIANTS = {"economy": ("Эконом", "Aquaviva"), "standard": ("Стандарт", "Hayward"), "premium": ("Премиум", "IML")}
BLOCKS = {"construction": "Строительство чаши", "embedded": "Закладные элементы", "equipment": "Оборудование", "finish": "Отделка", "options": "Дополнительные работы"}
KINDS = {"other", "pump", "filter", "heater"}
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
    for key, choices in {"location": {"indoor", "outdoor"}, "heating": {"none", "exchanger", "electric", "heatpump"}, "power": {"230", "400", "unknown"}}.items():
        if not isinstance(raw.get(key), str) or raw[key] not in choices:
            raise EstimateInputError(f"Выберите значение поля {key}.")
        data[key] = raw[key]
    data["address"] = string(raw.get("address", ""), "Место установки", 300)
    data["notes"] = string(raw.get("notes", ""), "Примечание", 1000)
    return data


def geometry(data):
    length, width, d1, d2, hours = (Decimal(data[k]) for k in ("length", "width", "depth_start", "depth_end", "turnover_hours"))
    average = (d1 + d2) / 2
    floor = (length * length + (d2 - d1) ** 2).sqrt() * width
    return {key: str(value.quantize(Decimal(".001"), rounding=ROUND_HALF_UP)) for key, value in {
        "volume": length * width * average, "surface": length * width,
        "perimeter": 2 * (length + width), "finish_area": floor + 2 * length * average + width * (d1 + d2),
        "required_flow": length * width * average / hours,
    }.items()}


def _optional(value):
    return str(value) if value is not None else None


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
            "construction": [make_row("bowl_material", "Материалы для чаши"), make_row("bowl_work", "Строительство чаши")],
            "embedded": [make_row("embedded_supply", "Закладные: подобрать состав и количество"), make_row("embedded_work", "Монтаж закладных")],
            "equipment": [make_row("pump", "Фильтрационный насос", kind="pump", brand=brand), make_row("filter", "Фильтр", kind="filter", brand=brand),
                          make_row("treatment", "Обеззараживание: подобрать комплект"), make_row("piping", "Обвязка и автоматика"), make_row("equipment_work", "Монтаж и пусконаладка")],
            "finish": [make_row("finish_material", "Материал отделки (без запаса)", "м²", metrics["finish_area"], "finish_area"),
                       make_row("finish_work", "Работы по отделке", "м²", metrics["finish_area"], "finish_area")],
            "options": [],
        }
        if inputs["heating"] != "none":
            rowsets["equipment"].insert(2, make_row("heater", "Подогрев: подобрать мощность и модель", kind="heater"))
        variants[key] = {"blocks": [{"id": bid, "title": title, "excluded": False, "reason": "", "rows": rowsets[bid]} for bid, title in BLOCKS.items()]}
    return evaluate({"schema": 1, "inputs": inputs, "variants": variants})


def normalize_snapshot(raw):
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise EstimateInputError("Неизвестный формат расчёта.")
    result = {"schema": 1, "inputs": normalize_inputs(raw.get("inputs")), "variants": {}}
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
                if not isinstance(row.get("kind"), str) or row["kind"] not in KINDS or not isinstance(row.get("basis", ""), str) or row.get("basis", "") not in {"", "finish_area", "surface", "perimeter", "volume"}:
                    raise EstimateInputError("Некорректный тип строки.")
                item = {"id": rid, "kind": row["kind"], "basis": row.get("basis", "")}
                for field, limit in (("name", 200), ("unit", 20), ("brand", 80), ("model", 120), ("reason", 300)):
                    item[field] = string(row.get(field, ""), field, limit, required=field in {"name", "unit"})
                for field in ("excluded", "existing", "auto_quantity"):
                    item[field] = boolean(row.get(field, False))
                item["quantity"] = str(number(row.get("quantity"), "Количество", minimum=".001", maximum="1000000"))
                item["price"] = _optional(number(row.get("price"), "Цена", optional=True, places=2))
                specs = row.get("specs", {})
                if not isinstance(specs, dict):
                    raise EstimateInputError("Некорректные характеристики.")
                item["specs"] = {field: _optional(number(specs.get(field), "Характеристика оборудования", minimum=".001", maximum="100000", optional=True)) for field in ("flow", "head", "filter_flow", "backwash_flow")}
                clean["rows"].append(item)
            cleaned.append(clean)
        result["variants"][key] = {"blocks": cleaned}
    return result


def evaluate(raw):
    """Recompute derived fields on the server; never trust client totals/warnings."""
    result = normalize_snapshot(raw)
    result["geometry"] = geometry(result["inputs"])
    for key, variant in result["variants"].items():
        total, missing, models = Decimal(0), 0, 0
        physical = []
        for block in variant["blocks"]:
            subtotal = Decimal(0)
            for row in block["rows"]:
                included = not block["excluded"] and not row["excluded"]
                if included or row["existing"]:
                    physical.append(row)
                charged = included and not row["existing"]
                amount = (Decimal(row["quantity"]) * Decimal(row["price"])).quantize(CENT, rounding=ROUND_HALF_UP) if charged and row["price"] is not None else None
                row["amount"] = _optional(amount)
                if charged and row["price"] is None:
                    missing += 1
                if charged and row["kind"] != "other" and not row["model"]:
                    models += 1
                if amount is not None:
                    subtotal += amount
            block["subtotal"] = str(subtotal.quantize(CENT))
            total += subtotal
        warnings = []
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
            else:
                if Decimal(pump["flow"]) > Decimal(filt["filter_flow"]):
                    warnings.append("Расход насоса выше допустимого для фильтра. Подберите больший фильтр либо пересчитайте режим насоса.")
                if Decimal(pump["flow"]) < Decimal(result["geometry"]["required_flow"]):
                    warnings.append("Расход насоса ниже требуемого для заданного времени оборота воды.")
            warnings.append("Обратную промывку, трубопроводы и рабочие режимы должен проверить инженер по кривым оборудования; эти проверки пока не автоматизированы.")
        elif pumps or filters:
            warnings.append("Несколько насосов/фильтров или неполный комплект: требуется расчёт гидравлической схемы.")
        if result["inputs"]["heating"] != "none":
            warnings.append("Мощность подогрева и совместимость с источником тепла требуют отдельного расчёта.")
        variant.update(total=str(total.quantize(CENT)), missing_prices=missing, missing_models=models, warnings=warnings, title=VARIANTS[key][0], preferred_brand=VARIANTS[key][1])
    return result


def recalculate(raw, inputs):
    """Keep exclusions and manual edits; update only explicitly automatic quantities."""
    result = normalize_snapshot(raw)
    result["inputs"] = normalize_inputs(inputs)
    metrics = geometry(result["inputs"])
    for variant in result["variants"].values():
        for block in variant["blocks"]:
            for row in block["rows"]:
                if row["auto_quantity"] and row["basis"]:
                    row["quantity"] = metrics[row["basis"]]
    return evaluate(result)
