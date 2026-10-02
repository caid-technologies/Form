"""Offline contract tests; CAD applications are not emulated as a validation claim."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from forma_core.cad_export import CadMetadata, build_export, export_capabilities
from forma_core.cad_export.importers import FUSION, ONSHAPE
from forma_core.cli.main import main

# Envelope-only fixture, deliberately not represented as a valid solid.
STEP = b"ISO-10303-21;\nHEADER;ENDSEC;\nDATA;ENDSEC;\nEND-ISO-10303-21;\n"


class ExportTests(unittest.TestCase):
    def test_all_targets_preserve_geometry_and_metadata(self):
        metadata = CadMetadata(title='Bracket "A"', part_number="A-1", revision="B", properties={"finish": "anodized"})
        for target in export_capabilities():
            with self.subTest(target=target["id"]), ZipFile(io.BytesIO(build_export(STEP, target["id"], metadata))) as archive:
                self.assertEqual(archive.read("geometry.step"), STEP)
                self.assertEqual(json.loads(archive.read("metadata.json")), metadata.model_dump())
                manifest = json.loads(archive.read("manifest.json"))
                self.assertFalse(manifest["native_history_preserved"])
                self.assertFalse(manifest["native_application_tested"])
                for name, descriptor in manifest["files"].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), descriptor["sha256"])
                self.assertTrue(all("/" not in name and ".." not in name for name in archive.namelist()))

    def test_packages_are_reproducible(self):
        self.assertEqual(build_export(STEP, "onshape"), build_export(STEP, "onshape"))

    def test_invalid_step_and_target_are_rejected(self):
        for data in (b"", b"solid a mesh", b"ISO-10303-21; truncated"):
            with self.assertRaises(ValueError):
                build_export(data, "solidworks")
        with self.assertRaises(ValueError):
            build_export(STEP, "invented")

    def test_metadata_is_explicit_and_bounded(self):
        for value in ({"unknown": "field"}, {"properties": {"API-Key": "private"}},
                      {"properties": {"access_token": "private"}}, {"title": "x" * 4097}):
            with self.assertRaises(ValueError):
                CadMetadata.model_validate(value)

    def test_metadata_cannot_inject_importer_code(self):
        hostile = CadMetadata(title="\"; __import__('os').system('bad') #")
        with ZipFile(io.BytesIO(build_export(STEP, "fusion360", hostile))) as archive:
            script = archive.read("import-fusion.py")
            self.assertNotIn(b"system('bad')", script)
            compile(script, "import-fusion.py", "exec")

    def test_cli_receipt_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "part.step").write_bytes(STEP)
            args = ["cad-export", "--target", "onshape", "--step", str(root / "part.step"), "--output", str(root / "handoff.zip")]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "packaged")
            self.assertEqual(receipt["sha256"], hashlib.sha256((root / "handoff.zip").read_bytes()).hexdigest())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual((root / "part.step").read_bytes(), STEP)

    def importer(self, root, source):
        with ZipFile(io.BytesIO(build_export(STEP, "onshape"))) as archive:
            archive.extractall(root)
        namespace = {"__file__": str(root / "importer.py"), "__name__": "importer"}
        exec(compile(source, "importer.py", "exec"), namespace)
        return namespace

    def test_importer_detects_tampering_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            namespace = self.importer(root, ONSHAPE)
            (root / "geometry.step").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "integrity"):
                namespace["package_files"]()

    def test_onshape_upload_polls_and_does_not_send_metadata_or_reupload(self):
        with tempfile.TemporaryDirectory() as folder:
            namespace = self.importer(Path(folder), ONSHAPE)
            calls = []
            def request(path, token, data=None, content_type=None):
                calls.append((path, token, data))
                if data:
                    self.assertIn(STEP, data)
                    self.assertNotIn(b"part_number", data)
                    return {"translationId": "a" * 24}
                return {"requestState": "DONE", "resultElementIds": ["b" * 24]}
            namespace["request"] = request
            with patch.dict("os.environ", {"ONSHAPE_ACCESS_TOKEN": "test-only"}), patch.object(sys, "argv", ["importer", "--document", "c" * 24, "--workspace", "d" * 24]), contextlib.redirect_stdout(io.StringIO()) as output:
                namespace["main"]()
            self.assertEqual(len(calls), 2)
            self.assertNotIn("test-only", output.getvalue())
            self.assertIn('"status": "imported"', output.getvalue())

    def test_onshape_failed_or_pending_translation_is_not_success(self):
        for state in ("FAILED", "ACTIVE"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                namespace = self.importer(Path(folder), ONSHAPE)
                namespace["request"] = lambda path, token, data=None, content_type=None: {"translationId": "a" * 24} if data else {"requestState": state}
                namespace["time"] = types.SimpleNamespace(sleep=lambda _: None)
                with patch.dict("os.environ", {"ONSHAPE_ACCESS_TOKEN": "test-only"}), patch.object(sys, "argv", ["importer", "--document", "c" * 24, "--workspace", "d" * 24]), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    namespace["main"]()

    def test_fusion_uses_new_document_and_stores_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            namespace = self.importer(Path(folder), FUSION)
            calls = []
            component = types.SimpleNamespace(attributes=types.SimpleNamespace(add=lambda *a: calls.append(a)))
            design = types.SimpleNamespace(rootComponent=component)
            document = types.SimpleNamespace(products=types.SimpleNamespace(itemByProductType=lambda _: design))
            application = types.SimpleNamespace(importManager=types.SimpleNamespace(createSTEPImportOptions=lambda p: p, importToNewDocument=lambda p: document), userInterface=types.SimpleNamespace(messageBox=lambda message: None))
            adsk = types.ModuleType("adsk")
            adsk.core = types.ModuleType("adsk.core")
            adsk.fusion = types.ModuleType("adsk.fusion")
            adsk.core.Application = types.SimpleNamespace(get=lambda: application)
            adsk.fusion.Design = types.SimpleNamespace(cast=lambda d: d)
            with patch.dict(sys.modules, {"adsk": adsk, "adsk.core": adsk.core, "adsk.fusion": adsk.fusion}):
                namespace["run"](None)
            self.assertEqual(component.name, "Forma model")
            self.assertEqual(calls[0][:2], ("Forma", "metadata"))


if __name__ == "__main__":
    unittest.main()
