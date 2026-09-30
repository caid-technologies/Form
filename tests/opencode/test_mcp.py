from __future__ import annotations

import json
import unittest
from itertools import product
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from forma_core.opencode.store import OpenCodeStore
from apps.api.opencode_api import router
from apps.api.opencode_mcp import _revision_identifier, opencode_mcp_tools
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.opencode.models import (
    McpInitializeParams,
    McpJsonRpcRequest,
    McpRequestParams,
    McpToolArguments,
    McpToolCallParams,
    McpToolsListParams,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from tests.opencode.test_outcomes import wired_project


class OpenCodeMcpModelTests(unittest.TestCase):
    def test_tool_parameters_do_not_depend_on_provider_reference_support(self) -> None:
        def check(value):
            self.assertIn(value["type"], {"object", "array", "string", "number", "integer", "boolean"})
            self.assertFalse({"$ref", "$defs", "anyOf", "oneOf", "allOf"}.intersection(value))
            for child in value.get("properties", {}).values():
                check(child)
            if "items" in value:
                check(value["items"])

        for tool in opencode_mcp_tools():
            with self.subTest(tool=tool["name"]):
                check(tool["inputSchema"])

    def test_authoring_string_retains_complete_schema_guidance(self) -> None:
        for tool in opencode_mcp_tools():
            if tool["name"] not in {
                "forma.opencode.update_project", "forma.opencode.compile_project",
                "forma.opencode.validate_project",
            }:
                continue
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual(["project_ir"], schema["required"])
                argument = schema["properties"]["project_ir"]
                self.assertEqual("string", argument["type"])
                guidance = json.loads(argument["description"].split("JSON Schema: ", 1)[1])
                self.assertEqual(HardwareIntermediateRepresentation.model_json_schema(), guidance)
                self.assertIn("pin_type", guidance["$defs"]["PinDefinition"]["required"])

                def check(value):
                    if isinstance(value, dict):
                        if "$ref" in value:
                            node = guidance
                            for key in value["$ref"].removeprefix("#/").split("/"):
                                node = node[key]
                        for child in value.values():
                            check(child)
                    elif isinstance(value, list):
                        for child in value:
                            check(child)

                check(guidance)

    def test_revision_identifier_uses_canonical_revision_id(self) -> None:
        revision = type("Revision", (), {"revision_id": "revision-1"})()

        self.assertEqual("revision-1", _revision_identifier(revision))

    def test_model_validate_retains_typed_variants_and_wire_aliases(self) -> None:
        for method, params, expected_type in (
            ("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "opencode", "version": "1.0.0"},
                "_meta": {"progressToken": "init"},
            }, McpInitializeParams),
            ("tools/call", {"name": "forma.opencode.read_project", "arguments": {}}, McpToolCallParams),
            ("tools/list", {"cursor": "opaque"}, McpToolsListParams),
            ("ping", {}, McpRequestParams),
            ("notifications/initialized", {}, McpRequestParams),
        ):
            with self.subTest(method=method):
                payload = {"jsonrpc": "2.0", "method": method, "params": params}
                request = McpJsonRpcRequest.model_validate(payload)
                self.assertIs(type(request.params), expected_type)
                self.assertEqual(payload, request.model_dump(by_alias=True, exclude_unset=True))
                self.assertEqual(request, McpJsonRpcRequest.model_validate_json(
                    request.model_dump_json(by_alias=True, exclude_unset=True),
                ))
                self.assertEqual(request, McpJsonRpcRequest.model_validate({**payload, "params": request.params}))
                if isinstance(request.params, McpToolCallParams):
                    self.assertIsInstance(request.params.arguments, McpToolArguments)

    def test_typed_params_cannot_bypass_method_validation(self) -> None:
        params = McpToolCallParams(name="forma.opencode.read_project")
        for method in ("initialize", "ping", "notifications/initialized", "tools/list"):
            with self.subTest(method=method), self.assertRaises(ValidationError):
                McpJsonRpcRequest.model_validate({"jsonrpc": "2.0", "method": method, "params": params})


