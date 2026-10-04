from __future__ import annotations

import unittest

from forma_core.workspaces.projects.models import (
    ComponentInstance,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    MechanicalPlacement,
    MechanicalVector3,
    SystemArchitecture,
    SystemNode,
)
from forma_core.workspaces.projects.opencad_assembly import build_opencad_assembly_spec


class OpenCADAssemblySpecTests(unittest.TestCase):
    def test_system_hierarchy_and_physical_components_are_preserved(self) -> None:
        project = HardwareIntermediateRepresentation(
            system_architecture=SystemArchitecture(
                summary="Robot arm",
                root=SystemNode(
                    system_id="product",
                    name="Micro Arm",
                    domain="product",
                    purpose="Manipulate small objects.",
                    children=[
                        SystemNode(
                            system_id="mechanical",
                            name="Mechanical",
                            domain="mechanical",
                            purpose="Own the articulated structure.",
                            children=[
                                SystemNode(
                                    system_id="mechanical.shoulder",
                                    name="Shoulder",
                                    domain="mechanical",
                                    purpose="Rotate the upper arm.",
                                )
                            ],
                        )
                    ],
                ),
            ),
            components=[
                ComponentInstance(
                    ref_des="M1",
                    part_number="SERVO-1",
                    name="Shoulder servo",
                    category="Actuator",
                    rationale="Drives the shoulder.",
                    configuration={"system_id": "mechanical.shoulder"},
                ),
                ComponentInstance(
                    ref_des="U1",
                    part_number="MCU-1",
                    name="Controller",
                    category="Microcontroller",
                    rationale="Controls the arm.",
                ),
            ],
            mechanical=MechanicalNotes(
                enclosure_type="Open frame",
                mounting_guidance="Mount parts on the frame.",
                manufacturability_rating="Easy",
                component_placements=[
                    MechanicalPlacement(
                        ref_des="M1",
                        label="Shoulder servo",
                        category="Actuator",
                        position=MechanicalVector3(x_mm=0, y_mm=0, z_mm=20),
                        size=MechanicalVector3(x_mm=20, y_mm=12, z_mm=28),
                    ),
                    MechanicalPlacement(
                        ref_des="U1",
                        label="Controller",
                        category="Microcontroller",
                        position=MechanicalVector3(x_mm=0, y_mm=20, z_mm=2),
                        size=MechanicalVector3(x_mm=40, y_mm=25, z_mm=8),
                    ),
                ],
            ),
        )

        spec = build_opencad_assembly_spec(project)
        components = spec["components"]

        self.assertEqual(["product"], spec["root_ids"])
        self.assertEqual({"kind": "exported"}, components["product"]["geometry_binding"])
        self.assertIn("mechanical", components["product"]["child_ids"])
        self.assertIn("M1", components["mechanical.shoulder"]["child_ids"])
        self.assertEqual(
            {"kind": "ref_des", "ref_des": "M1"},
            components["M1"]["geometry_binding"],
        )
        self.assertEqual("M1", components["M1"]["metadata"]["forma_component_id"])
        self.assertEqual(
            [0.0, 0.0, 20.0],
            components["M1"]["metadata"]["placement"]["position_mm"],
        )

        group_id = next(
            component_id
            for component_id, component in components.items()
            if component["name"] == "Physical Components"
        )
        self.assertIn(group_id, components["product"]["child_ids"])
        self.assertIn("U1", components[group_id]["child_ids"])

    def test_mechanism_bodies_become_semantic_components_without_bom_items(self) -> None:
        project = HardwareIntermediateRepresentation(
            mechanical=MechanicalNotes(
                enclosure_type="Mechanism",
                mounting_guidance="Print in place.",
                manufacturability_rating="Moderate",
                mechanism_benchmark={
                    "kind": "print_in_place_hinge",
                    "fixed_ref": "BASE",
                    "moving_ref": "LID",
                },
            )
        )

        spec = build_opencad_assembly_spec(project)
        components = spec["components"]

        self.assertIn("BASE", components)
        self.assertIn("LID", components)
        self.assertEqual(
            {"kind": "ref_des", "ref_des": "BASE"},
            components["BASE"]["geometry_binding"],
        )
        self.assertEqual(
            {"kind": "ref_des", "ref_des": "LID"},
            components["LID"]["geometry_binding"],
        )

    def test_component_id_collision_keeps_original_source_identity_in_metadata(self) -> None:
        project = HardwareIntermediateRepresentation(
            system_architecture=SystemArchitecture(
                summary="Collision fixture",
                root=SystemNode(
                    system_id="U1",
                    name="System U1",
                    domain="product",
                    purpose="Test namespace collision.",
                ),
            ),
            components=[
                ComponentInstance(
                    ref_des="U1",
                    part_number="PART-1",
                    name="Physical U1",
                    category="Component",
                    rationale="Test collision.",
                )
            ],
            mechanical=MechanicalNotes(
                enclosure_type="Fixture",
                mounting_guidance="None.",
                manufacturability_rating="Easy",
            ),
        )

        spec = build_opencad_assembly_spec(project)
        physical = [
            component
            for component in spec["components"].values()
            if component["metadata"].get("source") == "forma.component"
        ]

        self.assertEqual(1, len(physical))
        self.assertNotEqual("U1", physical[0]["id"])
        self.assertEqual("U1", physical[0]["metadata"]["forma_component_id"])


if __name__ == "__main__":
    unittest.main()
