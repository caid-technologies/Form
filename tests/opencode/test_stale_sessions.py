from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException

from apps.api import opencode_api as api
from apps.api.auth import UserContext
from forma_core.opencode.capabilities import issue_capability
from forma_core.opencode.models import (
    ConnectorCompletion, ConnectorEventInput, ConnectorHeartbeat, McpJsonRpcRequest,
    OpenCodeCommandStatus as CommandStatus, OpenCodeOperation, OpenCodeSessionStatus as SessionStatus,
    SubmitCommandRequest,
)
from forma_core.opencode.store import OpenCodeStore, SessionClosedError
from forma_core.persistence.providers import SupabaseProvider


class StaleSessionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key", "FORMA_OPENCODE_CAPABILITY_SECRET": "s" * 32})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = OpenCodeStore(":memory:")
        self.addCleanup(self.store.close)
        self.start = datetime.now(timezone.utc)
        self.clock = patch("forma_core.opencode.store._now", return_value=self.start).start()
        self.addCleanup(patch.stopall)
        self.session = self.store.create_session(session_id="session", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
        self.user = UserContext(provider="clerk", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
        patch.object(api, "OPENCODE_STORE", self.store).start()

    def command(self, command_id="command", session=None):
        return self.store.create_command(command_id=command_id, session=session or self.session,
                                        operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key=command_id, message="Build a hinge")

    def advance(self, seconds):
        self.clock.return_value = self.start + timedelta(seconds=seconds)

    def events(self):
        return api.list_opencode_events("session", cursor=0, limit=200, user=self.user).events

    def test_warning_then_atomic_terminal_failure_and_idempotent_polls(self):
        self.command()
        self.advance(119)
        self.assertEqual((), self.events())
        self.advance(120)
        self.assertEqual(["connector_unavailable"], [e.kind for e in self.events()])
        self.assertEqual(SessionStatus.ACTIVE, self.store.get_session("session").status)
        self.advance(299)
        self.assertEqual(1, len(self.events()))
        self.advance(300)
        events = self.events()
        self.assertEqual(["connector_unavailable", "failed"], [e.kind for e in events])
        self.assertEqual("command:terminal", events[-1].event_id)
        self.assertEqual("connector_timeout", events[-1].error.code)
        self.assertEqual(SessionStatus.FAILED, self.store.get_session("session").status)
        self.assertEqual(CommandStatus.FAILED, self.store.get_command("command").status)
        self.assertEqual(events, self.events())
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="session"))

    def test_reconnect_during_grace_resumes_and_later_outage_gets_new_deadline(self):
        self.command()
        self.advance(130)
        self.events()
        self.store.touch_session("session")
        self.advance(300)
        self.assertEqual(SessionStatus.ACTIVE, self.store.reconcile_session("session").status)
        lease = self.store.claim_next(connector_id="mini", session_id="session")
        command = self.store.get_command("command")
        self.store.complete(command, lease.lease_token, CommandStatus.SUCCEEDED)
        self.advance(900)
        self.assertEqual(SessionStatus.ACTIVE, self.store.reconcile_session("session").status)
        self.command("followup")
        self.assertEqual(SessionStatus.ACTIVE, self.store.reconcile_session("session").status)
        self.advance(1200)
        self.assertEqual(SessionStatus.FAILED, self.store.reconcile_session("session").status)
        self.assertEqual(CommandStatus.SUCCEEDED, self.store.get_command("command").status)
        self.assertEqual(CommandStatus.FAILED, self.store.get_command("followup").status)

    def test_cancel_works_offline_and_never_becomes_timeout(self):
        self.command()
        self.advance(130)
        self.events()
        cancelled = api.cancel_opencode_session("session", self.user)
        self.assertEqual(SessionStatus.CANCELLED, cancelled.status)
        self.advance(900)
        self.assertFalse(any(e.kind == "failed" for e in self.events()))
        self.assertEqual(CommandStatus.CANCELLED, self.store.get_command("command").status)
        self.assertEqual(cancelled, api.cancel_opencode_session("session", self.user))

    def test_late_connector_is_rejected_before_heartbeat_events_completion_or_mcp(self):
        self.command()
        lease = self.store.claim_next(connector_id="mini", session_id="session")
        snapshot = self.store.get_command("command")
        token = issue_capability(connector_id="mini", session_id="session", project_id=self.session.project_id,
                                 owner_user_id="owner", scopes=frozenset({"poll", "heartbeat", "events", "complete", "mcp"}))
        self.advance(300)
        # No browser poll is needed: the late connector itself must settle the outage.
        with self.assertRaises(HTTPException) as denied:
            api.heartbeat_opencode_command("command", ConnectorHeartbeat(lease_token=lease.lease_token), capability=token)
        self.assertEqual("opencode_session_closed", denied.exception.detail["code"])
        for operation in [
            lambda: api.ingest_opencode_event("command", ConnectorEventInput(event_id="late", kind="assistant_message", message="late result"), capability=token, lease_token=lease.lease_token),
            lambda: api.complete_opencode_command("command", ConnectorCompletion(lease_token=lease.lease_token, status="succeeded"), capability=token),
            lambda: asyncio.run(api.opencode_mcp_endpoint(McpJsonRpcRequest(method="tools/list", id=1, jsonrpc="2.0"), capability=token)),
        ]:
            with self.subTest(operation=operation), self.assertRaises(HTTPException):
                operation()
        for operation in [self.store.heartbeat, self.store.validate_lease]:
            with self.assertRaises(PermissionError):
                operation(snapshot, lease.lease_token)
        with self.assertRaises(PermissionError):
            self.store.complete(snapshot, lease.lease_token, CommandStatus.SUCCEEDED)
        self.assertEqual(1, len(self.events()))
        self.assertEqual(204, api.poll_opencode_command("session", capability=token).status_code)

    def test_retry_has_fresh_session_and_idempotent_command(self):
        self.command()
        self.advance(300)
        self.events()
        with self.assertRaises(HTTPException):
            api.submit_opencode_command("session", SubmitCommandRequest(message="Build a hinge", idempotency_key="retry"), self.user)
        with self.assertRaises(SessionClosedError):
            self.command("late-insert")
        retry = self.store.create_session(session_id="retry", connector_id="mini", owner_user_id="owner", project_id=self.session.project_id)
        first = self.command("retry-command", retry)
        self.assertEqual(first.command_id, self.command("retry-command", retry).command_id)
        self.assertEqual("retry-command", self.store.claim_next(connector_id="mini", session_id="retry").command_id)
        self.assertEqual(CommandStatus.FAILED, self.store.get_command("command").status)

    def test_healthy_long_running_request_is_not_a_total_duration_timeout(self):
        self.command()
        for second in range(0, 1801, 30):
            self.advance(second)
            self.store.touch_session("session")
            self.assertEqual(SessionStatus.ACTIVE, self.store.reconcile_session("session").status)
        self.assertEqual((), self.events())

    def test_owner_cannot_settle_or_read_foreign_session(self):
        self.command()
        self.advance(300)
        other = self.user.model_copy(update={"owner_user_id": "other"}) if hasattr(self.user, "model_copy") else UserContext(provider="clerk", subject="other", owner_user_id="other", is_authenticated=True, is_admin=False)
        with self.assertRaises(HTTPException):
            api.list_opencode_events("session", cursor=0, limit=50, user=other)
        self.assertEqual(SessionStatus.ACTIVE, self.store.get_session("session").status)

    def test_concurrent_pollers_write_one_terminal_event_per_command(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = OpenCodeStore(directory + "/sessions.db"), OpenCodeStore(directory + "/sessions.db")
            try:
                session = first.create_session(session_id="parallel", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
                first.create_command(command_id="parallel-command", session=session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="one", message="hinge")
                second.provider
                self.advance(300)
                with ThreadPoolExecutor(2) as executor:
                    results = list(executor.map(lambda store: store.reconcile_session("parallel"), [first, second]))
                self.assertTrue(all(result.status == SessionStatus.FAILED for result in results))
                self.assertEqual(1, len(first.list_events("parallel", 0, 100)))
            finally:
                first.close()
                second.close()

    def test_compile_that_outlives_the_session_cannot_save_a_late_revision(self):
        from apps.api.opencode_mcp import _compile
        from forma_core.opencode.capabilities import verify_capability
        from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
        self.command()
        token = issue_capability(connector_id="mini", session_id="session", project_id=self.session.project_id,
                                 owner_user_id="owner", scopes=frozenset({"mcp"}))
        capability = verify_capability(token, scope="mcp")
        with patch("apps.api.opencode_mcp.get_latest_project_revision", return_value=None), patch(
            "apps.api.opencode_mcp.get_project_revision_by_source_job", return_value=None,
        ), patch("apps.api.opencode_mcp.ensure_native_cad_model", side_effect=lambda *args, **kwargs: self.advance(300)), patch(
            "apps.api.opencode_mcp._persist_mcp_compile",
        ) as persist:
            with self.assertRaises(PermissionError):
                _compile(HardwareIntermediateRepresentation(components=[], nets=[]), self.session.project_id, self.user, capability)
            persist.assert_not_called()
        self.assertEqual(SessionStatus.FAILED, self.store.get_session("session").status)

    def test_supabase_uses_transactional_rpc(self):
        provider = Mock(spec=SupabaseProvider)
        provider.client = Mock()
        store = OpenCodeStore(provider=provider)
        with patch.object(store, "get_session", return_value=self.session):
            store.reconcile_session("session")
        provider.client.rpc.assert_called_once_with("reconcile_opencode_session", {
            "p_session_id": "session", "p_now": self.start.isoformat(timespec="microseconds").replace("+00:00", "Z"), "p_cancel": False,
        })