class OpenCodeMcpHttpTests(unittest.TestCase):
    def test_nested_authoring_errors_are_actionable_and_do_not_echo_secrets(self) -> None:
        cases = (
            ({"overview": {"title": "SECRET_CANARY"}}, ["project_ir", "overview", "description"]),
            ({"components": [{"ref_des": "SECRET_CANARY"}]}, ["project_ir", "components", 0, "rationale"]),
            ({"part_definitions": [{"part_definition_id": "p", "part_number": "p", "name": "p", "category": "Module", "pins": [{"pin_id": "SECRET_CANARY"}]}]}, ["project_ir", "part_definitions", 0, "pins", 0, "pin_type"]),
            ({"part_definitions": [{"part_definition_id": "p", "part_number": "p", "name": "p", "category": "Module", "dimensions_mm": {"SECRET_CANARY": "PRIVATE_VALUE"}}]}, ["project_ir", "part_definitions", 0, "dimensions_mm", "<key>"]),
        )
        for (ir, expected), encoded in product(cases, (False, True)):
            with self.subTest(expected=expected, encoded=encoded), patch("apps.api.opencode_mcp._persist_mcp_compile") as persist:
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": "bad", "method": "tools/call",
                    "params": {"name": "forma.opencode.compile_project", "arguments": {"project_ir": json.dumps(ir) if encoded else ir}},
                })
                self.assertEqual(200, response.status_code)
                self.assertTrue(response.json()["result"]["isError"])
                errors = response.json()["result"]["structuredContent"]["errors"]
                self.assertIn(expected, [error["path"] for error in errors])
                self.assertTrue(all(set(error) == {"path", "type"} for error in errors))
                self.assertNotIn("SECRET_CANARY", response.text)
                self.assertNotIn("PRIVATE_VALUE", response.text)
                persist.assert_not_called()

    def test_invalid_ir_cannot_bypass_authorization(self) -> None:
        self.authorize.side_effect = HTTPException(status_code=403, detail="denied")
        for ir in ({"overview": {}}, '{"overview": {}}', '{"SECRET_CANARY":'):
            with self.subTest(ir=ir):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "forma.opencode.compile_project", "arguments": {"project_ir": ir}},
                })
                self.assertEqual(403, response.status_code)
                self.assertNotIn("hardware_ir_invalid", response.text)

    def test_invalid_encoded_ir_returns_safe_validation_errors_without_saving(self) -> None:
        for ir in ('{"SECRET_CANARY":', "null", "[]", '"SECRET_CANARY"', "42"):
            with self.subTest(ir=ir), patch("apps.api.opencode_mcp._persist_mcp_compile") as persist:
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": "bad-json", "method": "tools/call",
                    "params": {"name": "forma.opencode.compile_project", "arguments": {"project_ir": ir}},
                })
                self.assertEqual(200, response.status_code)
                result = response.json()["result"]
                self.assertTrue(result["isError"])
                self.assertEqual("hardware_ir_invalid", result["structuredContent"]["code"])
                self.assertEqual(["project_ir"], result["structuredContent"]["errors"][0]["path"])
                self.assertNotIn("SECRET_CANARY", response.text)
                persist.assert_not_called()

    def test_bad_authoring_call_in_batch_does_not_block_corrected_call(self) -> None:
        response = self.client.post("/api/opencode/mcp", json=[
            {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                "name": "forma.opencode.validate_project", "arguments": {"project_ir": ir},
            }} for index, ir in enumerate(({"overview": {}}, {"components": [], "nets": []}))
        ])
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()[0]["result"]["isError"])
        self.assertTrue(response.json()[1]["result"]["structuredContent"]["is_valid"])

    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api")
        self.capability = ConnectorCapability(
            "mini", "session", str(uuid4()), "user", 2_000_000_000, "nonce", frozenset({"mcp"}),
        )
        self.authorize = self.enterContext(patch(
            "apps.api.opencode_api._connector_capability", return_value=self.capability,
        ))
        store = OpenCodeStore(":memory:")
        self.addCleanup(store.close)
        store.create_session(session_id="session", connector_id="mini", owner_user_id="user", project_id=self.capability.project_id)
        self.enterContext(patch("apps.api.opencode_api.OPENCODE_STORE", store))
        self.client = self.enterContext(TestClient(self.app))
        self.initialize_params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "opencode", "version": "1.0.0"},
        }

    def test_standard_initialize_reaches_handler(self) -> None:
        response = self.client.post("/api/opencode/mcp", json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": self.initialize_params,
        }, headers={"Authorization": "Bearer test-capability"})

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({
            "jsonrpc": "2.0", "id": 0,
            "result": {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "forma-opencode", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        }, response.json())
        self.authorize.assert_called_once_with("test-capability", scope="mcp")

    def test_initialize_accepts_capability_extensions_and_metadata(self) -> None:
        params = {
            **self.initialize_params,
            "clientInfo": {"name": "opencode", "version": "1.0.0", "title": "OpenCode"},
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
                "elicitation": {"form": {}},
                "experimental": {"example.test/feature": {"versions": [1, 2]}},
                "example.test/extension": {"enabled": True},
            },
            "_meta": {"progressToken": 1, "example.test/trace": {"tags": ["initialize"]}},
        }
        response = self.client.post("/api/opencode/mcp", json={
            "jsonrpc": "2.0", "id": "init", "method": "initialize", "params": params,
        })
        self.assertEqual(200, response.status_code, response.text)

    def test_initialize_accepts_newer_client_metadata_before_protocol_negotiation(self) -> None:
        client_info = {
            "name": "opencode", "version": "1.0.0", "title": "OpenCode",
            "description": "Project authoring client", "websiteUrl": "https://opencode.ai",
            "icons": [{"src": "https://opencode.ai/icon.svg", "mimeType": "image/svg+xml",
                       "sizes": ["any"], "theme": "dark"}],
        }
        payload = {
            "jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {**self.initialize_params, "protocolVersion": "2025-11-25", "clientInfo": client_info},
        }
        request = McpJsonRpcRequest.model_validate(payload)
        self.assertEqual(payload, request.model_dump(by_alias=True, exclude_unset=True))
        response = self.client.post("/api/opencode/mcp", json=payload)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("2025-06-18", response.json()["result"]["protocolVersion"])

    def test_openapi_includes_typed_initialization_and_client_metadata(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(200, response.status_code)
        schemas = response.json()["components"]["schemas"]
        self.assertEqual({"protocolVersion", "capabilities", "clientInfo"}, set(schemas["McpInitializeParams"]["required"]))
        self.assertEqual({"name", "version"}, set(schemas["McpClientInfo"]["required"]))
        self.assertIn("websiteUrl", schemas["McpClientInfo"]["properties"])
        self.assertIn("mimeType", schemas["McpIcon"]["properties"])

    def test_initialize_requires_protocol_capabilities_and_client_identity(self) -> None:
        invalid_params = [None, {}, []]
        invalid_params.extend(
            {key: value for key, value in self.initialize_params.items() if key != missing}
            for missing in ("protocolVersion", "capabilities", "clientInfo")
        )
        invalid_params.extend(
            {**self.initialize_params, "clientInfo": info}
            for info in ({}, {"name": "opencode"}, {"version": "1.0.0"})
        )
        for params in invalid_params:
            with self.subTest(params=params):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params,
                })
                self.assertEqual(422, response.status_code, response.text)
        response = self.client.post("/api/opencode/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        self.assertEqual(422, response.status_code, response.text)
        self.authorize.assert_not_called()

    def test_initialize_rejects_invalid_known_fields(self) -> None:
        for changes in (
            {"protocolVersion": 123},
            {"protocolVersion": None},
            {"capabilities": None},
            {"capabilities": []},
            {"capabilities": {"roots": {"listChanged": "true"}}},
            {"capabilities": {"sampling": True}},
            {"capabilities": {"elicitation": []}},
            {"capabilities": {"experimental": {"feature": True}}},
            {"clientInfo": None},
            {"clientInfo": {"name": "opencode", "version": 1}},
            {"clientInfo": {"name": "opencode", "version": "1", "unknown": True}},
            {"clientInfo": {"name": "opencode", "version": "1", "description": 1}},
            {"clientInfo": {"name": "opencode", "version": "1", "websiteUrl": []}},
            {"clientInfo": {"name": "opencode", "version": "1", "icons": [{}]}},
            {"clientInfo": {"name": "opencode", "version": "1", "icons": [{"src": "icon.svg", "sizes": "any"}]}},
            {"clientInfo": {"name": "opencode", "version": "1", "icons": [{"src": "icon.svg", "theme": "unknown"}]}},
            {"_meta": []},
            {"arguments": {}},
            {"unknown": True},
        ):
            with self.subTest(changes=changes):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {**self.initialize_params, **changes},
                })
                self.assertEqual(422, response.status_code, response.text)
        self.authorize.assert_not_called()

    def test_initialized_ping_and_tools_list_accept_optional_params(self) -> None:
        for method in ("notifications/initialized", "ping", "tools/list"):
            for fields in ({}, {"params": {}}, {"params": {"_meta": {"example.test/trace": "test"}}}):
                with self.subTest(method=method, fields=fields):
                    payload = {"jsonrpc": "2.0", "method": method, **fields}
                    if method != "notifications/initialized":
                        payload["id"] = "request"
                    response = self.client.post("/api/opencode/mcp", json=payload)
                    if method == "notifications/initialized":
                        self.assertEqual(202, response.status_code, response.text)
                        self.assertEqual(b"", response.content)
                    else:
                        self.assertEqual(200, response.status_code, response.text)
                        expected = {"tools": opencode_mcp_tools()} if method == "tools/list" else {}
                        self.assertEqual(expected, response.json()["result"])
        response = self.client.post("/api/opencode/mcp", json={
            "jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {"cursor": "opaque"},
        })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(opencode_mcp_tools(), response.json()["result"]["tools"])

    def test_params_are_validated_by_method_not_by_union_shape(self) -> None:
        for method, params in (
            ("initialize", {"name": "forma.opencode.read_project", "arguments": {}}),
            ("tools/call", self.initialize_params),
            ("ping", self.initialize_params),
            ("notifications/initialized", {"name": "forma.opencode.read_project"}),
            ("tools/list", {"name": "forma.opencode.read_project"}),
            ("tools/list", {"cursor": 123}),
            ("tools/call", {"name": "forma.opencode.read_project", "arguments": {"project_id": "foreign"}}),
        ):
            with self.subTest(method=method, params=params):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                })
                self.assertEqual(422, response.status_code, response.text)
        self.authorize.assert_not_called()

    def test_unknown_methods_and_forbidden_tools_still_rejected(self) -> None:
        response = self.client.post("/api/opencode/mcp", json={
            "jsonrpc": "2.0", "id": "unknown", "method": "unknown/method",
        })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(-32601, response.json()["error"]["code"])
        for name in ("forma.generate_project", "forma.opencode.unknown", "shell"):
            with self.subTest(name=name):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": name, "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                })
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(-32003, response.json()["error"]["code"])
                self.assertEqual("authorization_required", response.json()["error"]["data"]["code"])

    def test_missing_tool_name_retains_json_rpc_invalid_params_error(self) -> None:
        for fields in ({}, {"params": {}}, {"params": {"name": ""}}):
            with self.subTest(fields=fields):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call", **fields,
                })
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(-32602, response.json()["error"]["code"])

    def test_allowed_tool_retains_typed_arguments_with_metadata(self) -> None:
        with patch("apps.api.opencode_mcp.validate_circuit", return_value=[]) as validate:
            response = self.client.post("/api/opencode/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "forma.opencode.validate_project",
                    "arguments": {"project_ir": {"components": [], "nets": []}},
                    "_meta": {"progressToken": "validation"},
                },
            })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"is_valid": True, "issues": []}, response.json()["result"]["structuredContent"])
        validate.assert_called_once()

    def test_empty_draft_creation_remains_saveable(self) -> None:
        with patch("apps.api.opencode_mcp._persist_mcp_compile") as persist, \
             patch("apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None), \
             patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=None), \
             patch("apps.api.opencode_mcp.ensure_native_cad_model"):
            response = self.client.post("/api/opencode/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "forma.opencode.create_project", "arguments": {}},
            })
            self.assertEqual(200, response.status_code, response.text)
            result = response.json()["result"]["structuredContent"]
            self.assertEqual([], result["project_ir"]["components"])
            self.assertEqual([], result["project_ir"]["nets"])
            self.assertEqual("draft", result["design_outcome"]["project_readiness"])
            self.assertEqual({"is_valid": True, "issues": []}, result["validation"])
            persist.assert_called_once()

    def test_wired_design_compiles_with_populated_verified_outcome(self) -> None:
        with patch("apps.api.opencode_mcp._persist_mcp_compile") as persist, \
             patch("apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None), \
             patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=None), \
             patch("apps.api.opencode_mcp.ensure_native_cad_model"):
            response = self.client.post("/api/opencode/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "forma.opencode.compile_project", "arguments": {"project_ir": wired_project().model_dump(mode="json")}},
            })
            self.assertEqual(200, response.status_code, response.text)
            result = response.json()["result"]["structuredContent"]
            self.assertEqual("complete", result["design_outcome"]["project_readiness"])
            self.assertEqual(2, result["design_outcome"]["component_count"])
            self.assertEqual(2, result["design_outcome"]["pin_count"])
            self.assertEqual(1, result["design_outcome"]["net_count"])
            self.assertTrue(result["validation"]["is_valid"])
            persist.assert_called_once()

    def test_encoded_ir_preserves_recursive_and_arbitrary_fields_for_all_authoring_tools(self) -> None:
        project_ir = wired_project().model_dump(mode="json")
        node = {
            "system_id": "product", "name": "Product", "domain": "product",
            "purpose": "Exercise the complete authoring contract", "children": [],
        }
        # Deeper than Vertex's recursive-reference expansion limit.
        for index in range(4):
            node = {**node, "system_id": f"product.level{index}", "children": [node]}
        project_ir["system_architecture"] = {"summary": "Nested systems", "root": node}
        project_ir["assembly_metadata"] = {"custom": {"values": [None, True, 1.5, "µm"]}}
        project_ir["cad_model"] = {"custom_payload": [False, {"mesh": []}]}
        expected = HardwareIntermediateRepresentation.model_validate(project_ir)

        for operation in ("validate", "update", "compile"):
            with self.subTest(operation=operation), \
                 patch("apps.api.opencode_mcp._persist_mcp_compile") as persist, \
                 patch("apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None), \
                 patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=None), \
                 patch("apps.api.opencode_mcp.ensure_native_cad_model"):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": f"forma.opencode.{operation}_project", "arguments": {"project_ir": json.dumps(project_ir)}},
                })
                self.assertEqual(200, response.status_code, response.text)
                result = response.json()["result"]["structuredContent"]
                validation = result if operation == "validate" else result["validation"]
                self.assertTrue(validation["is_valid"])
                if operation == "validate":
                    persist.assert_not_called()
                else:
                    persist.assert_called_once()
                    saved = persist.call_args.args[0]
                    self.assertEqual(expected.system_architecture, saved.system_architecture)
                    self.assertEqual(expected.assembly_metadata["custom"], saved.assembly_metadata["custom"])
                    self.assertEqual(expected.cad_model, saved.cad_model)
                    self.assertEqual("complete", result["design_outcome"]["project_readiness"])

    def test_pinless_saved_shape_is_invalid_through_validate_update_and_compile(self) -> None:
        project_ir = {
            "part_definitions": [{
                "part_definition_id": "module", "part_number": "MODULE", "name": "Controller module",
                "category": "Microcontroller", "pins": [],
            }],
            "components": [{"ref_des": f"U{index}", "part_definition_id": "module", "rationale": "Control"}
                           for index in range(1, 10)],
            "nets": [],
            "power_rails": [{"rail_id": f"rail-{index}", "voltage": voltage,
                             "max_current_capacity_ma": 500, "source_component": "U1"}
                            for index, voltage in enumerate((3.3, 5, 12))],
            "buses": [],
            "pin_mappings": [],
            "is_valid": True,
            "validation": {"critical": [], "warning": [], "info": []},
        }
        for operation, encoded in product(("validate", "update", "compile"), (False, True)):
            with self.subTest(operation=operation, encoded=encoded), \
                 patch("apps.api.opencode_mcp._persist_mcp_compile") as persist, \
                 patch("apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None), \
                 patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=None), \
                 patch("apps.api.opencode_mcp.ensure_native_cad_model"):
                response = self.client.post("/api/opencode/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": f"forma.opencode.{operation}_project", "arguments": {"project_ir": json.dumps(project_ir) if encoded else project_ir}},
                })
                self.assertEqual(200, response.status_code, response.text)
                result = response.json()["result"]["structuredContent"]
                validation = result if operation == "validate" else result["validation"]
                self.assertFalse(validation["is_valid"])
                self.assertEqual(10, len(validation["issues"]))
                if operation != "validate":
                    self.assertEqual("partial", result["design_outcome"]["project_readiness"])
                    self.assertFalse(result["project_ir"]["is_valid"])
                    self.assertFalse(persist.call_args.args[0].is_valid)

    def test_initialize_cannot_bypass_capability_authorization(self) -> None:
        self.authorize.side_effect = HTTPException(status_code=403, detail="test denial")
        response = self.client.post("/api/opencode/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": self.initialize_params,
        })
        self.assertEqual(403, response.status_code, response.text)

    def test_batch_validates_initialize_and_omits_notification_responses(self) -> None:
        response = self.client.post("/api/opencode/mcp", json=[
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": self.initialize_params},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([1, 2], [item["id"] for item in response.json()])
        response = self.client.post("/api/opencode/mcp", json=[
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ])
        self.assertEqual(422, response.status_code, response.text)
