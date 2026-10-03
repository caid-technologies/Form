from __future__ import annotations

import unittest

from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    FunctionalRequirements,
    GenerateProjectRequest,
    IterateProjectRequest,
    PinDefinition,
    PinReference,
)
from forma_core.validation import validate_circuit


class ValidationAndModelTests(unittest.TestCase):
    def test_pinless_netless_electronics_cannot_pass(self) -> None:
        components = [ComponentInstance(
            ref_des=f"U{index}", name=f"Module {index}", category="Module", rationale="Circuit module",
        ) for index in range(1, 10)]
        issues = validate_circuit(components, [])
        self.assertEqual(9, sum(issue.category == "Missing Pin Definitions" for issue in issues))
        self.assertTrue(any(issue.category == "Missing Electrical Nets" for issue in issues))
        self.assertTrue(all(issue.severity == "CRITICAL" for issue in issues))

    def test_declared_passive_pins_still_require_a_netlist(self) -> None:
        component = ComponentInstance(
            ref_des="R1", category="Passives", rationale="Load",
            pins=[PinDefinition(pin_id="1", name="Terminal", pin_type="Passive")],
        )
        self.assertIn("Missing Electrical Nets", {issue.category for issue in validate_circuit([component], [])})

    def test_pinless_component_is_not_hidden_by_other_components_nets(self) -> None:
        component = ComponentInstance(ref_des="U1", category="Sensor", rationale="Sense")
        net = ConnectionNet(net_id="N1", name="Placeholder", net_type="Digital")
        self.assertIn("Missing Pin Definitions", {issue.category for issue in validate_circuit([component], [net])})

    def test_empty_draft_and_mechanical_only_bom_remain_allowed(self) -> None:
        self.assertEqual([], validate_circuit([], []))
        for category in ("Mechanical", "Enclosure", "Fastener", "3D Print"):
            with self.subTest(category=category):
                component = ComponentInstance(ref_des="M1", category=category, rationale="Housing")
                self.assertEqual([], validate_circuit([component], []))

    def test_module_with_external_terminals_does_not_require_internal_circuitry(self) -> None:
        components = [ComponentInstance(
            ref_des=ref, category=category, rationale="USB power",
            pins=[PinDefinition(pin_id="VBUS", name="USB power", pin_type="Power", voltage=5)],
        ) for ref, category in (("U1", "Module"), ("J1", "Connector"))]
        net = ConnectionNet(net_id="USB", name="USB power", net_type="Power", pins=[
            PinReference(ref_des=component.ref_des, pin_id="VBUS") for component in components
        ])
        self.assertEqual([], validate_circuit(components, [net]))

    def test_generation_request_strips_optional_runtime_selector_fields(self) -> None:
        request = GenerateProjectRequest(
            prompt="plant watering monitor",
            provider=" openai ",
            model=" gpt-5.5 ",
            chat_id=" chat-123 ",
            source_project_id=" source-456 ",
        )

        self.assertEqual("openai", request.provider)
        self.assertEqual("gpt-5.5", request.model)
        self.assertEqual("chat-123", request.chat_id)
        self.assertEqual("source-456", request.source_project_id)

    def test_iteration_request_strips_optional_runtime_selector_fields(self) -> None:
        request = IterateProjectRequest(
            instruction="  add battery charging  ",
            namespace=" Product.Mech ",
            provider=" openai ",
            model=" gpt-5.5 ",
        )

        self.assertEqual("add battery charging", request.instruction)
        self.assertEqual("Product.Mech", request.namespace)
        self.assertEqual("openai", request.provider)
        self.assertEqual("gpt-5.5", request.model)

    def test_validate_circuit_flags_power_to_ground_short(self) -> None:
        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="MCU",
                name="Microcontroller",
                category="Microcontroller",
                rationale="Controller",
                pins=[
                    PinDefinition(pin_id="3V3", name="3.3V", pin_type="Power", voltage=3.3),
                    PinDefinition(pin_id="GND", name="Ground", pin_type="Ground", voltage=0.0),
                ],
            )
        ]
        nets = [
            ConnectionNet(
                net_id="NET_SHORT",
                name="Accidental short",
                net_type="Power",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="3V3"),
                    PinReference(ref_des="U1", pin_id="GND"),
                ],
            )
        ]

        issues = validate_circuit(components, nets)

        self.assertTrue(any(issue.severity == "CRITICAL" for issue in issues))
        self.assertTrue(any(issue.category == "Short Circuit" for issue in issues))

    def test_validate_circuit_flags_uncovered_requested_hardware(self) -> None:
        requirements = FunctionalRequirements(
            requirements=["Measure soil moisture and sound a buzzer when the plant is dry."],
            power_needs="5V USB",
        )
        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="ESP32-WROOM-32D",
                name="ESP32 NodeMCU Development Board",
                category="Microcontroller",
                rationale="Controller",
            )
        ]

        issues = validate_circuit(components, [], requirements)

        coverage = [issue.description for issue in issues if issue.category == "Requirement Coverage"]
        self.assertTrue(any("soil-moisture sensing" in description for description in coverage))
        self.assertTrue(any("audible alert" in description for description in coverage))


if __name__ == "__main__":
    unittest.main()
