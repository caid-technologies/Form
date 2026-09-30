from __future__ import annotations

import unittest

from pydantic import ValidationError

from forma_core.workspaces.projects.models import (
    BOMLineItem,
    ComponentInstance,
    ConnectionNet,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    MechanicalPlacement,
    MechanicalVector3,
    PartDefinition,
    PinDefinition,
    PinReference,
)


def motor_payload(*, quantity: int = 4) -> dict:
    return {
        "ref_des": "M1",
        "part_number": "N20-6V",
        "name": "N20 gear motor",
        "category": "Actuator",
        "quantity": quantity,
        "unit_price": 3.25,
        "sourcing_url": "https://example.com/n20",
        "rationale": "Independent wheel drive",
        "pins": [
            {"pin_id": "+", "name": "Motor positive", "pin_type": "Power"},
            {"pin_id": "-", "name": "Motor negative", "pin_type": "Ground"},
        ],
    }


class PhysicalComponentInstanceTests(unittest.TestCase):
    def test_legacy_aggregate_expands_to_instances_and_one_bom_row(self) -> None:
        ir = HardwareIntermediateRepresentation(hardware_ir_version="0.1", components=[motor_payload()])

        self.assertEqual("0.2", ir.hardware_ir_version)
        self.assertEqual(["M1", "M2", "M3", "M4"], [item.ref_des for item in ir.components])
        self.assertTrue(all(item.quantity == 1 for item in ir.components))
        self.assertEqual(1, len(ir.part_definitions))
        self.assertEqual(1, len(ir.bom))
        self.assertEqual(4, ir.bom[0].quantity)
        self.assertEqual(["M1", "M2", "M3", "M4"], ir.bom[0].instance_refs)
        self.assertEqual(13.0, ir.bom[0].extended_price)

        serialized = ir.model_dump(mode="json")
        self.assertNotIn("quantity", serialized["components"][0])
        self.assertNotIn("part_number", serialized["components"][0])
        self.assertNotIn("pins", serialized["components"][0])

    def test_each_expanded_motor_can_be_wired_and_placed_independently(self) -> None:
        placements = [
            MechanicalPlacement(
                ref_des=f"M{index}",
                position=MechanicalVector3(x_mm=float(index), y_mm=0, z_mm=0),
                size=MechanicalVector3(x_mm=12, y_mm=10, z_mm=25),
            )
            for index in range(1, 5)
        ]
        nets = [
            ConnectionNet(
                net_id=f"MOTOR_{index}_POS",
                name=f"Motor {index} positive",
                net_type="Power",
                pins=[PinReference(ref_des=f"M{index}", pin_id="+")],
            )
            for index in range(1, 5)
        ]

        ir = HardwareIntermediateRepresentation(
            components=[motor_payload()],
            nets=nets,
            mechanical=MechanicalNotes(
                enclosure_type="Open frame",
                mounting_guidance="Mount each motor independently",
                manufacturability_rating="Easy",
                component_placements=placements,
            ),
        )

        self.assertEqual(4, len(ir.nets))
        self.assertEqual(4, len(ir.mechanical.component_placements))

    def test_unknown_instance_and_pin_references_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown component instance 'M5'"):
            HardwareIntermediateRepresentation(
                components=[motor_payload(quantity=1)],
                nets=[
                    ConnectionNet(
                        net_id="BAD_REF",
                        name="Bad reference",
                        net_type="Power",
                        pins=[PinReference(ref_des="M5", pin_id="+")],
                    )
                ],
            )

        with self.assertRaisesRegex(ValidationError, "unknown pin 'PWM'"):
            HardwareIntermediateRepresentation(
                components=[motor_payload(quantity=1)],
                nets=[
                    ConnectionNet(
                        net_id="BAD_PIN",
                        name="Bad pin",
                        net_type="PWM",
                        pins=[PinReference(ref_des="M1", pin_id="PWM")],
                    )
                ],
            )

    def test_invalid_bom_aggregation_is_rejected(self) -> None:
        definition = PartDefinition(
            part_definition_id="PART_N20_6V",
            part_number="N20-6V",
            name="N20 gear motor",
            category="Actuator",
            unit_price=3.25,
        )
        components = [
            ComponentInstance(
                ref_des=f"M{index}",
                part_definition_id=definition.part_definition_id,
                rationale="Wheel drive",
            )
            for index in range(1, 5)
        ]
        bad_bom = BOMLineItem(
            line_id="BOM_PART_N20_6V",
            part_definition_id=definition.part_definition_id,
            instance_refs=["M1", "M2", "M3"],
            quantity=3,
            part_number=definition.part_number,
            name=definition.name,
            category=definition.category,
            unit_price=definition.unit_price,
            extended_price=9.75,
        )

        with self.assertRaisesRegex(ValidationError, "must contain every physical instance"):
            HardwareIntermediateRepresentation(part_definitions=[definition], components=components, bom=[bad_bom])

    def test_source_agnostic_part_definition_round_trips(self) -> None:
        definition = PartDefinition(
            part_definition_id="PART_LED_RED",
            manufacturer="Example Semiconductor",
            part_number="LED-5MM-RED",
            name="Red LED",
            category="Display",
            description="Diffused indicator LED",
            electrical_specs={"forward_voltage_v": 2.0},
            pins=[
                PinDefinition(pin_id="A", name="Anode", pin_type="Power"),
                PinDefinition(pin_id="K", name="Cathode", pin_type="Ground"),
            ],
            dimensions_mm={"diameter": 5.0},
            datasheet_url="https://example.com/led.pdf",
            unit_price=0.12,
        )
        ir = HardwareIntermediateRepresentation(
            part_definitions=[definition],
            components=[
                ComponentInstance(
                    ref_des="D1",
                    part_definition_id=definition.part_definition_id,
                    rationale="Status indicator",
                    configuration={"color": "red"},
                )
            ],
        )

        restored = HardwareIntermediateRepresentation.model_validate(ir.model_dump(mode="json"))

        self.assertEqual(definition, restored.part_definitions[0])
        self.assertEqual("LED-5MM-RED", restored.components[0].part_number)
        self.assertEqual(["D1"], restored.bom[0].instance_refs)

    def test_duplicate_reference_designators_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Duplicate component reference designator 'M1'"):
            HardwareIntermediateRepresentation(components=[motor_payload(quantity=1), motor_payload(quantity=1)])


if __name__ == "__main__":
    unittest.main()
