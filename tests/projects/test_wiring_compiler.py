from __future__ import annotations

import unittest

from forma_core.validation import validate_circuit
from forma_core.wiring import (
    NetIntent,
    WiringIntent,
    build_endpoint_catalog,
    compile_wiring_intent,
    derive_pin_mappings,
    wiring_failure_category,
)
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    PinDefinition,
    PinReference,
    ValidationIssue,
)


def _components() -> list[ComponentInstance]:
    return [
        ComponentInstance(
            ref_des="U1",
            part_number="MCU",
            name="Controller",
            category="Microcontroller",
            rationale="Controls the bus",
            pins=[
                PinDefinition(
                    pin_id="3V3",
                    name="3.3V output",
                    pin_type="Power",
                    voltage=3.3,
                    power_role="regulated_output",
                ),
                PinDefinition(pin_id="GND", name="Ground", pin_type="Ground", power_role="return"),
                PinDefinition(
                    pin_id="SDA",
                    name="I2C data",
                    pin_type="I2C",
                    voltage=3.3,
                    direction="bidirectional",
                    interface="I2C",
                ),
                PinDefinition(
                    pin_id="SCL",
                    name="I2C clock",
                    pin_type="I2C",
                    voltage=3.3,
                    direction="output",
                    interface="I2C",
                ),
            ],
        ),
        ComponentInstance(
            ref_des="SEN1",
            part_number="SENSOR",
            name="Sensor One",
            category="Sensor",
            rationale="Measures a value",
            pins=[
                PinDefinition(pin_id="VCC", name="Supply", pin_type="Power", voltage=3.3, power_role="input"),
                PinDefinition(pin_id="GND", name="Ground", pin_type="Ground", power_role="return"),
                PinDefinition(pin_id="SDA", name="I2C data", pin_type="I2C", voltage=3.3, interface="I2C"),
                PinDefinition(pin_id="SCL", name="I2C clock", pin_type="I2C", voltage=3.3, interface="I2C"),
            ],
        ),
        ComponentInstance(
            ref_des="SEN2",
            part_number="SENSOR",
            name="Sensor Two",
            category="Sensor",
            rationale="Measures another value",
            pins=[
                PinDefinition(pin_id="VCC", name="Supply", pin_type="Power", voltage=5.0, power_role="input"),
                PinDefinition(pin_id="GND", name="Ground", pin_type="Ground", power_role="return"),
                PinDefinition(pin_id="SDA", name="I2C data", pin_type="I2C", voltage=3.3, interface="I2C"),
                PinDefinition(pin_id="SCL", name="I2C clock", pin_type="I2C", voltage=3.3, interface="I2C"),
            ],
        ),
    ]


