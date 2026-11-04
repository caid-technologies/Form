from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from forma_core.workspaces.projects.inspection import (
    cad_intent_digest, compile_project, inspect_project, manifest_digest, record_project_review,
)
from forma_core.workspaces.projects.manifest import (
    ProjectArtifactReference, ProjectManifest, load_project_manifest, write_project_manifest,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.portable import (
    PortableProjectManifest, export_project_bundle, import_project_bundle, local_artifact_reader,
)


class ProjectInspectionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") == "true", "native CAD gate")
    def test_canonical_revision_keeps_native_source_after_worker_workspace_is_deleted(self):
        from apps.api.project_foundation import revision_project
        from forma_core.config import config
        from forma_core.workers.generation import build_generation_draft
        project_id = "11111111-1111-4111-8111-111111111111"
        project = HardwareIntermediateRepresentation.model_validate({"mechanical": {
            "enclosure_type": "Plate", "mounting_guidance": "None", "manufacturability_rating": "Review required",
            "cad_operations": [{"shape": "box", "size": {"x_mm": 10, "y_mm": 20, "z_mm": 5}}]}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with config.override({"FORMA_CAD_WORKSPACE": str(root / "worker"),
                                  "FORMA_CLI_ARTIFACT_STORAGE_DIR": str(root / "durable"),
                                  "FORMA_CLI_ARTIFACT_STORAGE_BACKEND": "local"}):
                compiled = compile_project(project, project_id=project_id)
                draft = build_generation_draft(SimpleNamespace(project_id=project_id, summary="Plate", assumptions=[]), compiled)
                revision = SimpleNamespace(state=compiled, artifacts=draft.artifacts, revision_id="revision-1")
                self.assertTrue(any(item.artifact_id == "native-cad-model_source" for item in draft.artifacts))
                self.assertTrue(any(item.artifact_id == "native-cad-feature_tree" for item in draft.artifacts))
                shutil.rmtree(root / "worker")
                manifest, reader = revision_project(revision, project_id, "owner")
                report = inspect_project(manifest, read_artifact=reader)
                self.assertTrue(report.is_valid, report.model_dump_json(indent=2))
                archive = export_project_bundle(manifest, root / "project.zip", read_artifact=reader)
                restored = import_project_bundle(archive, root / "restored")
                self.assertTrue(inspect_project(restored, read_artifact=local_artifact_reader(root / "restored")).is_valid)

    def manifest(self, content=b"inspectable design"):
        return ProjectManifest(project_id="project-1", project_ir={"components": [], "nets": []},
            artifacts=[ProjectArtifactReference(path="source.txt", sha256=hashlib.sha256(content).hexdigest(),
                       size_bytes=len(content), media_type="text/plain")])

    def test_missing_and_corrupted_bytes_cannot_be_reported_as_verified(self):
        manifest = self.manifest()
        unknown = inspect_project(manifest)
        self.assertFalse(unknown.is_valid)
        self.assertEqual("unknown", unknown.checks[-1].status)
        corrupted = inspect_project(manifest, read_artifact=lambda _: b"changed")
        self.assertFalse(corrupted.is_valid)
        self.assertEqual("fail", corrupted.checks[-1].status)
        verified = inspect_project(manifest, read_artifact=lambda _: b"inspectable design")
        self.assertTrue(verified.is_valid)
        self.assertEqual("draft", verified.project_readiness)
        self.assertEqual("pending", verified.human_review.status)

    def test_agent_validation_claims_are_recomputed(self):
        manifest = ProjectManifest(project_id="project-1", project_ir={"components": [{
            "ref_des": "U1", "category": "Sensor", "rationale": "Sensing", "pins": []}],
            "nets": [], "is_valid": True, "validation": {"critical": [], "warning": [], "info": []}})
        report = inspect_project(manifest)
        self.assertFalse(report.is_valid)
        self.assertTrue(report.validation["critical"])

    def test_approval_survives_reopen_and_becomes_stale_after_design_edit(self):
        manifest = record_project_review(self.manifest(), decision="approved", reviewer="Engineer", note="Reviewed source.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forma-project.json"
            write_project_manifest(path, manifest)
            restored = load_project_manifest(path)
        report = inspect_project(restored, read_artifact=lambda _: b"inspectable design")
        self.assertEqual("approved", report.human_review.status)
        self.assertEqual("declaration_not_authenticated", report.human_review.authority)
        restored.title = "Changed design"
        self.assertEqual("stale", inspect_project(restored).human_review.status)

    def test_forged_review_fields_are_not_accepted_as_approval(self):
        manifest = self.manifest()
        manifest.engineering_review = {"decision": "approved", "reviewer": "Agent"}
        self.assertEqual("invalid", inspect_project(manifest).human_review.status)

    def test_rejection_remains_separate_from_passing_deterministic_checks(self):
        manifest = record_project_review(self.manifest(), decision="rejected", reviewer="Engineer")
        report = inspect_project(manifest, read_artifact=lambda _: b"inspectable design")
        self.assertTrue(report.is_valid)
        self.assertEqual("rejected", report.human_review.status)

    def test_stale_geometry_intent_is_detected_despite_matching_file_bytes(self):
        project = HardwareIntermediateRepresentation.model_validate({"mechanical": {
            "enclosure_type": "Plate", "mounting_guidance": "None", "manufacturability_rating": "Review required",
            "cad_operations": [{"shape": "box", "size": {"x_mm": 10, "y_mm": 20, "z_mm": 5}}]}})
        project.cad_model = {"intent_sha256": cad_intent_digest(project)}
        manifest = self.manifest()
        manifest.project_ir = project.model_dump(mode="json")
        before = inspect_project(manifest, read_artifact=lambda _: b"inspectable design")
        self.assertFalse(before.is_valid)  # Editable metadata alone cannot establish compiled geometry.
        self.assertEqual("pass", next(check.status for check in before.checks if check.name == "cad_source"))
        self.assertEqual("fail", next(check.status for check in before.checks if check.name == "cad_artifacts"))
        manifest.project_ir["mechanical"]["cad_operations"][0]["size"]["x_mm"] = 12
        after = inspect_project(manifest, read_artifact=lambda _: b"inspectable design")
        self.assertFalse(after.is_valid)
        self.assertEqual("fail", next(check.status for check in after.checks if check.name == "cad_source"))

    def test_compile_copies_input_and_records_only_executed_geometry_binding(self):
        project = HardwareIntermediateRepresentation()
        def native(compiled, **_kwargs):
            compiled.cad_model = {"generated": True}
            return True
        with patch("forma_core.workspaces.projects.cad_generation.ensure_native_cad_model", native):
            result = compile_project(project)
        self.assertIsNone(project.cad_model)
        self.assertEqual(cad_intent_digest(result), result.cad_model["intent_sha256"])

    def test_unsupported_manifest_versions_are_explicit_and_extensions_survive(self):
        for version in (2, True, "1"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "version"):
                ProjectManifest.from_document({"version": version, "project_ir": {}})
        with self.assertRaisesRegex(ValueError, "format"):
            ProjectManifest.from_document({"format": "unknown", "project_ir": {}})
        restored = ProjectManifest.from_document({"project_id": "id", "project_ir": {}, "source_revision": 4})
        self.assertEqual(4, restored.source_revision)

    def test_future_ir_is_rejected_before_legacy_normalization(self):
        manifest = ProjectManifest(project_id="project", project_ir={"hardware_ir_version": "9"})
        self.assertFalse(inspect_project(manifest).is_valid)
        with self.assertRaises(ValueError):
            PortableProjectManifest.model_validate(manifest.model_dump(mode="json"))

    def test_malformed_artifacts_cannot_be_erased_by_import_adapter(self):
        with self.assertRaisesRegex(ValueError, "declarations"):
            ProjectManifest.from_document({"project_id": "id", "project_ir": {}, "artifacts": [123]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invalid.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("forma-project.json", json.dumps({"project_id": "id", "project_ir": {}, "artifacts": [123]}))
                archive.writestr("forma-inspection.json", "{}")
            with self.assertRaises(ValueError):
                import_project_bundle(source, root / "new")
            self.assertFalse((root / "new").exists())

    def test_reserved_report_and_file_directory_collisions_cannot_form_bundles(self):
        from forma_core.workspaces.projects.manifest import validate_artifact_references
        for paths in (("forma-inspection.json",), ("part", "part/source.py"), ("PART", "part/source.py")):
            references = [{"path": path, "sha256": "0" * 64, "media_type": "text/plain"} for path in paths]
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                validate_artifact_references(references)
        with self.assertRaises(ValueError):
            PortableProjectManifest(project_id="project", project_ir={}, artifacts=[
                {"path": "parts/a:b.step", "sha256": "0" * 64, "media_type": "model/step"}])

    def test_concurrent_manifest_writes_never_share_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forma-project.json"
            def write(index):
                manifest = self.manifest()
                manifest.title = f"revision {index}"
                write_project_manifest(path, manifest)
            with ThreadPoolExecutor(max_workers=8) as writers:
                list(writers.map(write, range(32)))
            self.assertTrue(load_project_manifest(path).title.startswith("revision "))
            self.assertEqual([path], list(Path(directory).iterdir()))

    def test_manifest_write_retries_sharing_denial_then_publishes_its_own_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forma-project.json"
            previous = self.manifest()
            previous.title = "previous complete manifest"
            write_project_manifest(path, previous)
            old_bytes = path.read_bytes()
            revised = self.manifest()
            revised.title = "this caller's complete manifest"
            replace = Path.replace
            attempts = []

            def transient_sharing_denial(temporary, destination):
                attempts.append(temporary)
                self.assertEqual(revised.title, json.loads(temporary.read_text())["title"])
                if len(attempts) <= 3:
                    self.assertEqual(old_bytes, destination.read_bytes())
                    raise PermissionError("simulated Windows sharing denial")
                return replace(temporary, destination)

            with patch.object(Path, "replace", transient_sharing_denial), patch(
                "forma_core.workspaces.projects.manifest.time.sleep"
            ) as sleep:
                write_project_manifest(path, revised)
            self.assertEqual(4, len(attempts))
            self.assertEqual(1, len(set(attempts)))
            self.assertEqual(3, sleep.call_count)
            self.assertEqual(revised.title, load_project_manifest(path).title)
            self.assertEqual([path], list(Path(directory).iterdir()))

    def test_manifest_write_permanent_denial_is_bounded_and_preserves_previous_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forma-project.json"
            previous = self.manifest()
            previous.title = "previous complete manifest"
            write_project_manifest(path, previous)
            old_bytes = path.read_bytes()
            revised = self.manifest()
            revised.title = "unpublished revision"
            with patch.object(Path, "replace", side_effect=PermissionError("destination remains busy")) as replace, patch(
                "forma_core.workspaces.projects.manifest.time.sleep"
            ) as sleep:
                with self.assertRaisesRegex(PermissionError, "remains busy"):
                    write_project_manifest(path, revised)
            self.assertEqual(8, replace.call_count)
            self.assertEqual(7, sleep.call_count)
            self.assertEqual(old_bytes, path.read_bytes())
            self.assertEqual([path], list(Path(directory).iterdir()))

    def test_deterministic_bundle_roundtrip_preserves_source_and_review(self):
        content = b"inspectable design"
        manifest = record_project_review(self.manifest(content), decision="approved", reviewer="Engineer")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = export_project_bundle(manifest, root / "one.zip", read_artifact=lambda _: content)
            second = export_project_bundle(manifest, root / "two.zip", read_artifact=lambda _: content)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            restored = import_project_bundle(first, root / "restored")
            self.assertEqual(manifest_digest(manifest), manifest_digest(restored))
            self.assertEqual(content, (root / "restored/source.txt").read_bytes())
            report = inspect_project(restored, read_artifact=local_artifact_reader(root / "restored"))
            self.assertTrue(report.is_valid)
            self.assertEqual("approved", report.human_review.status)

    def test_export_rejects_corruption_without_replacing_existing_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project.zip"
            target.write_bytes(b"previous export")
            with self.assertRaisesRegex(ValueError, "verified"):
                export_project_bundle(self.manifest(), target, read_artifact=lambda _: b"changed")
            self.assertEqual(b"previous export", target.read_bytes())

    def test_import_rejects_undeclared_files_and_traversal_before_writing(self):
        for malicious in ("../escape", "extra.txt", "SOURCE.txt"):
            with self.subTest(path=malicious), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = export_project_bundle(self.manifest(), root / "project.zip", read_artifact=lambda _: b"inspectable design")
                with zipfile.ZipFile(source, "a") as archive:
                    archive.writestr(malicious, b"unexpected")
                with self.assertRaises(ValueError):
                    import_project_bundle(source, root / "new")
                self.assertFalse((root / "new").exists())
                self.assertFalse((root / "escape").exists())

    def test_import_does_not_trust_stored_validation_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = export_project_bundle(self.manifest(), root / "original.zip", read_artifact=lambda _: b"inspectable design")
            source = root / "forged.zip"
            with zipfile.ZipFile(original) as before, zipfile.ZipFile(source, "w") as after:
                for info in before.infolist():
                    after.writestr(info, b'{"is_valid":false}' if info.filename == "forma-inspection.json" else before.read(info))
            import_project_bundle(source, root / "new")
            self.assertTrue(json.loads((root / "new/forma-inspection.json").read_text())["is_valid"])


if __name__ == "__main__":
    unittest.main()
