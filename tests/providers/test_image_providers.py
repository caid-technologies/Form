from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from forma_core.image_providers import (
    GeneratedImage,
    GMIImageProvider,
    HuggingFaceImageProvider,
    OpenAIImageProvider,
    TogetherImageProvider,
    VertexAIImageProvider,
    build_image_provider,
    build_project_image_prompt,
    build_project_image_sequence_prompts,
)


class ImageProviderRoutingTests(unittest.TestCase):
    def test_mechanical_render_preserves_geometry_and_does_not_seed_electronics(self) -> None:
        ir = SimpleNamespace(
            overview=SimpleNamespace(title="Rounded gear", description="A plain gear with 36 rounded teeth and a keyed bore."),
            mechanical=SimpleNamespace(physical_form="One flat gear", cad_operations=[{"kind": "cylinder", "radius_mm": 25}],
                mechanism_benchmark={"kind": "spur_gear_pair", "driver_teeth": 36, "driven_teeth": 72},
                fabrication_details=["Bare nylon surfaces; no decorative panels"]),
            components=[], nets=[], constraints=["Strictly mechanical"],
            assembly_metadata={"source_prompt": "Make a plain nylon gear"},
        )
        request = "Use a neutral studio background. " * 12 + "Preserve the keyed bore and rounded tooth profile."
        for prompt in [build_project_image_prompt(request, ir), *[view["prompt"] for view in build_project_image_sequence_prompts(request, ir)]]:
            self.assertIn("36 rounded teeth", prompt)
            self.assertIn('"driver_teeth":36', prompt)
            self.assertIn('"radius_mm":25', prompt)
            self.assertIn("Bare nylon surfaces", prompt)
            self.assertIn("Preserve the keyed bore and rounded tooth profile.", prompt)
            self.assertIn("purely mechanical design must remain purely mechanical", prompt)
            self.assertIn("Do not add circuit boards", prompt)
            self.assertNotIn("maker electronics build", prompt)
            self.assertNotIn("PCB or component carrier", prompt)

    def test_electronic_render_keeps_explicit_parts_without_inventing_additions(self) -> None:
        ir = SimpleNamespace(overview=SimpleNamespace(title="Monitor", description="One board and one display"),
            components=[SimpleNamespace(ref_des="U1", name="ESP32", category="Microcontroller"),
                        SimpleNamespace(ref_des="DS1", name="OLED display", category="Display")])
        prompt = build_project_image_prompt("Show the saved monitor", ir)
        self.assertIn("U1 ESP32", prompt)
        self.assertIn("DS1 OLED display", prompt)
        self.assertIn("unless they are explicitly part of this design", prompt)

    def test_project_image_prompts_keep_measurements_and_text_out_of_rendered_pixels(self) -> None:
        ir = SimpleNamespace(
            overview=SimpleNamespace(title="Pocket monitor", description="A compact sensor monitor"),
            mechanical=SimpleNamespace(
                render_dimensions=SimpleNamespace(x_mm=120, y_mm=70, z_mm=28),
                enclosure_type="3D printed enclosure",
                physical_form="Crescent-shaped open-frame wearable",
            ),
            components=[],
            constraints=[],
            fabrication_notes=[],
            assembly_metadata={},
        )

        product_prompt = build_project_image_prompt("Build a pocket monitor", ir)
        self.assertIn("rendered pixels must contain no text", product_prompt.lower())
        self.assertIn("never print or annotate this in the image", product_prompt.lower())
        self.assertIn("crescent-shaped open-frame wearable", product_prompt.lower())
        self.assertIn("do not default to a rectangular project box", product_prompt.lower())

        sequence = build_project_image_sequence_prompts("Build a pocket monitor", ir)
        self.assertEqual(["case", "inside"], [item["view_id"] for item in sequence])
        self.assertEqual("Product exterior", sequence[0]["label"])
        for item in sequence:
            prompt = item["prompt"].lower()
            self.assertIn("rendered pixels must contain no text", prompt)
            self.assertIn("do not draw dimension lines", prompt)
            self.assertIn("crescent-shaped open-frame wearable", prompt)
            self.assertIn("do not default to a rectangular project box", prompt)
            self.assertNotIn("dimension callouts must", prompt)
        self.assertIn("without inventing a shell", sequence[1]["prompt"].lower())

    def test_openai_image_provider_does_not_inherit_llm_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-openai",
                "LLM_API_KEY": "rpa-runpod",
                "LLM_BASE_URL": "https://api.runpod.ai/v2/example/openai/v1",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, OpenAIImageProvider)
        self.assertEqual("openai", provider.provider_name)
        self.assertEqual("https://api.openai.com/v1", provider.base_url)
        self.assertEqual("sk-openai", provider.api_key)
        self.assertTrue(provider.is_configured)

    def test_explicit_openai_image_provider_requires_openai_key_family(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "openai",
                "LLM_API_KEY": "rpa-runpod",
                "LLM_BASE_URL": "https://api.runpod.ai/v2/example/openai/v1",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, OpenAIImageProvider)
        self.assertEqual("openai", provider.provider_name)
        self.assertEqual("https://api.openai.com/v1", provider.base_url)
        self.assertIsNone(provider.api_key)
        self.assertFalse(provider.is_configured)

    def test_openai_compatible_image_provider_can_use_llm_routing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "openai-compatible",
                "LLM_API_KEY": "local-compatible-key",
                "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, OpenAIImageProvider)
        self.assertEqual("openai-compatible", provider.provider_name)
        self.assertEqual("http://127.0.0.1:11434/v1", provider.base_url)
        self.assertEqual("local-compatible-key", provider.api_key)
        self.assertTrue(provider.is_configured)

    def test_vertex_nano_banana_uses_project_adc_and_native_image_response(self) -> None:
        inline_data = SimpleNamespace(data=b"fake-nano-banana", mime_type="image/png")
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=inline_data)]))]
        )
        client = Mock()
        client.models.generate_content.return_value = response
        fake_genai = SimpleNamespace(Client=Mock(return_value=client))

        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "vertex",
                "GOOGLE_CLOUD_PROJECT": "forma-image-test",
                "GOOGLE_CLOUD_LOCATION": "global",
                "VERTEX_AI_IMAGE_MODEL": "gemini-3.1-flash-image",
                "VERTEX_AI_IMAGE_RESOLUTION": "2K",
                "VERTEX_AI_IMAGE_ASPECT_RATIO": "16:9",
            },
            clear=True,
        ), patch("forma_core.image_providers.genai", fake_genai):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, VertexAIImageProvider)
            image = provider.generate_test_image("Render a compact environmental monitor")

        client_config = fake_genai.Client.call_args.kwargs
        self.assertTrue(client_config.pop("vertexai"))
        self.assertEqual("forma-image-test", client_config.pop("project"))
        self.assertEqual("global", client_config.pop("location"))
        self.assertEqual(120_000, client_config.pop("http_options").timeout)
        self.assertEqual({}, client_config)
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual("gemini-3.1-flash-image", request["model"])
        self.assertEqual("Render a compact environmental monitor", request["contents"])
        self.assertEqual(["TEXT", "IMAGE"], request["config"].response_modalities)
        self.assertEqual("2K", request["config"].image_config.image_size)
        self.assertEqual("16:9", request["config"].image_config.aspect_ratio)
        self.assertEqual("data:image/png;base64,ZmFrZS1uYW5vLWJhbmFuYQ==", image.data_url)
        self.assertEqual("vertex", image.provider)
        self.assertEqual("gemini-3.1-flash-image", image.model)

    def test_vertex_image_provider_auto_routes_from_google_cloud_project(self) -> None:
        fake_genai = SimpleNamespace(Client=Mock(return_value=Mock()))
        with patch.dict(
            os.environ,
            {"GOOGLE_CLOUD_PROJECT": "forma-image-test"},
            clear=True,
        ), patch("forma_core.image_providers.genai", fake_genai):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, VertexAIImageProvider)
        self.assertEqual("gemini-3.1-flash-image", provider.model_name)
        self.assertTrue(provider.is_configured)

    def test_vertex_image_provider_uses_vercel_workload_identity_credentials(self) -> None:
        credentials = object()
        fake_genai = SimpleNamespace(Client=Mock(return_value=Mock()))
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "vertex",
                "GOOGLE_CLOUD_PROJECT": "forma-image-test",
            },
            clear=True,
        ), patch("forma_core.image_providers.genai", fake_genai), patch(
            "forma_core.image_providers.build_vertex_credentials",
            return_value=credentials,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertTrue(provider.is_configured)
        self.assertIs(credentials, fake_genai.Client.call_args.kwargs["credentials"])

    def test_gmi_image_provider_routes_from_image_provider(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "gmi",
                "GMI_API_KEY": "gmi-secret",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, GMIImageProvider)
        self.assertEqual("gmi", provider.provider_name)
        self.assertEqual("https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/requests/v1", provider.base_url)
        self.assertEqual("gmi-secret", provider.api_key)
        self.assertEqual("gpt-image-2", provider.model_name)
        self.assertEqual("1024x1024", provider.size)
        self.assertEqual("medium", provider.quality)
        self.assertEqual("png", provider.output_format)
        self.assertTrue(provider.is_configured)

    def test_gmi_image_provider_auto_routes_from_api_key_only(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GMI_API_KEY": "gmi-secret",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, GMIImageProvider)
        self.assertEqual("gmi", provider.provider_name)
        self.assertEqual("gpt-image-2", provider.model_name)
        self.assertTrue(provider.is_configured)

    def test_gmi_native_gpt_image_uses_documented_fields(self) -> None:
        calls = []
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "gmi",
                "GMI_API_KEY": "gmi-secret",
                "GMI_IMAGE_MODEL": "gpt-image-2",
                "GMI_IMAGE_SIZE": "1536x1024",
                "GMI_IMAGE_QUALITY": "high",
                "GMI_IMAGE_OUTPUT_FORMAT": "jpeg",
                "GMI_IMAGE_OUTPUT_COMPRESSION": "82",
                "GMI_IMAGE_BACKGROUND": "opaque",
                "GMI_IMAGE_MODERATION": "low",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, GMIImageProvider)

            def fake_request(path: str, method: str = "GET", payload=None):
                calls.append({"path": path, "method": method, "payload": payload})
                return {"data": [{"b64_json": "ZmFrZS1pbWFnZQ=="}]}

            with patch.object(provider, "_request_json", side_effect=fake_request):
                image = provider._generate_image_from_prompt(
                    "render a hardware enclosure",
                    view_id="case",
                    label="Case",
                    reference_view_id=None,
                )

        self.assertEqual("gmi", image.provider)
        self.assertEqual("gpt-image-2", image.model)
        self.assertEqual(
            {
                "model": "gpt-image-2",
                "prompt": "render a hardware enclosure",
                "size": "1536x1024",
                "quality": "high",
                "output_format": "jpeg",
                "background": "opaque",
                "moderation": "low",
                "n": 1,
                "output_compression": 82,
            },
            calls[0]["payload"],
        )

    def test_direct_image_model_test_bypasses_project_sequence(self) -> None:
        with patch.dict(
            os.environ,
            {"IMAGE_PROVIDER": "gmi", "GMI_API_KEY": "gmi-secret", "GMI_IMAGE_MODEL": "gpt-image-2"},
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, GMIImageProvider)
            expected = GeneratedImage(
                data_url="data:image/png;base64,ZmFrZQ==",
                provider="gmi",
                model="gpt-image-2",
                size="1024x1024",
                prompt="test prompt",
            )
            with patch.object(provider, "_generate_image_from_prompt", return_value=expected) as generate:
                result = provider.generate_test_image("test prompt")

        self.assertIs(expected, result)
        generate.assert_called_once_with(
            "test prompt",
            view_id="image-model-test",
            label="Image model test",
            reference_view_id=None,
        )

    def test_gmi_gemini_image_uses_request_queue_and_model_specific_fields(self) -> None:
        calls = []
        responses = [
            {"request_id": "request-123", "status": "queued"},
            {
                "request_id": "request-123",
                "status": "success",
                "outcome": {"media_urls": [{"id": "0", "url": "https://storage.example/gmi-image.png"}]},
            },
        ]
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "gmi",
                "GMI_API_KEY": "gmi-secret",
                "GMI_IMAGE_MODEL": "gemini-3.1-flash-image-preview",
                "GMI_IMAGE_RESOLUTION": "2K",
                "GMI_IMAGE_ASPECT_RATIO": "16:9",
                "GMI_IMAGE_OUTPUT_FORMAT": "jpeg",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, GMIImageProvider)

            def fake_queue_request(*, method: str, request_id=None, payload=None):
                calls.append({"method": method, "request_id": request_id, "payload": payload})
                return responses.pop(0)

            with patch.object(provider, "_request_queue_json", side_effect=fake_queue_request), patch(
                "forma_core.image_providers.time.sleep"
            ):
                image = provider._generate_image_from_prompt(
                    "render a hardware enclosure",
                    view_id="case",
                    label="Case",
                    reference_view_id=None,
                )

        self.assertEqual("https://storage.example/gmi-image.png", image.data_url)
        self.assertEqual("2K 16:9", image.size)
        self.assertEqual(
            {
                "method": "POST",
                "request_id": None,
                "payload": {
                    "model": "gemini-3.1-flash-image-preview",
                    "payload": {
                        "prompt": "render a hardware enclosure",
                        "image_size": "2K",
                        "aspect_ratio": "16:9",
                        "image_output_format": "jpeg",
                    },
                },
            },
            calls[0],
        )
        self.assertEqual({"method": "GET", "request_id": "request-123", "payload": None}, calls[1])

    def test_gmi_queue_payloads_match_documented_model_schemas(self) -> None:
        cases = [
            (
                {
                    "GMI_IMAGE_MODEL": "gpt-image-1.5",
                    "GMI_IMAGE_SIZE": "1536x1024",
                    "GMI_IMAGE_QUALITY": "high",
                    "GMI_IMAGE_OUTPUT_FORMAT": "webp",
                    "GMI_IMAGE_OUTPUT_COMPRESSION": "75",
                    "GMI_IMAGE_BACKGROUND": "transparent",
                },
                {
                    "prompt": "render a hardware enclosure",
                    "size": "1536x1024",
                    "quality": "high",
                    "output_format": "webp",
                    "background": "transparent",
                    "n": 1,
                    "output_compression": 75,
                },
            ),
            (
                {
                    "GMI_IMAGE_MODEL": "flux-kontext-pro",
                    "GMI_IMAGE_ASPECT_RATIO": "7:3",
                    "GMI_IMAGE_SEED": "123",
                    "GMI_IMAGE_PROMPT_UPSAMPLING": "true",
                    "GMI_IMAGE_SAFETY_TOLERANCE": "5",
                    "GMI_IMAGE_OUTPUT_FORMAT": "jpeg",
                },
                {
                    "prompt": "render a hardware enclosure",
                    "aspect_ratio": "7:3",
                    "seed": 123,
                    "prompt_upsampling": True,
                    "safety_tolerance": 5,
                    "output_format": "jpeg",
                },
            ),
            (
                {
                    "GMI_IMAGE_MODEL": "seedream-5.0-lite",
                    "GMI_IMAGE_SIZE": "3K",
                    "GMI_IMAGE_OUTPUT_FORMAT": "png",
                    "GMI_IMAGE_SEQUENTIAL_GENERATION": "auto",
                    "GMI_IMAGE_WATERMARK": "true",
                },
                {
                    "prompt": "render a hardware enclosure",
                    "size": "3K",
                    "output_format": "png",
                    "max_images": 1,
                    "sequential_image_generation": "auto",
                    "watermark": True,
                },
            ),
            (
                {"GMI_IMAGE_MODEL": "seedream-5.0-pro"},
                {
                    "prompt": "render a hardware enclosure",
                    "size": "2048x2048",
                    "output_format": "jpeg",
                    "max_images": 1,
                    "sequential_image_generation": "disabled",
                    "watermark": False,
                },
            ),
            (
                {"GMI_IMAGE_MODEL": "wan2.7-image"},
                {"text": "render a hardware enclosure", "size": "2K", "n": 1},
            ),
            (
                {"GMI_IMAGE_MODEL": "wan2.7-image-pro", "GMI_IMAGE_RESOLUTION": "4K"},
                {"text": "render a hardware enclosure", "size": "4K", "n": 1},
            ),
        ]

        for model_env, expected_payload in cases:
            with self.subTest(model=model_env["GMI_IMAGE_MODEL"]), patch.dict(
                os.environ,
                {"IMAGE_PROVIDER": "gmi", "GMI_API_KEY": "gmi-secret", **model_env},
                clear=True,
            ):
                provider = build_image_provider(force_enabled=True)
                assert isinstance(provider, GMIImageProvider)
                self.assertEqual(expected_payload, provider._queue_payload("render a hardware enclosure"))

                if provider.model_name == "flux-kontext-pro":
                    self.assertEqual(2000, provider.prompt_max_chars)
                if provider.model_name == "seedream-5.0-lite":
                    self.assertEqual("3K", provider.size)
                if provider.model_name == "seedream-5.0-pro":
                    self.assertEqual("2048x2048", provider.size)
                    self.assertEqual("jpeg", provider.output_format)

    def test_together_image_provider_routes_from_image_provider(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "together-ai",
                "TOGETHER_API_KEY": "together-secret",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, TogetherImageProvider)
        self.assertEqual("together", provider.provider_name)
        self.assertEqual("https://api.together.ai/v1", provider.base_url)
        self.assertEqual("together-secret", provider.api_key)
        self.assertEqual("openai/gpt-image-2", provider.model_name)
        self.assertEqual("1024x1024", provider.size)
        self.assertEqual(0, provider.num_inference_steps)
        self.assertEqual("Forma-OSS/1.0", provider._headers()["User-Agent"])
        self.assertEqual("application/json", provider._headers()["Accept"])
        self.assertTrue(provider.is_configured)

    def test_together_image_provider_auto_routes_from_api_key_only(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TOGETHER_API_KEY": "together-secret",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, TogetherImageProvider)
        self.assertEqual("together", provider.provider_name)
        self.assertEqual("openai/gpt-image-2", provider.model_name)
        self.assertTrue(provider.is_configured)

    def test_together_default_gpt_image_generation_uses_minimal_payload(self) -> None:
        calls = []
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "together",
                "TOGETHER_API_KEY": "together-secret",
                "TOGETHER_IMAGE_SIZE": "768x512",
                "TOGETHER_IMAGE_STEPS": "8",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, TogetherImageProvider)

            def fake_request(path: str, method: str = "GET", payload=None):
                calls.append({"path": path, "method": method, "payload": payload})
                return {"data": [{"b64_json": "ZmFrZS1pbWFnZQ=="}]}

            with patch.object(provider, "_request_json", side_effect=fake_request):
                image = provider._generate_image_from_prompt(
                    "render a hardware enclosure",
                    view_id="case",
                    label="Case",
                    reference_view_id=None,
                )

        self.assertEqual("together", image.provider)
        self.assertEqual("openai/gpt-image-2", image.model)
        self.assertTrue(image.data_url.startswith("data:image/png;base64,"))
        self.assertEqual(
            {
                "model": "openai/gpt-image-2",
                "prompt": "render a hardware enclosure",
            },
            calls[0]["payload"],
        )

    def test_together_image_generation_sends_dimensions_and_steps(self) -> None:
        calls = []
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "together",
                "TOGETHER_API_KEY": "together-secret",
                "TOGETHER_IMAGE_MODEL": "black-forest-labs/FLUX.1.1-pro",
                "TOGETHER_IMAGE_SIZE": "768x512",
                "TOGETHER_IMAGE_STEPS": "8",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, TogetherImageProvider)

            def fake_request(path: str, method: str = "GET", payload=None):
                calls.append({"path": path, "method": method, "payload": payload})
                return {"data": [{"b64_json": "ZmFrZS1pbWFnZQ=="}]}

            with patch.object(provider, "_request_json", side_effect=fake_request):
                image = provider._generate_image_from_prompt(
                    "render a hardware enclosure",
                    view_id="case",
                    label="Case",
                    reference_view_id=None,
                )

        self.assertEqual("together", image.provider)
        self.assertEqual("black-forest-labs/FLUX.1.1-pro", image.model)
        self.assertTrue(image.data_url.startswith("data:image/png;base64,"))
        self.assertEqual("images/generations", calls[0]["path"])
        self.assertEqual("POST", calls[0]["method"])
        self.assertEqual(
            {
                "model": "black-forest-labs/FLUX.1.1-pro",
                "prompt": "render a hardware enclosure",
                "n": 1,
                "response_format": "base64",
                "output_format": "png",
                "width": 768,
                "height": 512,
                "steps": 8,
            },
            calls[0]["payload"],
        )

    def test_huggingface_image_provider_routes_from_image_provider(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "huggingface",
                "HF_TOKEN": "hf_scoped",
                "HUGGINGFACE_IMAGE_MODEL": "black-forest-labs/FLUX.1-schnell",
                "HUGGINGFACE_IMAGE_INFERENCE_PROVIDER": "fal-ai",
                "HUGGINGFACE_IMAGE_MODEL_LICENSE": "apache-2.0",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, HuggingFaceImageProvider)
        self.assertEqual("huggingface", provider.provider_name)
        self.assertEqual("black-forest-labs/FLUX.1-schnell", provider.model_name)
        self.assertEqual("fal-ai", provider.inference_provider)
        self.assertEqual("apache-2.0", provider.model_license)
        self.assertEqual(6000, provider.prompt_max_chars)
        self.assertEqual(4000, provider.prompt_target_chars)
        self.assertTrue(provider.is_configured)

    def test_huggingface_gated_image_models_are_disabled_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "hf",
                "HF_TOKEN": "hf_scoped",
                "HUGGINGFACE_IMAGE_MODEL": "black-forest-labs/FLUX.1-dev",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)

        self.assertIsInstance(provider, HuggingFaceImageProvider)
        self.assertFalse(provider.is_configured)
        self.assertIn("gated", provider.get_debug_config()["reason"].lower())

    def test_huggingface_image_generation_records_policy_metadata(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def text_to_image(self, prompt: str, model: str, **parameters):
                self.calls.append({"prompt": prompt, "model": model, "parameters": parameters})
                return b"fake-image-bytes"

        fake_client = FakeClient()
        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "huggingface",
                "HF_TOKEN": "hf_scoped",
                "HUGGINGFACE_IMAGE_MODEL": "Qwen/Qwen-Image",
                "HUGGINGFACE_IMAGE_INFERENCE_PROVIDER": "fal-ai",
                "HUGGINGFACE_IMAGE_MODEL_REVISION": "abc123",
                "HUGGINGFACE_IMAGE_MODEL_LICENSE": "apache-2.0",
                "HUGGINGFACE_IMAGE_SIZE": "768x512",
                "HUGGINGFACE_IMAGE_STEPS": "12",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, HuggingFaceImageProvider)
            with patch.object(provider, "_client", return_value=fake_client):
                image = provider._generate_image_from_prompt(
                    "render a hardware enclosure",
                    view_id="case",
                    label="Case",
                    reference_view_id=None,
                )

        self.assertEqual("huggingface", image.provider)
        self.assertEqual("Qwen/Qwen-Image", image.model)
        self.assertEqual("abc123", image.model_revision)
        self.assertEqual("fal-ai", image.inference_provider)
        self.assertEqual("apache-2.0", image.model_license)
        self.assertTrue(image.data_url.startswith("data:image/png;base64,"))
        self.assertEqual(768, fake_client.calls[0]["parameters"]["width"])
        self.assertEqual(512, fake_client.calls[0]["parameters"]["height"])
        self.assertEqual(12, fake_client.calls[0]["parameters"]["num_inference_steps"])

    def test_huggingface_image_generation_accepts_pillow_images(self) -> None:
        from PIL import Image

        class FakeClient:
            def text_to_image(self, prompt: str, model: str, **parameters):
                return Image.new("RGB", (16, 12), color=(25, 140, 210))

        with patch.dict(
            os.environ,
            {
                "IMAGE_PROVIDER": "huggingface",
                "HF_TOKEN": "hf_scoped",
                "HUGGINGFACE_IMAGE_MODEL": "black-forest-labs/FLUX.1-schnell",
                "HUGGINGFACE_IMAGE_INFERENCE_PROVIDER": "fal-ai",
                "HUGGINGFACE_IMAGE_SIZE": "16x12",
            },
            clear=True,
        ):
            provider = build_image_provider(force_enabled=True)
            assert isinstance(provider, HuggingFaceImageProvider)
            with patch.object(provider, "_client", return_value=FakeClient()):
                image = provider._generate_image_from_prompt(
                    "render a hardware enclosure",
                    view_id="case",
                    label="Case",
                    reference_view_id=None,
                )

        self.assertEqual("huggingface", image.provider)
        self.assertEqual("black-forest-labs/FLUX.1-schnell", image.model)
        self.assertTrue(image.data_url.startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
