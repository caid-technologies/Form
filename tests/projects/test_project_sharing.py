"""Authorization and immutable-version regression coverage for public links."""
from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from apps.api.project_share_api import router
from forma_core import database
from forma_core.persistence.models import DBProjectShare
from forma_core.persistence.repositories import SqlAlchemyRepository
from tests.projects import test_project_history


class ProjectSharingTests(test_project_history.ProjectVersionHistoryTests):
    """Exercise capabilities against actual SQLite revision persistence."""

    def setUp(self) -> None:
        """Create three versions and install the public sharing routes."""
        super().setUp()
        self.client.app.include_router(router)
        # Sharing must work without any additional deployment secret.
        self.enterContext(patch.dict(os.environ, {"FORMA_PROJECT_SHARE_SECRET": ""}))
        self.shared = f"/projects/{self.project_id}/shared"

    def issue(self, index: int = 0) -> dict[str, str]:
        """Issue a capability while acting as the project owner."""
        response = self.client.post(f"{self.shared}/{self.revisions[index].revision_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-Project-Share": response.json()["token"]}

    def test_anonymous_link_is_pinned_and_cannot_list_history(self) -> None:
        """Later saves do not change the version or broaden a recipient's access."""
        headers = self.issue()
        self.user = replace(self.user, owner_user_id="", is_authenticated=False)
        self.save("A newer private design")
        response = self.client.get(f"{self.shared}/{self.revisions[0].revision_id}", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(response.json()["title"], "Base")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 401)
        self.assertEqual(self.client.get(f"{self.url}/{self.revisions[0].revision_id}", headers=headers).status_code, 401)
        self.assertEqual(self.client.post(f"{self.shared}/{self.revisions[1].revision_id}", headers=headers).status_code, 401)

    def test_token_cannot_be_reused_for_another_revision_or_project(self) -> None:
        """A share capability is scoped to exactly one project and revision."""
        headers = self.issue()
        first = f"{self.shared}/{self.revisions[0].revision_id}"
        self.assertEqual(self.client.get(first).status_code, 404)
        self.assertEqual(self.client.get(first, headers={"X-Project-Share": "0" * 64}).status_code, 404)
        self.assertEqual(self.client.get(f"{self.shared}/{self.revisions[1].revision_id}", headers=headers).status_code, 404)
        other = str(uuid4())
        self.assertEqual(self.client.get(first.replace(self.project_id, other), headers=headers).status_code, 404)
        self.user = replace(self.user, owner_user_id="another-owner")
        self.assertEqual(self.client.post(first).status_code, 404)
        with patch("apps.api.project_share_api.get_project_identity", return_value={"owner_user_id": self.owner, "status": "deletion_pending"}):
            self.assertEqual(self.client.get(first, headers=headers).status_code, 404)

    def test_shared_projection_omits_private_provenance(self) -> None:
        """Internal generation metadata and instructions never leave the share API."""
        headers = self.issue()
        private = {"chat_id": "secret-chat", "last_iteration": {"instruction": "private prompt"},
                   "reference_image_url": "private-upload", "product_image_url": "https://example.test/design.png",
                   "product_visual_sequence": [{"url": "https://example.test/side.png", "prompt": "secret image prompt"}]}
        with patch("apps.api.project_history_api.hydrate_image_storage_metadata", return_value=private):
            response = self.client.get(f"{self.shared}/{self.revisions[0].revision_id}", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", response.text)
        self.assertNotIn("private prompt", response.text)
        self.assertNotIn("private-upload", response.text)
        self.assertEqual(response.json()["project_ir"]["project_version_history"], [])
        self.assertEqual(response.json()["project_ir"]["assembly_metadata"]["product_image_url"], "https://example.test/design.png")

    def test_shared_cad_is_bound_to_version_and_checksum(self) -> None:
        """Anonymous CAD reads use the same capability and artifact checksum."""
        content = b"saved STEP bytes"
        digest = hashlib.sha256(content).hexdigest()
        old = self.save("Shared CAD", cad={"stored_sha256": digest})
        headers = self.issue(-1)
        self.save("Later CAD", cad={"stored_sha256": "b" * 64})
        url = f"{self.shared}/{old.revision_id}/cad/{digest}"
        with patch("apps.api.project_history_api.ProjectArtifactStorage.get", return_value=SimpleNamespace(content=content)) as storage:
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.get(url.replace(digest, "b" * 64), headers=headers).status_code, 404)
            self.assertEqual(self.client.get(url, headers=headers).content, content)
            storage.assert_called_once_with(self.project_id, digest, "model/step")

    def test_links_are_random_hashed_and_survive_a_new_repository_instance(self) -> None:
        """Independent tokens persist in the database, with no signing key."""
        url = f"{self.shared}/{self.revisions[0].revision_id}"
        first = self.client.post(url).json()
        second = self.client.post(url).json()
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["token"], second["token"])
        self.assertRegex(first["token"], r"^[0-9a-f]{64}$")
        repository = database._DATABASE_REPOSITORY
        with repository._session() as session:
            rows = session.query(DBProjectShare).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].token_hash, hashlib.sha256(first["token"].encode()).hexdigest())
            self.assertFalse(hasattr(rows[0], "token"))
            self.assertNotIn(first["token"], str(vars(rows[0])))
        replacement = SqlAlchemyRepository(repository._session_factory)
        with patch.object(database, "_DATABASE_REPOSITORY", replacement):
            self.assertEqual(self.client.get(url, headers={"X-Project-Share": first["token"]}).status_code, 200)

    def test_owner_can_revoke_one_link_without_affecting_other_links(self) -> None:
        """Revocation is persistent, idempotent, and removes snapshot/CAD access."""
        content = b"saved STEP bytes"
        digest = hashlib.sha256(content).hexdigest()
        revision = self.save("Shared CAD", cad={"stored_sha256": digest})
        url = f"{self.shared}/{revision.revision_id}"
        first = self.client.post(url).json()
        second = self.client.post(url).json()
        revoke = f"{url}/links/{first['id']}"
        self.assertEqual(self.client.delete(revoke).status_code, 204)
        self.assertEqual(self.client.delete(revoke).status_code, 204)
        first_headers = {"X-Project-Share": first["token"]}
        second_headers = {"X-Project-Share": second["token"]}
        self.assertEqual(self.client.get(url, headers=first_headers).status_code, 404)
        with patch("apps.api.project_history_api.ProjectArtifactStorage.get", return_value=SimpleNamespace(content=content)) as storage:
            self.assertEqual(self.client.get(f"{url}/cad/{digest}", headers=first_headers).status_code, 404)
            storage.assert_not_called()
            self.assertEqual(self.client.get(f"{url}/cad/{digest}", headers=second_headers).content, content)
        self.assertEqual(self.client.get(url, headers=second_headers).status_code, 200)
        page = self.client.get(f"{url}/links").json()
        first_record = next(item for item in page["items"] if item["id"] == first["id"])
        self.assertIsNotNone(first_record["revoked_at"])
        self.assertIsNone(next(item for item in page["items"] if item["id"] == second["id"])["revoked_at"])

    def test_link_management_is_owner_only_and_scoped_to_the_revision(self) -> None:
        """Holding a capability grants no access to management or private history."""
        url = f"{self.shared}/{self.revisions[0].revision_id}"
        link = self.client.post(url).json()
        headers = {"X-Project-Share": link["token"]}
        management = f"{url}/links"
        self.user = replace(self.user, owner_user_id="another-owner")
        self.assertEqual(self.client.get(management, headers=headers).status_code, 404)
        self.assertEqual(self.client.delete(f"{management}/{link['id']}", headers=headers).status_code, 404)
        self.user = replace(self.user, owner_user_id="", is_authenticated=False)
        self.assertEqual(self.client.get(management, headers=headers).status_code, 401)
        self.assertEqual(self.client.delete(f"{management}/{link['id']}", headers=headers).status_code, 401)
        self.user = replace(self.user, owner_user_id=self.owner, is_authenticated=True)
        other = f"{self.shared}/{self.revisions[1].revision_id}/links"
        self.assertEqual(self.client.get(other).json()["items"], [])
        self.assertEqual(self.client.delete(f"{other}/{link['id']}").status_code, 204)
        self.assertEqual(self.client.get(url, headers=headers).status_code, 200)

    def test_link_list_is_paginated_and_never_returns_tokens_or_hashes(self) -> None:
        url = f"{self.shared}/{self.revisions[0].revision_id}"
        links = [self.client.post(url).json() for _ in range(3)]
        first = self.client.get(f"{url}/links?limit=2")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["cache-control"], "private, no-store")
        self.assertEqual(first.json()["next_offset"], 2)
        second = self.client.get(f"{url}/links?limit=2&offset=2").json()
        self.assertIsNone(second["next_offset"])
        records = first.json()["items"] + second["items"]
        self.assertEqual({item["id"] for item in records}, {link["id"] for link in links})
        for item in records:
            self.assertEqual(set(item), {"id", "revision_id", "created_at", "revoked_at"})
        for link in links:
            self.assertNotIn(link["token"], first.text)
        self.assertEqual(self.client.get(f"{url}/links?limit=101").status_code, 422)
        self.assertEqual(self.client.get(f"{url}/links?offset=-1").status_code, 422)

    def test_missing_revisions_and_failed_writes_do_not_issue_capabilities(self) -> None:
        with patch("apps.api.project_share_api.insert_project_share") as insert:
            self.assertEqual(self.client.post(f"{self.shared}/{uuid4()}").status_code, 404)
            insert.assert_not_called()
            insert.side_effect = RuntimeError("database unavailable")
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                self.issue()

    def test_deleted_project_and_changed_owner_invalidate_links(self) -> None:
        url = f"{self.shared}/{self.revisions[0].revision_id}"
        headers = self.issue()
        with patch("apps.api.project_share_api.get_project_identity", return_value={"owner_user_id": "new-owner", "status": "active"}):
            self.assertEqual(self.client.get(url, headers=headers).status_code, 404)
        database._DATABASE_REPOSITORY.hard_purge_project(self.project_id, self.owner)
        self.assertEqual(self.client.get(url, headers=headers).status_code, 404)
        with database._DATABASE_REPOSITORY._session() as session:
            self.assertEqual(session.query(DBProjectShare).count(), 0)
