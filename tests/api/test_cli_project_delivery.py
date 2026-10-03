from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from apps.api import cli_projects_api
from apps.api.auth import UserContext
from apps.api.cli_projects_api import ProjectDeliverRequest
from forma_core.database import CliProjectConflictError
from forma_core.persistence.project_artifacts import ProjectArtifactStorage


def _user(owner: str = "user-a") -> UserContext:
    return UserContext(
        provider="clerk",
        subject=owner,
        owner_user_id=owner,
        is_authenticated=True,
        is_admin=False,
        claims={"sub": owner},
    )


def _manifest(project_id: str = "project-delivery") -> dict[str, object]:
    return {
        "format": "forma-project",
        "version": 1,
        "project_id": project_id,
        "title": "Delivered project",
        "prompt": "deliver a project snapshot",
        "artifacts": [],
    }


def _deliver_request(
    *,
    manifest: dict[str, object] | None = None,
    idempotency_key: str = "key-1",
    visibility: str | None = None,
    parent_revision_id: str | None = None,
) -> ProjectDeliverRequest:
    return ProjectDeliverRequest(
        manifest=manifest if manifest is not None else _manifest(),
        idempotency_key=idempotency_key,
        visibility=visibility,
        parent_revision_id=parent_revision_id,
    )


def _saved_revision(project_id: str = "project-delivery") -> dict[str, object]:
    return {
        "revision_id": "rev-1",
        "project_id": project_id,
        "revision": 1,
        "parent_revision_id": None,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _delivery_record(project_id: str = "project-delivery") -> dict[str, object]:
    return {
        "delivery_id": "delivery-1",
        "project_id": project_id,
        "owner_user_id": "user-a",
        "idempotency_key": "key-1",
        "revision_id": "rev-1",
        "revision": 1,
        "parent_revision_id": None,
        "manifest": _manifest(project_id),
        "status": "pending",
        "receipt": None,
        "created_at": "2026-01-01T00:00:00Z",
        "completed_at": None,
    }


class CliProjectDeliveryApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_deliver_creates_pending_receipt_and_defaults_to_private(self) -> None:
        captured: dict[str, object] = {}

        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            captured["manifest"] = manifest
            captured["owner"] = owner
            return _saved_revision()

        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=None),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save) as save,
            patch.object(cli_projects_api, "insert_cli_project_delivery", return_value=_delivery_record()) as save_delivery,
        ):
            receipt = await cli_projects_api.deliver_cli_project(_deliver_request(), _user())

        self.assertEqual("pending", receipt["status"])
        self.assertEqual("delivery-1", receipt["delivery_id"])
        self.assertEqual("private", captured["manifest"]["visibility"])
        save.assert_called_once()
        save_delivery.assert_called_once()
        self.assertEqual("user-a", captured["owner"])

    async def test_deliver_honors_explicit_visibility(self) -> None:
        captured: dict[str, object] = {}

        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            captured["manifest"] = manifest
            return _saved_revision()

        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=None),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save),
            patch.object(cli_projects_api, "insert_cli_project_delivery", return_value=_delivery_record()) as save_delivery,
        ):
            await cli_projects_api.deliver_cli_project(_deliver_request(visibility="public"), _user())

        self.assertEqual("public", captured["manifest"]["visibility"])
        save_delivery.assert_called_once()

    async def test_deliver_rejects_invalid_visibility_and_owner_spoofing(self) -> None:
        with patch.object(cli_projects_api, "insert_cli_project_revision") as save:
            with self.assertRaisesRegex(HTTPException, "public or private"):
                await cli_projects_api.deliver_cli_project(_deliver_request(visibility="secret"), _user())
            with self.assertRaisesRegex(HTTPException, "cannot be supplied"):
                await cli_projects_api.deliver_cli_project(
                    _deliver_request(manifest={**_manifest(), "owner_user_id": "attacker"}),
                    _user(),
                )
        save.assert_not_called()

    async def test_deliver_replays_existing_delivery_without_new_revision(self) -> None:
        existing = _delivery_record()
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=existing) as fetch,
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=_saved_revision()),
            patch.object(cli_projects_api, "insert_cli_project_revision") as save,
            patch.object(cli_projects_api, "insert_cli_project_delivery") as save_delivery,
        ):
            receipt = await cli_projects_api.deliver_cli_project(_deliver_request(), _user())

        self.assertEqual("delivery-1", receipt["delivery_id"])
        fetch.assert_called_once_with("project-delivery", "user-a", "key-1")
        save.assert_not_called()
        save_delivery.assert_not_called()

    async def test_deliver_rejects_idempotency_key_reuse_with_different_content(self) -> None:
        existing = _delivery_record()
        existing["manifest_digest"] = "different-content"
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=existing),
            patch.object(cli_projects_api, "insert_cli_project_revision") as save,
        ):
            with self.assertRaises(HTTPException) as raised:
                await cli_projects_api.deliver_cli_project(_deliver_request(), _user())

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("IDEMPOTENCY_KEY_REUSE", raised.exception.detail["code"])
        save.assert_not_called()

    async def test_deliver_reserves_before_materializing_revision(self) -> None:
        events: list[str] = []

        def fake_insert_delivery(record):
            events.append("reserve")
            return {
                **_delivery_record(),
                "project_id": record["project_id"],
                "idempotency_key": record["idempotency_key"],
                "revision_id": record["revision_id"],
                "revision": record["revision"],
                "manifest": record["manifest_json"],
                "manifest_digest": record["manifest_digest"],
            }

        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            events.append("revision")
            return {**_saved_revision(), "revision_id": revision_id, "revision": revision}

        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=None),
            patch.object(cli_projects_api, "insert_cli_project_delivery", side_effect=fake_insert_delivery),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save),
        ):
            await cli_projects_api.deliver_cli_project(_deliver_request(), _user())

        self.assertEqual(["reserve", "revision"], events)

    async def test_deliver_409_is_enriched_with_current_server_revision(self) -> None:
        latest = {"revision_id": "server-rev-9", "revision": 9}

        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            raise CliProjectConflictError("The cloud project changed since the local project was last pulled.")

        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", side_effect=[None, None, latest]),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save),
            patch.object(cli_projects_api, "insert_cli_project_delivery", return_value=_delivery_record()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await cli_projects_api.deliver_cli_project(_deliver_request(), _user())

        self.assertEqual(409, raised.exception.status_code)
        detail = raised.exception.detail
        self.assertEqual("PROJECT_REVISION_CONFLICT", detail["code"])
        self.assertEqual("server-rev-9", detail["current_revision_id"])
        self.assertEqual(9, detail["current_revision"])

    def test_contains_reports_local_artifact_presence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ProjectArtifactStorage(
                {
                    "enabled": True,
                    "backend": "local",
                    "directory": Path(directory),
                    "bucket": "unused",
                    "max_bytes": 1024,
                }
            )
            sha256 = hashlib.sha256(b"native cad bytes").hexdigest()
            self.assertFalse(storage.contains("project-delivery", sha256))
            storage.put("project-delivery", sha256, b"native cad bytes", "model/step")
            self.assertTrue(storage.contains("project-delivery", sha256))

    async def test_complete_verifies_artifacts_and_returns_complete_receipt(self) -> None:
        content = b"native cad bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        delivery = _delivery_record()
        delivery["manifest"] = {
            **_manifest(),
            "artifacts": [
                {"path": "assembly.step", "sha256": sha256, "media_type": "model/step", "size_bytes": len(content)}
            ],
        }
        captured: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as directory:
            storage = ProjectArtifactStorage(
                {
                    "enabled": True,
                    "backend": "local",
                    "directory": Path(directory),
                    "bucket": "unused",
                    "max_bytes": 1024,
                }
            )
            storage.put(delivery["project_id"], sha256, content, "model/step")

            def fake_update(delivery_id, owner, updates):
                captured["updates"] = updates
                receipt = {
                    "delivery_id": delivery_id,
                    "status": "complete",
                    "project": {
                        "project_id": delivery["project_id"],
                        "revision_id": delivery["revision_id"],
                        "revision": delivery["revision"],
                        "parent_revision_id": None,
                        "visibility": "private",
                    },
                    "artifacts": [
                        {"path": "assembly.step", "sha256": sha256, "media_type": "model/step", "size_bytes": len(content), "status": "present"}
                    ],
                    "artifact_summary": {"declared": 1, "present": 1},
                    "created_at": delivery["created_at"],
                    "completed_at": updates["completed_at"],
                }
                return {**delivery, **updates, "receipt": receipt}

            with (
                patch.object(cli_projects_api, "get_cli_project_delivery_by_id", return_value=delivery),
                patch.object(cli_projects_api, "ProjectArtifactStorage", return_value=storage),
                patch.object(cli_projects_api, "update_cli_project_delivery", side_effect=fake_update) as update,
            ):
                receipt = await cli_projects_api.complete_cli_project_delivery("delivery-1", _user())

        self.assertEqual("complete", receipt["status"])
        self.assertEqual("present", receipt["artifacts"][0]["status"])
        update.assert_called_once()
        self.assertEqual("complete", captured["updates"]["status"])
        self.assertIsNotNone(captured["updates"]["completed_at"])

    async def test_complete_replays_a_completed_receipt(self) -> None:
        completed = _delivery_record()
        completed["status"] = "complete"
        completed["receipt"] = {"delivery_id": "delivery-1", "status": "complete", "project": {}, "artifacts": []}
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery_by_id", return_value=completed),
            patch.object(cli_projects_api, "update_cli_project_delivery") as update,
        ):
            receipt = await cli_projects_api.complete_cli_project_delivery("delivery-1", _user())

        self.assertEqual("complete", receipt["status"])
        update.assert_not_called()

    async def test_complete_rejects_missing_artifacts_before_commit(self) -> None:
        content = b"native cad bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        delivery = _delivery_record()
        delivery["manifest"] = {
            **_manifest(),
            "artifacts": [
                {"path": "assembly.step", "sha256": sha256, "media_type": "model/step", "size_bytes": len(content)}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            storage = ProjectArtifactStorage(
                {
                    "enabled": True,
                    "backend": "local",
                    "directory": Path(directory),
                    "bucket": "unused",
                    "max_bytes": 1024,
                }
            )
            with (
                patch.object(cli_projects_api, "get_cli_project_delivery_by_id", return_value=delivery),
                patch.object(cli_projects_api, "ProjectArtifactStorage", return_value=storage),
                patch.object(cli_projects_api, "update_cli_project_delivery") as update,
            ):
                with self.assertRaises(HTTPException) as raised:
                    await cli_projects_api.complete_cli_project_delivery("delivery-1", _user())

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("DELIVERY_ARTIFACTS_MISSING", raised.exception.detail["code"])
        self.assertEqual(0, raised.exception.detail["artifact_summary"]["present"])
        update.assert_not_called()

    async def test_complete_is_owner_scoped(self) -> None:
        delivery = _delivery_record()
        with patch.object(cli_projects_api, "get_cli_project_delivery_by_id", return_value=delivery):
            with self.assertRaises(HTTPException) as raised:
                await cli_projects_api.complete_cli_project_delivery("delivery-1", _user("intruder"))

        self.assertEqual(404, raised.exception.status_code)

    async def test_publish_surfaces_explicit_action_and_audit_trail(self) -> None:
        publish_result = {
            "project_id": "project-delivery",
            "visibility": "public",
            "published": True,
            "visibility_before": "private",
            "published_at": "2026-01-01T00:00:00Z",
        }
        audits = [
            {
                "acting_user_id": "user-a",
                "visibility_before": "private",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        with (
            patch.object(cli_projects_api, "publish_cli_project", return_value=publish_result) as publish_action,
            patch.object(cli_projects_api, "list_project_publish_audits", return_value=audits) as list_audits,
        ):
            result = await cli_projects_api.publish_cli_project_endpoint("project-delivery", _user())

        publish_action.assert_called_once_with("project-delivery", "user-a", acting_user_id="user-a")
        list_audits.assert_called_once_with("project-delivery", "user-a")
        self.assertTrue(result["published"])
        self.assertEqual(audits, result["audits"])

    async def test_publish_maps_missing_project_to_404(self) -> None:
        with patch.object(cli_projects_api, "publish_cli_project", side_effect=ValueError("Project identity not found.")):
            with self.assertRaises(HTTPException) as raised:
                await cli_projects_api.publish_cli_project_endpoint("missing-project", _user())

        self.assertEqual(404, raised.exception.status_code)

    def test_deliver_command_defaults_provide_a_stable_idempotency_key(self) -> None:
        import forma_cli.app as app

        key = app._delivery_idempotency_key(_manifest())
        self.assertTrue(key.startswith("deliver:"))
        self.assertEqual(64, len(key.removeprefix("deliver:")))

    def test_push_command_defaults_provide_a_stable_idempotency_key(self) -> None:
        import forma_cli.app as app

        key = app._push_idempotency_key(_manifest())
        self.assertTrue(key.startswith("push:"))
        self.assertEqual(64, len(key.removeprefix("push:")))


class PushApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_creates_pending_receipt_and_defaults_to_private(self) -> None:
        captured: dict[str, object] = {}
        captured_keys: list[str] = []

        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            captured["manifest"] = manifest
            captured["owner"] = owner
            return _saved_revision()

        def fake_insert_delivery(record):
            captured_keys.append(record["idempotency_key"])
            return _delivery_record()

        request = cli_projects_api.ProjectPushRequest(manifest=_manifest())
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=None),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save) as save,
            patch.object(cli_projects_api, "insert_cli_project_delivery", side_effect=fake_insert_delivery) as save_delivery,
        ):
            receipt = await cli_projects_api.push_cli_project(request, _user())

        self.assertEqual("pending", receipt["status"])
        self.assertEqual("delivery-1", receipt["delivery_id"])
        self.assertEqual("private", captured["manifest"]["visibility"])
        self.assertTrue(captured_keys[0].startswith("push:"))
        save.assert_called_once()
        save_delivery.assert_called_once()
        self.assertEqual("user-a", captured["owner"])

    async def test_push_honors_explicit_visibility_and_key(self) -> None:
        captured: dict[str, object] = {}

        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            captured["manifest"] = manifest
            return _saved_revision()

        request = cli_projects_api.ProjectPushRequest(
            manifest=_manifest(), visibility="public", idempotency_key="explicit-key"
        )
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=None),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save),
            patch.object(cli_projects_api, "insert_cli_project_delivery", return_value=_delivery_record()),
        ):
            await cli_projects_api.push_cli_project(request, _user())

        self.assertEqual("public", captured["manifest"]["visibility"])

    async def test_push_rejects_invalid_visibility_and_owner_spoofing(self) -> None:
        request_bad_vis = cli_projects_api.ProjectPushRequest(manifest=_manifest(), visibility="secret")
        request_spoofed = cli_projects_api.ProjectPushRequest(
            manifest={**_manifest(), "owner_user_id": "attacker"}
        )
        with patch.object(cli_projects_api, "insert_cli_project_revision") as save:
            with self.assertRaisesRegex(HTTPException, "public or private"):
                await cli_projects_api.push_cli_project(request_bad_vis, _user())
            with self.assertRaisesRegex(HTTPException, "cannot be supplied"):
                await cli_projects_api.push_cli_project(request_spoofed, _user())
        save.assert_not_called()

    async def test_push_replays_existing_delivery_without_new_revision(self) -> None:
        existing = _delivery_record()
        request = cli_projects_api.ProjectPushRequest(manifest=_manifest(), idempotency_key="key-1")
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=existing) as fetch,
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=_saved_revision()),
            patch.object(cli_projects_api, "insert_cli_project_revision") as save,
            patch.object(cli_projects_api, "insert_cli_project_delivery") as save_delivery,
        ):
            receipt = await cli_projects_api.push_cli_project(request, _user())

        self.assertEqual("delivery-1", receipt["delivery_id"])
        self.assertEqual("pending", receipt["status"])
        fetch.assert_called_once_with("project-delivery", "user-a", "key-1")
        save.assert_not_called()
        save_delivery.assert_not_called()

    async def test_push_derives_key_when_not_supplied(self) -> None:
        captured_key: list[str] = []

        def fake_insert_delivery(record):
            captured_key.append(record["idempotency_key"])
            return _delivery_record()

        request = cli_projects_api.ProjectPushRequest(manifest=_manifest())
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", return_value=None),
            patch.object(cli_projects_api, "insert_cli_project_revision", return_value=_saved_revision()),
            patch.object(cli_projects_api, "insert_cli_project_delivery", side_effect=fake_insert_delivery),
        ):
            await cli_projects_api.push_cli_project(request, _user())

        self.assertEqual(1, len(captured_key))
        self.assertTrue(captured_key[0].startswith("push:"))
        self.assertEqual(64, len(captured_key[0].removeprefix("push:")))

    async def test_push_409_is_enriched_with_current_server_revision(self) -> None:
        def fake_save(manifest, owner, *, expected_revision_id=None, revision_id=None, revision=None):
            raise CliProjectConflictError("The cloud project changed since the local project was last pulled.")

        latest = {"revision_id": "server-rev-9", "revision": 9}
        request = cli_projects_api.ProjectPushRequest(manifest=_manifest(), parent_revision_id="stale")
        with (
            patch.object(cli_projects_api, "get_cli_project_delivery", return_value=None),
            patch.object(cli_projects_api, "get_cli_project_revision", side_effect=[None, None, latest]),
            patch.object(cli_projects_api, "insert_cli_project_revision", side_effect=fake_save),
            patch.object(cli_projects_api, "insert_cli_project_delivery", return_value=_delivery_record()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await cli_projects_api.push_cli_project(request, _user())

        self.assertEqual(409, raised.exception.status_code)
        detail = raised.exception.detail
        self.assertEqual("PROJECT_REVISION_CONFLICT", detail["code"])
        self.assertEqual("server-rev-9", detail["current_revision_id"])
        self.assertEqual(9, detail["current_revision"])


if __name__ == "__main__":
    unittest.main()
