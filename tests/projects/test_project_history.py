from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.auth import UserContext, require_user_context
from apps.api.project_history_api import router
from forma_core import database
from forma_core.persistence.images import hydrate_image_storage_metadata
from forma_core.persistence.repositories.supabase import SupabaseRepository
from forma_core.workspaces.design_briefs import DesignBriefCreate
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ProjectOverview
from forma_core.workspaces.projects.state import ProjectRevisionDraft, ProjectStateService
from tests.persistence.test_design_briefs import sqlite_repository


class ProjectVersionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(sqlite_repository())
        self.project_id = str(uuid4())
        self.owner = "history-owner"
        self.user = UserContext(provider="test", subject=self.owner, owner_user_id=self.owner, is_authenticated=True, is_admin=False)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user_context] = lambda: self.user
        self.client = self.enterContext(TestClient(app))
        self.service = ProjectStateService(database._DATABASE_REPOSITORY)
        self.brief = database.create_design_brief_version(self.project_id, self.owner, DesignBriefCreate(
            schema_version="1.0", conversation_id="history-chat", intent="Build a robot arm", summary="Build a robot arm",
        ))
        self.revisions = []
        self.save("Base")
        self.save("Arm links")
        self.save("Gripper")
        self.url = f"/projects/{self.project_id}/history"

    def save(self, title, *, cad=None, source=None):
        ir = HardwareIntermediateRepresentation(overview=ProjectOverview(title=title, description=title, difficulty="Beginner", category="Mechanical"), cad_model=cad)
        draft = ProjectRevisionDraft(state=ir)
        kwargs = dict(project_id=self.project_id, owner_user_id=self.owner, source_job_id=source or f"update-{len(self.revisions) + 1}")
        if not self.revisions:
            outcome = self.service.create_initial_revision(draft, **kwargs, design_brief_id=self.brief.design_brief_id, design_brief_version=1)
        else:
            outcome = self.service.create_revision(draft, **kwargs)
        self.revisions.append(outcome.revision)
        return outcome.revision

    def test_pages_are_ordered_and_cursor_does_not_skip_when_new_versions_arrive(self):
        response = self.client.get(self.url, params={"limit": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        page = response.json()
        self.assertEqual([item["revision"] for item in page["items"]], [3, 2])
        self.assertEqual(page["next_before"], 2)
        self.assertNotIn("project_ir", page["items"][0])
        self.save("New image", source="opencode-image-test")
        older = self.client.get(self.url, params={"limit": 2, "before": page["next_before"]}).json()
        self.assertEqual([item["revision"] for item in older["items"]], [1])
        self.assertIsNone(older["next_before"])
        self.assertEqual(older["latest_revision"], 4)
        self.assertEqual(self.client.get(self.url).json()["items"][0]["summary"], "Generated concept image")

    def test_exact_snapshot_and_identity_survive_later_saves_without_mutating_latest(self):
        old = self.revisions[0]
        response = self.client.get(f"{self.url}/{old.revision_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["project_ir"]["overview"]["title"], "Base")
        self.assertEqual(payload["project_ir"]["assembly_metadata"]["canonical_revision_id"], str(old.revision_id))
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(self.service.get_latest(self.project_id, self.owner).state.overview.title, "Gripper")
        self.assertEqual(self.client.get(f"{self.url}/{uuid4()}").status_code, 404)
        self.assertEqual(self.client.get(f"{self.url}/not-a-revision").status_code, 422)
        self.assertEqual(self.client.get(self.url, params={"limit": 1000}).status_code, 422)

    def test_public_project_history_remains_private_and_deleted_projects_are_hidden(self):
        self.user = replace(self.user, owner_user_id="another-owner")
        with patch("apps.api.project_history_api.list_project_revisions") as query:
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.client.get(f"{self.url}/{self.revisions[0].revision_id}").status_code, 404)
            query.assert_not_called()
        self.user = replace(self.user, owner_user_id=self.owner)
        with patch("apps.api.project_history_api.get_project_identity", return_value={"owner_user_id": self.owner, "status": "deletion_pending"}):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        other = str(uuid4())
        database.ensure_project_identity(other, self.owner, title="Other project")
        self.assertEqual(self.client.get(f"/projects/{other}/history/{self.revisions[0].revision_id}").status_code, 404)

    def test_legacy_project_without_snapshots_returns_empty_history(self):
        other = str(uuid4())
        database.ensure_project_identity(other, self.owner, title="Legacy project")
        page = self.client.get(f"/projects/{other}/history").json()
        self.assertEqual(page["items"], [])
        self.assertIsNone(page["latest_revision"])

    def test_snapshot_images_only_resign_saved_keys_and_never_discover_newer_images(self):
        metadata = {"project_id": self.project_id, "product_image_s3_key": "saved/v1.png", "product_image_s3_bucket": "images"}
        with patch("forma_core.persistence.images.get_image_storage_config", return_value={"write_method": "supabase-client", "bucket": "images", "signed_url_seconds": 300}), \
             patch("forma_core.persistence.images._find_project_image_key") as discover, \
             patch("forma_core.persistence.images.create_signed_image_url", return_value="https://example.test/v1.png") as sign:
            hydrated = hydrate_image_storage_metadata(metadata, self.project_id, discover_missing=False)
            self.assertEqual(hydrated["product_image_url"], "https://example.test/v1.png")
            sign.assert_called_once_with("images", "saved/v1.png", 300)
            discover.assert_not_called()
            self.assertNotIn("product_image_url", metadata)
        with patch("apps.api.project_history_api.hydrate_image_storage_metadata", side_effect=lambda metadata, *args, **kwargs: metadata) as hydrate:
            self.client.get(f"{self.url}/{self.revisions[0].revision_id}")
            self.assertFalse(hydrate.call_args.kwargs["discover_missing"])

    def test_historical_step_is_scoped_to_exact_revision_and_checksum(self):
        content = b"ISO-10303-21;\nsaved STEP\nEND-ISO-10303-21;"
        digest = hashlib.sha256(content).hexdigest()
        old = self.save("Old CAD", cad={"stored_sha256": digest})
        self.save("New CAD", cad={"stored_sha256": "b" * 64})
        url = f"{self.url}/{old.revision_id}/cad/{digest}"
        snapshot = self.client.get(f"{self.url}/{old.revision_id}").json()
        self.assertEqual(snapshot["project_ir"]["cad_model"]["signed_url"], f"/api{url}")
        with patch("apps.api.project_history_api.ProjectArtifactStorage.get", return_value=SimpleNamespace(content=content)) as storage:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, content)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(self.client.get(url.replace(digest, "b" * 64)).status_code, 404)
            storage.assert_called_once_with(self.project_id, digest, "model/step")
            storage.return_value = SimpleNamespace(content=b"corrupted")
            self.assertEqual(self.client.get(url).status_code, 503)


class SupabaseHistoryQueryTests(unittest.TestCase):
    def test_history_queries_keep_owner_project_and_revision_filters(self):
        client = MagicMock()
        query = client.table.return_value
        for name in ("select", "eq", "lt", "order", "limit"):
            getattr(query, name).return_value = query
        query.execute.return_value.data = []
        repository = SupabaseRepository(client)
        self.assertEqual(repository.list_project_revisions("project", "owner", limit=21, before=9), [])
        query.eq.assert_any_call("project_id", "project")
        query.eq.assert_any_call("owner_user_id", "owner")
        query.lt.assert_called_once_with("revision", 9)
        query.order.assert_called_once_with("revision", desc=True)
        query.limit.assert_called_once_with(21)
        repository.get_project_revision_by_id("project", "owner", "revision")
        query.eq.assert_any_call("id", "revision")
