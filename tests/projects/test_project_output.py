from __future__ import annotations

import unittest

from forma_core.image_providers import GeneratedImage
from forma_core.agents.orchestrator import build_mechanical_render_data
from forma_core.workspaces.projects.models import ComponentInstance, HardwareIntermediateRepresentation, MechanicalNotes, ProjectOverview
from forma_core.workspaces.projects.objects import namespace_payload
from forma_core.workspaces.projects.output import attach_hardware_reference_image, attach_product_image


class ProjectOutputTests(unittest.TestCase):
    def test_mechanical_render_data_does_not_create_a_cad_model(self) -> None:
        ir = HardwareIntermediateRepresentation(
            components=[
                ComponentInstance(
                    ref_des="U1",
                    name="Controller",
                    category="Microcontroller",
                    rationale="Main controller.",
                )
            ],
            mechanical=MechanicalNotes(
                enclosure_type="3D Printed",
                mounting_guidance="Mount on internal standoffs.",
                manufacturability_rating="Easy",
            ),
        )

        rendered = build_mechanical_render_data(ir)

        self.assertIsNone(rendered.cad_model)

    def test_cad_model_survives_hardware_ir_round_trip_and_mechanical_namespace_projection(self) -> None:
        cad_model = {"path": "/srv/models/enclosure.step", "adapter": "forma-opencad"}
        ir = HardwareIntermediateRepresentation(cad_model=cad_model)

        restored = HardwareIntermediateRepresentation.model_validate(ir.model_dump(mode="json"))

        self.assertEqual(cad_model, restored.cad_model)
        self.assertEqual(cad_model, namespace_payload(restored, "product.mech")["cad_model"])

    def test_mechanical_namespace_does_not_promote_legacy_cad_model_fields(self) -> None:
        ir = HardwareIntermediateRepresentation.model_validate({
            "mechanical": {
                "cad_model": {"path": "/srv/models/legacy.step"},
                "enclosure_type": "3D Printed",
                "mounting_guidance": "Test",
                "manufacturability_rating": "Easy",
            },
            "assembly_metadata": {"cad_model": {"path": "/srv/models/metadata.step"}},
        })

        self.assertIsNone(ir.cad_model)
        self.assertIsNone(namespace_payload(ir, "product.mech")["cad_model"])

    def test_generated_image_is_attached_inline_when_storage_skips_upload(self) -> None:
        ir = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Test project",
                description="A test",
                difficulty="Beginner",
                category="IoT",
            ),
            assembly_metadata={"project_id": "29f2853c-ba3e-425a-b4c2-60f91cd2398b"},
        )
        image = GeneratedImage(
            data_url="data:image/png;base64,ZmFrZQ==",
            provider="gmi",
            model="gpt-image-2",
            size="1024x1024",
            prompt="render",
            output_format="png",
            view_id="case",
            label="Case",
        )

        class FakeProvider:
            def get_debug_config(self):
                return {
                    "provider": "gmi",
                    "model_name": "gpt-image-2",
                    "enabled": True,
                    "configured": True,
                }

            def generate_project_image_sequence(self, _prompt, _ir):
                return [image]

        def inline_storage(*_args, **_kwargs):
            return {"product_case_image_storage_enabled": False}

        attach_product_image(
            "test prompt",
            ir,
            generate_image=True,
            provider_factory=lambda **_kwargs: FakeProvider(),
            storage_handler=inline_storage,
        )

        metadata = ir.assembly_metadata
        self.assertEqual("succeeded", metadata["image_output_status"])
        self.assertEqual(image.data_url, metadata["product_image_data"])
        self.assertEqual(image.data_url, metadata["product_visual_sequence"][0]["data"])
        self.assertEqual(1, metadata["product_visual_sequence_count"])

    def test_hardware_reference_is_kept_inline_when_storage_skips_upload(self) -> None:
        ir = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Test project",
                description="A test",
                difficulty="Beginner",
                category="IoT",
            ),
            assembly_metadata={"project_id": "29f2853c-ba3e-425a-b4c2-60f91cd2398b"},
        )

        attach_hardware_reference_image(
            ir,
            "data:image/png;base64,aW1hZ2U=",
            media_type="image/png",
            storage_handler=lambda *_args, **_kwargs: {"reference_image_storage_enabled": False},
        )

        self.assertEqual("data:image/png;base64,aW1hZ2U=", ir.assembly_metadata["reference_image_data"])
        self.assertEqual("image/png", ir.assembly_metadata["reference_image_content_type"])
        self.assertEqual("prompt_image", ir.assembly_metadata["input_mode"])

    def test_hardware_reference_does_not_replace_an_existing_stored_image(self) -> None:
        ir = HardwareIntermediateRepresentation(assembly_metadata={"reference_image_url": "https://example.test/ref.png"})

        attach_hardware_reference_image(ir, "data:image/png;base64,aW1hZ2U=")

        self.assertEqual("https://example.test/ref.png", ir.assembly_metadata["reference_image_url"])
        self.assertNotIn("reference_image_data", ir.assembly_metadata)


if __name__ == "__main__":
    unittest.main()
