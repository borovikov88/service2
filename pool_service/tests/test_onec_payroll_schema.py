"""Synthetic schema-only diagnostics: no database or real 1C requests."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inspect_onec_payroll_schema.py"
SPEC = importlib.util.spec_from_file_location("inspect_onec_payroll_schema", SCRIPT)
schema = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schema)
BASE = "https://example.invalid/database/odata/standard.odata/"
NS = "http://schemas.microsoft.com/ado/2008/09/edm"


def metadata(body):
    return f'<Schema xmlns="{NS}" Namespace="Example" Alias="Self">{body}</Schema>'.encode()


class PayrollSchemaDiagnosticTests(unittest.TestCase):
    def test_names_only_and_referenced_complex_inherited_properties(self):
        raw = metadata('''
          <EntityType Name="Base"><Property Name="Period" Type="Edm.DateTime"/></EntityType>
          <ComplexType Name="Line"><Property Name="Amount" Type="Edm.Decimal"/></ComplexType>
          <EntityType Name="Payroll" BaseType="Self.Base">
            <Property Name="Lines" Type="Collection(Self.Line)"/>
            <Annotation Term="PRIVATE" String="private employee name and salary"/>
          </EntityType>
          <EntityType Name="Sales"><Property Name="Amount" Type="Edm.Decimal"/></EntityType>
          <EntityContainer Name="Service">
            <EntitySet Name="Начисления" EntityType="Self.Payroll"/>
            <EntitySet Name="Sales" EntityType="Self.Sales"/>
          </EntityContainer>''')
        preview = schema.describe_metadata(raw)
        self.assertFalse(preview["properties_included"])
        self.assertEqual(preview["types"], {})
        result = schema.describe_metadata(raw, entity_names=["Начисления"])
        self.assertEqual(result["entity_sets"], [{"name": "Начисления", "type": "Example.Payroll"}])
        self.assertEqual(set(result["types"]), {"Example.Payroll", "Example.Base", "Example.Line"})
        self.assertEqual(result["types"]["Example.Payroll"]["base_type"], "Example.Base")
        self.assertNotIn("private employee", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_candidates_can_be_found_by_inherited_employee_field(self):
        raw = metadata('''<EntityType Name="Base"><Property Name="Сотрудник_Key" Type="Edm.Guid"/></EntityType>
          <EntityType Name="Register" BaseType="Example.Base"/>
          <EntityContainer Name="Service"><EntitySet Name="Register" EntityType="Example.Register"/></EntityContainer>''')
        self.assertEqual(schema.describe_metadata(raw)["candidate_count"], 1)

    def test_many_candidates_list_names_then_explicit_selection(self):
        entities = ''.join(f'<EntitySet Name="Payroll{i}" EntityType="Example.Payroll"/>' for i in range(105))
        raw = metadata(f'<EntityType Name="Payroll"><Property Name="Amount" Type="Edm.Decimal"/></EntityType><EntityContainer Name="Service">{entities}</EntityContainer>')
        result = schema.describe_metadata(raw)
        self.assertEqual(result["candidate_count"], 105)
        self.assertEqual(len(result["entity_sets"]), 100)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["properties_included"])
        self.assertEqual(result["types"], {})
        selected = schema.describe_metadata(raw, entity_names=["Payroll104"])
        self.assertTrue(selected["properties_included"])
        self.assertEqual(selected["entity_sets"][0]["name"], "Payroll104")
        with self.assertRaisesRegex(schema.SchemaError, "REQUESTED_ENTITY_NOT_A_CANDIDATE"):
            schema.describe_metadata(raw, entity_names=["NotPresent"])

    def test_no_candidates_does_not_claim_payroll_available(self):
        result = schema.describe_metadata(metadata('<EntityType Name="Sales"/>'))
        self.assertEqual(result["candidate_count"], 0)
        self.assertIn("NOT verified", result["notice"])

    def test_get_metadata_only_with_bounded_read_and_timeout(self):
        response = Mock(status=200)
        response.read.return_value = metadata('')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        schema.fetch_metadata({"ONEC_ODATA_BASE_URL": BASE, "ONEC_ODATA_USERNAME": "user", "ONEC_ODATA_PASSWORD": "secret"}, opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, BASE + "$metadata")
        self.assertIsNone(request.data)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": schema.TIMEOUT_SECONDS})
        response.read.assert_called_once_with(schema.MAX_BYTES + 1)
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_redirects_fail_without_following_destination(self):
        handler = schema.NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(Request(BASE), None, 302, "Found", {}, "https://elsewhere.invalid/"))
        opener = Mock()
        opener.open.side_effect = HTTPError(BASE, 302, "private-url secret", {}, None)
        with patch.object(schema, "build_opener", return_value=opener) as build:
            with self.assertRaisesRegex(schema.SchemaError, "^METADATA_HTTP_302$"):
                schema.fetch_metadata({"ONEC_ODATA_BASE_URL": BASE})
        self.assertIsInstance(build.call_args.args[0], schema.NoRedirectHandler)
        self.assertEqual(opener.open.call_count, 1)

    def test_https_and_credential_url_restrictions(self):
        urls = [BASE.replace("https:", "http:"), BASE + "?token=secret", BASE + "#fragment", BASE.replace("example.invalid", "user:secret@example.invalid"), "https://example.invalid/private/", BASE + "\n"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(schema.SchemaError):
                schema.metadata_url(url)
        opener = Mock()
        with self.assertRaises(schema.SchemaError):
            schema.fetch_metadata({"ONEC_ODATA_BASE_URL": BASE, "ONEC_ODATA_USERNAME": "user"}, opener=opener)
        opener.open.assert_not_called()

    def test_payload_limits_malformed_xml_and_dtd(self):
        payloads = [b'<broken', b'<!DOCTYPE Schema [<!ENTITY secret "value">]><Schema/>', b'<html/>', '<Schema/>'.encode('utf-16')]
        for raw in payloads:
            with self.subTest(raw=raw), self.assertRaises(schema.SchemaError):
                schema.describe_metadata(raw)
        with patch.object(schema, "MAX_BYTES", 8):
            with self.assertRaisesRegex(schema.SchemaError, "METADATA_SIZE_LIMIT"):
                schema.describe_metadata(b'x' * 9)
        with patch.object(schema, "MAX_OUTPUT_BYTES", 1):
            with self.assertRaisesRegex(schema.SchemaError, "SCHEMA_OUTPUT_LIMIT"):
                schema.describe_metadata(metadata(''))
        result = schema.describe_metadata(metadata(''))
        actual_size = len(schema.serialize_result(result).encode())
        with patch.object(schema, "MAX_OUTPUT_BYTES", actual_size - 1):
            with self.assertRaisesRegex(schema.SchemaError, "SCHEMA_OUTPUT_LIMIT"):
                schema.describe_metadata(metadata(''))

    def test_main_safe_errors_env_precedence_and_no_settings_import(self):
        fake_dotenv = SimpleNamespace(dotenv_values=lambda path: {"ONEC_ODATA_USERNAME": "file-user", "ONEC_ODATA_PASSWORD": "file-secret"})
        output = io.StringIO()
        with patch.dict("sys.modules", {"dotenv": fake_dotenv}), patch.dict(schema.os.environ, {"ONEC_ODATA_USERNAME": "env-user"}, clear=True), patch.object(schema, "fetch_metadata", side_effect=URLError("private endpoint and file-secret")) as fetch, contextlib.redirect_stdout(output):
            self.assertEqual(schema.main(["--app-dir", "/unused"]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "METADATA_CHECK_FAILED"})
        self.assertEqual(fetch.call_args.args[0]["ONEC_ODATA_USERNAME"], "env-user")
        self.assertEqual(fetch.call_args.args[0]["ONEC_ODATA_PASSWORD"], "file-secret")
        source = SCRIPT.read_text()
        self.assertNotIn("django.setup", source)
        self.assertNotIn("service_site.settings", source)


if __name__ == "__main__":
    unittest.main()
