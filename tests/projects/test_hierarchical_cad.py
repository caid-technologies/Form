from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.workspaces.projects.design_lifecycle import (
    RepresentationKind,
    VisualApprovalStatus,
    bootstrap_design_lifecycle,
    load_design_lifecycle,
    register_system_render,
)
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    MechanicalPlacement,
    MechanicalVector3,
    SystemArchitecture,
    SystemNode,
)


def gated_project(*, policy: str = "auto_approve_visual", mode: str = "progressive") -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation(
        system_architecture=SystemArchitecture(
            summary="Small controller",
            root=SystemNode(
                system_id="product",
                name="Controller",
                domain="product",
                purpose="House and operate a controller board.",
                children=[
                    SystemNode(
                        system_id="mechanical.enclosure",
                        name="Enclosure",
                        domain="mechanical",
                        purpose="Protect and mount the electronics.",
                    )
                ],
            ),
        ),
        components=[
            ComponentInstance(
                ref_des="U1",
                part_number="MCU-TEST",
                name="Controller board",
                category="Microcontroller",
                rationale="Runs the project.",
            )
        ],
        mechanical=MechanicalNotes(
            enclosure_type="Open frame",
            mounting_guidance="Mount the controller on the base.",
            manufacturability_rating="Easy",
            render_dimensions=MechanicalVector3(x_mm=80, y_mm=60, z_mm=30),
            component_placements=[
                MechanicalPlacement(
                    ref_des="U1",
                    label="Controller board",
                    category="Microcontroller",
                    position=MechanicalVector3(x_mm=0, y_mm=0, z_mm=2),
                    size=MechanicalVector3(x_mm=40, y_mm=25, z_mm=8),
                )
            ],
        ),
        assembly_metadata={
            "generation_mode": mode,
            "design_brief_id": "22222222-2222-4222-8222-222222222222",
            "visual_approval_policy": policy,
        },
    )


def fake_run(order: list[str]):
    def run(_adapter: Path, model: Path, output: Path, tree: Path | None = None) -> dict:
        order.append(model.name)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix == ".step":
            output.write_bytes(b"ISO-10303-21;HEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;")
        else:
            output.write_text(
                "solid model\n"
                "facet normal 0 0 1\n"
                "outer loop\n"
                "vertex 0 0 0\n"
                "vertex 1 0 0\n"
                "vertex 0 1 0\n"
                "endloop\nendfacet\nendsolid model\n",
                encoding="ascii",
            )
        if tree is not None:
            tree.write_text("{}", encoding="utf-8")
        return {"valid": True, "opencad_version": "test"}

    return run


class HierarchicalCadTests(unittest.TestCase):
    def test_generation_worker_project_defers_cad_before_system_render(self) -> None:
        ir = gated_project()

        result = ensure_native_cad_model(ir, project_id=None, required=False)

        self.assertFalse(result)
        self.assertEqual(
            "waiting_for_visual_approval",
            ir.assembly_metadata["cad_generation"]["status"],
        )
        self.assertIsNone(ir.cad_model)
        self.assertEqual(
            VisualApprovalStatus.NOT_REQUESTED,
            load_design_lifecycle(ir).visual_gate.status,
        )

    def test_component_cad_is_generated_before_assembly_after_auto_approval(self) -> None:
        ir = gated_project()
        bootstrap_design_lifecycle(ir)
        register_system_render(
            ir,
            source_fingerprint="sha256:visual",
            uri="https://images.example.test/system.png",
        )
        order: list[str] = []

        with tempfile.TemporaryDirectory() as workspace, patch.dict(
            "os.environ",
            {"FORMA_CAD_WORKSPACE": workspace},
            clear=False,
        ), patch(
            "forma_core.workspaces.projects.cad_generation._adapter_path",
            return_value=Path(__file__),
        ), patch(
            "forma_core.workspaces.projects.cad_generation._run_adapter",
            side_effect=fake_run(order),
        ):
            self.assertTrue(ensure_native_cad_model(ir, project_id=None, required=False))

        self.assertEqual("U1.py", order[0])
        self.assertEqual("assembly.py", order[1])
        self.assertEqual("assembly.py", order[2])
        self.assertEqual("component-cad-then-assembly", ir.cad_model["authoring_mode"])
        self.assertEqual(1, len(ir.cad_model["component_artifact_ids"]))
        self.assertTrue(ir.cad_model["assembly_artifact_id"])

        lifecycle = load_design_lifecycle(ir)
        kinds = [item.kind for item in lifecycle.representations]
        self.assertIn(RepresentationKind.COMPONENT_CAD, kinds)
        self.assertIn(RepresentationKind.ASSEMBLY_CAD, kinds)

    def test_require_approval_remains_deferred_after_visual_exists(self) -> None:
        ir = gated_project(policy="require_approval")
        bootstrap_design_lifecycle(ir, policy="require_approval")
        register_system_render(
            ir,
            source_fingerprint="sha256:visual",
            uri="https://images.example.test/system.png",
        )

        result = ensure_native_cad_model(ir, project_id=None, required=False)

        self.assertFalse(result)
        self.assertEqual(
            VisualApprovalStatus.AWAITING_APPROVAL,
            load_design_lifecycle(ir).visual_gate.status,
        )
        self.assertEqual(
            "waiting_for_visual_approval",
            ir.assembly_metadata["cad_generation"]["status"],
        )

    def test_regular_mode_ignores_progressive_metadata_and_runs_one_shot_cad(self) -> None:
        ir = gated_project(policy="require_approval", mode="regular")
        order: list[str] = []

        with tempfile.TemporaryDirectory() as workspace, patch.dict(
            "os.environ",
            {"FORMA_CAD_WORKSPACE": workspace},
            clear=False,
        ), patch(
            "forma_core.workspaces.projects.cad_generation._adapter_path",
            return_value=Path(__file__),
        ), patch(
            "forma_core.workspaces.projects.cad_generation._run_adapter",
            side_effect=fake_run(order),
        ):
            self.assertTrue(ensure_native_cad_model(ir, project_id=None, required=False))

        self.assertEqual(["assembly.py", "assembly.py"], order)
        self.assertEqual("hardware-ir-to-opencad", ir.cad_model["authoring_mode"])
        self.assertNotIn("component_artifact_ids", ir.cad_model)
        self.assertNotIn("assembly_artifact_id", ir.cad_model)
        self.assertEqual("succeeded", ir.assembly_metadata["cad_generation"]["status"])


if __name__ == "__main__":
    unittest.main()
