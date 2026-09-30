from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException, Response

from apps.api.auth import UserContext
from apps.api.main import _runtime_config_settings
from apps.api.opencode_api import _record_connector_unavailable_if_stale, _require_owned_project, get_opencode_session_diagnostics, list_opencode_events
from apps.api.opencode_mcp import handle_opencode_mcp_json_rpc, opencode_mcp_tools
from forma_core.opencode.capabilities import CapabilityError, issue_capability, verify_capability
from forma_core.opencode.models import ConnectorEventInput, McpJsonRpcRequest, OpenCodeCommandStatus, OpenCodeEventKind, OpenCodeOperation, OpenCodeSessionStatus
from forma_core.opencode.public_events import project_public_event
from forma_core.opencode.store import OpenCodeStore, StoredSession


class OpenCodeBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_signed_in_runtime_config_falls_back_when_user_settings_are_unreadable(self) -> None:
        user = UserContext(provider="clerk", subject="user_1", owner_user_id="user_1", is_authenticated=True, is_admin=False)
        with patch("apps.api.main._resolve_user_integrations", side_effect=RuntimeError("key mismatch")), patch(
            "apps.api.main.has_opencode_authoring_access", return_value=True,
        ):
            self.assertIsNone(_runtime_config_settings(user))

        with patch("apps.api.main._resolve_user_integrations", side_effect=RuntimeError("key mismatch")), patch(
            "apps.api.main.has_opencode_authoring_access", return_value=False,
        ):
            with self.assertRaises(RuntimeError):
                _runtime_config_settings(user)

    def test_project_scope_rejects_a_foreign_owner_before_session_use(self) -> None:
        with patch("apps.api.opencode_api.get_project_identity", return_value={"owner_user_id": "another-user", "status": "active"}):
            with self.assertRaises(HTTPException) as denied:
                _require_owned_project(str(uuid4()), "user_a")
        self.assertEqual(404, denied.exception.status_code)
        self.assertEqual("opencode_project_not_found", denied.exception.detail["code"])

    def test_capability_rejects_cross_session_and_tampering(self) -> None:
        with patch.dict(os.environ, {"FORMA_OPENCODE_CAPABILITY_SECRET": "s" * 32}, clear=True):
            token = issue_capability(connector_id="mini", session_id="session_a", project_id="project_a", owner_user_id="user_a", scopes=frozenset({"poll"}))
            self.assertEqual("user_a", verify_capability(token, session_id="session_a", project_id="project_a", scope="poll").owner_user_id)
            with self.assertRaises(CapabilityError):
                verify_capability(token, session_id="session_b", scope="poll")
            with self.assertRaises(CapabilityError):
                verify_capability(token[:-1] + ("A" if token[-1] != "A" else "B"), session_id="session_a", scope="poll")

    def test_capability_defaults_to_a_twenty_minute_lifetime(self) -> None:
        with patch.dict(os.environ, {"FORMA_OPENCODE_CAPABILITY_SECRET": "s" * 32}, clear=True), patch(
            "forma_core.opencode.capabilities.time.time", return_value=1_000,
        ):
            token = issue_capability(
                connector_id="mini",
                session_id="session_a",
                project_id="project_a",
                owner_user_id="user_a",
                scopes=frozenset({"poll"}),
            )
            capability = verify_capability(token, session_id="session_a", scope="poll")

        self.assertEqual(1_000 + 20 * 60, capability.expires_at)

    def test_event_projection_removes_internal_payloads_and_raw_errors(self) -> None:
        project_id = uuid4()
        event = ConnectorEventInput.model_validate({
            "event_id": "evt_1",
            "kind": "assistant_message",
            "message": "diff: C:\\secret\\project\\main.py api_key=sk-secret",
            "tool_call": {"command": "cat C:\\secret"},
            "reasoning": "hidden reasoning",
            "raw_exception": "provider token=secret",
        })
        public = project_public_event(event, sequence=1, session_id="session", project_id=project_id)
        serialized = str(public.model_dump(mode="json"))
        self.assertNotIn("secret\\project", serialized)
        self.assertNotIn("sk-secret", serialized)
        self.assertNotIn("tool_call", serialized)
        self.assertIsNone(public.error)
        self.assertEqual(OpenCodeEventKind.ASSISTANT_MESSAGE, public.kind)

    def test_failed_event_preserves_only_bounded_sanitized_diagnostic_fields(self) -> None:
        project_id = uuid4()
        event = ConnectorEventInput.model_validate({
            "event_id": "command-1:terminal",
            "kind": "failed",
            "status": "failed",
            "error_code": "command_failed",
            "correlation_id": "command-1",
            "diagnostic": {
                "category": "model_unavailable",
                "code": "OPENCODE_MODEL_UNAVAILABLE",
                "phase": "authoring",
                "retryable": False,
                "provider": "openai",
                "model": "gpt-5.5",
            },
            "raw_exception": "secret-canary",
            "prompt": "private prompt canary",
        })
        public = project_public_event(event, sequence=1, session_id="session", project_id=project_id)
        payload = public.model_dump(mode="json")
        self.assertEqual("OPENCODE_MODEL_UNAVAILABLE", payload["diagnostic"]["code"])
        self.assertEqual("model_unavailable", payload["diagnostic"]["category"])
        self.assertNotIn("secret-canary", str(payload))
        self.assertNotIn("private prompt canary", str(payload))

    def test_session_diagnostics_is_owner_scoped_and_returns_latest_failure(self) -> None:
        project_id = str(uuid4())
        store = OpenCodeStore(":memory:")
        try:
            session = store.create_session(
                session_id="session-diag", connector_id="mini", owner_user_id="owner", project_id=project_id,
            )
            with store._connection() as connection:
                connection.execute(
                    "UPDATE opencode_sessions SET last_heartbeat_at = ? WHERE session_id = ?",
                    ("2026-09-22T12:00:00Z", session.session_id),
                )
            failure = project_public_event(
                ConnectorEventInput.model_validate({
                    "event_id": "command-diag:terminal",
                    "kind": "failed",
                    "status": "failed",
                    "error_code": "command_failed",
                    "correlation_id": "command-diag",
                    "diagnostic": {
                        "category": "rate_limit",
                        "code": "OPENCODE_RATE_LIMIT",
                        "phase": "authoring",
                        "retryable": True,
                        "provider": "openai",
                        "model": "gpt-5.5",
                    },
                }),
                sequence=1,
                session_id=session.session_id,
                project_id=UUID(project_id),
                created_at=datetime(2026, 9, 22, 12, 1, tzinfo=timezone.utc),
            )
            store.add_event(failure)
            owner = UserContext(provider="clerk", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
            with patch("apps.api.opencode_api.OPENCODE_STORE", new=store):
                result = get_opencode_session_diagnostics(session.session_id, Response(), owner)
                self.assertEqual("OPENCODE_RATE_LIMIT", result.latest_failure.code)
                self.assertEqual("command-diag", result.latest_failure.command_id)
                self.assertTrue(result.latest_failure.retryable)
                self.assertEqual("2026-09-22T12:00:00+00:00", result.last_successful_poll_at.isoformat())
                foreign = UserContext(provider="clerk", subject="other", owner_user_id="other", is_authenticated=True, is_admin=False)
                with self.assertRaises(HTTPException) as denied:
                    get_opencode_session_diagnostics(session.session_id, Response(), foreign)
                self.assertEqual(404, denied.exception.status_code)
        finally:
            store.close()

    def test_diagnostic_schema_rejects_raw_fields_and_oversized_codes(self) -> None:
        base = {
            "event_id": "command-1:terminal",
            "kind": "failed",
            "status": "failed",
            "diagnostic": {
                "category": "unknown",
                "code": "OPENCODE_PROMPT_FAILED",
                "phase": "authoring",
                "retryable": False,
            },
        }
        with self.assertRaises(Exception):
            ConnectorEventInput.model_validate({
                **base,
                "diagnostic": {**base["diagnostic"], "raw_response": "secret-canary"},
            })
        with self.assertRaises(Exception):
            ConnectorEventInput.model_validate({
                **base,
                "diagnostic": {**base["diagnostic"], "code": "X" * 81},
            })

    def test_restricted_mcp_surface_excludes_forbidden_tools(self) -> None:
        names = {str(tool["name"]) for tool in opencode_mcp_tools()}
        self.assertEqual({
            "forma.opencode.create_project",
            "forma.opencode.read_project",
            "forma.opencode.update_project",
            "forma.opencode.compile_project",
            "forma.opencode.validate_project",
            "forma.opencode.generate_image",
        }, names)
        with patch.dict(os.environ, {"FORMA_OPENCODE_CAPABILITY_SECRET": "s" * 32}, clear=True):
            from forma_core.opencode.capabilities import ConnectorCapability
            capability = ConnectorCapability("mini", "session", str(uuid4()), "user", int(datetime.now(timezone.utc).timestamp()) + 60, "nonce", frozenset({"mcp"}))
            response = asyncio.run(handle_opencode_mcp_json_rpc(McpJsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forma.generate_project", "arguments": {}}}), capability))
        self.assertEqual("authorization_required", response["error"]["data"]["code"])

    def test_list_events_for_new_session_does_not_record_connector_unavailable(self) -> None:
        store = OpenCodeStore(":memory:")
        try:
            session = store.create_session(session_id="session", connector_id="mini", owner_user_id="user", project_id=str(uuid4()))
            self.assertIsNone(session.last_heartbeat_at)
            user = UserContext(provider="clerk", subject="user", owner_user_id="user", is_authenticated=True, is_admin=False)
            with patch("apps.api.opencode_api.OPENCODE_STORE", new=store), patch("apps.api.opencode_api.datetime", new=Mock(wraps=datetime)) as clock:
                clock.now.return_value = datetime.fromisoformat(session.created_at.replace("Z", "+00:00"))
                page = list_opencode_events(session.session_id, cursor=0, limit=50, user=user)
            self.assertEqual((), page.events)
            self.assertEqual(0, page.next_cursor)
            self.assertEqual([], store.list_events(session.session_id, 0, 50))
        finally:
            store.close()

    def test_store_is_idempotent_leased_and_cursorable(self) -> None:
        project_id = str(uuid4())
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}, clear=False):
            path = os.path.join(directory, "opencode.sqlite")
            store = OpenCodeStore(path)
            try:
                session = store.create_session(session_id="session", connector_id="mini", owner_user_id="user", project_id=project_id)
                first = store.create_command(command_id="command", session=session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="same", message="build")
                duplicate = store.create_command(command_id="other", session=session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="same", message="build")
                self.assertEqual(first.command_id, duplicate.command_id)
                self.assertIsNotNone(first.message_ciphertext)
                self.assertNotIn("build", str(first.message_ciphertext))
            finally:
                store.close()

            reopened = OpenCodeStore(path)
            try:
                claimed = reopened.claim_next(connector_id="mini", session_id="session")
                self.assertIsNotNone(claimed)
                assert claimed is not None
                self.assertEqual("build", claimed.message)
                public = project_public_event(ConnectorEventInput(event_id="event", kind="working"), sequence=1, session_id="session", project_id=UUID(project_id))
                reopened.add_event(public)
                self.assertEqual(1, len(reopened.list_events("session", 0, 10)))
                self.assertEqual(2, reopened.next_event_sequence("session"))
            finally:
                reopened.close()

    def test_connector_session_discovery_excludes_idle_sessions_but_command_activity_reactivates_them(self) -> None:
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        with patch("forma_core.opencode.store._now", return_value=now), patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}, clear=False):
            store = OpenCodeStore(":memory:")
            try:
                idle = store.create_session(session_id="idle", connector_id="mini", owner_user_id="user", project_id=str(uuid4()))
                active = store.create_session(session_id="active", connector_id="mini", owner_user_id="user", project_id=str(uuid4()))
                with store._connection() as connection:
                    connection.execute("UPDATE opencode_sessions SET updated_at = ? WHERE session_id = ?", ("2026-09-12T11:00:00Z", idle.session_id))
                self.assertEqual([active.session_id], [session.session_id for session in store.list_connector_sessions("mini", idle_after_seconds=900)])
                store.create_command(
                    command_id="command", session=idle, operation=OpenCodeOperation.PROJECT_MESSAGE,
                    idempotency_key="message", message="reactivate",
                )
                self.assertEqual({"idle", "active"}, {session.session_id for session in store.list_connector_sessions("mini", idle_after_seconds=900)})
            finally:
                store.close()

    def test_store_is_session_scoped_renews_leases_and_completes_idempotently(self) -> None:
        first_project_id = str(uuid4())
        second_project_id = str(uuid4())
        with patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}, clear=False):
            store = OpenCodeStore(":memory:")
            try:
                first_session = store.create_session(session_id="session_a", connector_id="mini", owner_user_id="user", project_id=first_project_id)
                second_session = store.create_session(session_id="session_b", connector_id="mini", owner_user_id="user", project_id=second_project_id)
                store.create_command(command_id="command_a", session=first_session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="a", message="first")
                store.create_command(command_id="command_b", session=second_session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="b", message="second")
                claimed = store.claim_next(connector_id="mini", session_id="session_a")
                self.assertIsNotNone(claimed)
                assert claimed is not None
                self.assertEqual("command_a", claimed.command_id)
                stored = store.get_command(claimed.command_id)
                self.assertIsNotNone(stored)
                assert stored is not None
                old_expiry = stored.lease_expires_at
                renewed = store.heartbeat(stored, claimed.lease_token)
                self.assertNotEqual(old_expiry, renewed.lease_expires_at)
                completed = store.complete(renewed, claimed.lease_token, OpenCodeCommandStatus.SUCCEEDED)
                self.assertEqual(OpenCodeCommandStatus.SUCCEEDED, completed.status)
                self.assertEqual(completed, store.complete(completed, "not-needed-after-terminal", OpenCodeCommandStatus.SUCCEEDED))
                self.assertIsNotNone(store.claim_next(connector_id="mini", session_id="session_b"))
            finally:
                store.close()

    def test_store_cancels_claimed_command_without_message(self) -> None:
        project_id = str(uuid4())
        with patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}, clear=False):
            store = OpenCodeStore(":memory:")
            try:
                session = store.create_session(session_id="session", connector_id="mini", owner_user_id="user", project_id=project_id)
                store.create_command(command_id="invalid", session=session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="invalid", message="build")
                with store._connection() as connection:
                    connection.execute(
                        "UPDATE opencode_commands SET message_ciphertext = NULL, message_key_id = NULL WHERE command_id = ?",
                        ("invalid",),
                    )

                self.assertIsNone(store.claim_next(connector_id="mini", session_id="session"))
                command = store.get_command("invalid")
                self.assertIsNotNone(command)
                assert command is not None
                self.assertEqual(OpenCodeCommandStatus.CANCELLED, command.status)
            finally:
                store.close()
