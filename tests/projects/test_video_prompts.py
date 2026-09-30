from __future__ import annotations

import unittest

from forma_core.workspaces.projects.models import (
    AssemblyStep,
    ComponentInstance,
    FunctionalRequirements,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    PinDefinition,
    ProjectOverview,
)
from forma_core.video_prompts import generate_image_to_video_prompt_from_namespaces


class VideoPromptGenerationTests(unittest.TestCase):
    def test_image_to_video_prompt_uses_project_namespaces(self) -> None:
        ir = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Desk Air Quality Monitor",
                description="A compact monitor with OLED readout and environmental sensing.",
                difficulty="Beginner",
                estimated_cost=32.0,
                category="IoT",
            ),
            requirements=FunctionalRequirements(
                requirements=["Measure air quality", "Show status on OLED"],
                power_needs="USB-C 5V with 3.3V regulator",
                operating_voltage=3.3,
            ),
            components=[
                ComponentInstance(
                    ref_des="U1",
                    part_number="ESP32-WROOM-32D",
                    name="ESP32 Dev Board",
                    category="Microcontroller",
                    rationale="Runs sensor and display logic.",
                    pins=[PinDefinition(pin_id="3V3", name="3.3V", pin_type="Power", voltage=3.3)],
                ),
                ComponentInstance(
                    ref_des="DISP1",
                    part_number="SSD1306-I2C",
                    name="OLED Display",
                    category="Display",
                    rationale="Shows readings.",
                ),
            ],
            mechanical=MechanicalNotes(
                enclosure_type="3D Printed",
                mounting_guidance="Mount OLED on the front face and route sensor vents to the side.",
                fabrication_details=["Rounded desktop enclosure with visible sensor vents."],
                manufacturability_rating="Easy",
            ),
            assembly=[
                AssemblyStep(
                    step_num=1,
                    title="Mount display",
                    description="Install OLED into the front window before wiring.",
                )
            ],
            assembly_metadata={
                "project_id": "11111111-1111-4111-8111-111111111111",
                "product_image_prompt": "Clean realistic desk gadget render.",
            },
        )

        payload = generate_image_to_video_prompt_from_namespaces(ir)

        self.assertIn("Desk Air Quality Monitor", payload["prompt"])
        self.assertIn("ESP32", payload["prompt"])
        self.assertIn("OLED", payload["prompt"])
        self.assertIn("Mount display", payload["prompt"])
        self.assertIn("product.overview", payload["namespaces"])
        self.assertLessEqual(len(payload["prompt"]), payload["prompt_max_chars"])
        self.assertFalse(payload["prompt_truncated"])


if __name__ == "__main__":
    unittest.main()
