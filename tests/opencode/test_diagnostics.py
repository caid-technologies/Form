"""Owner visibility, durable delivery fallback, and diagnostic trust boundaries."""
from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import opencode_api as api
from apps.api.auth import UserContext
from forma_core.opencode.capabilities import issue_capability
from forma_core.opencode.models import ConnectorEventInput, OpenCodeOperation
from forma_core.opencode.public_events import project_public_event
from forma_core.opencode.store import OpenCodeStore
from forma_core.persistence.providers import SupabaseProvider


DIAGNOSTIC = {
    "category": "rate_limit", "code": "OPENCODE_RATE_LIMIT", "phase": "authoring",
    "retryable": True, "provider": "openai", "model": "gpt-5.5",
}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key", "FORMA_OPENCODE_CAPABILITY_SECRET": "s" * 32}).start()
        self.addCleanup(patch.stopall)
        self.now = datetime.now(timezone.utc)
        self.clock = patch("forma_core.opencode.store._now", return_value=self.now).start()
        self.store = OpenCodeStore(":memory:")
        self.addCleanup(self.store.close)
        patch.object(api, "OPENCODE_STORE", self.store).start()
        self.session = self.store.create_session(session_id="session", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
        self.store.create_command(command_id="command", session=self.session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="one", message="private-prompt-canary")
        self.lease = self.store.claim_next(connector_id="mini", session_id="session")
        self.token = issue_capability(connector_id="mini", session_id="session", project_id=self.session.project_id,
                                      owner_user_id="owner", scopes=frozenset({"events", "complete"}))
        self.owner = UserContext(provider="clerk", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.require_opencode_authoring_access] = lambda: self.owner
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def complete(self, **updates):
        return self.client.post("/opencode/connector/commands/command/complete",
                                headers={"X-Forma-OpenCode-Capability": self.token},
                                json={"lease_token": self.lease.lease_token, "status": "failed",
                                      "error_code": "OPENCODE_RATE_LIMIT", "diagnostic": DIAGNOSTIC, **updates})

    def diagnostics(self):
        return self.client.get("/opencode/sessions/session/diagnostics")

    def test_completion_fallback_persists_diagnostic_once_and_enforces_ownership(self):
        self.assertIsNone(self.diagnostics().json()["latest_failure"])
        self.assertEqual(200, self.complete().status_code)
        self.assertEqual(200, self.complete(diagnostic={**DIAGNOSTIC, "code": "OPENCODE_PROVIDER_AUTH"}).status_code)
        self.assertEqual(1, len(self.store.list_events("session", 0, 50)))
        response = self.diagnostics()
        self.assertEqual("private, no-store", response.headers["cache-control"])
        failure = response.json()["latest_failure"]
        self.assertEqual("OPENCODE_RATE_LIMIT", failure["code"])
        self.assertEqual("command", failure["command_id"])
        self.assertEqual("command", failure["correlation_id"])
        for secret in [self.token, self.lease.lease_token, "private-prompt-canary"]:
            self.assertNotIn(secret, response.text)
        self.owner = replace(self.owner, owner_user_id="foreign")
        self.assertEqual(404, self.diagnostics().status_code)

    def test_event_command_correlation_is_gateway_owned(self):
        response = self.client.post("/opencode/connector/commands/command/events",
                                   headers={"X-Forma-OpenCode-Capability": self.token, "X-Forma-OpenCode-Lease": self.lease.lease_token},
                                   json={"event_id": "arbitrary:event", "kind": "failed", "status": "failed",
                                         "correlation_id": "secret-canary", "diagnostic": DIAGNOSTIC})
        self.assertEqual(200, response.status_code, response.text)
        result = self.diagnostics().json()["latest_failure"]
        self.assertEqual("command", result["command_id"])
        self.assertEqual("command", result["correlation_id"])
        self.assertNotIn("secret-canary", response.text)

    def test_latest_failure_survives_more_than_200_progress_events(self):
        self.assertEqual(200, self.complete().status_code)
        for sequence in range(2, 210):
            self.store.add_event(project_public_event(
                ConnectorEventInput(event_id=f"progress:{sequence}", kind="progress"), sequence=sequence,
                session_id="session", project_id=self.session.project_id,
            ))
        self.assertEqual("OPENCODE_RATE_LIMIT", self.diagnostics().json()["latest_failure"]["code"])

    def test_offline_session_read_reports_gateway_timeout_without_worker_diagnostic(self):
        self.clock.return_value = self.now + timedelta(seconds=301)
        response = self.diagnostics().json()
        self.assertEqual("failed", response["status"])
        self.assertEqual("connector_timeout", response["latest_failure"]["code"])
        self.assertEqual("connector_cloud_connectivity", response["latest_failure"]["category"])

    def test_cancelled_completion_keeps_cancellation_diagnostic(self):
        diagnostic = {"category": "cancellation", "code": "cancelled", "phase": "finalizing", "retryable": False}
        self.assertEqual(200, self.complete(status="cancelled", diagnostic=diagnostic).status_code)
        self.assertEqual("cancellation", self.diagnostics().json()["latest_failure"]["category"])

    def test_invalid_lease_and_unbounded_or_raw_diagnostics_cannot_persist(self):
        self.assertEqual(403, self.complete(lease_token="wrong").status_code)
        for changes in [{"raw_response": "secret"}, {"code": "X" * 81}, {"provider": "X" * 81}, {"model": "X" * 161}]:
            with self.subTest(changes=changes):
                self.assertEqual(422, self.complete(diagnostic={**DIAGNOSTIC, **changes}).status_code)
        self.assertIsNone(self.store.latest_failure("session"))
        self.assertEqual("leased", self.store.get_command("command").status)
        self.assertEqual(200, self.complete(diagnostic={**DIAGNOSTIC, "code": "OPENCODE_SECRET_CANARY"}).status_code)
        self.assertEqual("unknown", self.diagnostics().json()["latest_failure"]["code"])

    def test_supabase_latest_failure_filters_session_and_orders_before_limit(self):
        self.assertEqual(200, self.complete().status_code)
        expected = self.store.latest_failure("session")
        provider = Mock(spec=SupabaseProvider)
        provider.client = Mock()
        query = Mock()
        provider.client.table.return_value = query
        for method in ["select", "eq", "in_", "order", "limit"]:
            getattr(query, method).return_value = query
        query.execute.return_value.data = [{"event_json": expected.model_dump(mode="json")}]
        with patch.object(self.store, "_ensure_provider", return_value=provider):
            self.assertEqual(expected, self.store.latest_failure("session"))
        query.eq.assert_called_once_with("session_id", "session")
        query.in_.assert_called_once_with("event_json->>kind", ["failed", "cancelled"])
        query.order.assert_called_once_with("sequence", desc=True)
        query.limit.assert_called_once_with(1)
