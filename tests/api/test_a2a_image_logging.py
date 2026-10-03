from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apps.api import a2a
from apps.api.storage import StoredImage
from forma_core.image_providers import GeneratedImage


class A2AImageLoggingTests(unittest.TestCase):
    def test_stored_image_logging_uses_storage_method(self) -> None:
        ir = SimpleNamespace(assembly_metadata={"project_id": "project_123"})
        stored = StoredImage(
            bucket="contents",
            key="project_123/product.png",
            url="https://example.test/product.png",
            s3_endpoint="https://example.test/storage/v1/s3",
            storage_method="supabase-client",
            content_type="image/png",
            size_bytes=4,
        )

        with patch.object(a2a, "upload_image_to_supabase_s3", return_value=stored), self.assertLogs(
            "apps.api.a2a", level="INFO"
        ) as logs:
            metadata = a2a._attach_stored_image_metadata(
                ir,
                image_data="data:image/png;base64,ZmFrZQ==",
                metadata_prefix="product_case_image",
                object_prefix="product-case",
            )

        self.assertEqual("supabase-client", metadata["product_case_image_storage_method"])
        self.assertIn("method=supabase-client", "\n".join(logs.output))

    def test_image_generation_logs_view_and_storage_outcome(self) -> None:
        ir = SimpleNamespace(assembly_metadata={"project_id": "project_123"})
        image = GeneratedImage(
            data_url="data:image/png;base64,ZmFrZQ==",
            provider="huggingface",
            model="black-forest-labs/FLUX.1-schnell",
            size="1024x1024",
            prompt="render case",
            output_format="png",
            view_id="case",
            label="Case",
            prompt_final_length=11,
            prompt_compacted=False,
        )

        class FakeProvider:
            def get_debug_config(self):
                return {
                    "provider": "huggingface",
                    "model_name": "black-forest-labs/FLUX.1-schnell",
                    "enabled": True,
                    "configured": True,
                    "size": "1024x1024",
                    "reason": None,
                }

            def generate_project_image_sequence(self, prompt_text, ir):
                return [image]

        with patch.object(a2a, "build_image_provider", return_value=FakeProvider()), patch.object(
            a2a, "_attach_stored_image_metadata", return_value={"product_case_image_storage_enabled": False}
        ), self.assertLogs("apps.api.a2a", level="INFO") as logs:
            a2a._attach_product_image("test prompt", ir, generate_image=True)

        output = "\n".join(logs.output)
        self.assertIn("Image generation operation starting", output)
        self.assertIn("Image generation provider returned", output)
        self.assertIn("Image generation view ready", output)
        self.assertIn("Image generation view kept inline", output)
        self.assertIn("Image generation operation completed", output)

    def test_image_generation_failure_logs_stack_trace(self) -> None:
        ir = SimpleNamespace(assembly_metadata={"project_id": "project_123"})

        class FakeProvider:
            def get_debug_config(self):
                return {
                    "provider": "huggingface",
                    "model_name": "broken-model",
                    "enabled": True,
                    "configured": True,
                    "size": "1024x1024",
                    "reason": None,
                }

            def generate_project_image_sequence(self, prompt_text, ir):
                raise RuntimeError("provider exploded")

        with patch.object(a2a, "build_image_provider", return_value=FakeProvider()), self.assertLogs(
            "apps.api.a2a", level="WARNING"
        ) as logs:
            a2a._attach_product_image("test prompt", ir, generate_image=True)

        output = "\n".join(logs.output)
        self.assertIn("Image generation operation failed", output)
        self.assertIn("RuntimeError: provider exploded", output)

    def test_unconfigured_image_provider_records_debug_details(self) -> None:
        ir = SimpleNamespace(assembly_metadata={"project_id": "project_123"})

        class FakeProvider:
            def get_debug_config(self):
                return {
                    "provider": "none",
                    "model_name": "none",
                    "enabled": True,
                    "configured": False,
                    "reason": "Image provider API key is missing.",
                    "base_url": "https://api.example.test/v1",
                }

            def generate_project_image_sequence(self, prompt_text, ir):
                raise AssertionError("provider should not be called")

        with patch.object(a2a, "build_image_provider", return_value=FakeProvider()), self.assertLogs(
            "apps.api.a2a", level="WARNING"
        ) as logs:
            a2a._attach_product_image("test prompt", ir, generate_image=True)

        metadata = ir.assembly_metadata
        operation = next(item for item in metadata["operation_statuses"] if item["id"] == "image_generation")
        self.assertEqual("failed", metadata["image_output_status"])
        self.assertEqual("configuration", metadata["image_output_error_type"])
        self.assertEqual("Image generation could not be completed.", metadata["image_output_error"])
        self.assertRegex(metadata["image_output_error_correlation_id"], r"^err_[0-9a-f]{32}$")
        self.assertEqual("none", metadata["image_output_debug"]["provider"])
        self.assertEqual("https://api.example.test/v1", operation["details"]["image_output_debug"]["base_url"])
        self.assertIn("code=image_generation_failed", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
