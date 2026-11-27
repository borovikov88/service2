"""Employee-scoped transport for document and catalog creation on the existing Fresh base."""
import hashlib
import http.client
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request, build_opener
from uuid import UUID

from django.conf import settings

from pool_service import onec_diagnostic as diagnostic
from pool_service.finance_imports.odata_profit import NoRedirectHandler
from pool_service.models import OrganizationAccess


ITEMS = "Catalog_Номенклатура"
UNITS = "Catalog_КлассификаторЕдиницИзмерения"
SUPPLIER_ITEMS = "Catalog_НоменклатураКонтрагентов"
IDENT = re.compile(r"[^\W\d]\w*\Z", re.UNICODE)
MAX_RESPONSE = 512 * 1024


class WriteError(ValueError):
    """Only operator-safe messages are surfaced through MCP."""


class AccessDenied(WriteError):
    pass


class TransportFailure(WriteError):
    pass


def article_key(text):
    return unicodedata.normalize("NFKC", text).strip().casefold()


def guid(value):
    try:
        parsed = UUID(str(value))
        if parsed.int:
            return str(parsed)
    except (TypeError, ValueError):
        pass
    raise WriteError("1С не вернула действительную ссылку на объект.")


def text_filter(field, value):
    return field + " eq '" + value.replace("'", "''") + "'"


def reference_filter(field, value):
    return field + " eq guid'" + guid(value) + "'"


def _created_location_key(base_url, entity, location):
    if not location:
        return None
    try:
        base = urlsplit(base_url)
        target = urlsplit(urljoin(base_url, location))
        if (target.scheme != "https" or base.scheme != "https" or target.hostname != base.hostname
                or (target.port or 443) != (base.port or 443) or target.username is not None
                or target.password is not None or target.query or target.fragment):
            return None
        collection = unquote(base.path).rstrip("/") + "/" + entity
        match = re.fullmatch(re.escape(collection) + r"\((?:Ref_Key=)?guid'([0-9A-Fa-f-]{36})'\)", unquote(target.path))
        return guid(match[1]) if match else None
    except ValueError:
        return None


def employee_config(user_id, org_id):
    """The trusted mapping is provisioned out-of-band; chat cannot choose a login."""
    try:
        raw = getattr(settings, "ONEC_MCP_WRITE_BINDINGS", None)
        bindings = raw if raw is not None else json.loads(os.environ.get("ONEC_MCP_WRITE_BINDINGS_JSON", "{}"))
        username = bindings[str(user_id)]
        if (not isinstance(username, str) or not username.strip() or username.strip() != username
                or ":" in username or any(ord(char) < 32 for char in username)):
            raise ValueError
        if sum(str(value).strip().casefold() == username.casefold() for value in bindings.values()) != 1:
            raise ValueError
        config = diagnostic.config_from_settings()
        if username.casefold() == config.username.strip().casefold():
            owner = str(getattr(settings, "ONEC_MCP_WRITE_CONNECTION_OWNER_ID",
                                os.environ.get("ONEC_MCP_WRITE_CONNECTION_OWNER_ID", "")))
            if owner != str(user_id) or not OrganizationAccess.objects.filter(
                organization_id=org_id, user_id=user_id, role="owner"
            ).exists():
                raise ValueError
            password = config.password
        else:
            password = os.environ.get(f"ONEC_MCP_WRITE_PASSWORD_{int(user_id)}", "")
        if not password:
            raise ValueError
        return diagnostic._validated_config(replace(config, username=username, password=password))
    except (ValueError, TypeError, KeyError, AttributeError, diagnostic.OneCDiagnosticError):
        raise AccessDenied("Для сотрудника не настроена личная учётная запись 1С для записи.") from None


def integration_config():
    """Use only the provisioned writer identity on the already configured Fresh base."""
    username = getattr(settings, "ONEC_MCP_DRAFT_USERNAME", os.getenv("ONEC_MCP_DRAFT_USERNAME", ""))
    password = getattr(settings, "ONEC_MCP_DRAFT_PASSWORD", os.getenv("ONEC_MCP_DRAFT_PASSWORD", ""))
    if not isinstance(username, str) or not username or username.strip() != username or ":" in username or not password:
        raise AccessDenied("Для черновиков не настроена служебная учётная запись 1С.")
    try:
        config = diagnostic.config_from_settings()
        if not config.organization_guids:
            raise AccessDenied("Не настроена область организаций для черновиков 1С.")
        return diagnostic._validated_config(replace(config, username=username, password=password))
    except diagnostic.OneCDiagnosticError:
        raise AccessDenied("Подключение для черновиков 1С недоступно.") from None


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def binding_key(config):
    return fingerprint(config.base_url + "\n" + config.username.casefold())


