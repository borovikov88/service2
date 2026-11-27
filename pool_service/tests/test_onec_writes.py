import base64
import json
import os
import re
from datetime import datetime, timezone as dt_timezone
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess
from pool_service.onec_draft_models import OneCWriteFence
from pool_service.staff_call_models import StaffAccount
from pool_service.services import onec_draft_requests as drafts
from pool_service.services import onec_write_requests as writes
from pool_service.services.onec_write_connection import AccessDenied, ITEMS, SUPPLIER_ITEMS, UNITS, WriteError, employee_config
from pool_service.services.onec_write_documents import ORDER


ORG, SUPPLIER = "Catalog_Организации", "Catalog_Контрагенты"
WAREHOUSE = "Document_ПеремещениеЗапасов"
LOCATIONS = "Catalog_СтруктурныеЕдиницы"
BASE = "https://fresh.example/db/odata/standard.odata/"
G, S, B, D, N = "Edm.Guid", "Edm.String", "Edm.Boolean", "Edm.DateTime", "Edm.Decimal"
CAT = {"Ref_Key": G, "Description": S, "DeletionMark": B}
DOC = {"Ref_Key": G, "Number": S, "Posted": B, "Date": D, "DeletionMark": B, "Организация_Key": G,
       "Комментарий": S}
SCHEMA = {
    ITEMS: {**CAT, "Code": S, "Артикул": S, "НаименованиеПолное": S, "ТипНоменклатуры": S,
            "ЕдиницаИзмерения_Key": G, "IsFolder": B, "Parent_Key": G},
    UNITS: CAT, ORG: {**CAT, "ИНН": S, "КПП": S}, SUPPLIER: CAT,
    ORDER: {**DOC, "Контрагент_Key": G, "НомерВходящегоДокумента": S, "ДатаВходящегоДокумента": D,
            "Запасы": "Collection(StandardODATA.Rows)", "СуммаДокумента": N},
    WAREHOUSE: {**DOC, "СтруктурнаяЕдиница_Key": G, "СтруктурнаяЕдиницаПолучатель_Key": G, "Запасы": "Collection(StandardODATA.Rows)"}, LOCATIONS: CAT,
    SUPPLIER_ITEMS: {**CAT, "IsFolder": B, "Артикул": S, "ВладелецНоменклатуры_Key": G, "КоличествоБазовойЕдиницыИзмерения": N, "Характеристика_Key": G, "Номенклатура_Key": G, "Недействителен": B},
}
LINE = {"LineNumber": "Edm.Int64", "Номенклатура_Key": G, "Количество": N, "Цена": N, "Сумма": N,
        "Всего": N, "ЕдиницаИзмерения": S, "ЕдиницаИзмерения_Type": S}


def xml_schema():
    schema = ET.Element("Schema", xmlns="http://schemas.microsoft.com/ado/2009/11/edm", Namespace="StandardODATA")
    container = ET.SubElement(schema, "EntityContainer", Name="Container")
    for name, fields in {**SCHEMA, "Rows": LINE}.items():
        node = ET.SubElement(schema, "ComplexType" if name == "Rows" else "EntityType", Name=name)
        if name != "Rows":
            ET.SubElement(container, "EntitySet", Name=name, EntityType="StandardODATA." + name)
        for key, value in fields.items():
            ET.SubElement(node, "Property", Name=key, Type=value)
    return ET.tostring(schema, encoding="utf-8", xml_declaration=True)


