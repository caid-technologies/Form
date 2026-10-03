from __future__ import annotations

import unittest
from unittest.mock import patch

from forma_core.image_providers import GeneratedImage
from forma_core.workspaces.projects.design_lifecycle import (
    RepresentationKind,
    VisualApprovalStatus,
    load_design_lifecycle,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, SystemArchitecture, SystemNode
from forma_core.workspaces.projects.output import attach_product_image


class FakeImageProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.counter = 0

    def get_debug_config(self):
        return {
            "provider": "test-images",
            "enabled": True,
            "configured": True,
            "model_name": "test-image-model",
        }

    def _image(self, prompt: str, *, view_id: str, label: str) -> GeneratedImage:
        self.counter += 1
        return GeneratedImage(
            data_url=f"data:image/png;base64,image-{self.counter}",
            provider="test-images",
            model="test-image-model",
            size="1024x1024",
            prompt=prompt,
            view_id=view_id,
            label=label,
        )

    def generate_test_image(self, prompt: str) -> GeneratedImage:
        self.calls.append(("system", prompt))
        return self._image(prompt, view_id="test", label="System")

    def generate_project_image_sequence(self, prompt: str, ir: HardwareIntermediateRepresentation):
        self.calls.append(("project", prompt))
        return [self._image(prompt, view_id="hero", label="Whole system")]


def project(*, policy: str = "require_approval", mode: str = "progressive") -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation(
        system_architecture=SystemArchitecture(
            summary="Desk robot",
            root=SystemNode(
                system_id="product",
                name="Desk Robot",
                domain="product",
                purpose="Move objects around a desk.",
                children=[
                    SystemNode(
                        system_id="mechanical.arm",
                        name="Robot arm",
                        domain="mechanical",
                        purpose="Position the end effector.",
                        responsibilities=["Provide three-axis motion"],
                        expected_component_roles=["links", "joints", "actuators"],
                    ),
                    SystemNode(
                        system_id="electrical.control",
                        name="Control electronics",
                        domain="electrical",
                        purpose="Coordinate motion and power.",
                        expected_component_roles=["MCU", "motor drivers", "power regulation"],
                    ),
                ],
            ),
        ),
        assembly_metadata={
            "generation_mode": mode,
            "project_id": "11111111-1111-4111-8111-111111111111",
            "design_brief_id": "22222222-2222-4222-8222-222222222222",
            "visual_approval_policy": policy,
        },
    )


def fake_storage(ir, *, image_data, metadata_prefix, object_prefix, **kwargs):
    del ir, image_data, object_prefix, kwargs
    return {
        f"{metadata_prefix}_url": f"https://images.example.test/{metadata_prefix}.png",
        f"{metadata_prefix}_content_type": "image/png",
        f"{metadata_prefix}_storage_method": "test",
    }


class HierarchicalVisualTests(unittest.TestCase):
    def test_subsystems_are_generated_before_whole_system_render(self) -> None:
        ir = project()
        provider = FakeImageProvider()

        attach_product_image(
            "Build a desk robot",
            ir,
            generate_image=True,
            provider_factory=lambda **_: provider,
            storage_handler=fake_storage,
        )

        self.assertEqual(["system", "system", "project"], [kind for kind, _ in provider.calls])
        visuals = ir.assembly_metadata["system_visuals"]
        self.assertEqual(2, len(visuals))
        self.assertEqual(2, ir.assembly_metadata["system_visual_succeeded_count"])
        self.assertTrue(ir.assembly_metadata["product_image_url"].endswith("product_hero_image.png"))

        lifecycle = load_design_lifecycle(ir)
        system_images = [
            item for item in lifecycle.representations if item.kind == RepresentationKind.SYSTEM_IMAGE
        ]
        system_renders = [
            item for item in lifecycle.representations if item.kind == RepresentationKind.SYSTEM_RENDER
        ]
        self.assertEqual(2, len(system_images))
        self.assertEqual(1, len(system_renders))
        self.assertEqual(
            VisualApprovalStatus.AWAITING_APPROVAL,
            lifecycle.visual_gate.status,
        )
        self.assertEqual(
            system_renders[0].artifact_id,
            ir.assembly_metadata["system_render_artifact_id"],
        )

    def test_system_visuals_are_reused_when_semantics_have_not_changed(self) -> None:
        ir = project()
        provider = FakeImageProvider()
        attach_product_image(
            "Build a desk robot",
            ir,
            generate_image=True,
            provider_factory=lambda **_: provider,
            storage_handler=fake_storage,
        )
        provider.calls.clear()

        attach_product_image(
            "Build a desk robot",
            ir,
            generate_image=True,
            provider_factory=lambda **_: provider,
            storage_handler=fake_storage,
        )

        self.assertEqual(["project"], [kind for kind, _ in provider.calls])
        self.assertTrue(all(item.get("reused") for item in ir.assembly_metadata["system_visuals"]))

    def test_auto_approved_render_resumes_deferred_cad(self) -> None:
        ir = project(policy="auto_approve_visual")
        provider = FakeImageProvider()

        with patch(
            "forma_core.workspaces.projects.cad_generation.ensure_native_cad_model",
            return_value=True,
        ) as ensure_cad:
            attach_product_image(
                "Build a desk robot",
                ir,
                generate_image=True,
                provider_factory=lambda **_: provider,
                storage_handler=fake_storage,
            )

        self.assertEqual(
            VisualApprovalStatus.APPROVED,
            load_design_lifecycle(ir).visual_gate.status,
        )
        ensure_cad.assert_called_once()
        kwargs = ensure_cad.call_args.kwargs
        self.assertEqual("11111111-1111-4111-8111-111111111111", kwargs["project_id"])
        self.assertEqual("forma-generation-worker", kwargs["authoring_agent"])

    def test_regular_mode_keeps_one_shot_visual_flow_even_with_progressive_metadata(self) -> None:
        ir = project(policy="require_approval", mode="regular")
        provider = FakeImageProvider()

        with patch(
            "forma_core.workspaces.projects.cad_generation.ensure_native_cad_model",
            return_value=True,
        ) as ensure_cad:
            attach_product_image(
                "Build a desk robot",
                ir,
                generate_image=True,
                provider_factory=lambda **_: provider,
                storage_handler=fake_storage,
            )

        self.assertEqual(["project"], [kind for kind, _ in provider.calls])
        self.assertNotIn("system_visuals", ir.assembly_metadata)
        self.assertNotIn("system_render_artifact_id", ir.assembly_metadata)
        self.assertNotIn("visual_approval_status", ir.assembly_metadata)
        ensure_cad.assert_not_called()


if __name__ == "__main__":
    unittest.main()