class FreshWriter:
    def __init__(self, config):
        self.config = config
        self.client = build_opener(NoRedirectHandler())
        try:
            self.metadata = diagnostic.fetch_metadata(config, opener=self.client)
            self.entities = diagnostic.parse_metadata(self.metadata)
        except (diagnostic.OneCDiagnosticError, OSError, http.client.HTTPException):
            raise AccessDenied("Не удалось прочитать метаданные базы под учётной записью подключения.") from None

        self.edm_nodes = {}
        self.edm_aliases = {}
        self.edm_ends = {}
        self.edm_navigations = {}
        self.resolved = {}
        self.reviewed_lines = []
        self.choices = {}
        self.candidates = {}
        for schema in ET.fromstring(self.metadata).iter():
            if schema.tag.rsplit("}", 1)[-1] != "Schema":
                continue
            namespace = schema.get("Namespace", "")
            if schema.get("Alias"):
                self.edm_aliases[schema.get("Alias")] = namespace
            for node in schema:
                owner = namespace + "." + node.get("Name", "")
                for part in node:
                    tag = part.tag.rsplit("}", 1)[-1]
                    if tag == "End" and node.tag.rsplit("}", 1)[-1] == "Association":
                        self.edm_ends[(owner, part.get("Role"))] = part.get("Type")
                    elif tag == "NavigationProperty":
                        self.edm_navigations[(owner, part.get("Name"))] = (part.get("Relationship", ""), part.get("ToRole"))
                if node.tag.rsplit("}", 1)[-1] in ("EntityType", "ComplexType"):
                    self.edm_nodes[owner] = node

    def _call(self, method, path, *, query=None, body=None):
        url = self.config.base_url + quote(path, safe="()'")
        query = dict(query or {}, **{"$format": "json"})
        url += "?" + urlencode(query, quote_via=quote)
        headers = {"Authorization": diagnostic._authorization(self.config), "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with self.client.open(req, timeout=self.config.timeout_seconds) as response:
                status = response.getcode()
                if (method == "POST" and status not in (200, 201, 204)) or (method == "GET" and status != 200):
                    raise TransportFailure("1С вернула неожиданный статус.")
                if method == "POST":
                    created_key = _created_location_key(self.config.base_url, path, response.headers.get("Location"))
                    if created_key:
                        # Only use the UUID for a verified GET on the configured collection.
                        return b"", created_key
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if status in (401, 403):
                raise AccessDenied("1С отказала в доступе для учётной записи сотрудника.") from None
            raise TransportFailure(f"1С ответила HTTP {status}.") from None
        except (OSError, URLError, http.client.HTTPException, ValueError):
            raise TransportFailure("Нет подтверждённого ответа от 1С.") from None
        if len(raw) > MAX_RESPONSE:
            raise TransportFailure("Ответ 1С превысил допустимый размер.")
        return raw, None

    def schema(self, entity, fields):
        schema = self.entities.get(entity)
        if not schema or not set(fields) <= schema.field_types.keys():
            raise WriteError("Запрошенный объект или реквизит отсутствует в опубликованной схеме 1С.")
        return schema

    def rows(self, entity, fields, where):
        self.schema(entity, fields)
        raw, _ = self._call("GET", entity, query={"$select": ",".join(fields), "$filter": where, "$top": "2"})
        try:
            rows, more = diagnostic._payload_page(raw)
        except diagnostic.OneCDiagnosticError:
            raise TransportFailure("Не удалось прочитать результат поиска в 1С.") from None
        if more or len(rows) > 1:
            raise WriteError("В базе несколько совпадений. Уточните выбор в 1С.")
        if rows and (not isinstance(rows[0], dict) or not set(fields) <= rows[0].keys()):
            raise TransportFailure("Ответ поиска 1С неполон.")
        return rows

    def active(self, entity, fields, where, *, folder=None):
        fields = list(dict.fromkeys(["Ref_Key", "DeletionMark", *fields, *(["IsFolder"] if folder is not None else [])]))
        rows = self.rows(entity, fields, where)
        if not rows:
            return None
        row = rows[0]
        if row["DeletionMark"] is not False or (folder is not None and row["IsFolder"] is not folder):
            raise WriteError("Найден объект другого вида или с пометкой удаления. Требуется проверка.")
        guid(row["Ref_Key"])
        return row

    def text_limits(self, entity):
        target = self.entities[entity].entity_type
        node = self.edm_nodes.get(target)
        if node is None:
            return {}
        return {p.get("Name"): int(p.get("MaxLength")) for p in node
                if p.tag.rsplit("}", 1)[-1] == "Property" and (p.get("MaxLength") or "").isdigit()}

    def candidates_for(self, name):
        term = max(re.findall(r"[\w-]{3,}", name), key=len, default="")
        if len(term) < 3:
            return []
        if len(term) > 5:
            term = term[:-1]
        fields = ["Ref_Key", "Description", "Артикул", "ЕдиницаИзмерения_Key"]
        self.schema(ITEMS, fields + ["DeletionMark", "IsFolder"])
        safe_term = term.replace("'", "''")
        raw, _ = self._call("GET", ITEMS, query={"$select": ",".join(fields), "$top": "6",
                         "$filter": f"IsFolder eq false and DeletionMark eq false and substringof('{safe_term}',Description)"})
        try:
            rows, _ = diagnostic._payload_page(raw)
        except diagnostic.OneCDiagnosticError:
            raise TransportFailure("Не удалось получить варианты номенклатуры.") from None
        if not all(isinstance(row, dict) and set(fields) <= row.keys() for row in rows[:6]):
            raise TransportFailure("Список вариантов номенклатуры неполон.")
        return rows[:6]

    def item(self, article, *, supplier=None):
        fields = ["Code", "Артикул", "ЕдиницаИзмерения_Key"]
        row = self.active(ITEMS, fields, text_filter("Артикул", article), folder=False)
        if row and article_key(row["Артикул"]) != article_key(article):
            raise WriteError("1С вернула позицию с отличающимся артикулом.")
        if not supplier or SUPPLIER_ITEMS not in self.entities:
            return row
        where = text_filter("Артикул", article)
        if supplier:
            where += " and " + reference_filter("ВладелецНоменклатуры_Key", supplier)
        alias_fields = ["Номенклатура_Key", "Недействителен"]
        if supplier:
            alias_fields += ["КоличествоБазовойЕдиницыИзмерения", "Характеристика_Key"]
        alias = self.active(SUPPLIER_ITEMS, alias_fields, where, folder=False)
        if not alias:
            return row
        if alias["Недействителен"] is not False:
            raise WriteError("Найден неактуальный артикул поставщика. Уточните карточку в 1С.")
        match = self.active(ITEMS, fields, reference_filter("Ref_Key", alias["Номенклатура_Key"]), folder=False)
        if not match:
            raise WriteError("Артикул поставщика найден, связанная номенклатура недоступна.")
        if row and guid(row["Ref_Key"]) != guid(match["Ref_Key"]):
            raise WriteError("Артикул поставщика и внутренний артикул указывают на разные товары. Уточните сопоставление.")
        if supplier:
            try:
                factor = Decimal(str(alias["КоличествоБазовойЕдиницыИзмерения"]))
            except (InvalidOperation, ValueError):
                raise WriteError("Уточните количество базовых единиц в упаковке поставщика.") from None
            if factor != 1 or str(alias["Характеристика_Key"]) != "00000000-0000-0000-0000-000000000000":
                raise WriteError("Артикул поставщика относится к упаковке или характеристике. Сверьте пересчёт и укажите точные строки заказа.")
        return match

    def create(self, entity, body):
        if entity != ITEMS and not (IDENT.fullmatch(entity) and entity.startswith("Document_")
                                    and body.get("Posted") is False):
            raise WriteError("Эта операция записи недоступна.")
        self.schema(entity, body)
        raw, created_key = self._call("POST", entity, body=body)
        if created_key or not raw:
            return created_key
        try:
            result = json.loads(raw.decode("utf-8-sig"))
            result = result.get("d", result)
            key = result.get("Ref_Key")
            return guid(key) if key else None
        except (UnicodeError, ValueError, AttributeError):
            raise TransportFailure("Не удалось проверить ответ 1С после создания.") from None
