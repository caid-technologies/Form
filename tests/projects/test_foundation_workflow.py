"""Public-process acceptance of real CAD compilation and portable inspection."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FoundationWorkflowTests(unittest.TestCase):
    def call(self, *arguments, expected=0):
        result = subprocess.run([sys.executable, "-m", "forma_core", "project", *map(str, arguments)],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(expected, result.returncode, result.stderr + result.stdout)
        return json.loads(result.stdout)

    @unittest.skipUnless(os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") == "true", "native CAD gate")
    def test_create_compile_review_export_reopen_edit_and_recompile(self):
        import cadquery as cq

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.loads((ROOT / "examples/foundation/plate.json").read_text())
            attachment = b"Preserved component source and provenance, independent of assembly geometry."
            (root / "components").mkdir()
            (root / "components/reference.txt").write_bytes(attachment)
            source["artifacts"] = [{"path": "components/reference.txt", "media_type": "text/plain",
                                    "sha256": hashlib.sha256(attachment).hexdigest(), "size_bytes": len(attachment)}]
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source))
            compiled = root / "compiled"
            self.call("compile", source_path, "--directory", compiled)
            self.assertEqual(attachment, (compiled / "components/reference.txt").read_bytes())
            manifest_path = compiled / "forma-project.json"
            manifest = json.loads(manifest_path.read_text())
            self.assertTrue({"assembly.py", "assembly.tree.json", "assembly.step", "assembly.stl"}
                            <= {a["path"] for a in manifest["artifacts"]})
            first = cq.importers.importStep(str(compiled / "assembly.step")).val()
            self.assertTrue(first.isValid())
            self.assertAlmostEqual(40, first.BoundingBox().xlen, places=5)
            self.assertAlmostEqual(40 * 20 * 4 - 3.141592653589793 * 3 ** 2 * 4, first.Volume(), places=4)
            initial = self.call("inspect", manifest_path)
            self.assertEqual("pending", initial["human_review"]["status"])
            reviewed = self.call("review", manifest_path, "--decision", "approved", "--reviewer", "Acceptance reviewer",
                                 "--note", "Reviewed exported dimensions; physical performance unverified.")
            self.assertEqual("approved", reviewed["human_review"]["status"])
            archive = root / "plate.zip"
            self.call("export", manifest_path, "--bundle", archive)
            reopened = root / "reopened"
            report = self.call("reopen", archive, "--directory", reopened)
            self.assertEqual(reviewed["manifest_sha256"], report["manifest_sha256"])
            self.assertEqual("approved", report["human_review"]["status"])
            changed_path = reopened / "forma-project.json"
            changed = json.loads(changed_path.read_text())
            changed["project_ir"]["mechanical"]["cad_operations"][0]["size"]["x_mm"] = 60
            changed_path.write_text(json.dumps(changed))
            stale = self.call("inspect", changed_path, expected=1)
            self.assertEqual("stale", stale["human_review"]["status"])
            revised = root / "revised"
            self.call("compile", changed_path, "--directory", revised)
            self.assertEqual(attachment, (revised / "components/reference.txt").read_bytes())
            updated = cq.importers.importStep(str(revised / "assembly.step")).val()
            self.assertAlmostEqual(60, updated.BoundingBox().xlen, places=5)
            self.assertAlmostEqual(40, first.BoundingBox().xlen, places=5)
            # A damaged artifact fails in a new process; validation cannot be cached green.
            (reopened / "assembly.step").write_bytes(b"damaged")
            corrupt = self.call("inspect", changed_path, expected=1)
            self.assertFalse(corrupt["is_valid"])
            # Recompilation can regenerate stale CAD, but must not silently
            # discard or replace a corrupt reusable component/source attachment.
            (reopened / "components/reference.txt").write_bytes(b"damaged")
            failed = subprocess.run([sys.executable, "-m", "forma_core", "project", "compile", str(changed_path),
                                     "--directory", str(root / "failed")], cwd=ROOT,
                                    capture_output=True, text=True, timeout=180)
            self.assertNotEqual(0, failed.returncode)
            self.assertIn("differs from its declared bytes", failed.stderr)
            self.assertFalse((root / "failed").exists())

    def test_manifest_schema_is_discoverable_without_a_model_provider(self):
        schema = self.call("schema")
        self.assertIn("project_ir", schema["properties"])

    def test_compile_preserves_attachments_named_by_old_cad_metadata(self):
        from forma_core.cli.project import _preserve_artifacts
        from forma_core.workspaces.projects.manifest import ProjectArtifactReference, ProjectManifest

        content = b"immutable reusable source"
        reference = ProjectArtifactReference(path="components/reference.txt", media_type="text/plain",
                    sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))
        manifest = ProjectManifest(project_id="project", artifacts=[reference],
                    project_ir={"cad_model": {"model_source_path": reference.path}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source/components").mkdir(parents=True)
            (root / "stage").mkdir()
            (root / "source" / reference.path).write_bytes(content)
            preserved = _preserve_artifacts(manifest, root / "source", root / "stage", [])
            self.assertEqual([reference], preserved)
            self.assertEqual(content, (root / "stage" / reference.path).read_bytes())

    def test_compile_refuses_to_replace_an_unrelated_attachment(self):
        from forma_core.cli.project import _preserve_artifacts
        from forma_core.workspaces.projects.manifest import ProjectArtifactReference, ProjectManifest

        reference = ProjectArtifactReference(path="assembly.py", media_type="text/x-python", sha256="a" * 64)
        manifest = ProjectManifest(project_id="project", artifacts=[reference])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "conflicts with generated output"):
                _preserve_artifacts(manifest, root, root, [reference])

    def test_compile_rejects_future_ir_before_legacy_normalization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/"source.json"
            source.write_text(json.dumps({"project_id":"fixture","project_ir":{"hardware_ir_version":"999.0"}}))
            result=subprocess.run([sys.executable,"-m","forma_core","project","compile",str(source),
                                   "--directory",str(root/"compiled")],cwd=ROOT,capture_output=True,text=True,timeout=30)
            self.assertNotEqual(0,result.returncode)
            self.assertIn("not supported",result.stderr)
            self.assertFalse((root/"compiled").exists())

    def test_report_output_cannot_replace_source_or_export_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            path=root/"source.json"
            original=json.dumps({"project_id":"fixture","project_ir":{},"artifacts":[]}).encode()
            path.write_bytes(original)
            commands=[("inspect",str(path),"--output",str(path))]
            bundle=root/"project.zip"
            bundle.write_bytes(b"previous export")
            commands.append(("export",str(path),"--bundle",str(bundle),"--output",str(bundle)))
            commands.append(("export",str(path),"--bundle",str(path)))
            for arguments in commands:
                with self.subTest(action=arguments[0]):
                    result=subprocess.run([sys.executable,"-m","forma_core","project",*arguments],cwd=ROOT,
                                          capture_output=True,text=True,timeout=30)
                    self.assertNotEqual(0,result.returncode)
                    self.assertIn("separate path",result.stderr)
                    self.assertEqual(original,path.read_bytes())
                    self.assertEqual(b"previous export",bundle.read_bytes())

    def test_report_output_cannot_replace_new_compiled_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            target=root/"compiled"
            result=subprocess.run([sys.executable,"-m","forma_core","project","compile",
                                   str(ROOT/"examples/foundation/plate.json"),"--directory",str(target),
                                   "--output",str(target/"forma-project.json")],cwd=ROOT,
                                   capture_output=True,text=True,timeout=30)
            self.assertNotEqual(0,result.returncode)
            self.assertIn("separate path",result.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
