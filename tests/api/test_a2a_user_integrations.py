from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.api import a2a
from apps.api.auth import UserContext
from forma_core import user_integrations
from forma_core.image_providers import build_image_provider
from forma_core.user_integrations import UserIntegrationStore


class A2AUserIntegrationTests(unittest.TestCase):
    def test_persist_updated_project_ir_skips_legacy_projection_for_authenticated_owner(self) -> None:
        project_id = "fd54de37-2fbb-485a-92e4-8bfaf4a2f08c"

        class FakeIR:
            assembly_metadata = {
                "project_id": project_id,
                "chat_id": "chat_123",
                "source_prompt": "desk lamp",
            }
            overview = SimpleNamespace(title="Low Voltage Desk Lamp")

            def model_dump(self):
                return {
                    "assembly_metadata": self.assembly_metadata,
                    "overview": {"title": self.overview.title},
                    "components": [],
                }

        with patch.object(a2a, "refresh_legacy_project_projection", return_value=False) as update_project, patch.object(
            a2a, "persist_legacy_project_projection"
        ) as save_project:
            a2a._persist_updated_project_ir(
                FakeIR(),
                prompt_text="desk lamp",
                owner_user_id="user_123",
            )

        update_project.assert_not_called()
        save_project.assert_not_called()

    def test_generation_response_applies_owner_image_provider_before_images(self) -> None:
        observed: dict[str, object] = {}

        def fake_generate_project_with_workflow(*_args, **_kwargs):
            self.assertFalse(_kwargs["persist_project"])
            return SimpleNamespace(
                assembly_metadata={},
                constraints=[],
                components=[],
                nets=[],
                is_valid=True,
                overview=SimpleNamespace(title="Test"),
                model_dump=lambda *args, **kwargs: {
                    "assembly_metadata": {},
                    "constraints": [],
                    "components": [],
                    "nets": [],
                    "is_valid": True,
                    "overview": {"title": "Test"},
                },
            )

        def fake_attach_product_image(*_args, **_kwargs):
            provider = build_image_provider(force_enabled=True, settings=_kwargs["settings"])
            observed.update(provider.get_debug_config())

        with tempfile.TemporaryDirectory() as tmpdir:
            user_integrations._APPLIED_ENV_VALUES.clear()
            user_integrations._ORIGINAL_ENV_VALUES.clear()
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration("image", enabled=True, field_values={"provider": "huggingface"})
            store.update_integration(
                "huggingface",
                enabled=True,
                field_values={
                    "api_key": "hf-scoped-token",
                    "image_model": "black-forest-labs/FLUX.1-schnell",
                    "image_inference_provider": "fal-ai",
                },
            )

            with patch.dict(os.environ, {"IMAGE_PROVIDER": "none", "FORMA_AUTH_MODE": "clerk"}, clear=True), patch.object(
                UserIntegrationStore, "for_user", return_value=store
            ), patch.object(a2a, "get_workflow_debug_config", return_value={"runtime": {}}), patch.object(
                a2a, "deployment_runtime_config", return_value={"alpha_generation_gate_active": False}
            ), patch.object(
                a2a, "generate_project_with_workflow", side_effect=fake_generate_project_with_workflow
            ), patch.object(
                a2a, "_attach_product_image", side_effect=fake_attach_product_image
            ), patch.object(
                a2a, "_persist_updated_project_ir"
            ), patch.object(
                a2a, "generate_mermaid_chart", return_value=""
            ), patch.object(
                a2a, "generate_svg_schematic", return_value=""
            ):
                try:
                    a2a.build_generation_response(
                        "test",
                        generate_image=True,
                        owner_user_id="user_123",
                    )
                finally:
                    user_integrations._APPLIED_ENV_VALUES.clear()
                    user_integrations._ORIGINAL_ENV_VALUES.clear()

        self.assertEqual("huggingface", observed["provider"])
        self.assertEqual("black-forest-labs/FLUX.1-schnell", observed["model_name"])
        self.assertEqual("fal-ai", observed["inference_provider"])

    def test_generation_response_reapplies_owner_image_provider_after_workspace_polling_clears_env(self) -> None:
        observed: dict[str, object] = {}

        def fake_generate_project_with_workflow(*_args, **_kwargs):
            self.assertFalse(_kwargs["persist_project"])
            return SimpleNamespace(
                assembly_metadata={},
                constraints=[],
                components=[],
                nets=[],
                is_valid=True,
                overview=SimpleNamespace(title="Test"),
                model_dump=lambda *args, **kwargs: {
                    "assembly_metadata": {},
                    "constraints": [],
                    "components": [],
                    "nets": [],
                    "is_valid": True,
                    "overview": {"title": "Test"},
                },
            )

        def fake_attach_product_image(*_args, **_kwargs):
            provider = build_image_provider(force_enabled=True, settings=_kwargs["settings"])
            observed.update(provider.get_debug_config())

        with tempfile.TemporaryDirectory() as tmpdir:
            user_integrations._APPLIED_ENV_VALUES.clear()
            user_integrations._ORIGINAL_ENV_VALUES.clear()
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration("image", enabled=True, field_values={"provider": "gmi"})
            store.update_integration("gmi", enabled=True, field_values={"api_key": "gmi-secret"})

            with patch.dict(os.environ, {"IMAGE_PROVIDER": "none", "FORMA_AUTH_MODE": "clerk"}, clear=True), patch.object(
                UserIntegrationStore, "for_user", return_value=store
            ), patch.object(a2a, "get_workflow_debug_config", return_value={"runtime": {}}), patch.object(
                a2a, "deployment_runtime_config", return_value={"alpha_generation_gate_active": False}
            ), patch.object(
                a2a, "generate_project_with_workflow", side_effect=fake_generate_project_with_workflow
            ), patch.object(
                a2a, "_attach_product_image", side_effect=fake_attach_product_image
            ), patch.object(
                a2a, "_persist_updated_project_ir"
            ), patch.object(
                a2a, "generate_mermaid_chart", return_value=""
            ), patch.object(
                a2a, "generate_svg_schematic", return_value=""
            ):
                try:
                    a2a.build_generation_response(
                        "test",
                        generate_image=True,
                        owner_user_id="user_123",
                    )
                finally:
                    user_integrations._APPLIED_ENV_VALUES.clear()
                    user_integrations._ORIGINAL_ENV_VALUES.clear()

        self.assertEqual("gmi", observed["provider"])
        self.assertTrue(observed["configured"])
        self.assertEqual("gpt-image-2", observed["model_name"])

    def test_queued_generation_applies_owner_image_provider_before_building(self) -> None:
        observed: dict[str, object] = {}
        env_keys = (
            "IMAGE_PROVIDER",
            "OPENAI_API_KEY",
            "OPENAI_IMAGE_MODEL",
            "HF_TOKEN",
            "HUGGINGFACE_IMAGE_MODEL",
            "HUGGINGFACE_IMAGE_INFERENCE_PROVIDER",
        )

        def fake_build_generation_response(*_args, **_kwargs):
            provider = build_image_provider(force_enabled=True, settings=_kwargs["settings"])
            observed.update(provider.get_debug_config())
            return {"ok": True}

        async def run_to_thread_inline(func, *args, **kwargs):
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            user_integrations._APPLIED_ENV_VALUES.clear()
            user_integrations._ORIGINAL_ENV_VALUES.clear()
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "huggingface",
                field_values={
                    "api_key": "hf-scoped-token",
                    "image_model": "black-forest-labs/FLUX.1-schnell",
                    "image_inference_provider": "fal-ai",
                },
            )
            store.update_integration(
                "image",
                field_values={"provider": "huggingface"},
            )

            with patch.dict(
                os.environ,
                {
                    "FORMA_AUTH_MODE": "clerk",
                    "IMAGE_PROVIDER": "openai",
                    "OPENAI_API_KEY": "sk-invalid-openai",
                    "OPENAI_IMAGE_MODEL": "gpt-image-2",
                },
                clear=True,
            ), patch.object(UserIntegrationStore, "for_user", return_value=store) as for_user, patch.object(
                a2a, "build_generation_response", side_effect=fake_build_generation_response
            ), patch.object(
                a2a.asyncio, "to_thread", side_effect=run_to_thread_inline
            ):
                try:
                    asyncio.run(
                        a2a.call_forma_action(
                            "forma.generate_project",
                            {
                                "prompt": "test",
                                "generate_image": True,
                                "owner_user_id": "attacker-user",
                            },
                            UserContext(
                                provider="test",
                                subject="user_123",
                                owner_user_id="user_123",
                                is_authenticated=True,
                                is_admin=False,
                            ),
                        )
                    )
                finally:
                    for key in env_keys:
                        os.environ.pop(key, None)
                    user_integrations._APPLIED_ENV_VALUES.clear()
                    user_integrations._ORIGINAL_ENV_VALUES.clear()

        for_user.assert_called_once_with("user_123")
        self.assertEqual("huggingface", observed["provider"])
        self.assertEqual("black-forest-labs/FLUX.1-schnell", observed["model_name"])
        self.assertEqual("fal-ai", observed["inference_provider"])


if __name__ == "__main__":
    unittest.main()
