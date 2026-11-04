from __future__ import annotations

import hashlib
import errno
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

            # New and identical objects use create-once publication, never a
            # replace that can interrupt another writer's concurrent read.
            with patch.object(Path, "replace", side_effect=AssertionError("Immutable winner must not be replaced")):
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

            with patch("forma_core.persistence.project_artifacts.os.link", competing_publish), patch.object(
                Path, "replace", side_effect=AssertionError("Verified winner must not be replaced")
            ), patch(
                "forma_core.persistence.project_artifacts.time.sleep"
            ):
                storage.put("project", sha256, content, "application/octet-stream")
            self.assertEqual(content, target.read_bytes())
            self.assertEqual([target], [p for p in Path(directory).rglob("*") if p.is_file()])

    def test_local_read_retries_transient_sharing_denial(self) -> None:
        content = b"verified stored bytes"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            storage.put("project", digest, content, "application/octet-stream")
            target = Path(directory) / project_artifact_storage_key("project", digest)
            read_bytes = Path.read_bytes
            reads = []

            def temporarily_busy(path: Path) -> bytes:
                if path == target:
                    reads.append(path)
                    if len(reads) <= 3:
                        raise PermissionError("simulated Windows sharing denial")
                return read_bytes(path)

            with patch.object(Path, "read_bytes", temporarily_busy), patch(
                "forma_core.persistence.project_artifacts.time.sleep"
            ) as sleep:
                result = storage.get("project", digest, "application/octet-stream")
            self.assertEqual(content, result.content)
            self.assertEqual(4, len(reads))
            self.assertEqual(3, sleep.call_count)
            self.assertEqual(content, target.read_bytes())

    def test_local_read_permanent_denial_is_unavailable_not_corrupt_or_missing(self) -> None:
        content = b"stored bytes remain intact"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            storage.put("project", digest, content, "application/octet-stream")
            target = Path(directory) / project_artifact_storage_key("project", digest)
            with patch.object(Path, "read_bytes", side_effect=PermissionError("access denied")) as read, patch(
                "forma_core.persistence.project_artifacts.time.sleep"
            ) as sleep:
                with self.assertRaisesRegex(ProjectArtifactStorageError, "busy or access is denied") as failure:
                    storage.get("project", digest, "application/octet-stream")
            self.assertIsInstance(failure.exception.__cause__, PermissionError)
            self.assertEqual(8, read.call_count)
            self.assertEqual(7, sleep.call_count)
            self.assertEqual(content, target.read_bytes())
            self.assertEqual([target], [p for p in Path(directory).rglob("*") if p.is_file()])

    def test_unsupported_hardlinks_fall_back_atomically_and_allow_corrupt_repair(self) -> None:
        content = b"content on a filesystem without hardlinks"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            with patch("forma_core.persistence.project_artifacts.os.link",
                       side_effect=OSError(getattr(errno, "EOPNOTSUPP", errno.ENOSYS), "hardlinks unsupported")):
                storage.put("project", digest, content, "application/octet-stream")
                target = Path(directory) / project_artifact_storage_key("project", digest)
                target.write_bytes(b"corrupt")
                with self.assertRaisesRegex(ProjectArtifactStorageError, "integrity check"):
                    storage.get("project", digest, "application/octet-stream")
                storage.put("project", digest, content, "application/octet-stream")
            self.assertEqual(content, storage.get("project", digest, "application/octet-stream").content)
            self.assertEqual([target], [p for p in Path(directory).rglob("*") if p.is_file()])

    def test_hardlink_access_denial_never_falls_back_to_replacement(self) -> None:
        content = b"unpublished bytes"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            with patch("forma_core.persistence.project_artifacts.os.link", side_effect=PermissionError("access denied")) as link, patch.object(
                Path, "replace", side_effect=AssertionError("Access denial is not unsupported hardlinks")
            ), patch("forma_core.persistence.project_artifacts.time.sleep") as sleep:
                with self.assertRaisesRegex(ProjectArtifactStorageError, "access is denied"):
                    storage.put("project", digest, content, "application/octet-stream")
            self.assertEqual(8, link.call_count)
            self.assertEqual(7, sleep.call_count)
            self.assertEqual([], [p for p in Path(directory).rglob("*") if p.is_file()])

    def test_published_hardlink_cleanup_retries_reader_sharing_denial(self) -> None:
        content = b"new immutable object"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            storage = self.local_storage(directory)
            target = Path(directory) / project_artifact_storage_key("project", digest)
            unlink = Path.unlink
            cleanups = []

            def reader_holds_file(path: Path, missing_ok: bool = False) -> None:
                if path != target:
                    cleanups.append(path)
                    self.assertEqual(content, target.read_bytes())
                    if len(cleanups) <= 3:
                        raise PermissionError("reader holds published hardlink")
                unlink(path, missing_ok=missing_ok)

            with patch.object(Path, "unlink", reader_holds_file), patch(
                "forma_core.persistence.project_artifacts.time.sleep"
            ) as sleep:
                storage.put("project", digest, content, "application/octet-stream")
            self.assertEqual(4, len(cleanups))
            self.assertEqual(3, sleep.call_count)
            self.assertEqual(content, storage.get("project", digest, "application/octet-stream").content)
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
