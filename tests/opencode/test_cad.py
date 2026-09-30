import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from apps.api.auth import UserContext, require_opencode_authoring_access
from forma_core.opencode.store import OpenCodeStore
from apps.api.opencode_api import router
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.workspaces.projects.cad_generation import CadGenerationError
from tests.projects.test_solid_cad import cube, PROJECT_ID


class CadApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api")
        self.user = UserContext(provider="clerk", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
        self.app.dependency_overrides[require_opencode_authoring_access] = lambda: self.user
        store = OpenCodeStore(":memory:")
        self.addCleanup(store.close)
        store.create_session(session_id="session", connector_id="mini", owner_user_id="owner", project_id=PROJECT_ID)
        self.enterContext(patch("apps.api.opencode_api.OPENCODE_STORE", store))
        self.client = self.enterContext(TestClient(self.app))

    def test_download_requires_owner_matching_saved_artifact_and_intact_bytes(self):
        content = b"ISO-10303-21;\nsynthetic storage test\nEND-ISO-10303-21;"
        digest = hashlib.sha256(content).hexdigest()
        project = cube()
        project.cad_model = {"stored_sha256": digest}
        url = f"/api/opencode/projects/{PROJECT_ID}/cad/{digest}"
        with patch("apps.api.opencode_api.get_project_identity", return_value={"owner_user_id": "owner", "status": "active"}) as identity, patch(
            "apps.api.opencode_api.get_latest_project_revision", return_value=SimpleNamespace(state=project)), patch(
            "forma_core.persistence.project_artifacts.ProjectArtifactStorage.get", return_value=SimpleNamespace(content=content)) as storage:
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, content)
            self.assertEqual(result.headers["cache-control"], "private, no-store")
            self.assertIn("assembly.step", result.headers["content-disposition"])
            storage.reset_mock()
            self.assertEqual(self.client.get(url.replace(digest, "b" * 64)).status_code, 404)
            storage.assert_not_called()
            identity.return_value = {"owner_user_id": "another-owner", "status": "active"}
            self.assertEqual(self.client.get(url).status_code, 404)
            storage.assert_not_called()
            identity.return_value = {"owner_user_id": "owner", "status": "active"}
            storage.return_value = SimpleNamespace(content=b"corrupted")
            self.assertEqual(self.client.get(url).status_code, 503)

    def test_create_returns_existing_project_without_resetting_or_compiling(self):
        cap = ConnectorCapability("mini", "session", PROJECT_ID, "owner", 2_000_000_000, "nonce", frozenset({"mcp"}))
        revision = SimpleNamespace(state=cube(), revision_id="saved-revision")
        with patch("apps.api.opencode_api._connector_capability", return_value=cap), patch(
            "apps.api.opencode_mcp.get_latest_project_revision", return_value=revision), patch("apps.api.opencode_mcp._compile") as compile:
            result = self.client.post("/api/opencode/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forma.opencode.create_project", "arguments": {}}})
            self.assertEqual(result.status_code, 200)
            payload = result.json()["result"]["structuredContent"]
            self.assertEqual(payload["revision_id"], "saved-revision")
            self.assertEqual(payload["project_ir"]["mechanical"]["cad_operations"][0]["size"]["x_mm"], 20)
            compile.assert_not_called()

    def test_required_cad_failure_returns_tool_error_without_saving_draft(self):
        cap = ConnectorCapability("mini", "session", PROJECT_ID, "owner", 2_000_000_000, "nonce", frozenset({"mcp"}))
        with patch("apps.api.opencode_api._connector_capability", return_value=cap), patch(
            "apps.api.opencode_mcp.get_latest_project_revision", return_value=None), patch(
            "apps.api.opencode_mcp.ensure_native_cad_model", side_effect=CadGenerationError("PRIVATE_DIAGNOSTIC")) as generate, patch(
            "apps.api.opencode_mcp._persist_mcp_compile") as persist:
            result = self.client.post("/api/opencode/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "forma.opencode.compile_project", "arguments": {"project_ir": cube().model_dump()}}})
            self.assertTrue(result.json()["result"]["isError"])
            self.assertEqual(result.json()["result"]["structuredContent"]["code"], "cad_generation_failed")
            self.assertNotIn("PRIVATE_DIAGNOSTIC", result.text)
            self.assertTrue(generate.call_args.kwargs["required"])
            persist.assert_not_called()