class Response(BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status


class FreshEndpoint:
    def __init__(self):
        self.objects = {entity: [] for entity in SCHEMA}
        self.posts = []
        self.requests = []
        self.deny_read = False
        self.deny_post = False
        self.lose_response = False
        self.drop_write = False
        self.assigned_key = False
        self.location_only = False
        self.organ = self.seed(ORG, Description="Организация")
        self.supplier = self.seed(SUPPLIER, Description="Поставщик")
        self.unit = self.seed(UNITS, Description="шт")

    def seed(self, entity, **data):
        obj = {"Ref_Key": str(uuid4()), "DeletionMark": False, **({"Description": "Пример"} if entity.startswith("Catalog_") else {}), **data}
        self.objects[entity].append(obj)
        return obj["Ref_Key"]

    def open(self, req, timeout):
        assert 0 < timeout <= 120
        uri = urlsplit(req.full_url)
        assert uri.hostname == "fresh.example" and uri.scheme == "https"
        method, path = req.get_method(), unquote(uri.path).split("/")[-1]
        self.requests.append((method, path, req.get_header("Authorization")))
        if path == "$metadata":
            return Response(xml_schema())
        assert path in self.objects
        if method == "GET":
            if self.deny_read:
                raise HTTPError(req.full_url, 403, "denied", {}, None)
            qs = parse_qs(uri.query)
            if qs["$top"] == ["6"]:
                token = re.search(r"substringof\('([^']*)'", qs["$filter"][0]).group(1).casefold()
                rows = [r for r in self.objects[path] if r.get("IsFolder") is False and not r["DeletionMark"]
                        and token in r.get("Description", "").casefold()]
                return Response(json.dumps({"value": rows[:6]}).encode())
            assert qs["$top"] == ["2"]
            rows = self.objects[path]
            for field, value in re.findall(r"(\w+) eq (?:guid|datetime)?'((?:''|[^'])*)'", qs["$filter"][0]):
                rows = [r for r in rows if str(r.get(field, "")).casefold() == value.replace("''", "'").casefold()]
            return Response(json.dumps({"value": rows[:2]}).encode())
        assert method == "POST"
        body = json.loads(req.data)
        self.posts.append((path, body))
        if self.deny_post:
            raise HTTPError(req.full_url, 403, "denied", {}, None)
        if not self.drop_write:
            if self.assigned_key:
                body = {**body, "Ref_Key": str(uuid4())}
            self.seed(path, **body, **({"Code": "T-001"} if path == ITEMS else {"Number": "D-001"}))
        if self.lose_response:
            raise URLError("lost")
        if self.location_only:
            location = BASE.replace("fresh.example/", "fresh.example:443/") + quote(path) + "(guid'" + body["Ref_Key"] + "')"
            return Response(b"", 201, {"Location": location})
        return Response(json.dumps({"Ref_Key": body["Ref_Key"]}).encode(), 201)


class OneCWriteTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Пример")
        self.user = get_user_model().objects.create_user("example-writer")
        self.peer = get_user_model().objects.create_user("example-peer")
        for user in (self.user, self.peer):
            OrganizationAccess.objects.create(user=user, organization=self.org, role="manager")
        self.fresh = FreshEndpoint()
        self.enable(override_settings(
            ONEC_ODATA_BASE_URL=BASE, ONEC_ODATA_USERNAME="configured-reader", ONEC_ODATA_PASSWORD="test-reader",
            ONEC_ODATA_ORGANIZATION_GUIDS=[self.fresh.organ], ONEC_ODATA_TARGET_ORGANIZATION_ID=self.org.pk,
            ONEC_MCP_WRITE_BINDINGS={str(self.user.pk): "employee-one", str(self.peer.pk): "employee-two"},
        ))
        self.enable(patch.dict(os.environ, {f"ONEC_MCP_WRITE_PASSWORD_{self.user.pk}": "example-one",
                                           f"ONEC_MCP_WRITE_PASSWORD_{self.peer.pk}": "example-two"}))
        self.enable(patch("pool_service.services.onec_write_connection.build_opener", return_value=self.fresh))
        self.client.force_login(self.user)

    def enable(self, cm):
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def call(self, tool_name, **args):
        res = self.client.post(reverse("onec_drafts_mcp"), data=json.dumps({"jsonrpc": "2.0", "id": 7,
            "method": "tools/call", "params": {"name": tool_name, "arguments": args}}),
            content_type="application/json", HTTP_ACCEPT="application/json, text/event-stream")
        return res.json()

    def prepare(self, kind, args, *, user=None, approve=True):
        actor = user or self.user
        row, ticket = drafts.issue(actor.pk, kind)
        tool = "request_nomenclature" if kind == "nomenclature" else "prepare_document"
        self.assertFalse(self.call(tool, ticket=ticket, **args)["result"]["isError"])
        row.refresh_from_db()
        if approve:
            plan = writes.review_plan(row, {})
            self.assertFalse(plan["message"], plan["message"])
            writes.approve(actor.pk, row.pk, writes.review_digest(row), plan["digest"])
        return row, ticket

    def cards(self, *articles):
        return {"items": [{"name": "Пример " + sku, "unit": "шт", "article": sku} for sku in articles]}

    def commit(self, ticket, kind="nomenclature"):
        res = self.call("create_1c_" + kind, ticket=ticket)["result"]
        return json.loads(res["content"][0]["text"])

    def test_personal_approval_must_match_exact_content_and_the_intended_actor(self):
        row, ticket = self.prepare("nomenclature", self.cards("T-01"), approve=False)
        url = reverse("onec_drafts_detail", args=[row.pk])
        self.assertContains(self.client.get(url), "T-01")
        self.assertTrue(self.call("create_1c_nomenclature", ticket=ticket)["result"]["isError"])
        shared = Client(enforce_csrf_checks=True)
        shared.force_login(self.user)
        self.assertEqual(shared.post(url, {"digest": writes.review_digest(row)}).status_code, 403)
        self.client.force_login(self.peer)
        self.assertEqual(self.client.post(url, {"digest": writes.review_digest(row)}).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(url, {"digest": "different"}).status_code, 404)
        self.assertEqual(self.client.post(url, {"digest": writes.review_digest(row), "plan": writes.review_plan(row, {})["digest"]}).status_code, 200)
        self.assertFalse(self.fresh.posts)
        self.assertIn("error", self.call("create_1c_nomenclature", ticket=ticket, confirmed=True))
        self.assertTrue(self.call("create_1c_document", ticket=ticket)["result"]["isError"])
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "created")

    def test_batch_checks_existing_article_preserves_strings_and_reuses_request(self):
        self.fresh.seed(ITEMS, Code="001", Артикул="0001", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        _, ticket = self.prepare("nomenclature", self.cards("0001", "X'03", "T-02"))
        result = self.commit(ticket)
        self.assertEqual([r["status"] for r in result["results"]], ["exists", "created", "created"])
        self.assertEqual([body["Артикул"] for _, body in self.fresh.posts], ["X'03", "T-02"])
        self.assertTrue(all(r["code"] and r["ref_key"] for r in result["results"]))
        self.assertEqual(self.commit(ticket)["unattempted"], 0)
        self.assertEqual(len(self.fresh.posts), 2)
        auth = "Basic " + base64.b64encode(b"employee-one:example-one").decode()
        self.assertTrue(all(entry[2] == auth for entry in self.fresh.requests))
        self.assertTrue(all(body["ТипНоменклатуры"] == "Запас" for _, body in self.fresh.posts))

    def test_unknown_outcome_survives_new_ticket_and_another_employee(self):
        _, ticket = self.prepare("nomenclature", self.cards("T-01", "T-02"))
        self.fresh.lose_response = self.fresh.drop_write = True
        result = self.commit(ticket)
        self.assertEqual((result["results"][0]["status"], result["unattempted"]), ("unconfirmed", 1))
        self.fresh.lose_response = self.fresh.drop_write = False
        _, peer = self.prepare("nomenclature", self.cards("T-01"), user=self.peer)
        self.assertEqual(self.commit(peer)["results"][0]["status"], "unconfirmed")
        self.assertEqual(len(self.fresh.posts), 1)

    def test_response_loss_after_commit_reconciles_without_another_post(self):
        _, ticket = self.prepare("nomenclature", self.cards("T-01"))
        self.fresh.lose_response = True
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "unconfirmed")
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "verified")
        self.assertEqual(len(self.fresh.posts), 1)

    def test_rights_are_checked_on_each_attempt_and_on_receipt(self):
        _, ticket = self.prepare("nomenclature", self.cards("T-01"))
        self.fresh.deny_post = True
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "unconfirmed")
        self.assertFalse(OneCWriteFence.objects.exists())
        self.fresh.deny_post = False
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "created")
        self.fresh.deny_read = True
        result = self.commit(ticket)["results"][0]
        self.assertEqual(result["status"], "unconfirmed")
        self.assertNotIn("ref_key", result)
        OrganizationAccess.objects.filter(user=self.user).delete()
        self.assertTrue(self.call("create_1c_nomenclature", ticket=ticket)["result"]["isError"])

    def test_no_implicit_service_account_and_no_duplicate_bindings(self):
        for bindings in ({}, {str(self.user.pk): "configured-reader"},
                         {str(self.user.pk): "EMPLOYEE-ONE", str(self.peer.pk): "employee-one"}):
            with override_settings(ONEC_MCP_WRITE_BINDINGS=bindings), self.assertRaises(AccessDenied):
                employee_config(self.user.pk, self.org.pk)
        OrganizationAccess.objects.filter(user=self.user).update(role="owner")
        with override_settings(ONEC_MCP_WRITE_BINDINGS={str(self.user.pk): "configured-reader"},
                               ONEC_MCP_WRITE_CONNECTION_OWNER_ID=self.user.pk):
            self.assertEqual(employee_config(self.user.pk, self.org.pk).username, "configured-reader")

    def test_technical_account_cannot_use_its_previously_approved_ticket(self):
        account, _ = StaffAccount.objects.update_or_create(
            user=self.user, organization=self.org, defaults={"kind": "person"}
        )
        row, ticket = self.prepare("nomenclature", self.cards("T-01"))
        for kind, superuser in (("service", False), ("system", False), ("person", True)):
            with self.subTest(kind=kind, superuser=superuser):
                StaffAccount.objects.filter(pk=account.pk).update(kind=kind)
                get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=superuser)
                with self.assertRaises(drafts.DraftUnavailable):
                    drafts.issue(self.user.pk, "nomenclature")
                with self.assertRaises(drafts.DraftUnavailable):
                    writes.approve(self.user.pk, row.pk, writes.review_digest(row))
                self.assertTrue(self.call("create_1c_nomenclature", ticket=ticket)["result"]["isError"])
        self.assertFalse(self.fresh.posts)

    def test_replacing_base_or_identity_invalidates_signed_review(self):
        _, ticket = self.prepare("nomenclature", self.cards("T-01"))
        with override_settings(ONEC_ODATA_BASE_URL=BASE.replace("/db/", "/other/")):
            self.assertTrue(self.call("create_1c_nomenclature", ticket=ticket)["result"]["isError"])
        with override_settings(ONEC_MCP_WRITE_BINDINGS={str(self.user.pk): "someone-else"}):
            self.assertTrue(self.call("create_1c_nomenclature", ticket=ticket)["result"]["isError"])
        self.assertFalse(self.fresh.posts)

    @override_settings(ONEC_MCP_WRITE_HOURLY_LIMIT=2)
    def test_rate_caps_written_objects_instead_of_requests(self):
        _, ticket = self.prepare("nomenclature", self.cards("T-01", "T-02", "T-03", "T-04"))
        result = self.commit(ticket)
        self.assertEqual([r["status"] for r in result["results"]], ["created", "created", "unconfirmed"])
        self.assertEqual((result["unattempted"], len(self.fresh.posts)), (1, 2))

    def test_supplier_order_uses_same_connection_and_is_read_back_unposted(self):
        self.fresh.seed(ITEMS, Code="001", Артикул="0001", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        args = {"document_type": "Заказ поставщику", "organization": "Организация", "counterparty": "Поставщик",
                "source_date": "2026-01-02", "source_number": "DEMO", "lines": [
                {"article": "0001", "item": "Пример", "unit": "шт", "quantity": "2", "price": "20.01"}]}
        _, ticket = self.prepare("document", args)
        self.fresh.assigned_key = True
        receipt = self.commit(ticket, "document")["results"][0]
        self.assertEqual((receipt["number"], receipt["posted"]), ("D-001", False))
        self.assertEqual(receipt["ref_key"], self.fresh.objects[ORDER][0]["Ref_Key"])
        entity, body = self.fresh.posts[0]
        self.assertEqual((entity, body["Posted"], body["СуммаДокумента"]), (ORDER, False, "40.02"))
        self.assertEqual(body["Запасы"][0]["Количество"], "2")
        _, again = self.prepare("document", args)
        self.assertEqual(self.commit(again, "document")["results"][0]["status"], "exists")
        self.assertEqual(len(self.fresh.posts), 1)

    def test_metadata_driven_document_uses_current_fresh_rights(self):
        item = self.fresh.seed(ITEMS, Артикул="T-17", Code="017", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        source, destination = (self.fresh.seed(LOCATIONS, Description=name) for name in ("Первый", "Второй"))
        args = {"document_type": "Перемещение запасов", "fields": {"Организация_Key": self.fresh.organ,
                "СтруктурнаяЕдиница_Key": source, "СтруктурнаяЕдиницаПолучатель_Key": destination},
                "tables": {"Запасы": [{"Номенклатура_Key": item, "Количество": "1"}]}}
        _, ticket = self.prepare("document", args)
        self.fresh.deny_post = True
        self.assertEqual(self.commit(ticket, "document")["results"][0]["status"], "unconfirmed")
        self.fresh.deny_post = False
        self.fresh.assigned_key = self.fresh.location_only = True
        receipt = self.commit(ticket, "document")["results"][0]
        self.assertIs(receipt["posted"], False)
        self.assertEqual(receipt["ref_key"], self.fresh.objects[WAREHOUSE][0]["Ref_Key"])
        self.assertEqual(self.fresh.posts[-1][0], WAREHOUSE)
        self.assertEqual(self.commit(ticket, "document")["results"][0]["status"], "verified")
        self.assertEqual(len(self.fresh.posts), 2)

    @override_settings(ONEC_FINANCE_TIME_ZONE="Asia/Barnaul")
    def test_implicit_document_date_is_reviewed_and_checked_in_the_accounting_zone(self):
        self.fresh.seed(ITEMS, Артикул="T", Code="01", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        request = {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                   "lines": [{"item": "Пример", "article": "T", "quantity": "1"}]}
        row, ticket = self.prepare("document", request, approve=False)
        row.submitted_at = datetime(2026, 2, 2, 18, 2, tzinfo=dt_timezone.utc)
        row.save(update_fields=["submitted_at"])
        expected = "2026-02-03T01:02:00"
        with timezone.override("Europe/Moscow"):
            plan = writes.review_plan(row, {})
            self.assertEqual(plan["document_date"], expected)
            self.assertContains(self.client.get(reverse("onec_drafts_detail", args=[row.pk])), expected)
            writes.approve(self.user.pk, row.pk, writes.review_digest(row), plan["digest"])
        self.fresh.lose_response = True
        with timezone.override("UTC"):
            self.assertEqual(self.commit(ticket, "document")["results"][0]["status"], "unconfirmed")
        self.assertEqual(self.fresh.posts[0][1]["Date"], expected)
        self.fresh.objects[ORDER][0]["Date"] = "2026-02-02T01:02:00"
        self.assertEqual(self.commit(ticket, "document")["results"][0]["status"], "unconfirmed")
        self.assertTrue(OneCWriteFence.objects.get().mismatch)
        self.assertEqual(len(self.fresh.posts), 1)

    def test_partial_supplier_prices_require_an_explicit_value_for_every_line(self):
        self.fresh.seed(ITEMS, Артикул="T", Code="01", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        line = {"item": "Пример", "article": "T", "quantity": "2"}
        request = {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                   "lines": [{**line, "price": "3.50"}, line]}
        row, ticket = self.prepare("document", request, approve=False)
        self.assertIn("цену каждой позиции", writes.review_plan(row, {})["message"])
        self.assertTrue(self.call("create_1c_document", ticket=ticket)["result"]["isError"])
        self.assertFalse(self.fresh.posts)
        request["lines"][1] = {**line, "price": "0"}
        _, approved = self.prepare("document", request)
        self.assertEqual(self.commit(approved, "document")["results"][0]["status"], "created")
        self.assertEqual(self.fresh.posts[0][1]["СуммаДокумента"], "7.00")

    def test_typed_tables_validate_columns_and_protected_properties(self):
        item = self.fresh.seed(ITEMS, Артикул="T-17", Code="017", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        args = {"document_type": ORDER, "fields": {"Организация_Key": self.fresh.organ, "Контрагент_Key": self.fresh.supplier},
                "tables": {"Запасы": [{"Номенклатура_Key": item, "Количество": "1.25", "Цена": "9.99"}]}}
        _, ticket = self.prepare("document", args)
        receipt = self.commit(ticket, "document")["results"][0]
        self.assertEqual(receipt["status"], "created")
        self.assertEqual(self.fresh.posts[0][1]["Запасы"][0]["LineNumber"], 1)
        bad = {**args, "tables": {"Запасы": [{"НеИзСхемы": 1}]}}
        row, ticket = self.prepare("document", bad, approve=False)
        self.assertTrue(writes.review_plan(row, {})["message"])
        self.assertTrue(self.call("create_1c_document", ticket=ticket)["result"]["isError"])
        for prop in ("Posted", "Ref_Key", "Number", "DeletionMark"):
            _, ticket = drafts.issue(self.user.pk, "document")
            self.assertTrue(self.call("prepare_document", ticket=ticket,
                document_type=ORDER, fields={prop: True})["result"]["isError"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_missing_product_and_other_organization_cannot_be_written(self):
        args = {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                "lines": [{"item": "Пример", "article": "missing", "quantity": "2"}]}
        row, ticket = self.prepare("document", args, approve=False)
        self.assertTrue(writes.review_plan(row, {})["message"])
        self.assertTrue(self.call("create_1c_document", ticket=ticket)["result"]["isError"])
        other, _ = self.prepare("document", {"document_type": WAREHOUSE, "fields": {"Организация_Key": str(uuid4())}}, approve=False)
        self.assertTrue(writes.review_plan(other, {})["message"])
        self.assertFalse(self.fresh.posts)

    def test_typed_stock_rows_require_live_active_items_and_required_warehouse_fields(self):
        item = self.fresh.seed(ITEMS, Description="Старый", IsFolder=False, DeletionMark=True)
        for key in [str(uuid4()), item, self.fresh.supplier]:
            args = {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                    "tables": {"Запасы": [{"Номенклатура_Key": key, "Количество": "1"}]}}
            row, _ = self.prepare("document", args, approve=False)
            self.assertTrue(writes.review_plan(row, {})["message"])
        row, _ = self.prepare("document", {"document_type": WAREHOUSE, "organization": "Организация", "details": "Заметка"}, approve=False)
        self.assertTrue(writes.review_plan(row, {})["message"])
        self.assertFalse(self.fresh.posts)

    def test_conflicting_supplier_article_is_not_silently_replaced(self):
        first, second = (self.fresh.seed(ITEMS, Code=sku, Артикул=sku, Description=sku, IsFolder=False,
                                        ЕдиницаИзмерения_Key=self.fresh.unit) for sku in ("A", "B"))
        self.fresh.seed(SUPPLIER_ITEMS, Артикул="A", Номенклатура_Key=second, ВладелецНоменклатуры_Key=self.fresh.supplier,
                        КоличествоБазовойЕдиницыИзмерения="1", Характеристика_Key="00000000-0000-0000-0000-000000000000",
                        IsFolder=False, Недействителен=False)
        row, _ = self.prepare("document", {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                    "lines": [{"item": "Пример", "article": "A", "quantity": "1"}]}, approve=False)
        self.assertIn("разные товары", writes.review_plan(row, {})["message"])
        self.assertFalse(self.fresh.posts)

    def test_private_review_selects_and_freezes_a_real_catalog_record(self):
        keys = [self.fresh.seed(ITEMS, Артикул=sku, Code=sku, Description="Средство " + sku, IsFolder=False,
                               ЕдиницаИзмерения_Key=self.fresh.unit) for sku in ("20л", "30л")]
        row, ticket = self.prepare("document", {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                    "lines": [{"item": "Средство", "quantity": "10"}]}, approve=False)
        page = reverse("onec_drafts_detail", args=[row.pk])
        self.assertContains(self.client.get(page), "Средство 20л")
        selected = self.client.post(page, {"action": "preview", "line_1": keys[0]})
        self.assertTrue(selected.context["plan"]["digest"])
        approved = self.client.post(page, {"action": "approve", "line_1": keys[0], "digest": writes.review_digest(row),
                                          "plan": selected.context["plan"]["digest"]})
        self.assertIsNotNone(approved.context["row"].approved_at)
        self.fresh.objects[ITEMS][0]["Description"] = "Изменена фасовка"
        self.assertEqual(self.commit(ticket, "document")["results"][0]["status"], "unconfirmed")
        self.assertFalse(self.fresh.posts)

    def test_mismatching_created_card_stays_blocked_across_retries(self):
        _, ticket = self.prepare("nomenclature", self.cards("T-77"))
        self.fresh.lose_response = True
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "unconfirmed")
        self.fresh.objects[ITEMS][0]["ТипНоменклатуры"] = "Услуга"
        for _ in range(2):
            self.assertEqual(self.commit(ticket)["results"][0]["status"], "unconfirmed")
        self.assertEqual(len(self.fresh.posts), 1)
        self.assertTrue(OneCWriteFence.objects.get().mismatch)

    def test_same_unconfirmed_unnumbered_order_survives_new_user_ticket(self):
        args = {"document_type": ORDER, "organization": "Организация", "counterparty": "Поставщик",
                "lines": [{"item": "Товар", "article": "T", "quantity": "1"}]}
        self.fresh.seed(ITEMS, Артикул="T", Code="01", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        row, ticket = self.prepare("document", args)
        self.fresh.lose_response = self.fresh.drop_write = True
        self.assertEqual(self.commit(ticket, "document")["results"][0]["status"], "unconfirmed")
        _, next_ticket = self.prepare("document", args)
        self.assertEqual(self.commit(next_ticket, "document")["results"][0]["status"], "unconfirmed")
        renewed = drafts.renew_approved(self.user.pk, row.pk)
        self.assertTrue(self.call("create_1c_document", ticket=ticket)["result"]["isError"])
        self.assertEqual(self.commit(renewed, "document")["results"][0]["status"], "unconfirmed")
        self.assertEqual(len(self.fresh.posts), 1)

    def test_card_approval_binds_unit_and_group_before_creating_the_batch(self):
        self.fresh.seed(ITEMS, IsFolder=True, Description="Группа")
        _, ticket = self.prepare("nomenclature", {"name": "Товар", "article": "X-6", "unit": "шт", "group": "Группа"})
        self.fresh.objects[ITEMS][0]["Ref_Key"] = str(uuid4())
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "unconfirmed")
        self.assertFalse(self.fresh.posts)
        _, second = self.prepare("nomenclature", self.cards("X-7"))
        self.fresh.objects[UNITS][0]["Ref_Key"] = str(uuid4())
        self.assertEqual(self.commit(second)["results"][0]["status"], "unconfirmed")
        self.assertFalse(self.fresh.posts)

    def test_explicit_internal_card_is_not_replaced_by_an_unrelated_suppliers_alias(self):
        key = self.fresh.seed(ITEMS, Артикул="C-2", Code="02", IsFolder=False, ЕдиницаИзмерения_Key=self.fresh.unit)
        self.fresh.seed(SUPPLIER_ITEMS, Артикул="D-2", Номенклатура_Key=key, ВладелецНоменклатуры_Key=str(uuid4()),
                        IsFolder=False, Недействителен=False, КоличествоБазовойЕдиницыИзмерения="1",
                        Характеристика_Key="00000000-0000-0000-0000-000000000000")
        _, ticket = self.prepare("nomenclature", self.cards("D-2"))
        self.assertEqual(self.commit(ticket)["results"][0]["status"], "created")
        self.assertEqual(self.fresh.posts[0][1]["Артикул"], "D-2")
