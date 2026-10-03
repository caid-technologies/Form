from __future__ import annotations

import unittest

from forma_core.workspaces.projects.design_lifecycle import (
    ChangeKind,
    DesignRepresentation,
    FidelityLevel,
    RepresentationKind,
    RepresentationStatus,
    VisualApprovalPolicy,
    VisualApprovalStatus,
    bootstrap_design_lifecycle,
    cad_may_execute,
    cheapest_representation_for_change,
    invalidate_representations,
    load_design_lifecycle,
    record_visual_decision,
    register_cad_representation,
    register_system_render,
    register_system_visual,
    representation_fingerprint,
    stable_artifact_id,
    stage_cost,
    stage_fidelity,
    system_node_fingerprint,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, SystemArchitecture, SystemNode


def project() -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation(
        system_architecture=SystemArchitecture(
            summary="Small robot",
            root=SystemNode(
                system_id="product",
                name="Robot",
                domain="product",
                purpose="Move around a room.",
                children=[
                    SystemNode(
                        system_id="mechanical.drivetrain",
                        name="Drivetrain",
                        domain="mechanical",
                        purpose="Produce ground motion.",
                        children=[
                            SystemNode(
                                system_id="mechanical.drivetrain.left_wheel",
                                name="Left wheel",
                                domain="mechanical",
                                purpose="Provide left-side traction.",
                            ),
                            SystemNode(
                                system_id="mechanical.drivetrain.right_wheel",
                                name="Right wheel",
                                domain="mechanical",
                                purpose="Provide right-side traction.",
                            ),
                        ],
                    ),
                    SystemNode(
                        system_id="electrical.control",
                        name="Control",
                        domain="electrical",
                        purpose="Coordinate sensors and motors.",
                    ),
                ],
            ),
        )
    )


class DesignLifecycleTests(unittest.TestCase):
    def test_bootstrap_persists_canonical_topology_representation(self) -> None:
        ir = project()
        state = bootstrap_design_lifecycle(ir, policy=VisualApprovalPolicy.REQUIRE_APPROVAL)

        self.assertIsNotNone(state.topology_fingerprint)
        self.assertEqual(VisualApprovalPolicy.REQUIRE_APPROVAL, state.visual_gate.policy)
        topology = state.by_id()[stable_artifact_id("project", RepresentationKind.TOPOLOGY)]
        self.assertEqual(RepresentationStatus.READY, topology.status)
        self.assertEqual(FidelityLevel.SEMANTIC, topology.fidelity)
        self.assertEqual(5, topology.metadata["system_count"])
        self.assertEqual(state, load_design_lifecycle(ir))

    def test_visual_gate_blocks_cad_until_approved(self) -> None:
        ir = project()
        bootstrap_design_lifecycle(ir, policy=VisualApprovalPolicy.REQUIRE_APPROVAL)
        self.assertFalse(cad_may_execute(ir))

        drivetrain = ir.system_architecture.root.children[0]
        subsystem = register_system_visual(
            ir,
            node_id=drivetrain.system_id,
            source_fingerprint=system_node_fingerprint(drivetrain),
            uri="forma://visuals/drivetrain.png",
        )
        render = register_system_render(
            ir,
            source_fingerprint=representation_fingerprint(
                ir.system_architecture.model_dump(mode="json"),
                subsystem.source_fingerprint,
            ),
            uri="forma://visuals/system.png",
            depends_on=[subsystem.artifact_id],
        )

        state = load_design_lifecycle(ir)
        self.assertEqual(VisualApprovalStatus.AWAITING_APPROVAL, state.visual_gate.status)
        self.assertEqual(render.artifact_id, state.visual_gate.visual_artifact_id)
        self.assertFalse(cad_may_execute(ir))

        record_visual_decision(ir, approved=True)
        self.assertTrue(cad_may_execute(ir))
        self.assertEqual(
            VisualApprovalStatus.APPROVED,
            load_design_lifecycle(ir).visual_gate.status,
        )

    def test_stop_before_cad_never_auto_continues(self) -> None:
        ir = project()
        bootstrap_design_lifecycle(ir, policy=VisualApprovalPolicy.STOP_BEFORE_CAD)
        register_system_render(
            ir,
            source_fingerprint="sha256:visual",
            uri="forma://visuals/system.png",
        )
        record_visual_decision(ir, approved=True)

        self.assertFalse(cad_may_execute(ir))

    def test_auto_approval_keeps_legacy_one_shot_callers_working(self) -> None:
        ir = project()
        bootstrap_design_lifecycle(ir, policy=VisualApprovalPolicy.AUTO_APPROVE_VISUAL)
        register_system_render(
            ir,
            source_fingerprint="sha256:visual",
            uri="forma://visuals/system.png",
        )

        self.assertEqual(
            VisualApprovalStatus.APPROVED,
            load_design_lifecycle(ir).visual_gate.status,
        )
        self.assertTrue(cad_may_execute(ir))

    def test_leaf_change_invalidates_only_dependent_branch(self) -> None:
        ir = project()
        state = bootstrap_design_lifecycle(ir)
        topology_id = stable_artifact_id("project", RepresentationKind.TOPOLOGY)
        left_visual_id = stable_artifact_id(
            "mechanical.drivetrain.left_wheel",
            RepresentationKind.SYSTEM_IMAGE,
        )
        right_visual_id = stable_artifact_id(
            "mechanical.drivetrain.right_wheel",
            RepresentationKind.SYSTEM_IMAGE,
        )
        left_cad_id = stable_artifact_id(
            "mechanical.drivetrain.left_wheel",
            RepresentationKind.COMPONENT_CAD,
        )
        right_cad_id = stable_artifact_id(
            "mechanical.drivetrain.right_wheel",
            RepresentationKind.COMPONENT_CAD,
        )
        assembly_id = stable_artifact_id("project", RepresentationKind.ASSEMBLY_CAD)

        state.representations.extend(
            [
                DesignRepresentation(
                    artifact_id=left_visual_id,
                    node_id="mechanical.drivetrain.left_wheel",
                    kind=RepresentationKind.SYSTEM_IMAGE,
                    fidelity=FidelityLevel.VISUAL,
                    source_fingerprint="sha256:left-visual",
                    depends_on=[topology_id],
                ),
                DesignRepresentation(
                    artifact_id=right_visual_id,
                    node_id="mechanical.drivetrain.right_wheel",
                    kind=RepresentationKind.SYSTEM_IMAGE,
                    fidelity=FidelityLevel.VISUAL,
                    source_fingerprint="sha256:right-visual",
                    depends_on=[topology_id],
                ),
                DesignRepresentation(
                    artifact_id=left_cad_id,
                    node_id="mechanical.drivetrain.left_wheel",
                    kind=RepresentationKind.COMPONENT_CAD,
                    fidelity=FidelityLevel.COMPONENT_CAD,
                    source_fingerprint="sha256:left-cad",
                    depends_on=[left_visual_id],
                ),
                DesignRepresentation(
                    artifact_id=right_cad_id,
                    node_id="mechanical.drivetrain.right_wheel",
                    kind=RepresentationKind.COMPONENT_CAD,
                    fidelity=FidelityLevel.COMPONENT_CAD,
                    source_fingerprint="sha256:right-cad",
                    depends_on=[right_visual_id],
                ),
                DesignRepresentation(
                    artifact_id=assembly_id,
                    node_id="project",
                    kind=RepresentationKind.ASSEMBLY_CAD,
                    fidelity=FidelityLevel.ASSEMBLY_CAD,
                    source_fingerprint="sha256:assembly",
                    depends_on=[left_cad_id, right_cad_id],
                ),
            ]
        )

        invalidated = invalidate_representations(
            state,
            changed_node_ids={"mechanical.drivetrain.left_wheel"},
        )

        self.assertEqual({left_visual_id, left_cad_id, assembly_id}, invalidated)
        records = state.by_id()
        self.assertEqual(RepresentationStatus.STALE, records[left_visual_id].status)
        self.assertEqual(RepresentationStatus.STALE, records[left_cad_id].status)
        self.assertEqual(RepresentationStatus.STALE, records[assembly_id].status)
        self.assertEqual(RepresentationStatus.READY, records[right_visual_id].status)
        self.assertEqual(RepresentationStatus.READY, records[right_cad_id].status)

    def test_register_cad_representation_tracks_explicit_dependencies(self) -> None:
        ir = project()
        bootstrap_design_lifecycle(ir)
        component = register_cad_representation(
            ir,
            node_id="mechanical.drivetrain.left_wheel",
            kind=RepresentationKind.COMPONENT_CAD,
            source_fingerprint="sha256:component",
            uri="forma://cad/left-wheel.step",
            depends_on=[stable_artifact_id("project", RepresentationKind.TOPOLOGY)],
        )
        assembly = register_cad_representation(
            ir,
            node_id="project",
            kind=RepresentationKind.ASSEMBLY_CAD,
            source_fingerprint="sha256:assembly",
            uri="forma://cad/assembly.step",
            depends_on=[component.artifact_id],
        )

        self.assertIn(component.artifact_id, assembly.depends_on)
        self.assertEqual(FidelityLevel.COMPONENT_CAD, component.fidelity)
        self.assertEqual(FidelityLevel.ASSEMBLY_CAD, assembly.fidelity)

    def test_planner_picks_cheapest_representation_for_change(self) -> None:
        self.assertEqual(
            RepresentationKind.TOPOLOGY,
            cheapest_representation_for_change(ChangeKind.ARCHITECTURE),
        )
        self.assertEqual(
            RepresentationKind.SYSTEM_IMAGE,
            cheapest_representation_for_change(ChangeKind.AESTHETIC),
        )
        self.assertEqual(
            RepresentationKind.GEOMETRY_SPEC,
            cheapest_representation_for_change(ChangeKind.DIMENSION),
        )
        self.assertEqual(FidelityLevel.SEMANTIC, stage_fidelity("system_architecture"))
        self.assertEqual(FidelityLevel.ASSEMBLY_CAD, stage_fidelity("cad_generation"))
        self.assertNotEqual(stage_cost("system_architecture"), stage_cost("cad_generation"))


if __name__ == "__main__":
    unittest.main()
