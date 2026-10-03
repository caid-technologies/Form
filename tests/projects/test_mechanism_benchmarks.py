from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.workspaces.projects.mechanism_benchmarks import (
    MonolithicFlexureBenchmark,
    PrintInPlaceHingeBenchmark,
    mechanism_cad_source,
)
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    MechanicalPlacement,
    MechanicalVector3,
)


def inspect_step(path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("inspect_native_step.py")), path],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def print_in_place_project() -> HardwareIntermediateRepresentation:
    components = [
        ComponentInstance(
            ref_des="PIP_FIXED",
            part_number="PIP-FIXED",
            name="Print-in-place fixed hinge leaf",
            category="Mechanical",
            rationale="Stationary body of the captive hinge benchmark.",
        ),
        ComponentInstance(
            ref_des="PIP_MOVING",
            part_number="PIP-MOVING",
            name="Print-in-place moving hinge leaf",
            category="Mechanical",
            rationale="Moving body carrying the captive pin.",
        ),
    ]
    return HardwareIntermediateRepresentation(
        components=components,
        mechanical=MechanicalNotes(
            physical_form="Print-in-place captive hinge calibration coupon",
            enclosure_type="3D Printed",
            mounting_guidance="Print both bodies in one job; do not assemble a separate pin.",
            manufacturability_rating="Moderate",
            fabrication_details=[
                "Clearance is a calibration parameter, not a universal printer value.",
                "Allow the print to cool before gradually freeing the hinge.",
            ],
            render_dimensions=MechanicalVector3(x_mm=70, y_mm=58, z_mm=18),
            component_placements=[
                MechanicalPlacement(
                    ref_des="PIP_FIXED",
                    label="Fixed hinge leaf",
                    category="Mechanical",
                    layer="mechanism",
                    position=MechanicalVector3(x_mm=0, y_mm=-12, z_mm=0),
                    size=MechanicalVector3(x_mm=56, y_mm=24, z_mm=10),
                ),
                MechanicalPlacement(
                    ref_des="PIP_MOVING",
                    label="Moving hinge leaf",
                    category="Mechanical",
                    layer="mechanism",
                    position=MechanicalVector3(x_mm=0, y_mm=12, z_mm=0),
                    size=MechanicalVector3(x_mm=56, y_mm=24, z_mm=10),
                ),
            ],
            mechanism_benchmark=PrintInPlaceHingeBenchmark(),
        ),
    )


def flexure_project() -> HardwareIntermediateRepresentation:
    spec = MonolithicFlexureBenchmark()
    overlap = max(0.5, min(1.5, spec.flexure_length_mm * 0.12))
    center = spec.flexure_length_mm / 2.0 + spec.rigid_body_length_mm / 2.0 - overlap
    components = [
        ComponentInstance(
            ref_des="FLEX_FIXED",
            part_number="FLEX-FIXED",
            name="Flexure fixed rigid region",
            category="Mechanical",
            rationale="Fixed rigid region of the monolithic compliant benchmark.",
        ),
        ComponentInstance(
            ref_des="FLEX_MOVING",
            part_number="FLEX-MOVING",
            name="Flexure moving rigid region",
            category="Mechanical",
            rationale="Moving rigid region used by the approximate deformation preview.",
        ),
    ]
    return HardwareIntermediateRepresentation(
        components=components,
        mechanical=MechanicalNotes(
            physical_form="Monolithic flexure hinge calibration coupon",
            enclosure_type="3D Printed",
            mounting_guidance="Print as one continuous solid with the thin web aligned to the intended bend direction.",
            manufacturability_rating="Challenging",
            fabrication_details=[
                "The flexure CAD remains one connected body.",
                "Preview motion is approximate and is not stress, strain, or fatigue validation.",
            ],
            render_dimensions=MechanicalVector3(x_mm=64, y_mm=34, z_mm=18),
            component_placements=[
                MechanicalPlacement(
                    ref_des="FLEX_FIXED",
                    label="Fixed rigid region",
                    category="Mechanical",
                    layer="mechanism",
                    position=MechanicalVector3(x_mm=-center, y_mm=0, z_mm=0),
                    size=MechanicalVector3(
                        x_mm=spec.rigid_body_length_mm,
                        y_mm=spec.rigid_body_width_mm,
                        z_mm=spec.rigid_body_thickness_mm,
                    ),
                ),
                MechanicalPlacement(
                    ref_des="FLEX_MOVING",
                    label="Moving rigid region",
                    category="Mechanical",
                    layer="mechanism",
                    position=MechanicalVector3(x_mm=center, y_mm=0, z_mm=0),
                    size=MechanicalVector3(
                        x_mm=spec.rigid_body_length_mm,
                        y_mm=spec.rigid_body_width_mm,
                        z_mm=spec.rigid_body_thickness_mm,
                    ),
                ),
            ],
            mechanism_benchmark=spec,
        ),
    )


