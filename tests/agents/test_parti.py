from __future__ import annotations

import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from forma_core.agents.orchestrator import HardwarePipelineOrchestrator, _parti_seed_quality_issue
from forma_core.config.parti import DEFAULT_PARTI_MODEL_ID, LEGACY_PARTI_MODEL_ID, resolve_parti_model
from forma_core.llm import LLMProviderInputError, LLMProviderOutputError, model_image_input_support
from forma_core.llm_providers import OpenAICompatibleProvider, resolve_llm_runtime_config


SEED = {"project_title": "Desk Climate Monitor", "summary": "A low-voltage desktop temperature display."}


class PartiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {
            "FORMA_DEV_MODE": "true",
            "FORMA_DEPLOYMENT": "false",
            "LLM_PROVIDER": "runpod",
            "RUNPOD_API_KEY": "offline-test-key",
            "RUNPOD_OPENAI_BASE_URL": "https://runpod.invalid/v1",
            "FORMA_STRICT_GENERATION": "true",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def pipeline(self, model: str = DEFAULT_PARTI_MODEL_ID) -> HardwarePipelineOrchestrator:
        pipeline = HardwarePipelineOrchestrator.__new__(HardwarePipelineOrchestrator)
        pipeline.llm_provider = OpenAICompatibleProvider(provider_name="runpod", model_name=model)
        pipeline.llm_provider._request_json = Mock(return_value={
            "choices": [{"message": {"content": json.dumps(SEED)}}]
        })
        pipeline.validate_configured_model = Mock(return_value=SimpleNamespace(
            provider="runpod", actual_model=model, requested_model=model, fallback_active=False,
        ))
        pipeline.use_simulation = False
        pipeline.model_name = model
        pipeline.runtime_config = resolve_llm_runtime_config("runpod", model)
        pipeline.save_project_to_db = Mock()
        return pipeline

    def test_normal_runpod_selection_and_optional_served_alias(self) -> None:
        for variables, expected in (
            ({"RUNPOD_MODEL": DEFAULT_PARTI_MODEL_ID}, DEFAULT_PARTI_MODEL_ID),
            ({"RUNPOD_PARTI_MODEL": "company/parti-v2"}, "company/parti-v2"),
            ({"RUNPOD_PARTI_MODEL": "company/parti-v2", "RUNPOD_MODEL": "unrelated"}, "unrelated"),
        ):
            with self.subTest(variables=variables), patch.dict(os.environ, variables):
                runtime = resolve_llm_runtime_config()
                provider = OpenAICompatibleProvider(provider_name="runpod")
                self.assertEqual(expected, runtime.model)
                self.assertEqual(expected, provider.model_name)

    def test_model_family_does_not_capture_unrelated_providers_or_models(self) -> None:
        self.assertIsNone(resolve_parti_model("runpod", "other/model"))
        self.assertIsNone(resolve_parti_model("runpod-serverless", DEFAULT_PARTI_MODEL_ID))
        self.assertIsNone(resolve_parti_model("openai", DEFAULT_PARTI_MODEL_ID))
        self.assertTrue(resolve_parti_model("runpod", DEFAULT_PARTI_MODEL_ID).supports_images)
        self.assertFalse(resolve_parti_model("runpod", LEGACY_PARTI_MODEL_ID).supports_images)
        with patch.dict(os.environ, {"RUNPOD_PARTI_MODEL": "company/checkpoint-v2"}):
            self.assertTrue(model_image_input_support("runpod", "company/checkpoint-v2"))
            self.assertIsNotNone(resolve_parti_model("runpod", DEFAULT_PARTI_MODEL_ID))

    def test_vision_and_legacy_models_route_through_the_shared_adapter(self) -> None:
        for model in (DEFAULT_PARTI_MODEL_ID, LEGACY_PARTI_MODEL_ID):
            with self.subTest(model=model):
                pipeline = self.pipeline(model)
                pipeline._generate_parti_project = Mock(return_value="parti-result")
                pipeline._generate_staged_project = Mock(side_effect=AssertionError("wrong pipeline"))
                self.assertEqual("parti-result", pipeline.generate_project("A desk climate monitor"))
                pipeline._generate_parti_project.assert_called_once()
                pipeline._generate_staged_project.assert_not_called()

    def test_unrelated_runpod_model_keeps_normal_staged_generation(self) -> None:
        pipeline = self.pipeline("other/model")
        pipeline._generate_staged_project = Mock(return_value="staged-result")
        pipeline._generate_parti_project = Mock(side_effect=AssertionError("wrong pipeline"))
        self.assertEqual("staged-result", pipeline.generate_project("A desk climate monitor"))
        pipeline._generate_parti_project.assert_not_called()

