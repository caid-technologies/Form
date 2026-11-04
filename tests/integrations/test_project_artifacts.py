from __future__ import annotations

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from forma_core.persistence.project_artifacts import (
    ProjectArtifactStorage,
    ProjectArtifactStorageError,
    project_artifact_storage_key,
)
from forma_core.workspaces.projects.manifest import validate_artifact_references


class ProjectArtifactStorageTests(unittest.TestCase):
    def local_storage(self, directory: str) -> ProjectArtifactStorage:
        return ProjectArtifactStorage({"enabled": True, "backend": "local", "directory": Path(directory),
                                       "bucket": "unused", "max_bytes": 1024})

    def test_independent_writers_publish_one_complete_object(self) -> None:
        content = b"the same immutable bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            def write(_: int) -> bytes:
                storage = self.local_storage(directory)
                storage.put("project", sha256, content, "application/octet-stream")
                return storage.get("project", sha256, "application/octet-stream").content

            with ThreadPoolExecutor(max_workers=8) as writers:
                self.assertEqual([content] * 32, list(writers.map(write, range(32))))
            self.assertEqual(1, len([p for p in Path(directory).rglob("*") if p.is_file()]))

    def test_windows_denial_after_competing_publish_is_successful(self) -> None:
        content = b"complete winner"
        sha256 = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            target = Path(directory) / project_artifact_storage_key("project", sha256)

            def competing_publish(temporary: Path, destination: Path) -> None:
                # Simulate Windows refusing replacement because the winner is
                # being read. Its content already satisfies this write.
                destination.write_bytes(content)
                raise PermissionError("target opened by another reader")

            with patch.object(Path, "replace", competing_publish), patch(
                "forma_core.persistence.project_artifacts.time.sleep"
            ):
                storage.put("project", sha256, content, "application/octet-stream")
            self.assertEqual(content, target.read_bytes())
            self.assertEqual([target], [p for p in Path(directory).rglob("*") if p.is_file()])

    def test_busy_corrupt_target_fails_without_success_or_temporary_files(self) -> None:
        content = b"expected immutable bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            target = Path(directory) / project_artifact_storage_key("project", sha256)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupt")
            with patch.object(Path, "replace", side_effect=PermissionError("busy")) as replace, patch(
                "forma_core.persistence.project_artifacts.time.sleep"
            ):
                with self.assertRaisesRegex(ProjectArtifactStorageError, "busy"):
                    storage.put("project", sha256, content, "application/octet-stream")
            self.assertEqual(8, replace.call_count)
            self.assertEqual(b"corrupt", target.read_bytes())
            self.assertEqual([target], [p for p in Path(directory).rglob("*") if p.is_file()])
            with self.assertRaisesRegex(ProjectArtifactStorageError, "SHA-256"):
                storage.get("project", sha256, "application/octet-stream")

    def test_download_checks_integrity_for_remote_storage(self) -> None:
        content = b"private expected artifact"
        sha256 = hashlib.sha256(content).hexdigest()
        storage = ProjectArtifactStorage({"enabled": True, "backend": "supabase", "bucket": "unused", "max_bytes": 1024})

        class CorruptBucket:
            def download(self, key: str) -> bytes:
                return b"different bytes"

        storage._supabase_bucket = lambda: CorruptBucket()  # type: ignore[method-assign]
        with self.assertRaisesRegex(ProjectArtifactStorageError, "SHA-256"):
            storage.get("project", sha256, "application/octet-stream")

    def test_local_storage_round_trip_is_project_scoped(self) -> None:
        content = b"private project artifact"
        sha256 = hashlib.sha256(content).hexdigest()
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

            stored = storage.put("project-a", sha256, content, "application/octet-stream")
            restored = storage.get("project-a", sha256, "application/octet-stream")

            self.assertEqual(content, restored.content)
            self.assertTrue((Path(directory) / Path(*project_artifact_storage_key("project-a", sha256).split("/"))).is_file())
            with self.assertRaises(FileNotFoundError):
                storage.get("project-b", sha256, "application/octet-stream")
            self.assertEqual(sha256, stored.sha256)
            with self.assertRaisesRegex(ProjectArtifactStorageError, "does not match"):
                storage.put("project-a", "0" * 64, content, "application/octet-stream")
            self.assertEqual(1, storage.delete_project("project-a"))
            with self.assertRaises(FileNotFoundError):
                storage.get("project-a", sha256, "application/octet-stream")
            self.assertEqual(0, storage.delete_project("project-a"))

    def test_supabase_contains_matches_object_under_folder_listing(self) -> None:
        content = b"supabase-backed artifact"
        sha256 = hashlib.sha256(content).hexdigest()
        key = project_artifact_storage_key("project-sb", sha256)
        folder, _, name = key.rpartition("/")

        class FakeSupabaseBucket:
            """Reproduce the storage list quirk: a prefix equal to an object
            key behaves like a folder and only lists its children."""

            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def upload(self, path: str, data: bytes, file_options: dict | None = None) -> None:
                self.objects[path] = bytes(data)

            def update(self, path: str, data: bytes, file_options: dict | None = None) -> None:
                self.objects[path] = bytes(data)

            def download(self, path: str) -> bytes:
                if path not in self.objects:
                    raise FileNotFoundError(path)
                return self.objects[path]

            def list(self, path: str, options: dict | None = None) -> list[dict]:
                prefix = (path or "").rstrip("/") + "/"
                children: list[dict] = []
                for object_key in self.objects:
                    if object_key.startswith(prefix):
                        remainder = object_key[len(prefix):]
                        children.append({"name": remainder.split("/", 1)[0]})
                return children

        storage = ProjectArtifactStorage(
            {
                "enabled": True,
                "backend": "supabase",
                "bucket": "cli-project-artifacts",
                "max_bytes": 1024,
            }
        )
        fake_bucket = FakeSupabaseBucket()
        storage._supabase_bucket = lambda: fake_bucket  # type: ignore[method-assign]

        storage.put("project-sb", sha256, content, "application/octet-stream")

        self.assertTrue(storage.contains("project-sb", sha256))
        self.assertFalse(storage.contains("project-sb", "0" * 64))

    def test_artifact_declarations_require_integrity_and_reject_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            validate_artifact_references([{"path": "assembly.step", "media_type": "model/step"}])
        with self.assertRaisesRegex(ValueError, "escape"):
            validate_artifact_references(
                [{"path": "../secret.txt", "sha256": "0" * 64, "media_type": "text/plain"}]
            )
        with self.assertRaisesRegex(ValueError, "duplicated"):
            validate_artifact_references(
                [
                    {"path": "Assembly.step", "sha256": "0" * 64, "media_type": "model/step"},
                    {"path": "assembly.step", "sha256": "1" * 64, "media_type": "model/step"},
                ]
            )


if __name__ == "__main__":
    unittest.main()
