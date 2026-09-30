from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from apps.api import a2a
from apps.api.a2a import A2AMessage, MCP_DEFAULT_PROTOCOL_VERSION, _persist_mcp_compile, handle_mcp_json_rpc
from apps.api.auth import UserContext
from apps.api.main import app
from forma_core.workspaces.projects import ProjectStateError
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


class McpAgentCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _user(owner_user_id: str = "agent-user") -> UserContext:
        return UserContext(
            provider="test",
            subject=owner_user_id,
            owner_user_id=owner_user_id,
            is_authenticated=True,
            is_admin=False,
        )

    def test_mcp_route_accepts_a_dedicated_agent_api_key(self) -> None:
        api_key = "n" * 32
        with patch.dict(
            os.environ,
            {"FORMA_AUTH_MODE": "clerk", "FORMA_MCP_API_KEY": api_key},
            clear=True,
        ):
            response = TestClient(app).post(
                "/mcp",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
            )

        self.assertEqual(200, response.status_code)
        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        self.assertIn("forma.compile_project", names)

    async def test_initialize_negotiates_current_protocol(self) -> None:
        response = await handle_mcp_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "opencode"}},
            }
        )

        self.assertEqual("2025-06-18", response["result"]["protocolVersion"])
        self.assertEqual("forma-oss", response["result"]["serverInfo"]["name"])

    async def test_unknown_protocol_falls_back_to_supported_version(self) -> None:
        response = await handle_mcp_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": "2099-01-01"},
            }
        )

        self.assertEqual(MCP_DEFAULT_PROTOCOL_VERSION, response["result"]["protocolVersion"])

    async def test_initialized_notification_has_no_json_rpc_response(self) -> None:
        response = await handle_mcp_json_rpc(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )

        self.assertIsNone(response)

    async def test_invalid_mcp_params_use_sanitized_error_contract(self) -> None:
        response = await handle_mcp_json_rpc(
            {"jsonrpc": "2.0", "id": "invalid", "method": "tools/call", "params": []}
        )

        self.assertEqual(-32602, response["error"]["code"])
        self.assertEqual("The MCP request parameters are invalid.", response["error"]["message"])
        self.assertEqual("mcp_invalid_params", response["error"]["data"]["code"])
        self.assertRegex(response["error"]["data"]["correlation_id"], r"^err_[0-9a-f]{32}$")

    async def test_batch_omits_notification_responses(self) -> None:
        response = await handle_mcp_json_rpc(
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ]
        )

        self.assertEqual([{"jsonrpc": "2.0", "id": 2, "result": {}}], response)

    async def test_tools_include_host_authored_compiler(self) -> None:
        response = await handle_mcp_json_rpc(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        )

        tools = response["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertIn("forma.compile_project", names)
        compiler = next(tool for tool in tools if tool["name"] == "forma.compile_project")
        authoring_agents = compiler["inputSchema"]["properties"]["authoring_agent"]["enum"]
        self.assertIn("nemoclaw", authoring_agents)

    async def test_compile_project_persists_public_project_and_returns_identity(self) -> None:
        with patch("apps.api.a2a.get_project_identity", return_value=None), patch(
            "apps.api.a2a.persist_legacy_project_projection"
        ) as save_project:
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "forma.compile_project",
                        "arguments": {
                            "project_ir": {"components": [], "nets": []},
                            "authoring_agent": "nemoclaw",
                        },
                    },
                }
            )

        compiled = response["result"]["structuredContent"]
        self.assertTrue(compiled["persisted"])
        self.assertIsNone(compiled["chat_id"])
        self.assertEqual("public", compiled["visibility"])
        self.assertRegex(compiled["project_id"], r"^[0-9a-f-]{36}$")
        save_project.assert_called_once()
        self.assertEqual(compiled["project_id"], save_project.call_args.kwargs["project_id"])
        self.assertEqual("public", save_project.call_args.kwargs["visibility"])

    async def test_compile_project_persists_authenticated_private_project(self) -> None:
        user = UserContext(
            provider="test",
            subject="agent-user",
            owner_user_id="agent-user",
            is_authenticated=True,
            is_admin=False,
        )
        with patch("apps.api.a2a.get_project_identity", return_value=None), patch(
            "apps.api.a2a.persist_chat_project_revision"
        ) as persist_revision:
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "forma.compile_project",
                        "arguments": {
                            "project_ir": {"components": [], "nets": []},
                            "prompt": "Build a private sensor enclosure",
                            "visibility": "private",
                        },
                    },
                },
                user,
            )

        compiled = response["result"]["structuredContent"]
        self.assertEqual("private", compiled["visibility"])
        self.assertIsNotNone(compiled["chat_id"])
        self.assertEqual("agent-user", persist_revision.call_args.args[1])
        self.assertEqual("Build a private sensor enclosure", persist_revision.call_args.kwargs["prompt"])
        self.assertEqual("private", persist_revision.call_args.kwargs["visibility"])

    def test_compile_recovers_a_partial_identity_without_a_revision(self) -> None:
        project_id = "12345678-1234-4234-8234-123456789012"
        project = HardwareIntermediateRepresentation.model_validate({"components": [], "nets": []})
        with (
            patch(
                "apps.api.a2a.get_project_identity",
                return_value={
                    "owner_user_id": "agent-user",
                    "status": "active",
                    "chat_id": "chat-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "visibility": "public",
                },
            ),
            patch(
                "apps.api.a2a.get_latest_project_revision",
                side_effect=ProjectStateError("project_revision_not_found", "missing"),
            ),
            patch("apps.api.a2a.persist_chat_project_revision") as persist_revision,
        ):
            result = _persist_mcp_compile(
                project,
                {
                    "project_id": project_id,
                    "prompt": "OpenCode project",
                    "visibility": "private",
                    "authoring_agent": "opencode",
                },
                self._user(),
            )

        self.assertEqual(project_id, result["project_id"])
        self.assertEqual("private", result["visibility"])
        persist_revision.assert_called_once()

    def test_opencode_revisions_preserve_the_original_brief_and_chat(self) -> None:
        project_id = "12345678-1234-4234-8234-123456789012"
        original = "A nylon mechanical gear with 36 rounded teeth, no electronics."
        prior = HardwareIntermediateRepresentation.model_validate({"components": [], "nets": [], "assembly_metadata": {"source_prompt": original}})
        project = HardwareIntermediateRepresentation.model_validate({"components": [], "nets": [], "assembly_metadata": {"source_prompt": "Render this project"}})
        with (
            patch("apps.api.a2a.get_project_identity", return_value={
                "owner_user_id": "agent-user", "status": "active", "chat_id": "original-chat",
                "created_at": "2026-01-01T00:00:00Z", "visibility": "private", "prompt": original,
            }),
            patch("apps.api.a2a.get_latest_project_revision", return_value=SimpleNamespace(state=prior)),
            patch("apps.api.a2a.persist_chat_project_revision") as persist,
        ):
            result = _persist_mcp_compile(project, {
                "project_id": project_id, "authoring_agent": "opencode", "prompt": "OpenCode project",
            }, self._user())
        self.assertEqual(original, persist.call_args.kwargs["prompt"])
        self.assertEqual(original, project.assembly_metadata["source_prompt"])
        self.assertEqual("original-chat", result["chat_id"])

    async def test_compile_project_persists_and_validates(self) -> None:
        with patch("apps.api.a2a.get_project_identity", return_value=None), patch(
            "apps.api.a2a.persist_legacy_project_projection"
        ), patch("apps.api.a2a.persist_chat_project_revision"):
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "forma.compile_project",
                        "arguments": {
                            "project_ir": {"components": [], "nets": []},
                            "authoring_agent": "nemoclaw",
                        },
                    },
                }
            )

        compiled = response["result"]["structuredContent"]
        self.assertTrue(compiled["is_valid"])
        self.assertEqual("nemoclaw", compiled["project_ir"]["assembly_metadata"]["authoring_agent"])
        self.assertIn("mermaid_code", compiled)
        self.assertIn("svg_schematic", compiled)

    async def test_compile_project_cannot_update_another_owner(self) -> None:
        project_id = "12345678-1234-4234-8234-123456789012"
        with patch(
            "apps.api.a2a.get_project_identity",
            return_value={
                "owner_user_id": "different-user",
                "status": "active",
                "chat_id": None,
                "created_at": "2026-01-01T00:00:00Z",
                "visibility": "private",
            },
        ), patch("apps.api.a2a.refresh_legacy_project_projection") as update_project:
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "forma.compile_project",
                        "arguments": {
                            "project_id": project_id,
                            "project_ir": {"components": [], "nets": []},
                        },
                    },
                },
                UserContext(
                    provider="test",
                    subject="agent-user",
                    owner_user_id="agent-user",
                    is_authenticated=True,
                    is_admin=False,
                ),
            )

        self.assertEqual(-32000, response["error"]["code"])
        self.assertEqual("The MCP tool could not complete the request.", response["error"]["message"])
        self.assertEqual("mcp_tool_failed", response["error"]["data"]["code"])
        self.assertRegex(response["error"]["data"]["correlation_id"], r"^err_[0-9a-f]{32}$")
        update_project.assert_not_called()

    async def test_mcp_tool_error_does_not_echo_exception_details(self) -> None:
        with patch.object(
            a2a,
            "call_forma_action",
            new=AsyncMock(
                side_effect=RuntimeError(
                    "provider response: api_key=sk-testsecret123456 at C:\\secret\\forma.db"
                )
            ),
        ):
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": "safe-error",
                    "method": "tools/call",
                    "params": {"name": "forma.debug_config", "arguments": {}},
                },
                self._user(),
            )

        serialized = str(response)
        self.assertEqual("The MCP tool could not complete the request.", response["error"]["message"])
        self.assertNotIn("sk-testsecret123456", serialized)
        self.assertNotIn("forma.db", serialized)
        self.assertNotIn("provider response", serialized.lower())

    async def test_queued_a2a_error_event_is_sanitized(self) -> None:
        message = A2AMessage(sender="test-agent", action="forma.generate_project")
        with patch.object(
            a2a,
            "call_forma_action",
            new=AsyncMock(side_effect=RuntimeError("provider response: sk-testsecret123456 at C:\\secret\\db")),
        ), patch.object(a2a.JOB_STORE, "mark_running"), patch.object(
            a2a.JOB_STORE, "mark_failed"
        ) as mark_failed, patch.object(a2a.A2A_HUB, "publish", new=AsyncMock()) as publish:
            await a2a._process_server_message(message)

        event = publish.await_args.args[0]
        serialized = str(event.model_dump())
        self.assertEqual("error", event.type)
        self.assertEqual("a2a_action_failed", event.payload["error"]["code"])
        self.assertRegex(event.payload["error"]["correlation_id"], r"^err_[0-9a-f]{32}$")
        self.assertNotIn("sk-testsecret123456", serialized)
        self.assertNotIn("provider response", serialized.lower())
        mark_failed.assert_called_once()
        self.assertEqual("a2a_action_failed", mark_failed.call_args.kwargs["error_code"])

    async def test_mcp_action_forwards_authenticated_context(self) -> None:
        user = self._user()
        with patch.object(a2a, "call_forma_action", new=AsyncMock(return_value={"ok": True})) as action:
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": "context",
                    "method": "tools/call",
                    "params": {"name": "forma.debug_config", "arguments": {}},
                },
                user,
            )

        self.assertEqual({"ok": True}, response["result"]["structuredContent"])
        action.assert_awaited_once()
        self.assertIs(user, action.await_args.args[2])

    async def test_queued_a2a_action_keeps_context_and_replaces_payload_owner(self) -> None:
        user = self._user()
        message = A2AMessage(
            sender="test-agent",
            action="forma.generate_project",
            payload={"prompt": "Build it", "owner_user_id": "attacker-user"},
        )
        with patch.object(a2a.A2A_HUB, "register", new=AsyncMock()), patch.object(
            a2a.A2A_HUB, "publish", new=AsyncMock()
        ), patch.object(a2a.JOB_STORE, "create_job", return_value={"job_id": message.job_id}) as create_job, patch.object(
            a2a, "_process_server_message", new=AsyncMock()
        ) as process:
            await a2a.submit_a2a_message(message, user)
            await asyncio.sleep(0)

        self.assertEqual("agent-user", create_job.call_args.kwargs["payload"]["owner_user_id"])
        process.assert_awaited_once()
        self.assertIs(user, process.await_args.args[1])

    async def test_mcp_a2a_send_forwards_authenticated_context(self) -> None:
        user = self._user()
        ack = SimpleNamespace(model_dump=lambda: {"accepted": True})
        with patch.object(a2a, "submit_a2a_message", new=AsyncMock(return_value=ack)) as submit:
            response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": "send",
                    "method": "tools/call",
                    "params": {
                        "name": "forma.a2a.send_message",
                        "arguments": {"action": "forma.generate_project", "payload": {"prompt": "Build it"}},
                    },
                },
                user,
            )

        self.assertEqual({"accepted": True}, response["result"]["structuredContent"])
        submit.assert_awaited_once()
        self.assertIs(user, submit.await_args.args[1])

    async def test_project_action_cannot_use_payload_owner_without_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "authenticated user context"):
            await a2a.call_forma_action(
                "forma.generate_project",
                {
                    "project_id": "12345678-1234-4234-8234-123456789012",
                    "owner_user_id": "victim-user",
                },
            )


if __name__ == "__main__":
    unittest.main()
