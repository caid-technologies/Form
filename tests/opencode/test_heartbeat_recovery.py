"""Lease failure categories, safe recovery, and replay on the hosted gateway."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import opencode_api as api
from forma_core.opencode.capabilities import issue_capability
from forma_core.opencode.models import OpenCodeOperation
from forma_core.opencode.store import OpenCodeStore
from forma_core.persistence.providers import SupabaseProvider


class HeartbeatRecoveryTests(unittest.TestCase):
    def setUp(self):
        patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key", "FORMA_OPENCODE_CAPABILITY_SECRET": "s" * 32}).start()
        self.addCleanup(patch.stopall)
        self.now = datetime.now(timezone.utc)
        self.clock = patch("forma_core.opencode.store._now", return_value=self.now).start()
        self.cap_clock = patch("forma_core.opencode.capabilities.time.time", return_value=self.now.timestamp()).start()
        self.store = OpenCodeStore(":memory:")
        self.addCleanup(self.store.close)
        patch.object(api, "OPENCODE_STORE", self.store).start()
        patch.object(api, "get_latest_project_revision", return_value=None).start()
        self.session = self.store.create_session(session_id="session", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
        self.store.create_command(command_id="command", session=self.session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="one", message="private-prompt-canary")
        self.lease = self.store.claim_next(connector_id="mini", session_id="session", lease_seconds=60)
        self.token = self.capability()
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def capability(self, **overrides):
        args = dict(connector_id="mini", session_id="session", project_id=self.session.project_id,
                    owner_user_id="owner", scopes=frozenset({"poll", "heartbeat", "events", "complete"}))
        return issue_capability(**(args | overrides))

    def post(self, route="heartbeat", *, token=None, lease=None, **body):
        return self.client.post(f"/opencode/connector/commands/command/{route}",
                                headers={"X-Forma-OpenCode-Capability": token or self.token,
                                         "X-Forma-OpenCode-Lease": lease or self.lease.lease_token},
                                json=body if route == "events" else {"lease_token": lease or self.lease.lease_token, **body})

    def assert_code(self, response, code):
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual(code, response.json()["detail"]["code"])
        self.assertTrue(response.json()["detail"]["correlation_id"])
        for secret in [self.token, self.lease.lease_token, "private-prompt-canary", "secret-canary"]:
            self.assertNotIn(secret, response.text)

    def test_capability_rejection_expiry_and_scopes_are_distinct(self):
        self.assert_code(self.post(token="secret-canary"), "opencode_capability_invalid")
        expired = self.capability(ttl_seconds=30)
        self.cap_clock.return_value = int(self.now.timestamp()) + 30
        self.assert_code(self.post(token=expired), "opencode_capability_expired")
        for overrides in [dict(scopes=frozenset({"poll"})), dict(session_id="other"), dict(project_id=str(uuid4())), dict(owner_user_id="other"), dict(connector_id="other")]:
            with self.subTest(overrides=overrides):
                self.assert_code(self.post(token=self.capability(**overrides)), "opencode_scope_mismatch")
        self.assertEqual("leased", self.store.get_command("command").status)

    def test_expired_lease_settles_command_and_one_canonical_public_event(self):
        self.clock.return_value = self.now + timedelta(seconds=60)
        self.assert_code(self.post(), "opencode_lease_expired")
        command = self.store.get_command("command")
        self.assertEqual("failed", command.status)
        self.assertIsNone(command.lease_token_hash)
        self.assertIsNone(command.lease_expires_at)
        self.assert_code(self.post(), "opencode_command_closed")
        self.assert_code(self.post("complete", status="succeeded"), "opencode_command_closed")
        events = self.store.list_events("session", 0, 50)
        self.assertEqual(1, len(events))
        self.assertEqual("command:terminal", events[0].event_id)
        self.assertEqual("opencode_lease_expired", events[0].error.code)
        self.assertEqual("active", self.store.get_session("session").status)

    def test_invalid_token_does_not_cancel_valid_or_reclaimed_lease(self):
        self.assert_code(self.post(lease="secret-canary"), "opencode_lease_invalid")
        self.clock.return_value = self.now + timedelta(seconds=61)
        newer = self.store.claim_next(connector_id="mini", session_id="session")
        self.assert_code(self.post(), "opencode_lease_invalid")
        self.assertEqual(200, self.post(lease=newer.lease_token).status_code)
        self.assertEqual([], self.store.list_events("session", 0, 50))

    def test_stale_renewal_and_expiry_cannot_overwrite_a_new_lease(self):
        old = self.store.get_command("command")
        self.clock.return_value = self.now + timedelta(seconds=61)
        newer = self.store.claim_next(connector_id="mini", session_id="session")
        self.store.fail_expired_lease(old, old.lease_token_hash)
        with self.assertRaises(PermissionError):
            self.store.heartbeat(old, self.lease.lease_token)
        self.assertEqual(200, self.post(lease=newer.lease_token).status_code)

    def test_expired_lease_on_event_or_completion_also_settles(self):
        for route in ["events", "complete"]:
            with self.subTest(route=route):
                # Restore a new fixture for each independent terminal transition.
                if route == "complete":
                    with self.store._connection() as db:
                        db.execute("DELETE FROM opencode_events")
                        db.execute("UPDATE opencode_commands SET status = 'leased', lease_token_hash = ?, lease_expires_at = ?", (digest, expiry))
                command = self.store.get_command("command")
                digest, expiry = command.lease_token_hash, command.lease_expires_at
                self.clock.return_value = self.now + timedelta(seconds=60)
                body = dict(event_id="late", kind="progress") if route == "events" else dict(status="succeeded")
                self.assert_code(self.post(route, **body), "opencode_lease_expired")
                self.assertEqual("failed", self.store.get_command("command").status)

    def test_reconnect_replays_events_and_completion_without_duplicate_execution(self):
        # Simulate a lost response: repeat the exact request after reconnect.
        event = dict(event_id="command:1", kind="progress", status="running")
        first = self.post("events", **event)
        self.clock.return_value = self.now + timedelta(seconds=45)
        renewed = self.post()
        self.assertEqual(200, renewed.status_code)
        self.assertGreater(renewed.json()["lease_expires_at"], self.lease.lease_expires_at.isoformat())
        self.assertEqual(first.json(), self.post("events", **event).json())
        complete = self.post("complete", status="succeeded")
        self.assertEqual(200, complete.status_code)
        self.assertEqual(complete.json(), self.post("complete", status="succeeded").json())
        self.assertEqual(first.json(), self.post("events", **(event | {"kind": "assistant_message", "message": "changed-canary"})).json())
        self.assert_code(self.post("events", event_id="new-after-completion", kind="progress"), "opencode_command_closed")
        self.assert_code(self.post("events", **(event | {"project_id": str(uuid4())})), "opencode_scope_mismatch")
        self.assertEqual(["command:1", "command:terminal"], [e.event_id for e in self.store.list_events("session", 0, 50)])
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="session"))

    def test_supabase_expiry_uses_the_atomic_rpc(self):
        provider = Mock(spec=SupabaseProvider)
        provider.client = Mock()
        command = self.store.get_command("command")
        with patch.object(self.store, "_ensure_provider", return_value=provider):
            self.store.fail_expired_lease(command, "digest")
        provider.client.rpc.assert_called_once_with("fail_expired_opencode_lease", {
            "p_command_id": "command", "p_lease_hash": "digest", "p_now": self.now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        })
