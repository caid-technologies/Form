import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pydantic import ValidationError
from forma_core.persistence.project_artifacts import ProjectArtifactStorage
from forma_core.workspaces.projects import cad_generation
from forma_core.workspaces.projects.cad_generation import CadGenerationError, ensure_native_cad_model
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.outcomes import evaluate_design_outcome

PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def cube(labels=True, size=20):
    return HardwareIntermediateRepresentation(mechanical={"enclosure_type": "Solid", "mounting_guidance": "None", "manufacturability_rating": "Easy",
        "cad_operations": [{"shape": "box", "size": {"x_mm": size, "y_mm": size, "z_mm": size}, "axis_labels": labels}]})


def inspect_step(path):
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("inspect_native_step.py")), path],
                            check=True, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


class SolidCadTests(unittest.TestCase):
    def test_invalid_geometry_is_rejected_before_execution(self):
        for size in (0, -20, float("nan"), float("inf"), 10001):
            with self.subTest(size=size), self.assertRaises(ValidationError):
                cube(size=size)
        for updates in ({"operation": "cut"}, {"shape": "python", "source": "print('no')"}):
            payload = cube().model_dump()
            payload["mechanical"]["cad_operations"][0].update(updates)
            with self.assertRaises(ValidationError):
                HardwareIntermediateRepresentation.model_validate(payload)

    def test_requested_geometry_without_artifacts_is_partial(self):
        self.assertEqual(evaluate_design_outcome(cube()).project_readiness, "partial")
        self.assertEqual(evaluate_design_outcome(HardwareIntermediateRepresentation()).project_readiness, "draft")

    def test_preview_mesh_serializes_to_obj_and_3mf(self):
        mesh = {
            "vertices": [0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0],
            "faces": [0, 1, 2],
        }
        obj = cad_generation._obj_mesh_bytes(mesh)
        self.assertIn(b"v 10 0 0", obj)
        self.assertIn(b"f 1 2 3", obj)

        three_mf = cad_generation._three_mf_mesh_bytes(mesh)
        with zipfile.ZipFile(io.BytesIO(three_mf)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"},
            )
            model = archive.read("3D/3dmodel.model")
        self.assertIn(b'unit="millimeter"', model)
        self.assertIn(b'<triangle v1="0" v2="1" v3="2"/>', model)

    def test_failed_regeneration_does_not_reuse_stale_preview(self):
        project = cube()
        project.cad_model = {"meshes": [{"vertices": [1, 2, 3]}], "stored_sha256": "old"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FORMA_CAD_WORKSPACE": directory}), patch(
            "forma_core.workspaces.projects.cad_generation._run_adapter", side_effect=CadGenerationError("failed")):
            with self.assertRaises(CadGenerationError):
                ensure_native_cad_model(project, project_id=PROJECT_ID, required=True)
        self.assertIsNone(project.cad_model)

    @unittest.skipUnless(os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") == "true", "Native OCCT integration gate")
    def test_real_step_cube_labels_preview_storage_and_dimension_edit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "FORMA_CAD_WORKSPACE": directory, "FORMA_CLI_ARTIFACT_STORAGE_BACKEND": "local", "FORMA_CLI_ARTIFACT_STORAGE_DIR": directory + "/stored",
        }):
            project = cube()
            ensure_native_cad_model(project, project_id=PROJECT_ID, required=True)
            first = dict(project.cad_model)
            shape = inspect_step(first["path"])
            self.assertEqual(shape["bounds"], [20, 20, 20])
            self.assertTrue(shape["valid"])
            self.assertEqual(shape["solids"], 1)
            self.assertGreater(shape["faces"], 6)
            self.assertAlmostEqual(shape["volume"], 7948.3706, places=3)
            mesh = first["meshes"][0]
            for axis in range(3):
                coords = mesh["vertices"][axis::3]
                self.assertAlmostEqual(max(coords) - min(coords), 20, places=5)
            self.assertGreater(len(mesh["faces"]), 36)
            storage = ProjectArtifactStorage()
            stored = storage.get(PROJECT_ID, first["stored_sha256"], "model/step").content
            self.assertEqual(stored, Path(first["path"]).read_bytes())
            self.assertEqual(hashlib.sha256(stored).hexdigest(), first["stored_sha256"])
            self.assertEqual(set(first["exports"]), {"stl", "3mf", "obj"})
            for descriptor in first["exports"].values():
                portable = storage.get(PROJECT_ID, descriptor["sha256"], descriptor["media_type"]).content
                self.assertEqual(hashlib.sha256(portable).hexdigest(), descriptor["sha256"])
            self.assertEqual(evaluate_design_outcome(project).project_readiness, "complete")
            project.mechanical.cad_operations[0].size.x_mm = 30
            ensure_native_cad_model(project, project_id=PROJECT_ID, required=True)
            self.assertNotEqual(first["path"], project.cad_model["path"])
            self.assertEqual(Path(first["path"]).read_bytes(), stored)
            revised = inspect_step(project.cad_model["path"])
            self.assertAlmostEqual(revised["bounds"][0], 30)
            # STEP storage is independent of the temporary export location.
            Path(project.cad_model["path"]).unlink()
            self.assertTrue(ProjectArtifactStorage().get(PROJECT_ID, project.cad_model["stored_sha256"], "model/step").content)