class MechanismBenchmarkTests(unittest.TestCase):
    def test_print_in_place_clearance_must_leave_barrel_wall(self) -> None:
        with self.assertRaises(ValidationError):
            PrintInPlaceHingeBenchmark(
                barrel_diameter_mm=8,
                pin_diameter_mm=6,
                radial_clearance_mm=0.8,
                wall_thickness_mm=1.0,
            )

    def test_flexure_requires_thinner_web_than_rigid_regions(self) -> None:
        with self.assertRaises(ValidationError):
            MonolithicFlexureBenchmark(
                flexure_thickness_mm=4,
                rigid_body_thickness_mm=4,
            )

    def test_generated_benchmark_sources_are_valid_python(self) -> None:
        for benchmark in (PrintInPlaceHingeBenchmark(), MonolithicFlexureBenchmark()):
            with self.subTest(kind=benchmark.kind):
                source = mechanism_cad_source(benchmark)
                compile(source, "mechanism.py", "exec")

        pip_source = mechanism_cad_source(PrintInPlaceHingeBenchmark())
        self.assertIn("create_kinematic_joint", pip_source)
        self.assertIn("FORMA_EXPORT_SHAPE_IDS = [fixed.shape_id, moving.shape_id]", pip_source)
        flexure_source = mechanism_cad_source(MonolithicFlexureBenchmark())
        self.assertIn("FORMA_COMPLIANT_PREVIEW", flexure_source)
        self.assertIn("structural_validation", flexure_source)
        self.assertIn("FORMA_EXPORT_SHAPE_IDS = [model.shape_id]", flexure_source)


    def test_example_projects_validate_as_hardware_ir(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        for filename in ("print_in_place_hinge.json", "monolithic_flexure_hinge.json"):
            with self.subTest(filename=filename):
                project = HardwareIntermediateRepresentation.model_validate_json(
                    (repo_root / "examples" / filename).read_text(encoding="utf-8")
                )
                self.assertIsNotNone(project.mechanical)
                self.assertIsNotNone(project.mechanical.mechanism_benchmark)

    @unittest.skipUnless(
        os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") == "true",
        "Native OCCT integration gate",
    )
    def test_print_in_place_hinge_exports_two_bodies_and_opencad_motion(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "FORMA_CAD_WORKSPACE": directory,
                "FORMA_CLI_ARTIFACT_STORAGE_BACKEND": "local",
                "FORMA_CLI_ARTIFACT_STORAGE_DIR": directory + "/stored",
            },
        ):
            project = print_in_place_project()
            ensure_native_cad_model(
                project,
                project_id="52500000-0000-4525-8525-000000000001",
                required=True,
                authoring_agent="Issue 525 print-in-place benchmark",
            )
            step = inspect_step(project.cad_model["path"])

        self.assertTrue(step["valid"])
        self.assertEqual(2, step["solids"])
        self.assertEqual("print-in-place mechanism", project.cad_model["mechanism"]["family"])
        self.assertEqual(2, project.cad_model["mechanism"]["body_count"])
        self.assertEqual({"stl", "3mf", "obj"}, set(project.cad_model["exports"]))
        tracks = project.cad_model["kinematics"]["tracks"]
        self.assertEqual(1, len(tracks))
        self.assertEqual("PIP_MOVING", tracks[0]["target_ref"])
        self.assertEqual("PIP_FIXED", tracks[0]["parent_ref"])
        self.assertEqual("revolute", tracks[0]["joint"]["type"])

    @unittest.skipUnless(
        os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") == "true",
        "Native OCCT integration gate",
    )
    def test_monolithic_flexure_exports_one_body_and_approximate_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "FORMA_CAD_WORKSPACE": directory,
                "FORMA_CLI_ARTIFACT_STORAGE_BACKEND": "local",
                "FORMA_CLI_ARTIFACT_STORAGE_DIR": directory + "/stored",
            },
        ):
            project = flexure_project()
            ensure_native_cad_model(
                project,
                project_id="52500000-0000-4525-8525-000000000002",
                required=True,
                authoring_agent="Issue 525 flexure benchmark",
            )
            step = inspect_step(project.cad_model["path"])

        self.assertTrue(step["valid"])
        self.assertEqual(1, step["solids"])
        self.assertEqual("monolithic compliant mechanism", project.cad_model["mechanism"]["family"])
        self.assertEqual(1, project.cad_model["mechanism"]["body_count"])
        self.assertIsNone(project.cad_model["kinematics"])
        preview = project.cad_model["compliant_preview"]
        self.assertEqual("forma-compliant-approximation", preview["source"])
        self.assertFalse(preview["structural_validation"])
        self.assertEqual("compliant", preview["tracks"][0]["type"])
        self.assertEqual("FLEX_MOVING", preview["tracks"][0]["target_ref"])
        self.assertEqual(41, len(preview["tracks"][0]["samples"]))
        self.assertEqual({"stl", "3mf", "obj"}, set(project.cad_model["exports"]))


if __name__ == "__main__":
    unittest.main()
