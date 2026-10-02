"""Customer worker boundaries; synthetic outputs do not certify native CAD."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from zipfile import ZipFile
from io import BytesIO

from forma_core.cad_migrations.worker import WorkerClient, result_bundle
from forma_core.cad_migrations.evidence import compare_evidence
from tests.integrations.test_cad_native_evidence import sample, evidence


class WorkerTests(unittest.TestCase):
    def test_credentials_are_bound_to_https_origin_and_project(self):
        for url in ("http://example.com/api", "https://user:password@example.com/api", "https://example.com/api?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WorkerClient(url, "test", str(uuid4()))
        client = WorkerClient("http://127.0.0.1:8016/api", "test", str(uuid4()))
        with self.assertRaises(ValueError):
            client.request("https://other.example/model")

    def test_native_bytes_are_returned_only_with_matching_evidence_hash(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            content = b"synthetic native model"
            (root / "part.f3d").write_bytes(content)
            report = evidence(sample())
            report.native_artifacts = {"part.f3d": hashlib.sha256(content).hexdigest()}
            (root / "evidence-test.json").write_text(report.model_dump_json())
            with ZipFile(BytesIO(result_bundle(root))) as archive:
                self.assertEqual(archive.read("part.f3d"), content)
            (root / "part.f3d").write_bytes(b"tampered")
            with self.assertRaises(ValueError): result_bundle(root)
            report.native_artifacts = {"../part.f3d": "0" * 64}
            (root / "evidence-test.json").write_text(report.model_dump_json())
            with self.assertRaises(ValueError): result_bundle(root)

    def test_inspection_shows_actual_metadata_and_geometry_differences(self):
        model = sample()
        report = evidence(model)
        report.metadata["part_number"] = "different"
        comparison = compare_evidence(model, "fusion360", report)
        self.assertEqual(comparison["status"], "needs_repair")
        self.assertEqual(comparison["metadata_differences"][0]["target"], "different")
        self.assertEqual(comparison["source_geometry"], model.source_geometry.model_dump(mode="json"))
        self.assertEqual(set(comparison["parameter_checks"]), set(model.parameters))

    def test_missing_history_measurements_use_only_the_bound_saved_step(self):
        model = sample(); report = evidence(model)
        model.source_geometry = None
        report.rebuild_sha256 = hashlib.sha256(json.dumps(model.normalized(), sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n").hexdigest()
        report.source_geometry = report.geometry
        report.source_step_sha256 = "a" * 64
        with self.assertRaises(ValueError): compare_evidence(model, "fusion360", report)
        with self.assertRaises(ValueError): compare_evidence(model, "fusion360", report, source_step_sha256="b"*64)
        self.assertEqual(compare_evidence(model, "fusion360", report, source_step_sha256="a"*64)["status"], "checks_passed")
