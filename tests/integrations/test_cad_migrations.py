"""Offline reconstruction contracts; generated native programs still need vendor validation."""
import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from forma_core.cad_migrations import MigrationModel, plan_migration
from forma_core.cad_migrations.cli import build_migration
from forma_core.cad_migrations.planner import ROUTES
from forma_core.cli.main import main

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/cad-migrations/bracket.solidworks.json"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads(EXAMPLE.read_text())

    def model(self):
        return MigrationModel.model_validate(self.value)

    def test_four_routes_emit_history_metadata_and_target_programs(self):
        for source, target in ROUTES:
            with self.subTest(source=source, target=target):
                self.value["source"]["system"] = source
                with ZipFile(io.BytesIO(build_migration(self.model(), target))) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertFalse(manifest["native_execution_verified"])
                    for path, descriptor in manifest["files"].items():
                        self.assertEqual(hashlib.sha256(archive.read(path)).hexdigest(), descriptor["sha256"])
                    report = json.loads(archive.read("plan.json"))
                    self.assertEqual(report["status"], "ready_for_rebuild")
                    self.assertEqual([item["source_id"] for item in report["features"]], ["base", "hole"])
                    self.assertEqual(json.loads(archive.read("source-history.json"))["metadata"], self.value["metadata"])
                    if target != "onshape":
                        compile(archive.read("rebuild.py"), "rebuild.py", "exec")
                    self.assertTrue(all(".." not in name and "/" not in name for name in archive.namelist()))

    def test_packages_are_reproducible(self):
        self.assertEqual(build_migration(self.model(), "nx"), build_migration(self.model(), "nx"))

    def test_rejects_unsupported_route(self):
        report = plan_migration(self.model(), "fusion360")
        self.assertIn("unsupported_route", [item["code"] for item in report["blockers"]])
        with self.assertRaises(ValueError):
            build_migration(self.model(), "fusion360")

    def test_unsupported_feature_is_never_silently_dropped(self):
        self.value["features"][1].update(kind="unsupported", source_type="Fillet")
        report = plan_migration(self.model(), "nx")
        self.assertEqual(report["features"][1]["status"], "blocked")
        with self.assertRaisesRegex(ValueError, "unsupported_feature"):
            build_migration(self.model(), "nx", approve_inferred=True)

    def test_incomplete_inventory_blocks(self):
        self.value["inventory_complete"] = False
        with self.assertRaisesRegex(ValueError, "incomplete_source_inventory"):
            build_migration(self.model(), "nx")

    def test_inferred_feature_requires_review_and_low_confidence_stays_blocked(self):
        self.value["features"][1]["provenance"]["method"] = "ai_inferred"
        with self.assertRaisesRegex(ValueError, "inferred_feature_requires_review"):
            build_migration(self.model(), "nx")
        self.assertTrue(build_migration(self.model(), "nx", approve_inferred=True))
        self.value["features"][1]["provenance"]["confidence"] = 0.6
        with self.assertRaisesRegex(ValueError, "insufficient_evidence"):
            build_migration(self.model(), "nx", approve_inferred=True)

    def test_suppression_and_non_linear_history_block(self):
        cases = ({"suppressed": True}, {"operation": "new"}, {"depends_on": []})
        original = deepcopy(self.value)
        for change in cases:
            self.value = deepcopy(original)
            self.value["features"][1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_migration(self.model(), "nx")

    def test_missing_forward_cycle_and_duplicate_ids_rejected(self):
        for dependencies in (["missing"], ["hole"], ["base", "base"]):
            self.value["features"][0]["depends_on"] = dependencies
            with self.assertRaises(ValueError):
                self.model()
        self.value["features"][0]["depends_on"] = []
        self.value["features"][1]["id"] = "base"
        with self.assertRaises(ValueError):
            self.model()

    def test_inch_dimensions_are_normalized_without_losing_shared_parameter_identity(self):
        self.value["source"]["units"] = "in"
        model = self.model()
        normalized = model.normalized()
        self.assertEqual(normalized["parameters"]["thickness"], 101.6)
        self.assertEqual(normalized["features"][1]["origin"], [254.0, 254.0, 0.0])
        self.assertEqual(normalized["features"][0]["depth"], "thickness")
        self.assertEqual(model.source.units, "in")

    def test_ambiguous_invalid_or_executable_dimensions_rejected(self):
        original = deepcopy(self.value)
        for value in ("missing", "width * 2", "__import__('os')", -1, 0, True, float("nan"), float("inf")):
            self.value = deepcopy(original)
            self.value["features"][0]["depth"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.model()
        self.value = deepcopy(original)
        self.value["features"][0]["radius"] = 4
        with self.assertRaises(ValueError):
            self.model()

    def test_unknown_semantics_rejected(self):
        self.value["features"][0]["draft_angle"] = 5
        with self.assertRaises(ValueError):
            self.model()

    def test_metadata_is_data_not_python(self):
        self.value["metadata"]["description"] = "'); __import__('os').system('bad') #"
        with ZipFile(io.BytesIO(build_migration(self.model(), "nx"))) as archive:
            self.assertNotIn(b"system('bad')", archive.read("rebuild.py"))
            self.assertEqual(json.loads(archive.read("rebuild.json"))["metadata"]["description"], self.value["metadata"]["description"])

    def test_rebuild_integrity_is_checked_before_vendor_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with ZipFile(io.BytesIO(build_migration(self.model(), "nx"))) as archive:
                archive.extractall(root)
            namespace = {"__file__": str(root / "rebuild.py"), "__name__": "rebuild_test"}
            exec(compile((root / "rebuild.py").read_text(), "rebuild.py", "exec"), namespace)
            self.assertEqual(namespace["load_history"]()[1]["parameters"]["width"], 40.0)
            (root / "rebuild.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "changed"):
                namespace["load_history"]()

    def test_onshape_named_parameters_and_scoped_boolean(self):
        with ZipFile(io.BytesIO(build_migration(self.model(), "onshape"))) as archive:
            script = archive.read("rebuild.fs").decode()
        self.assertIn("isLength(definition.p_thickness", script)
        self.assertEqual(script.count('"endDepth" : definition.p_thickness'), 2)
        self.assertIn('"targets" : body', script)
        self.assertNotIn("qEverything", script)

    def test_cli_blocked_build_does_not_write_artifact_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "result.zip"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["cad-migrate", "build", str(EXAMPLE), "--target", "fusion360", "--output", str(output)]), 2)
            self.assertFalse(output.exists())
            args = ["cad-migrate", "build", str(EXAMPLE), "--target", "nx", "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()) as receipt:
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(receipt.getvalue())["status"], "rebuild_package_created")
            original = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual(output.read_bytes(), original)

    def test_cli_schema_and_routes_are_discoverable(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["cad-migrate", "routes"]), 0)
        self.assertEqual(len(json.loads(output.getvalue())), 4)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["cad-migrate", "schema"]), 0)
        self.assertEqual(json.loads(output.getvalue())["additionalProperties"], False)

    def test_fingerprints_change_when_history_changes(self):
        old = plan_migration(self.model(), "nx")["history_sha256"]
        self.value["parameters"]["width"] = 45
        self.assertNotEqual(old, plan_migration(self.model(), "nx")["history_sha256"])


if __name__ == "__main__":
    unittest.main()