    def test_vision_seed_sends_original_image_and_parses_output(self) -> None:
        pipeline = self.pipeline()
        image = b"reference-image-fixture"
        seed, error = pipeline._request_parti_seed("Use this sketch", image, "image/png")
        self.assertEqual(SEED, seed)
        self.assertIsNone(error)
        request = pipeline.llm_provider._request_json.call_args
        self.assertEqual(("chat/completions",), request.args)
        payload = request.kwargs["payload"]
        self.assertEqual(DEFAULT_PARTI_MODEL_ID, payload["model"])
        content = payload["messages"][0]["content"]
        self.assertIn("Use this sketch", content[0]["text"])
        self.assertEqual("image_url", content[1]["type"])
        self.assertEqual("data:image/png;base64," + base64.b64encode(image).decode(), content[1]["image_url"]["url"])

    def test_text_only_seed_and_legacy_parser_remain_supported(self) -> None:
        for model in (DEFAULT_PARTI_MODEL_ID, LEGACY_PARTI_MODEL_ID):
            with self.subTest(model=model):
                pipeline = self.pipeline(model)
                pipeline.llm_provider._request_json.return_value["choices"][0]["message"]["content"] = (
                    "```json\n" + json.dumps(SEED) + "\n```"
                )
                self.assertEqual((SEED, None), pipeline._request_parti_seed("A desk climate monitor"))
                content = pipeline.llm_provider._request_json.call_args.kwargs["payload"]["messages"][0]["content"]
                self.assertIsInstance(content, str)

    def test_legacy_image_is_rejected_before_request_instead_of_ignored(self) -> None:
        pipeline = self.pipeline(LEGACY_PARTI_MODEL_ID)
        self.assertFalse(model_image_input_support("runpod", LEGACY_PARTI_MODEL_ID))
        with self.assertRaisesRegex(LLMProviderInputError, LEGACY_PARTI_MODEL_ID):
            pipeline.generate_project("A desk climate monitor", b"image", "image/png")
        pipeline.llm_provider._request_json.assert_not_called()

    def test_unsupported_mime_type_is_rejected_before_request(self) -> None:
        pipeline = self.pipeline()
        with self.assertRaisesRegex(LLMProviderInputError, "unsupported image MIME"):
            pipeline._request_parti_seed("A desk monitor", b"document", "application/pdf")
        pipeline.llm_provider._request_json.assert_not_called()

    def test_failed_seed_includes_model_and_restores_timeout(self) -> None:
        pipeline = self.pipeline()
        original = pipeline.llm_provider.timeout_seconds
        pipeline.llm_provider._request_json.side_effect = TimeoutError("request timed out")
        with patch.dict(os.environ, {"RUNPOD_PARTI_SEED_TIMEOUT_SECONDS": "7"}):
            seed, error = pipeline._request_parti_seed("A desk monitor")
        self.assertIsNone(seed)
        self.assertIn(DEFAULT_PARTI_MODEL_ID, error)
        self.assertEqual(original, pipeline.llm_provider.timeout_seconds)

    def test_malformed_or_missing_seed_content_reports_selected_model(self) -> None:
        for response in ({}, {"choices": [{"message": {"content": "{truncated"}}]}):
            with self.subTest(response=response):
                pipeline = self.pipeline()
                pipeline.llm_provider._request_json.return_value = response
                seed, error = pipeline._request_parti_seed("A desk monitor")
                self.assertIsNone(seed)
                self.assertIn(DEFAULT_PARTI_MODEL_ID, error)

    def test_strict_quality_failures_do_not_fall_back_and_identify_model(self) -> None:
        seeds = (
            None,
            {"summary": "No title"},
            {**SEED, "synthetic": True},
            {**SEED, "project_title": "Sensor"},
            {**SEED, "summary": "unknown"},
        )
        for model in (DEFAULT_PARTI_MODEL_ID, LEGACY_PARTI_MODEL_ID):
            for seed in seeds:
                with self.subTest(model=model, seed=seed):
                    pipeline = self.pipeline(model)
                    pipeline._request_parti_seed = Mock(return_value=(seed, None))
                    with self.assertRaisesRegex(LLMProviderOutputError, model):
                        pipeline.generate_project("A desk monitor")

    def test_valid_seed_passes_existing_quality_checks(self) -> None:
        self.assertIsNone(_parti_seed_quality_issue(SEED, SEED["project_title"]))

    def test_seed_generates_project_and_records_actual_model(self) -> None:
        for model, adapter in ((DEFAULT_PARTI_MODEL_ID, "parti-seed-v1"), (LEGACY_PARTI_MODEL_ID, "parti-base-v1")):
            with self.subTest(model=model), patch(
                "forma_core.agents.orchestrator.get_component_template_by_part_number", return_value=None,
            ):
                pipeline = self.pipeline(model)
                project = pipeline.generate_project("A low-voltage desk climate monitor")
                self.assertEqual(SEED["project_title"], project.overview.title)
                self.assertEqual(SEED, project.assembly_metadata["parti_seed"])
                self.assertEqual(model, project.assembly_metadata["parti_model"])
                self.assertEqual(adapter, project.assembly_metadata["parti_adapter"])
                self.assertTrue(project.components)
                pipeline.save_project_to_db.assert_called_once_with("A low-voltage desk climate monitor", project)


if __name__ == "__main__":
    unittest.main()
