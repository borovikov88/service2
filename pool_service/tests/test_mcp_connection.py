import json

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from pool_service.mcp_views import MAX_REQUEST_BYTES, MCP_PROTOCOL_VERSION, TEST_MESSAGE, TOOL_NAME


@override_settings(ADVISOR_MCP_TEST_ENABLED=True)
class McpConnectionTestTests(SimpleTestCase):
    def post(self, payload, **extra):
        extra.setdefault("HTTP_MCP_PROTOCOL_VERSION", MCP_PROTOCOL_VERSION)
        return self.client.post(
            reverse("mcp_test"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            **extra,
        )

    def test_initialize_is_available_without_login_or_database(self):
        response = self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test-client", "version": "1.0"}},
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(response.json()["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(response["MCP-Protocol-Version"], "2025-03-26")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_each_supported_protocol_version_is_used_for_followup_requests(self):
        for version in ("2025-03-26", MCP_PROTOCOL_VERSION):
            with self.subTest(version=version):
                initialize = self.post(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {"name": "test-client", "version": "1.0"},
                        },
                    },
                    HTTP_MCP_PROTOCOL_VERSION=version,
                )
                tools = self.post(
                    {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
                    HTTP_MCP_PROTOCOL_VERSION=version,
                )
                call = self.post(
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {"name": TOOL_NAME, "arguments": {}},
                    },
                    HTTP_MCP_PROTOCOL_VERSION=version,
                )

                self.assertEqual(initialize.json()["result"]["protocolVersion"], version)
                self.assertEqual(tools.status_code, 200)
                self.assertEqual(call.status_code, 200)
                for response in (initialize, tools, call):
                    self.assertEqual(response["MCP-Protocol-Version"], version)

    def test_initialize_requires_complete_handshake_fields(self):
        valid = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        }
        invalid_params = [
            {},
            {**valid, "protocolVersion": None},
            {**valid, "capabilities": []},
            {**valid, "clientInfo": {}},
            {**valid, "clientInfo": {"name": "test-client", "version": 1}},
        ]

        for params in invalid_params:
            with self.subTest(params=params):
                response = self.post(
                    {"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": params}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["error"]["code"], -32602)

    def test_tools_list_exposes_only_fixed_test_tool(self):
        response = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [TOOL_NAME])
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
        self.assertFalse(tools[0]["annotations"]["destructiveHint"])
        self.assertIn("financial data", tools[0]["description"])

    def test_tool_call_returns_explicit_non_financial_test_result(self):
        response = self.post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": {}},
            }
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["content"], [{"type": "text", "text": TEST_MESSAGE}])
        self.assertEqual(
            result["structuredContent"],
            {"kind": "test", "financial_data": False, "message": "Connector invocation succeeded."},
        )
        self.assertFalse(result["isError"])

    def test_notifications_are_acknowledged_without_a_json_body(self):
        response = self.post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_initialized_notification_with_id_is_rejected_as_a_request(self):
        response = self.post(
            {
                "jsonrpc": "2.0",
                "id": 16,
                "method": "notifications/initialized",
                "params": {},
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32600)
        self.assertEqual(response.json()["id"], 16)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_unknown_method_returns_jsonrpc_error(self):
        response = self.post({"jsonrpc": "2.0", "id": 4, "method": "unknown", "params": {}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"]["code"], -32601)

    def test_unknown_tool_and_arguments_are_rejected(self):
        unknown = self.post(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "unknown", "arguments": {}},
            }
        )
        arguments = self.post(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": {"month": "test"}},
            }
        )

        self.assertEqual(unknown.json()["error"]["code"], -32602)
        self.assertEqual(arguments.json()["error"]["code"], -32602)

    def test_invalid_requests_are_rejected(self):
        malformed = self.client.post(
            reverse("mcp_test"),
            data="{",
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            HTTP_MCP_PROTOCOL_VERSION=MCP_PROTOCOL_VERSION,
        )
        wrong_media_type = self.client.post(
            reverse("mcp_test"), data="{}", content_type="text/plain", HTTP_ACCEPT="application/json"
        )
        unacceptable = self.client.post(
            reverse("mcp_test"), data="{}", content_type="application/json", HTTP_ACCEPT="text/html"
        )

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], -32700)
        self.assertEqual(wrong_media_type.status_code, 415)
        self.assertEqual(unacceptable.status_code, 406)
        self.assertIn("application/json and text/event-stream", unacceptable.json()["error"]["message"])

    def test_security_boundaries_reject_origin_oversize_and_protocol_mismatch(self):
        payload = {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}}
        unknown_origin = self.post(payload, HTTP_ORIGIN="https://untrusted.example")
        oversized = self.client.post(
            reverse("mcp_test"),
            data=b"{}",
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            HTTP_CONTENT_LENGTH=str(MAX_REQUEST_BYTES + 1),
        )
        wrong_protocol = self.post(payload, HTTP_MCP_PROTOCOL_VERSION="2024-11-05")

        self.assertEqual(unknown_origin.status_code, 403)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(wrong_protocol.status_code, 400)

    @override_settings(ADVISOR_MCP_TEST_ENABLED=False)
    def test_probe_is_disabled_by_default(self):
        response = self.post({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}})

        self.assertEqual(response.status_code, 404)

    def test_get_is_not_allowed_and_post_is_csrf_exempt(self):
        get_response = self.client.get(reverse("mcp_test"))
        csrf_client = self.client_class(enforce_csrf_checks=True)
        post_response = csrf_client.post(
            reverse("mcp_test"),
            data=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}),
            content_type="application/json",
            HTTP_ACCEPT="application/json, text/event-stream",
            HTTP_MCP_PROTOCOL_VERSION=MCP_PROTOCOL_VERSION,
        )

        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(get_response["Cache-Control"], "no-store")
        self.assertEqual(post_response.status_code, 200)

    def test_empty_error_and_notification_responses_disable_caching(self):
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        notification = self.post(payload, HTTP_MCP_PROTOCOL_VERSION="2025-03-26")
        unknown_origin = self.post(
            {"jsonrpc": "2.0", "id": 14, "method": "tools/list", "params": {}},
            HTTP_ORIGIN="https://untrusted.example",
        )
        with override_settings(ADVISOR_MCP_TEST_ENABLED=False):
            disabled = self.post(
                {"jsonrpc": "2.0", "id": 15, "method": "tools/list", "params": {}}
            )

        for response in (notification, unknown_origin, disabled):
            self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(notification["MCP-Protocol-Version"], "2025-03-26")
