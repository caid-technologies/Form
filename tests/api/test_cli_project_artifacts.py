from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from apps.api import cli_projects_api
from apps.api.auth import UserContext
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, project_artifact_storage_key


def _user(owner: str = "user-a") -> UserContext:
    return UserContext(
        provider="clerk",
        subject=owner,
        owner_user_id=owner,
        is_authenticated=True,
        is_admin=False,
        claims={"sub": owner},
    )


def _request(content: bytes, media_type: str) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": content, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/cli/projects/project-a/revisions/revision-1/artifacts",
            "headers": [
                (b"content-type", media_type.encode("ascii")),
                (b"content-length", str(len(content)).encode("ascii")),
            ],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        },
        receive,
    )


class CliProjectArtifactApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_project_list_reads_shared_project_identities(self) -> None:
        identities = [
            {"project_id": "hosted-project", "creation_channel": "hosted", "title": "Hosted project"},
            {"project_id": "cli-project", "creation_channel": "cli", "title": "CLI project"},
        ]
        with patch.object(cli_projects_api, "list_project_identities", return_value=identities) as list_identities:
            response = await cli_projects_api.list_cli_projects_endpoint(_user())

        list_identities.assert_called_once_with("user-a")
        self.assertEqual(identities, response["items"])

    def setUp(self) -> None:
        self.content = b"native cad bytes"
        self.sha256 = hashlib.sha256(self.content).hexdigest()
        self.revision = {
            "revision_id": "revision-1",
            "project_id": "project-a",
            "revision": 1,
            "parent_revision_id": None,
            "manifest": {
                "format": "forma-project",
                "project_id": "project-a",
                "project_ir": {"cad_model": {"path": "assembly.step"}},
                "artifacts": [
                    {
                        "path": "assembly.step",
                        "sha256": self.sha256,
                        "media_type": "model/step",
                        "size_bytes": len(self.content),
                    }
                ],
            },
        }

    def _storage(self, directory: str) -> ProjectArtifactStorage:
        return ProjectArtifactStorage(
            {
                "enabled": True,
                "backend": "local",
                "directory": Path(directory),
                "bucket": "unused",
                "max_bytes": 1024,
            }
        )

    async def test_upload_and_download_are_owner_scoped_and_integrity_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = self._storage(directory)
            with (
                patch.object(cli_projects_api, "get_cli_project_revision", return_value=self.revision),
                patch.object(cli_projects_api, "ProjectArtifactStorage", return_value=storage),
            ):
                uploaded = await cli_projects_api.upload_cli_project_artifact(
                    "project-a",
                    "revision-1",
                    self.sha256.upper(),
                    _request(self.content, "model/step"),
                    _user(),
                )
                downloaded = await cli_projects_api.download_cli_project_artifact(
                    "project-a",
                    "revision-1",
                    self.sha256,
                    _user(),
                )
                object_path = Path(directory) / Path(*project_artifact_storage_key("project-a", self.sha256).split("/"))
                object_path.write_bytes(b"corrupt")
                with self.assertRaisesRegex(HTTPException, "integrity check"):
                    await cli_projects_api.download_cli_project_artifact(
                        "project-a",
                        "revision-1",
                        self.sha256,
                        _user(),
                    )

        self.assertEqual("uploaded", uploaded["status"])
        self.assertEqual(self.sha256, uploaded["sha256"])
        self.assertEqual(self.content, downloaded.body)
        self.assertEqual("model/step", downloaded.headers["content-type"])
        self.assertEqual(self.sha256, downloaded.headers["x-forma-artifact-sha256"])

    async def test_upload_rejects_bad_bytes_and_wrong_media_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = self._storage(directory)
            with (
                patch.object(cli_projects_api, "get_cli_project_revision", return_value=self.revision),
                patch.object(cli_projects_api, "ProjectArtifactStorage", return_value=storage),
            ):
                with self.assertRaisesRegex(HTTPException, "hash mismatch"):
                    await cli_projects_api.upload_cli_project_artifact(
                        "project-a",
                        "revision-1",
                        self.sha256,
                        _request(b"tampered", "model/step"),
                        _user(),
                    )
                with self.assertRaisesRegex(HTTPException, "media type mismatch"):
                    await cli_projects_api.upload_cli_project_artifact(
                        "project-a",
                        "revision-1",
                        self.sha256,
                        _request(self.content, "application/octet-stream"),
                        _user(),
                    )

    async def test_artifact_endpoints_hide_unowned_revisions(self) -> None:
        with patch.object(cli_projects_api, "get_cli_project_revision", return_value=None):
            with self.assertRaisesRegex(HTTPException, "not found or not authorized") as raised:
                await cli_projects_api.download_cli_project_artifact(
                    "project-a",
                    "revision-1",
                    self.sha256,
                    _user("user-b"),
                )
        self.assertEqual(404, raised.exception.status_code)

    async def test_push_rejects_artifact_without_integrity_metadata(self) -> None:
        request = cli_projects_api.ProjectPushRequest(
            manifest={
                "format": "forma-project",
                "project_id": "project-a",
                "project_ir": {},
                "artifacts": [{"path": "assembly.step", "media_type": "model/step"}],
            }
        )
        with self.assertRaisesRegex(HTTPException, "SHA-256") as raised:
            await cli_projects_api.push_cli_project(request, _user())
        self.assertEqual(400, raised.exception.status_code)

    async def test_push_rejects_unsupported_hardware_ir_before_persisting(self) -> None:
        request = cli_projects_api.ProjectPushRequest(
            manifest={
                "format": "forma-project",
                "project_id": "project-a",
                "project_ir": {"hardware_ir_version": "9.0"},
            }
        )
        with patch.object(cli_projects_api, "insert_cli_project_revision") as insert:
            with self.assertRaises(HTTPException) as raised:
                await cli_projects_api.push_cli_project(request, _user())

        self.assertEqual(426, raised.exception.status_code)
        self.assertEqual("UNSUPPORTED_HARDWARE_IR_VERSION", raised.exception.detail["code"])
        insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
