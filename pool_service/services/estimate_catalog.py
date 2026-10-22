"""Read-only shop catalogue and conservative, explainable equipment selection.

The public retail feed is the price source. No supplier credentials, checkout
operations or inferred pump curves. Saved estimates never call this module.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time
from urllib.error import URLError
from urllib.parse import urlsplit, unquote
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from django.core.cache import cache

from . import estimate_embedded as embedded
from . import estimate_lighting as lighting
from . import estimate_temperature_control as temperature
from . import estimate_filter_media as filter_media
from .pool_estimates import EstimateInputError, VARIANTS, evaluate, heating_requirement, heating_types, row_heating_type, ensure_heating_rows, make_row, string

SHOP_HOST = "shop.aqualine22.ru"
FEED_URL = "https://shop.aqualine22.ru/index.php?route=extension/feed/yandex_products"
CACHE_KEY = "pool-estimate-shop-v21"
CACHE_SECONDS = 900
MAX_BYTES = 20_000_000
FLOW = r"(\d+(?:[.,]\d+)?)\s*(?:м[³3]\s*/\s*(?:час|ч)|m[³3]\s*/\s*h)"


class CatalogUnavailable(EstimateInputError):
    pass


def safe_shop_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        return ""
    try:
        u = urlsplit(value)
        if u.scheme == "https" and u.hostname == SHOP_HOST and not u.username and not u.password and u.port in (None, 443) and not u.fragment:
            return value
    except ValueError:
        pass
    return ""


def safe_image_url(value):
    url = safe_shop_url(value)
    if not url:
        return ''
    path = unquote(urlsplit(url).path)
    if not path.startswith('/image/') or any(part in ('.', '..') for part in path.split('/')) or '\\' in path:
        return ''
    return url


def _decimal(text, maximum="1000000000"):
    try:
        value = Decimal(str(text).replace(",", "."))
        return str(value) if value.is_finite() and 0 < value <= Decimal(maximum) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _one_number(pattern, text):
    values = {_decimal(m, "100000") for m in re.findall(pattern, text, re.I)} - {None}
    return next(iter(values)) if len(values) == 1 else None


def _specifications(name, description):
    text = name.casefold().replace("\xa0", " ")
    kind, heater_type = "other", ""
    if re.match(r"^(?:тепловой|тепловый|инверторный|тепловой инверторный)\s+.*насос", text):
        kind, heater_type = "heater", "heatpump"
    elif text.startswith("теплообменник "):
        kind, heater_type = "heater", "exchanger"
    elif (text.startswith(("электронагреватель ", "электрический нагреватель ")) and not re.search(r"воздух|осушител", text)) or re.match(r"^нагреватель для бассейн(?:а|ов)\b.*(?:квт|kw)\b", text):
        kind, heater_type = "heater", "electric"
    elif re.match(r"^(?:насос циркуляцион|циркуляцион\w* насос)", text):
        kind = "circulation_pump"
    elif re.match(r"^(?:(?:электромагнитн|соленоидн)\w* клапан|клапан (?:электромагнитн|соленоидн))", text):
        kind = "solenoid_valve"
    elif re.match(r"^(?:песочный )?фильтр\s", text) and not re.search(r"пруд|картридж|дисков|магистраль|осмос", text):
        kind = "filter"
    elif re.match(r"^насос\s", text) and not re.search(r"дозир|дренаж|канализац|теплов|циркуляцион|скважин|фонтан", text):
        kind = "pump"
    flow = _one_number(FLOW, text) if kind in {"pump", "filter"} else None
    point = re.search(FLOW + r"\s*[*×x]\s*(\d+(?:[.,]\d+)?)\s*[мm]\b", text, re.I)
    head = _decimal(point.group(2), "100") if point and kind == "pump" else None
    # A slash in a heater title denotes explicitly supported supply options.
    # Do not derive voltage from descriptions containing tables for other models.
    power_values = set(re.findall(r"(?<!\d)(220|230|380|400)\s*(?:[вv]\b|/(?=\s*(?:220|230|380|400)\s*[вv]\b))", text, re.I))
    power_values = {"230" if p in {"220", "230"} else "400" for p in power_values}
    power = next(iter(power_values)) if len(power_values) == 1 else "unknown"
    # Positive evidence of filtration/pre-filter required; never choose a cheap
    # spa/attraction pump merely because its nominal flow matches.
    evidence = (name + " " + description).casefold()
    filtration = bool(re.search(r"префильтр|предварительн\w* фильтр|систем\w* фильтрации|\bпф\b", evidence))
    if re.search(r"без префильтр|без предварительн|гидромассаж|\bспа\b|\bspa\b|противоток", evidence):
        filtration = False
    capacities = {_decimal(v, '100000') for v in re.findall(FLOW if kind in {'pump','filter'} else r"(\d+(?:[.,]\d+)?)\s*(?:квт|kw)\b", text, re.I)} - {None}
    conflict = len(capacities) > 1 or kind == 'pump' and len(power_values) > 1
    return {"specification_conflict": bool(conflict), "kind": kind, "nominal_flow": flow,
            "flow": _decimal(point.group(1), "100000") if head else None,
            "head": head, "filter_flow": flow if kind == "filter" else None,
            "backwash_flow": None, "power": power, "filtration": filtration,
            "heater_type": heater_type,
            "power_options": sorted(power_values) if kind == "heater" else ([power] if power != "unknown" else []),
            "titanium": kind == "heater" and bool(re.search(r"\b(?:titan(?:ium)?|титан\w*)\b", text)),
            "heating_power": _one_number(r"(\d+(?:[.,]\d+)?)\s*(?:квт|kw)\b", text) if kind == "heater" else None}


def parse_catalog(payload, checked_at=None):
    if len(payload) > MAX_BYTES:
        raise CatalogUnavailable("Каталог магазина имеет неподдерживаемый формат.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CatalogUnavailable("Каталог магазина должен быть в кодировке UTF-8.") from None
    if "\x00" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise CatalogUnavailable("DTD и сущности в каталоге магазина не поддерживаются.")
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, LookupError):
        raise CatalogUnavailable("Магазин вернул некорректную выгрузку.") from None
    if root.tag != "yml_catalog":
        raise CatalogUnavailable("Магазин вернул страницу вместо каталога.")
    offers = root.findall("./shop/offers/offer")
    if not offers or len(offers) > 15000:
        raise CatalogUnavailable("В выгрузке магазина нет допустимого списка товаров.")
    categories = {c.get("id"): (c.text or "")[:200] for c in root.findall("./shop/categories/category")}
    parents = {c.get("id"): c.get("parentId") for c in root.findall("./shop/categories/category")}
    def category_path(cid):
        parts, seen = [], set()
        while cid and cid not in seen and len(seen) < 30:
            seen.add(cid)
            parts.append(categories.get(cid, ''))
            cid = parents.get(cid)
        return ' / '.join(reversed(parts))
    products = {}
    for offer in offers:
        pid, url = offer.get("id", ""), safe_shop_url(offer.findtext("url", ""))
        name = offer.findtext("name", "").strip()
        price = _decimal(offer.findtext("price"))
        if not re.fullmatch(r"\d{1,12}", pid) or not url or not name or len(name) > 200 or not price or offer.findtext("currencyId") not in {"RUB", "RUR"}:
            continue
        if pid in products:
            raise CatalogUnavailable("В выгрузке магазина повторяются идентификаторы товаров.")
        brand = offer.findtext("vendor", "").strip()[:80]
        for _, canonical in VARIANTS.values():
            if brand.casefold() == canonical.casefold() or (not brand and re.search(r"\b" + re.escape(canonical) + r"\b", name, re.I)):
                brand = canonical
                break
        description = offer.findtext("description", "")[:12000]
        core_specs = _specifications(name, description)
        specs = {**core_specs, **embedded.specifications(name, description, brand)}
        if core_specs.get('specification_conflict'):
            specs['specification_conflict'] = True
            specs['specification_note'] = (specs.get('specification_note', '') + ' Противоречивые значения мощности/расхода/питания в названии товара.').strip()
        from .estimate_fitting_facts import enrich
        specs["level_control"] = "unknown"
        specs.setdefault('specification_note', '')
        specs.setdefault('specification_source', '')
        enrich(specs, name, brand, offer.findtext('vendorCode', '')[:80], description)
        from .estimate_level_control import classify
        classify(specs, name, brand, offer.findtext('vendorCode', '')[:80])
        from .estimate_catalog_params import apply as apply_params
        apply_params(specs, offer)
        if set(specs.get('lining', [])) & set(specs.get('lining_denied', [])):
            specs.update(specification_conflict=True, lining=[], specification_note=(specs.get('specification_note','') + ' Противоречивое указание допуска и запрета отделки.').strip())
        category = categories.get(offer.findtext("categoryId"), "")
        from .estimate_auxiliary import classify as classify_auxiliary
        from .estimate_geotextile import classify as classify_geotextile
        lighting.classify(specs, name, description, category_path(offer.findtext("categoryId")), offer)
        classify_auxiliary(specs, name, description, offer)
        temperature.classify(specs,name,brand,description,offer,url)
        filter_media.classify(specs,name,description,category_path(offer.findtext("categoryId")),offer,url)
        from .estimate_liner_facts import classify as classify_liner
        from .estimate_liner_corner import classify as classify_corner
        classify_liner(specs,name,description,category_path(offer.findtext("categoryId")),offer)
        classify_corner(specs,name,brand,offer,url)
        classify_geotextile(specs, name, description, category_path(offer.findtext("categoryId")), offer)
        from .estimate_heater_purpose import purpose_note
        specs['heater_purpose_note'] = purpose_note(name, category_path(offer.findtext('categoryId')), url, description) if specs['heater_type'] == 'heatpump' else ''
        circulation_section = (bool(re.search(r"циркуляцион\w* насос", category, re.I))
                               or "/tsirkulyatsionnye-nasosy/" in url)
        if circulation_section and specs["kind"] in {"pump", "circulation_pump"}:
            specs.update(kind="circulation_pump", nominal_flow=None, flow=None, head=None,
                         filter_flow=None, filtration=False)
        # Only explicit model-title evidence. Do not infer VFD or duty from SKU/Qmax/Hmax.
        frequency = None
        if specs["kind"] == "circulation_pump":
            if re.search(r"без частот", name, re.I):
                frequency = False
            elif re.search(r"с частотн\w* (?:регулирован|преобразовател)", name, re.I):
                frequency = True
        specs["frequency_control"] = frequency
        if specs['embedded_role']:
            specs['kind'] = specs['embedded_role']
        model = re.sub(r"^(?:песочный )?(?:фильтр|насос)(?: для бассейна)?\s+", "", name, flags=re.I).split("(")[0].strip()[:120]
        products[pid] = {"id": pid, "url": url, "name": name, "brand": brand, "model": model,
                         "sku": offer.findtext("vendorCode", "")[:80],
                         "image": next((url for p in offer.findall("picture") if (url := safe_image_url(p.text))), ""),
                         "price": str(Decimal(price).quantize(Decimal(".01"))),
                         "available": offer.get("available") == "true",
                         "category": category, **specs}
    if not products:
        raise CatalogUnavailable("В каталоге не найдены товары с рублёвой ценой и ссылкой на магазин.")
    return {"checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
            "feed_date": (root.get("date") or "")[:50], "products": products}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download():
    started = time.monotonic()
    request = Request(FEED_URL, headers={"User-Agent": "Aqualine-Service2-Estimates/1.0", "Accept": "application/xml"})
    chunks, size = [], 0
    with build_opener(_NoRedirect()).open(request, timeout=15) as response:
        if response.status != 200:
            raise CatalogUnavailable("Каталог магазина временно недоступен.")
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES or time.monotonic() - started > 25:
                raise CatalogUnavailable("Магазин слишком долго передаёт каталог. Повторите позже.")
            chunks.append(chunk)
    return b"".join(chunks)


def get_catalog():
    existing = cache.get(CACHE_KEY)
    if existing:
        return existing
    if cache.get(CACHE_KEY + "-failed"):
        raise CatalogUnavailable("Каталог магазина временно недоступен. Повторите через минуту; сохранённые цены не меняются.")
    if not cache.add(CACHE_KEY + "-loading", True, 45):
        raise CatalogUnavailable("Каталог уже загружается. Повторите через несколько секунд.")
    try:
        result = parse_catalog(_download())
        cache.set(CACHE_KEY, result, CACHE_SECONDS)
        return result
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        cache.set(CACHE_KEY + "-failed", True, 60)
        if isinstance(exc, CatalogUnavailable):
            raise
        raise CatalogUnavailable("Не удалось загрузить каталог магазина. Сохранённые модели и цены не изменены.") from None
    finally:
        cache.delete(CACHE_KEY + "-loading")


def catalog_status(catalog):
    products = list(catalog["products"].values())
    return {"checked_at": catalog["checked_at"], "feed_date": catalog["feed_date"], "count": len(products),
            "heater_brands": sorted({p["brand"] for p in products if p["kind"] == "heater" and p["brand"]}),
            "brands": {brand: sum(p["brand"] == brand for p in products) for key, (_, brand) in VARIANTS.items() if key != "premium"}}


def candidate_reason(product, inputs, required_flow, *, for_auto=False):
    """None means eligible for a preliminary proposal, never engineering signoff."""
    if not product["available"]:
        return "Выгрузка магазина не подтверждает доступность к заказу."
    if product.get("specification_conflict"):
        return product.get("specification_note") or "Конфликт характеристик товара."
    if product["kind"] in {"circulation_pump", "solenoid_valve"}:
        return "Требуется подтверждение характеристик первичного контура и совместимости: расход/напор, температура, давление, теплоноситель, питание и присоединения."
    if product["kind"] == "pump":
        if not product["filtration"]:
            return "Назначение для фильтрации с префильтром не подтверждено."
        if not product["nominal_flow"] or Decimal(product["nominal_flow"]) < required_flow:
            return "Указанная производительность ниже требуемой либо не определена."
        if inputs["power"] != "unknown" and product["power"] != inputs["power"]:
            return "Питание не соответствует выбранному или не указано в карточке."
        if inputs.get("required_head") and (not product["head"] or Decimal(product["head"]) != Decimal(inputs["required_head"])):
            return "В каталоге нет рабочей точки при указанном расчётном напоре."
    if product['kind'] == 'level_regulator':
        from .estimate_level_control import regulator_reason
        return regulator_reason(product)
    if product["kind"] == "filter" and (not product["filter_flow"] or Decimal(product["filter_flow"]) < required_flow):
        return "Производительность фильтра ниже требуемой либо не определена."
    if product["kind"] == "heater":
        from .estimate_heater_purpose import reason as purpose_reason
        if purpose_reason(product):
            return purpose_reason(product)
        if product["heater_type"] not in heating_types(inputs):
            return "Другой тип подогрева."
        required_power = heating_requirement(inputs)["power"]
        if not required_power:
            return "Задайте мощность либо температуры, время прогрева и запас мощности."
        if not product["heating_power"] or Decimal(product["heating_power"]) < Decimal(required_power):
            return "Мощность ниже заданной либо не определена."
        if product["heater_type"] != "exchanger" and inputs["power"] != "unknown" and inputs["power"] not in product["power_options"]:
            return "Питание не соответствует выбранному или не указано в карточке."
    if product.get('embedded_role'):
        finish = 'film' if inputs['finish'] == 'film' else 'concrete' if inputs['finish'] in ('tile', 'mosaic') else ''
        if not finish or finish not in product.get('lining', []):
            return 'Совместимость с выбранной отделкой не подтверждена.'
        if not product.get('material'):
            return 'Материал или марка стали не подтверждены.'
    return None


def apply_product(row, product, catalog, selection="manual"):
    from .estimate_liner import validate_selection
    validate_selection(row, product)
    row['construction_price'] = None
    row['construction_price_suspended'] = False
    row['construction_computed_price'] = None
    old_kind = row['kind']
    row["selection_note"] = ""
    row["catalog_review_note"] = ""
    row['level_status'], row['level_evidence'] = 'unknown', ''
    # A successful product selection explicitly adopts its catalogue price.
    row.update(name=product["name"], brand=product["brand"], model=product["model"], price=product["price"], kind=product["kind"], unit_price_manual=False)
    if old_kind == 'level_inlet':
        row['kind'] = old_kind
        row['embedded_role'] = ''
    if product["kind"] == "heater":
        row["heating_type"] = product["heater_type"]
    row["specs"] = {k: product[k] for k in ("flow", "head", "filter_flow", "backwash_flow")}
    row["catalog"] = {k: product[k] for k in ("id", "url", "sku", "price", "available", "nominal_flow", "power", "power_options", "heating_power", "heater_type", "titanium", "embedded_role", "material", "lining")}
    row["catalog"]["filtration"] = product.get("filtration")
    row["catalog"]["heater_purpose_note"] = product.get("heater_purpose_note", "")
    row["catalog"]["lighting"] = deepcopy(product.get("lighting", {}))
    row["catalog"]["auxiliary"] = deepcopy(product.get("auxiliary", {}))
    row['catalog']['temperature'] = deepcopy(product.get('temperature',{}))
    row['catalog']['filter_loading'] = deepcopy(product.get('filter_loading',{}))
    row['catalog']['filter_media'] = deepcopy(product.get('filter_media',{}))
    row['catalog']['liner'] = deepcopy(product.get('liner',{}))
    row["catalog"]["geotextile"] = deepcopy(product.get("geotextile", {}))
    row["catalog"]["body_family"] = product.get("body_family", "")
    row["catalog"]["lining_denied"] = product.get("lining_denied", [])
    row["catalog"]["level_control"] = product.get("level_control", "unknown")
    for field, default in (("level_data_issue", ""), ("level_purpose", ""), ("level_passage", ""), ("level_connection", ""), ("level_diameter", ""), ("level_scheme", ""), ("level_regulator_skus", []), ("specification_conflict", False)):
        row["catalog"][field] = deepcopy(product.get(field, default))
    row["catalog"]["specification_note"] = product.get("specification_note", "")
    row["catalog"]["specification_source"] = product.get("specification_source", "")
    row["catalog"]["image"] = safe_image_url(product.get("image", ""))
    row["catalog"].update(checked_at=catalog["checked_at"], feed_date=catalog["feed_date"], selection=selection, missing=False)

    from .estimate_liner import set_conversion
    set_conversion(row, product)
    filter_media.selected(row, product)


def refresh_auto_facts(row, product, catalog):
    # Automatic selection can refresh technical evidence, never frozen prices.
    frozen = {key: row['catalog'][key] for key in ('price','checked_at','feed_date')}
    media_origin = deepcopy(row['catalog'].get('filter_media',{})) if row['kind']=='filter_media' else None
    media_note = row.get('catalog_review_note','')
    media_unit = row.get('unit')
    apply_product(row, product, catalog, 'auto')
    row['catalog'].update(frozen)
    row['price'] = frozen['price']
    if media_origin is not None:
        row['catalog']['filter_media']=media_origin
        row['unit']=media_unit
        row['catalog_review_note']=media_note


def select_price_tier(choices, tier):
    """Call only after technical filtering: 1 -> all; 2 -> low/high/high."""
    comparable = sorted(choices, key=lambda p: (Decimal(p["price"]), int(p["id"])))
    if not comparable:
        return None
    index = 0 if tier == "economy" else len(comparable) // 2 if tier == "standard" else -1
    return comparable[index]


def select_heater(choices, tier):
    if not choices:
        return None
    capacity = min(Decimal(p["heating_power"]) for p in choices)
    return select_price_tier([p for p in choices if Decimal(p["heating_power"]) == capacity], tier)


def choose_filtration_product(choices, tier):
    minimum = min(Decimal(p["nominal_flow"]) for p in choices)
    comparable = [p for p in choices if Decimal(p["nominal_flow"]) <= minimum * Decimal("1.25")]
    return select_price_tier(comparable, tier)


def auto_select(snapshot, catalog, only_variant=None):
    from . import estimate_candidates as candidates
    result = deepcopy(snapshot)
    ensure_heating_rows(result, only_variant)
    embedded.ensure_rows(result, only_variant)
    required = Decimal(result["geometry"]["required_flow"])
    filled = 0
    for key, variant in result["variants"].items():
        if only_variant and key != only_variant:
            continue
        brand = VARIANTS[key][1]
        pumps = [r for b in variant["blocks"] for r in b["rows"] if (not b["excluded"] and not r["excluded"] or r["existing"]) and r["kind"] == "pump"]
        for kind in ("pump", "filter", "heater"):
            for block in variant["blocks"]:
                if block["excluded"]:
                    continue
                for row in block["rows"]:
                    origin = row.get("catalog")
                    untouched_auto = origin and origin["selection"] == "auto" and row["price"] == origin["price"]
                    template_name = {"pump": "Фильтрационный насос", "filter": "Фильтр", "heater": "Подогрев: подобрать мощность и модель"}[kind]
                    placeholder = (not origin and not row["model"] and row["price"] is None
                                   and row["name"] == template_name and row["brand"] in ({brand, ""} if kind == "heater" else {brand})
                                   and row["unit"] == "компл." and not row["basis"] and not any(row["specs"].values()))
                    if row["kind"] != kind or row["excluded"] or row["existing"] or row.get("selection_locked") or not (untouched_auto or placeholder) or row.get("unit_price_manual") or Decimal(row["quantity"]) != 1:
                        continue
                    flow = required
                    if kind == "filter":
                        if len(pumps) > 1 or (pumps and Decimal(pumps[0]["quantity"]) != 1):
                            continue
                        if pumps:
                            pump = pumps[0]
                            pump_flow = pump["specs"].get("flow") or (pump.get("catalog") or {}).get("nominal_flow")
                            if pump_flow:
                                flow = max(flow, Decimal(pump_flow))
                    choices = [p for p in catalog['products'].values() if p['kind'] == kind and (kind != 'heater' or p['heater_type'] == row_heating_type(row, result['inputs']))]
                    product = candidates.choose(choices, key, result['inputs'], flow)
                    if product:
                        if origin and product['id'] == origin['id']:
                            # Refresh facts, retain frozen price until explicit price refresh.
                            refresh_auto_facts(row, product, catalog)
                            continue
                        apply_product(row, product, catalog, 'auto')
                        filled += 1
                    elif untouched_auto:
                        current = catalog['products'].get(origin['id']) or {**origin, **row['specs'], 'kind':kind, 'name':row['name']}
                        issue, _ = candidates.assess(current, result['inputs'], flow)
                        if issue:
                            row.update(name=template_name, brand=brand, model='', price=None, catalog=None,
                                       selection_note='Проверить: прежний автоматический кандидат отклонён. ' + issue + ' Подходящая замена не найдена.')
                            row['specs'] = dict.fromkeys(('flow','head','filter_flow','backwash_flow'))
        block = next(b for b in variant['blocks'] if b['id'] == 'embedded')
        if not block['excluded']:
            for row in block['rows']:
                role, origin = row.get('embedded_role'), row.get('catalog')
                if not role or row['excluded'] or row['existing'] or row.get('selection_locked') or row.get('embedded_profile') != variant['embedded_profile']:
                    continue
                untouched = origin and origin['selection'] == 'auto' and row['price'] == origin['price']
                placeholder = not origin and not row['model'] and row['price'] is None and row['name'] == embedded.ROLES[role] + ': подобрать модель'
                if not (untouched or placeholder) or row.get('unit_price_manual') or not row['auto_quantity']:
                    continue
                choices = [p for p in catalog['products'].values() if p.get('embedded_role') == role]
                product = candidates.choose(choices, key, result['inputs'], required, variant['embedded_profile'])
                if product:
                    if not origin or product['id'] != origin['id']:
                        apply_product(row, product, catalog, 'auto')
                        filled += 1
                    else:
                        refresh_auto_facts(row, product, catalog)
                elif untouched:
                    current = catalog['products'].get(origin['id']) or {**origin, 'kind':row['kind'], 'name':row['name'], 'brand':row['brand']}
                    issue, _ = candidates.assess(current, result['inputs'], required, variant['embedded_profile'])
                    if issue:
                        row.update(name=embedded.ROLES[role] + ': подобрать модель', brand='', model='', price=None, catalog=None,
                                   selection_note='Проверить: прежний автоматический кандидат отклонён. ' + issue + ' Подходящая замена не найдена.')
                        row['specs'] = dict.fromkeys(('flow','head','filter_flow','backwash_flow'))
    from .estimate_primary import fill as fill_primary
    filled += fill_primary(result, catalog, only_variant)
    from .estimate_liner import fill as fill_liner
    filled += fill_liner(result, catalog, only_variant)
    from .estimate_liner_corner import fill as fill_corner
    filled += fill_corner(result,catalog,only_variant)
    from .estimate_auxiliary import fill as fill_auxiliary
    from .estimate_geotextile import fill as fill_geotextile
    filled += fill_auxiliary(result, catalog, only_variant)
    filled += temperature.fill(result, catalog, only_variant)
    filled += filter_media.fill(result, catalog, only_variant)
    filled += fill_geotextile(result, catalog, only_variant)
    filled += lighting.fill(result, catalog, only_variant)
    from .estimate_level_control import fill as fill_level
    filled += fill_level(result, catalog, only_variant)
    # Explicit refresh also warns about newly known conflicts on protected rows;
    # it does not replace their frozen product, price, quantity or user facts.
    from .estimate_compatibility import duty
    for tier, variant in result['variants'].items():
        if only_variant and only_variant != tier: continue
        pump_duty, _ = duty(result, variant)
        for block in variant['blocks']:
            if block['excluded']: continue
            for row in block['rows']:
                if row['excluded'] or not row.get('catalog'): continue
                current = catalog['products'].get(row['catalog']['id'])
                if not current or row['kind'] not in candidates.CORE and not row.get('embedded_role'): continue
                issue, _ = candidates.assess(current, result['inputs'], max(required, pump_duty or required) if row['kind'] == 'filter' else required, variant['embedded_profile'])
                row['catalog_review_note'] = ('Текущая выгрузка ' + catalog['checked_at'] + ': ' + issue)[:1000] if issue else ''
    embedded.update_quantities(result, only_variant)
    result = evaluate(result)
    explain_unfilled(result, catalog, only_variant)
    return result, filled


def search_text(value):
    value = value.casefold().replace("ё", "е")
    return re.sub(r"(?:электромагнитн|соленоидн)\w*", "соленоидный", value)


def profile_reason(product, inputs, required_flow, profile):
    issues = [candidate_reason(product, inputs, required_flow)]
    if product.get('embedded_role'):
        if not product.get('material'):
            issues.append('Материал или марка стали не подтверждены.')
        elif not embedded.profile_matches(product, profile):
            issues.append('Материал/бренд не соответствует выбранному профилю КП: ' + embedded.PROFILES[profile] + '.')
        lining = 'film' if inputs['finish'] == 'film' else 'concrete' if inputs['finish'] in ('tile', 'mosaic') else ''
        if not lining or lining not in product.get('lining', []):
            issues.append('Совместимость с выбранной отделкой не подтверждена.')
    return ' '.join(dict.fromkeys(issue for issue in issues if issue)) or None


def search_catalog(catalog, query, kind, brand, inputs, required_flow, variant="economy", profile=None):
    if variant not in VARIANTS:
        raise EstimateInputError("Выберите смету.")
    profile = profile or embedded.DEFAULTS[variant]
    if not isinstance(profile, str) or profile not in embedded.PROFILES:
        raise EstimateInputError('Выберите комплектацию закладных.')
    def reason(product):
        if product['kind'] in lighting.ROLES:
            issue,notes=lighting.assess(product,profile or embedded.DEFAULTS[variant],finish=inputs['finish'],color=inputs.get('light_color'))
            return issue or 'Проверить: '+' '.join(notes)
        if product['kind'] in {'treatment', 'control_panel'}:
            from .estimate_auxiliary import search_reason
            return search_reason(product, inputs, required_flow, variant)
        if product['kind'] == 'geotextile':
            from .estimate_geotextile import assess
            return assess(product) or ('Цена за м². Пригодность под плёнку бассейна требует проверки.' if product.get('geotextile', {}).get('purpose') != 'pool' else 'Цена за м²; запас 15% уже учтён в количестве материала.')
        if product['kind']=='liner':
            from .estimate_liner import search_note
            return search_note(product)
        if product['kind']=='liner_corner':
            return 'Крепёжный уголок: цена за отрезок 2 м. Количество округляется по периметру, без дополнительного запаса.'
        if product['kind']=='filter_media':
            return filter_media.assess(product) or 'Проверить массу загрузки и фракцию по паспорту выбранного фильтра; фасовка и единица цены — по карточке песка.'
        if product['kind']=='thermostat':
            return 'Проверить схему коммутации, датчик и нагрузку контактов; панель с регулированием температуры может заменить термостат.'
        issue = profile_reason(product, inputs, required_flow, profile)
        if product['kind'] == 'level_regulator' and not issue:
            return product.get('specification_note') or 'Предварительный подбор; проверьте монтаж по паспорту модели.'
        if kind == 'level_inlet':
            return (issue or 'Для отдельной линии уровня проверьте свободный проход, тип и диаметр присоединений этой форсунки и выбранного регулятора. Совпадение бренда или список артикулов не обязательны.')
        return issue
    query = search_text(string(query, "Поиск товара", 120)).split()
    kind = string(kind, "Тип товара", 20)
    brand = string(brand, "Бренд", 80)
    if kind not in {*lighting.ROLES, "thermostat", "filter_media", "liner", "liner_corner", "", "other", "pump", "filter", "heater", "circulation_pump", "solenoid_valve", "level_regulator", "level_inlet", "treatment", "control_panel", "geotextile", *embedded.ROLES}:
        raise EstimateInputError("Неизвестный тип товара.")
    matches = [p for p in catalog["products"].values() if (not kind or (p['kind'] in {'inlet', 'vacuum'} if kind == 'level_inlet' else p['kind'] == kind)) and (not brand or p["brand"].casefold() == brand.casefold()) and all(q in search_text(p["name"] + " " + p["sku"] + " " + p["id"]) for q in query)]
    matches.sort(key=lambda p: (bool(reason(p)), not p["available"], bool(kind == "circulation_pump" and variant != "economy" and p.get("frequency_control") is not True), Decimal(p["price"]), int(p["id"])))
    from .estimate_liner import area
    return {"products": [{**p, "warning": reason(p), "roll_area": area(p)} for p in matches[:40]], "total": len(matches), "status": catalog_status(catalog)}


def refresh_prices(snapshot, catalog, variant_key):
    if not isinstance(variant_key, str) or variant_key not in VARIANTS:
        raise EstimateInputError("Выберите смету.")
    result, updated, skipped = deepcopy(snapshot), 0, 0
    for block in result["variants"][variant_key]["blocks"]:
        for row in block["rows"]:
            origin = row.get("catalog")
            if block["excluded"] or row["excluded"] or row["existing"] or not origin:
                continue
            if row.get("unit_price_manual") or row["price"] != origin["price"]:
                skipped += 1
                continue
            product = catalog["products"].get(origin["id"])
            if not product:
                origin["missing"] = True
                continue
            if product['kind']=='liner' and row['unit'] in {'рулон','рул.'} and (product.get('liner',{}).get('price_unit')=='m2' or product.get('liner',{}).get('unit_explicit') and product.get('liner',{}).get('price_unit')=='unknown'):
                row['catalog_review_note']='Единица цены каталога больше не подтверждает цену за рулон; прежняя цена сохранена.'
                skipped+=1
                continue
            if row['kind']=='liner' and not origin.get('roll_area') and origin.get('liner',{}).get('price_unit')=='m2':
                if product.get('liner',{}).get('price_unit')!='m2' or product.get('liner',{}).get('conflict'):
                    row['catalog_review_note']='Единица цены или характеристики лайнера изменились. Сохранена прежняя цена за м²; перевыберите товар после проверки.'
                    skipped+=1
                    continue
                origin['liner']=deepcopy(product.get('liner',{}))
                row['catalog_review_note']=''
            if origin.get('roll_area'):
                from .estimate_liner import area, set_conversion
                if not area(product) or Decimal(area(product)) != Decimal(origin['roll_area']) or row['unit'] != 'м²' or product.get('liner',{}).get('price_unit')=='m2' or product.get('liner',{}).get('unit_explicit') and product.get('liner',{}).get('price_unit')=='unknown':
                    row['catalog_review_note']='Площадь рулона или единица цены изменилась. Прежние цена и размер сохранены; перевыберите товар после проверки.'
                    skipped += 1
                    continue
                row['catalog_review_note']=''
                origin['liner']=deepcopy(product.get('liner',{}))
                set_conversion(row, product)
                origin.update(available=product['available'], missing=False, checked_at=catalog['checked_at'], feed_date=catalog['feed_date'])
                updated += 1
                continue
            if row['kind']=='filter_media':
                old=origin.get('filter_media') or {};new=product.get('filter_media') or {}
                if not row.get('auto_quantity') and any(old.get(k)!=new.get(k) for k in ('bag_kg','price_unit')):
                    row['catalog_review_note']='Фасовка/единица цены изменились; ручное количество защищено. Перевыберите товар и подтвердите количество.'
                    skipped+=1
                    continue
                origin['filter_media']=deepcopy(new)
                filter_media.selected(row,product)
                row['catalog_review_note']=''
            row["price"] = product["price"]
            origin.update(price=product["price"], available=product["available"], missing=False, checked_at=catalog["checked_at"], feed_date=catalog["feed_date"])
            updated += 1
    return evaluate(result), updated, skipped


def explain_unfilled(snapshot, catalog, only_variant=None):
    """Explain skipped preliminary selection without relaxing technical filters."""
    for tier, variant in snapshot['variants'].items():
        if only_variant and tier != only_variant:
            continue
        for block in variant['blocks']:
            for row in block['rows']:
                if row['kind'] != 'heater' and not row.get('embedded_role'):
                    continue
                if row.get('selection_status') == 'check_core' or not row.get('catalog') and row.get('selection_note', '').startswith('Проверить: прежний автоматический кандидат отклонён.'):
                    continue
                row['selection_note'] = ''
                if block['excluded'] or row['excluded'] or row['existing']:
                    continue
                previous = ''
                if row['model']:
                    product = catalog['products'].get((row.get('catalog') or {}).get('id'))
                    issue = profile_reason(product, snapshot['inputs'], Decimal(snapshot['geometry']['required_flow']), variant['embedded_profile']) if product else 'Модель отсутствует в текущей выгрузке; характеристики не подтверждены.'
                    if product and product.get('embedded_role') and not issue and not embedded.matches(product, variant['embedded_profile'], snapshot['inputs']['finish']):
                        issue = 'Материал/бренд не соответствует выбранному профилю КП.'
                    if not issue:
                        continue
                    previous = 'Выбранная модель НЕ подтверждена для текущих параметров: ' + issue + ' Цена остаётся в сумме до явной замены/исключения. '
                if row.get('selection_locked') or (row['price'] is not None and not row.get('catalog')) or (row.get('catalog') or {}).get('selection') == 'manual':
                    row['selection_note'] = previous + 'Автоподбор не изменяет вручную отредактированную строку. Для замены модели откройте каталог.'
                    continue
                if row['kind'] == 'heater':
                    source = row_heating_type(row, snapshot['inputs'])
                    if Decimal(row['quantity']) != 1:
                        row['selection_note'] = 'Автоподбор нагревателя рассчитан на одну единицу полной требуемой мощности. Количество изменено; состав нескольких приборов требует отдельного подбора.'
                        continue
                    products = [p for p in catalog['products'].values() if p['kind'] == 'heater' and p['heater_type'] == source]
                    required = heating_requirement(snapshot['inputs'])['power']
                    heading = previous + f'Требуемая мощность: не менее {required or "не задана"} кВт; питание: {snapshot["inputs"]["power"] if snapshot["inputs"]["power"] != "unknown" else "не задано"} В. '
                    reason = lambda p: candidate_reason(p, snapshot['inputs'], Decimal(snapshot['geometry']['required_flow']), for_auto=True)
                else:
                    role = row['embedded_role']
                    products = [p for p in catalog['products'].values() if p.get('embedded_role') == role]
                    heading = previous + 'Профиль: ' + embedded.PROFILES[variant['embedded_profile']] + '. '
                    def reason(p):
                        return profile_reason(p, snapshot['inputs'], Decimal(snapshot['geometry']['required_flow']), variant['embedded_profile'])
                if not products:
                    row['selection_note'] = heading + 'В текущей выгрузке нет распознанных товаров этой категории.'
                    continue
                reasons = sorted({reason(p) for p in products if reason(p)})
                candidates = sorted([p for p in products if p['available']], key=lambda p: (Decimal(p['price']), int(p['id'])))[:3]
                candidate_text = '; '.join(p['name'][:100] + ' — ' + (reason(p) or 'проверьте ручные изменения строки') for p in candidates)
                row['selection_note'] = (heading + 'Автоподбор не завершён: ' + (' '.join(reasons) or 'проверьте ручные изменения строки.')
                    + (' Для ручной проверки в каталоге (не подтверждённые замены): ' + candidate_text if candidate_text else ' Доступных предварительных кандидатов нет.'))[:1000]