class WiringCompilerTests(unittest.TestCase):
    def test_catalog_uses_stable_endpoint_ids_and_semantics(self) -> None:
        catalog = build_endpoint_catalog(_components())

        self.assertIn("U1.SDA", catalog)
        self.assertEqual("I2C", catalog["U1.SDA"].interface)
        self.assertEqual("regulated_output", catalog["U1.3V3"].power_role)

    def test_compiler_rejects_hallucinated_pin_without_dropping_valid_net(self) -> None:
        result = compile_wiring_intent(
            _components(),
            WiringIntent(nets=[
                NetIntent(name="I2C Data", net_type="I2C", voltage=3.3, endpoint_ids=["U1.SDA", "SEN1.SDA"]),
                NetIntent(name="Bad Data", net_type="Digital", endpoint_ids=["U1.GPIO99", "SEN2.SDA"]),
            ]),
        )

        self.assertEqual(["NET_I2C_DATA"], [net.net_id for net in result.nets])
        self.assertEqual(1, len(result.rejected))
        self.assertIn("Unknown Pin Reference", {issue.category for issue in result.issues})

    def test_targeted_repair_preserves_previously_compiled_net(self) -> None:
        components = _components()
        initial = compile_wiring_intent(
            components,
            WiringIntent(nets=[
                NetIntent(name="I2C Data", net_type="I2C", endpoint_ids=["U1.SDA", "SEN1.SDA", "SEN2.SDA"]),
                NetIntent(name="Broken Clock", net_type="I2C", endpoint_ids=["U1.NOPE", "SEN1.SCL"]),
            ]),
        )
        preserved_dump = initial.nets[0].model_dump()

        repaired = compile_wiring_intent(
            components,
            WiringIntent(nets=[
                NetIntent(name="I2C Clock", net_type="I2C", endpoint_ids=["U1.SCL", "SEN1.SCL", "SEN2.SCL"]),
            ]),
            existing_nets=initial.nets,
        )

        self.assertFalse(repaired.rejected)
        self.assertEqual(preserved_dump, initial.nets[0].model_dump())
        self.assertEqual("NET_I2C_CLOCK", repaired.nets[0].net_id)

    def test_targeted_replacement_preserves_unrelated_nets(self) -> None:
        components = _components()
        initial = compile_wiring_intent(
            components,
            WiringIntent(nets=[
                NetIntent(name="I2C Data", net_type="I2C", endpoint_ids=["U1.SDA", "SEN1.SDA"]),
                NetIntent(name="I2C Clock", net_type="I2C", endpoint_ids=["U1.SCL", "SEN1.SCL"]),
            ]),
        )

        repaired = compile_wiring_intent(
            components,
            WiringIntent(nets=[NetIntent(
                name="I2C Data Repaired",
                net_type="I2C",
                endpoint_ids=["U1.SDA", "SEN1.SDA", "SEN2.SDA"],
                replace_net_id="NET_I2C_DATA",
            )]),
            existing_nets=initial.nets,
        )

        self.assertEqual(
            ["NET_I2C_CLOCK", "NET_I2C_DATA"],
            [net.net_id for net in repaired.all_nets],
        )
        self.assertEqual(
            ["SEN2"],
            [pin.ref_des for pin in repaired.all_nets[1].pins if pin.ref_des == "SEN2"],
        )

    def test_compiler_rejects_signal_reuse_across_nets(self) -> None:
        result = compile_wiring_intent(
            _components(),
            WiringIntent(nets=[
                NetIntent(name="Bus Data", net_type="I2C", endpoint_ids=["U1.SDA", "SEN1.SDA"]),
                NetIntent(name="Other Data", net_type="I2C", endpoint_ids=["U1.SDA", "SEN2.SDA"]),
            ]),
        )

        self.assertEqual(1, len(result.nets))
        self.assertIn("Pin Conflict", {issue.category for issue in result.issues})

    def test_compiler_does_not_treat_equal_voltage_as_a_power_source(self) -> None:
        result = compile_wiring_intent(
            _components(),
            WiringIntent(nets=[
                NetIntent(
                    name="Input Only Rail",
                    net_type="Power",
                    voltage=3.3,
                    endpoint_ids=["SEN1.VCC"],
                ),
            ]),
        )

        self.assertFalse(result.nets)
        self.assertIn("Missing Power Source", {issue.category for issue in result.issues})

    def test_pin_mappings_are_derived_from_shared_i2c_bus(self) -> None:
        components = _components()
        result = compile_wiring_intent(
            components,
            WiringIntent(nets=[
                NetIntent(
                    name="Shared I2C Data",
                    net_type="I2C",
                    endpoint_ids=["U1.SDA", "SEN1.SDA", "SEN2.SDA"],
                ),
            ]),
        )

        mappings = derive_pin_mappings(components, result.nets)

        self.assertEqual(1, len(mappings))
        self.assertEqual("SDA", mappings[0].mcu_pin)
        self.assertEqual("NET_SHARED_I2C_DATA", mappings[0].net_name)
        self.assertIn("SEN1", mappings[0].connected_to)
        self.assertIn("SEN2", mappings[0].connected_to)

    def test_validation_blocks_unknown_refs_duplicates_and_signal_conflicts(self) -> None:
        nets = [
            ConnectionNet(
                net_id="NET_BAD",
                name="Bad",
                net_type="Digital",
                pins=[
                    PinReference(ref_des="U9", pin_id="SDA"),
                    PinReference(ref_des="U1", pin_id="GPIO99"),
                    PinReference(ref_des="U1", pin_id="SDA"),
                    PinReference(ref_des="U1", pin_id="SDA"),
                ],
            ),
            ConnectionNet(
                net_id="NET_REUSE",
                name="Reuse",
                net_type="I2C",
                pins=[PinReference(ref_des="U1", pin_id="SDA"), PinReference(ref_des="SEN1", pin_id="SDA")],
            ),
            ConnectionNet(net_id="NET_EMPTY", name="Empty", net_type="Digital", pins=[]),
        ]

        categories = {issue.category for issue in validate_circuit(_components(), nets)}

        self.assertTrue({
            "Unknown Component Reference",
            "Unknown Pin Reference",
            "Duplicate Endpoint",
            "Pin Conflict",
            "Empty Net",
        }.issubset(categories))

    def test_validation_reports_voltage_mismatch_and_unpowered_components(self) -> None:
        nets = [
            ConnectionNet(
                net_id="NET_BAD_POWER",
                name="Bad Power",
                net_type="Power",
                voltage=3.3,
                pins=[PinReference(ref_des="U1", pin_id="3V3"), PinReference(ref_des="SEN2", pin_id="VCC")],
            ),
        ]

        issues = validate_circuit(_components(), nets)
        categories = {issue.category for issue in issues}

        self.assertIn("Voltage Mismatch", categories)
        self.assertIn("Unpowered IC", categories)

    def test_failure_categories_are_specific_for_pipeline_telemetry(self) -> None:
        issue = ValidationIssue(
            severity="CRITICAL",
            category="Unknown Pin Reference",
            description="U1.GPIO99 is unknown",
            troubleshooting="Use a catalog endpoint",
        )
        missing_ground = ValidationIssue(
            severity="CRITICAL",
            category="Unpowered IC",
            description="Sensor has no ground reference",
            troubleshooting="Connect ground",
        )

        self.assertEqual("unknown_pin_reference", wiring_failure_category(issue))
        self.assertEqual("missing_ground_net", wiring_failure_category(missing_ground))


if __name__ == "__main__":
    unittest.main()
