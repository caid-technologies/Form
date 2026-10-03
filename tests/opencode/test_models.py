import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.auth import UserContext, require_opencode_authoring_access
from apps.api.opencode_api import router
from forma_core.opencode.models import OpenCodeOperation
from forma_core.opencode.store import OpenCodeStore


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-only-key"}))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = self.directory + "/models.sqlite"
        self.store = OpenCodeStore(self.path)
        self.addCleanup(self.store.close)
        self.session = self.store.create_session(session_id="chat", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))

    def create(self, key, model=None):
        return self.store.create_command(command_id=key, session=self.session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key=key, message="Continue", model=model)

    def test_model_is_persisted_per_command_across_restart_and_lease_retry(self):
        self.create("first", "google/gemini-2.5-flash")
        self.create("second", "openrouter/anthropic/claude-sonnet-4")
        self.store.close()
        reopened = OpenCodeStore(self.path)
        self.addCleanup(reopened.close)
        leased = reopened.claim_next(connector_id="mini", session_id="chat")
        self.assertEqual("google/gemini-2.5-flash", leased.model)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE opencode_commands SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE command_id = 'first'")
        retried = reopened.claim_next(connector_id="mini", session_id="chat")
        self.assertEqual(2, retried.attempt_count)
        self.assertEqual(leased.model, retried.model)

    def test_same_key_cannot_change_model(self):
        original = self.create("first", "google/gemini-2.5-flash")
        self.assertEqual(original.command_id, self.create("first", original.model).command_id)
        with self.assertRaises(ValueError):
            self.create("first", "openai/gpt-4.1")

    def test_existing_sqlite_database_migrates_with_default_inheritance(self):
        self.create("legacy")
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE opencode_commands DROP COLUMN model")
        reopened = OpenCodeStore(self.path)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.claim_next(connector_id="mini", session_id="chat").model)

    def test_api_validates_model_and_returns_conflict_for_changed_retry(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_opencode_authoring_access] = lambda: UserContext(provider="test", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
        with patch("apps.api.opencode_api.OPENCODE_STORE", self.store), TestClient(app) as client:
            url = "/opencode/sessions/chat/commands"
            body = {"message": "Continue", "idempotency_key": "request", "model": "google/gemini-2.5-flash"}
            response = client.post(url, json=body)
            self.assertEqual(201, response.status_code, response.text)
            self.assertEqual(body["model"], response.json()["model"])
            self.assertEqual(409, client.post(url, json={**body, "model": "openai/gpt-4.1"}).status_code)
            for invalid in ["bare-model", "provider/", "provider/model\n", "https://user:password@example.com", "p/" + "a" * 200]:
                self.assertEqual(422, client.post(url, json={**body, "model": invalid}).status_code, invalid)
