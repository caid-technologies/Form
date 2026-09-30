from __future__ import annotations

import unittest

from forma_core.agents.lattice import default_namespace_agent_cards
from forma_core.workspaces.projects.context_governance import (
    ContextGovernancePolicy,
    ContextShareRule,
    project_context_for_agent,
)
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    FunctionalRequirements,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    PinDefinition,
    PinReference,
    ProjectOverview,
)
from forma_core.workspaces.projects.objects import build_project_object


class ContextGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        ir = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Controller",
                description="A low-voltage controller.",
                difficulty="Beginner",
                category="IoT",
            ),
            requirements=FunctionalRequirements(
                requirements=["Read a button"],
                power_needs="USB-C 5 V",
            ),
            components=[
                ComponentInstance(
                    ref_des="U1",
                    part_number="MCU-1",
                    name="Controller",
                    category="Microcontroller",
                    rationale="Runs the product.",
                    pins=[PinDefinition(pin_id="GPIO1", name="Input", pin_type="Digital")],
                )
            ],
            nets=[
                ConnectionNet(
                    net_id="NET_BUTTON",
                    name="Button input",
                    net_type="Digital",
                    pins=[PinReference(ref_des="U1", pin_id="GPIO1")],
                )
            ],
            mechanical=MechanicalNotes(
                enclosure_type="3D Printed",
                mounting_guidance="Use standoffs.",
                manufacturability_rating="Easy",
            ),
            assembly_metadata={"project_id": "00000000-0000-0000-0000-000000000001"},
        )
        self.project_object = build_project_object(ir).model_dump(mode="json")

    def test_mechanical_agent_receives_summary_without_pin_data(self) -> None:
        context = project_context_for_agent(self.project_object, "product.mech")

        electrical = context["namespaces"]["product.electrical"]
        self.assertEqual(["U1"], [item["ref_des"] for item in electrical["components"]])
        self.assertNotIn("pins", electrical["components"][0])
        self.assertIn("components[].pins", context["omitted_attributes"]["product.electrical"])
        self.assertNotIn("product.meta", context["namespaces"])
        self.assertIn("project.meta", context["denied_sources"])

    def test_assembly_receives_build_connectivity_without_pin_ids(self) -> None:
        context = project_context_for_agent(self.project_object, "product.assembly")

        electrical = context["namespaces"]["product.electrical"]
        self.assertEqual(["NET_BUTTON"], [item["net_id"] for item in electrical["nets"]])
        self.assertNotIn("pin_id", str(electrical))
        self.assertIn("mechanical", context["namespaces"]["product.mech"])

    def test_same_namespace_access_is_full_but_inline_data_is_sanitized(self) -> None:
        context = project_context_for_agent(self.project_object, "product.mech")

        mechanical = context["namespaces"]["product.mech"]["mechanical"]
        self.assertEqual("3D Printed", mechanical["enclosure_type"])

        mech_namespace = next(
            item for item in self.project_object["namespaces"] if item["name"] == "product.mech"
        )
        mech_namespace["payload"]["token"] = "do-not-share"
        mech_namespace["payload"]["inline"] = "data:text/plain;base64,secret"
        context = project_context_for_agent(self.project_object, "product.mech")
        own = context["namespaces"]["product.mech"]
        self.assertNotIn("token", own)
        self.assertEqual("<redacted inline data: 29 chars>", own["inline"])

    def test_lattice_card_publishes_recipient_policy(self) -> None:
        cards = {card.agent_id: card for card in default_namespace_agent_cards()}

        manifest = cards["product.mech"].metadata["context_governance"]
        self.assertEqual("none", manifest["default_mode"])
        self.assertEqual("summary", manifest["grants"]["product.electrical"]["mode"])
        self.assertNotIn("project.meta", manifest["grants"])

    def test_project_object_records_the_policy_version(self) -> None:
        governance = self.project_object["metadata"]["context_governance"]

        self.assertEqual("1.0", governance["schema_version"])
        self.assertEqual("none", governance["default_mode"])

    def test_nested_secret_fields_are_removed_from_custom_grants(self) -> None:
        source = {
            "object_id": "project-1",
            "version": 1,
            "namespaces": [{"name": "product.source", "payload": {"record": {"token": "secret", "safe": "ok"}}}],
        }
        policy = ContextGovernancePolicy(rules=[ContextShareRule(
            source_namespace="product.source",
            recipient_namespace="product.target",
            allowed_attributes={"record": ["token", "safe"]},
            reason="Test grant",
        )])

        context = project_context_for_agent(source, "product.target", policy=policy)

        self.assertEqual({"safe": "ok"}, context["namespaces"]["product.source"]["record"])
        self.assertIn("record.token", context["omitted_attributes"]["product.source"])


if __name__ == "__main__":
    unittest.main()
