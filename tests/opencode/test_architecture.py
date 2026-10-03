"""Regression tests for topology continuity across hosted OpenCode turns."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from apps.api.auth import UserContext
from apps.api.opencode_api import poll_opencode_command
from apps.api.opencode_mcp import _compile, handle_opencode_mcp_json_rpc
from forma_core.opencode.architecture import ArchitectureContinuityError, architecture_turn_context, reconcile_architecture
from forma_core.opencode.models import ConnectorCommand, McpJsonRpcRequest, OpenCodeSessionStatus
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, SystemArchitecture, SystemNode, SystemInterface


def saved_project() -> HardwareIntermediateRepresentation:
    """Return a mechanical-only saved design with stable topology."""
    return HardwareIntermediateRepresentation(system_architecture=SystemArchitecture(summary="Fan enclosure", root=SystemNode(
        system_id="product", name="Fan enclosure", domain="product", purpose="Protect the fan",
        children=[SystemNode(system_id="mechanical.enclosure", name="Enclosure", domain="mechanical", purpose="Mount the fan")],
    )))


class ArchitectureContinuityTests(TestCase):
    """Exercise persistence guards and the actual connector polling boundary."""

    def test_first_turn_requests_architecture_before_implementation(self) -> None:
        """A missing revision still produces actionable first-turn guidance."""
        message = architecture_turn_context("Build a fan", None, None)
        self.assertIn('"system_architecture":null', message)
        self.assertIn("before detailed implementation", message)
        self.assertTrue(message.endswith("Build a fan"))

    def test_omitted_and_null_topology_preserve_saved_tree(self) -> None:
        """Full IR writes cannot accidentally erase the hierarchy."""
        previous = saved_project()
        for payload in ({}, {"system_architecture": None}):
            project = HardwareIntermediateRepresentation.model_validate(payload)
            reconcile_architecture(project, previous)
            self.assertEqual(previous.system_architecture, project.system_architecture)
            self.assertIsNot(previous.system_architecture, project.system_architecture)

    def test_explicit_topology_changes_are_retained(self) -> None:
        """Agents may deliberately add and remove systems using a complete tree."""
        previous = saved_project()
        project = previous.model_copy(deep=True)
        project.system_architecture.root.children = []
        reconcile_architecture(project, previous)
        self.assertEqual([], project.system_architecture.root.children)
        self.assertEqual(1, len(previous.system_architecture.root.children))

    def test_duplicate_ids_and_dangling_interfaces_fail(self) -> None:
        """Reject ambiguous IDs and references to removed systems."""
        for duplicate in (True, False):
            project = saved_project()
            root = project.system_architecture.root
            if duplicate:
                root.children[0].system_id = root.system_id
            else:
                root.interfaces = [SystemInterface(name="Power", connects_to="missing", purpose="Supply power")]
            with self.assertRaises(ArchitectureContinuityError):
                reconcile_architecture(project, None)

    def test_empty_draft_stays_empty_and_mechanical_seed_has_no_electronics(self) -> None:
        """Fallback topology does not invent hardware for a CAD-only design."""
        empty = HardwareIntermediateRepresentation()
        reconcile_architecture(empty, None)
        self.assertIsNone(empty.system_architecture)
        project = HardwareIntermediateRepresentation.model_validate({"mechanical": {"enclosure_type": "open", "mounting_guidance": "Mount fan", "manufacturability_rating": "easy"}})
        reconcile_architecture(project, None)
        self.assertEqual(["mechanical"], [node.domain for node in project.system_architecture.root.children])

    def test_poll_injects_latest_owner_scoped_tree_without_mutating_command(self) -> None:
        """An existing connector receives topology in the message it already uses."""
        project_id = str(uuid4())
        command = ConnectorCommand(
            command_id="command", session_id="session", connector_id="connector", owner_user_id="owner",
            project_id=project_id, operation="project_message", message="Add a vent", attempt_count=1,
            lease_expires_at=datetime.now(timezone.utc), lease_token="lease",
        )
        session = SimpleNamespace(session_id="session", connector_id="connector", project_id=project_id,
                                  owner_user_id="owner", status=OpenCodeSessionStatus.ACTIVE)
        with patch("apps.api.opencode_api._connector_capability"), \
             patch("apps.api.opencode_api._scoped_connector_session", return_value=session), \
             patch("apps.api.opencode_api.OPENCODE_STORE") as store, \
             patch("apps.api.opencode_api.get_latest_project_revision", return_value=SimpleNamespace(state=saved_project(), revision_id="revision-2")) as read:
            store.claim_next.return_value = command
            result = poll_opencode_command("session", capability="cap", lease_seconds=60)
        read.assert_called_once_with(project_id, "owner")
        self.assertIn('"revision_id":"revision-2"', result.message)
        self.assertIn("mechanical.enclosure", result.message)
        self.assertTrue(result.message.endswith("Add a vent"))
        self.assertEqual("Add a vent", command.message)

    def test_compile_persists_hierarchy_in_same_revision(self) -> None:
        """Persistence receives the preserved hierarchy together with the new IR."""
        previous = SimpleNamespace(state=saved_project(), revision_id="revision-1")
        user = UserContext(provider="test", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
        with patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=previous), \
             patch("apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None), \
             patch("apps.api.opencode_mcp.ensure_native_cad_model"), \
             patch("apps.api.opencode_mcp._persist_mcp_compile") as persist:
            result = _compile(HardwareIntermediateRepresentation(), str(uuid4()), user)
        self.assertEqual(previous.state.system_architecture, persist.call_args.args[0].system_architecture)
        self.assertEqual("product", result["project_ir"]["system_architecture"]["root"]["system_id"])

    def test_invalid_topology_returns_repairable_error_without_saving(self) -> None:
        """The MCP boundary reports structural errors and preserves the prior revision."""
        project = saved_project()
        project.system_architecture.root.children[0].system_id = "product"
        request = McpJsonRpcRequest.model_validate({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "forma.opencode.update_project", "arguments": {"project_ir": project.model_dump_json()}},
        })
        capability = SimpleNamespace(project_id=str(uuid4()), owner_user_id="owner")
        with patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=None), \
             patch("apps.api.opencode_mcp._persist_mcp_compile") as persist:
            response = asyncio.run(handle_opencode_mcp_json_rpc(request, capability))
        self.assertTrue(response["result"]["isError"])
        self.assertEqual("invalid_system_architecture", response["result"]["structuredContent"]["code"])
        persist.assert_not_called()
