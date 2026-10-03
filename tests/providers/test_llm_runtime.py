from __future__ import annotations

import os
import copy
import json
import unittest
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from forma_core.agents.web_research_workflow import WebResearchHardwarePipeline
from forma_core.llm import (
    LLMProviderConfigError,
    LLMProviderInputError,
    LLMProviderOutputError,
    LLMProviderPreflightError,
    LLMProviderValidation,
    build_llm_provider,
    enforce_production_llm_preflight,
    model_image_input_support,
    resolve_llm_runtime_config,
)
from forma_core.llm_providers import OpenAICompatibleProvider
from forma_core.workspaces.projects.models import ProjectOverview
from forma_core.selectors import parse_llm_selector, split_llm_selector


LLM_ENV_KEYS = {
    "RUNPOD_PARTI_MODEL",
    "ALLOWED_LLM_MODELS",
    "ALLOWED_LLM_PROVIDERS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_JSON_SCHEMA_OUTPUT",
    "ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_TEMPERATURE",
    "ANTHROPIC_VALIDATE_MODELS",
    "ANTHROPIC_VERSION",
    "ALLOWED_OPENAI_MODELS",
    "ALLOWED_RUNPOD_MODELS",
    "BASETEN_ALLOWED_MODELS",
    "BASETEN_API_KEY",
    "BASETEN_BASE_URL",
    "BASETEN_MODEL",
    "FORMA_DEPLOYMENT",
    "FORMA_DEPLOYMENT_MODE",
    "FORMA_DEV_MODE",
    "CLAUDE_API_KEY",
    "CLAUDE_API_VERSION",
    "CLAUDE_BASE_URL",
    "CLAUDE_JSON_SCHEMA_OUTPUT",
    "CLAUDE_MAX_TOKENS",
    "CLAUDE_MODEL",
    "CLAUDE_TEMPERATURE",
    "CLAUDE_VALIDATE_MODELS",
    "DEPLOYMENT",
    "DEPLOYMENT_MODE",
    "NEXT_PUBLIC_FORMA_DEPLOYMENT",
    "GEMINI_ALLOWED_MODELS",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GCLOUD_PROJECT",
    "GMI_ALLOWED_MODELS",
    "GMI_API_KEY",
    "GMI_BASE_URL",
    "GMI_CLOUD_API_KEY",
    "GMI_CLOUD_BASE_URL",
    "GMI_CLOUD_FALLBACK_MODEL",
    "GMI_CLOUD_MODEL",
    "GMI_CLOUD_RESPONSE_FORMAT",
    "GMI_CLOUD_TEMPERATURE",
    "GMI_CLOUD_TIMEOUT_SECONDS",
    "GMI_CLOUD_VALIDATE_MODELS",
    "GMI_FALLBACK_MODEL",
    "GMI_MODEL",
    "GMI_RESPONSE_FORMAT",
    "GMI_TEMPERATURE",
    "GMI_TIMEOUT_SECONDS",
    "GMI_VALIDATE_MODELS",
    "GMICLOUD_API_KEY",
    "GMICLOUD_BASE_URL",
    "GMICLOUD_FALLBACK_MODEL",
    "GMICLOUD_MODEL",
    "GMICLOUD_RESPONSE_FORMAT",
    "GMICLOUD_TEMPERATURE",
    "GMICLOUD_TIMEOUT_SECONDS",
    "GMICLOUD_VALIDATE_MODELS",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "HF_ALLOWED_MODELS",
    "HF_API_TOKEN",
    "HF_BASE_URL",
    "HF_MODEL",
    "HF_TOKEN",
    "HUGGINGFACE_ALLOWED_MODELS",
    "HUGGINGFACE_API_KEY",
    "HUGGINGFACE_BASE_URL",
    "HUGGINGFACE_HUB_TOKEN",
    "HUGGINGFACE_MODEL",
    "LLM_ALLOWED_MODELS",
    "LLM_ALLOWED_PROVIDERS",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_FALLBACK_MODEL",
    "LLM_MAX_TOKENS",
    "LLM_MODEL",
    "LLM_CONTEXT_LENGTH",
    "LLM_PROVIDER",
    "LLM_RESPONSE_FORMAT",
    "LLM_TIMEOUT_SECONDS",
    "OLLAMA_CONTEXT_LENGTH",
    "OLLAMA_NATIVE_CHAT",
    "OLLAMA_NUM_CTX",
    "OLLAMA_THINK",
    "CLOUDFLARE_ALLOWED_MODELS",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_AI_API_KEY",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_BASE_URL",
    "CLOUDFLARE_ENABLE_THINKING",
    "CLOUDFLARE_FALLBACK_MODEL",
    "CLOUDFLARE_MAX_TOKENS",
    "CLOUDFLARE_MODEL",
    "CLOUDFLARE_RESPONSE_FORMAT",
    "CLOUDFLARE_TEMPERATURE",
    "CLOUDFLARE_TIMEOUT_SECONDS",
    "CLOUDFLARE_VALIDATE_MODELS",
    "NIM_API_KEY",
    "NVIDIA_ALLOWED_MODELS",
    "NVIDIA_API_KEY",
    "NVIDIA_BASE_URL",
    "NVIDIA_MODEL",
    "OPENAI_ALLOWED_MODELS",
    "TOGETHER_API_KEY",
    "TOGETHER_IMAGE_API_KEY",
    "TOGETHER_LLM_BASE_URL",
    "TOGETHER_MODEL",
    "XAI_API_KEY",
    "XAI_BASE_URL",
    "XAI_MODEL",
    "GROK_API_KEY",
    "GROK_BASE_URL",
    "GROK_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_FALLBACK_MODEL",
    "OPENAI_MAX_TOKENS",
    "OPENAI_MODEL",
    "OPENAI_RESPONSE_FORMAT",
    "OPENAI_VALIDATE_MODELS",
    "RUNPOD_ALLOWED_MODELS",
    "RUNPOD_API_KEY",
    "RUNPOD_BASE_URL",
    "RUNPOD_ENDPOINT_ID",
    "RUNPOD_ENDPOINT_URL",
    "RUNPOD_ENDPOINTS_BY_MODEL",
    "RUNPOD_FALLBACK_MODEL",
    "RUNPOD_MAX_TOKENS",
    "RUNPOD_MODEL",
    "RUNPOD_MODEL_ENDPOINTS",
    "RUNPOD_OPENAI_BASE_URL",
    "RUNPOD_OPENAI_FALLBACK_MODEL",
    "RUNPOD_OPENAI_MODEL",
    "RUNPOD_RESPONSE_FORMAT",
    "RUNPOD_SERVERLESS_MODEL",
    "RUNPOD_TEMPERATURE",
    "RUNPOD_VALIDATE_MODELS",
    "STRICT_ANTHROPIC",
    "STRICT_CLAUDE",
    "STRICT_GMI",
    "STRICT_GMI_CLOUD",
    "STRICT_GMICLOUD",
    "STRICT_LLM",
    "STRICT_CLOUDFLARE",
    "STRICT_VERTEX",
    "STRICT_VERTEX_AI",
    "VERTEX_AI_ALLOWED_MODELS",
    "VERTEX_AI_FALLBACK_MODEL",
    "VERTEX_AI_LOCATION",
    "VERTEX_AI_MODEL",
    "VERTEX_AI_PROJECT",
    "VERTEX_AI_TIMEOUT_SECONDS",
    "VERTEX_ALLOWED_MODELS",
    "VERTEX_FALLBACK_MODEL",
    "VERTEX_MODEL",
    "VERTEX_TIMEOUT_SECONDS",
}


@contextmanager
def isolated_llm_env(**overrides: str) -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in LLM_ENV_KEYS}
    try:
        for key in LLM_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({"FORMA_DEV_MODE": "true", **overrides})
        yield
    finally:
        for key in LLM_ENV_KEYS:
            os.environ.pop(key, None)
            if old_values[key] is not None:
                os.environ[key] = old_values[key] or ""


class LLMRuntimeTests(unittest.TestCase):
    def test_production_preflight_rejects_provider_without_live_model_check(self) -> None:
        validation = LLMProviderValidation(
            provider="openai",
            requested_model="gpt-test",
            actual_model="gpt-test",
            requested_model_available=True,
            strict_mode=True,
            fallback_active=False,
            model_availability_checked=False,
        )

        with isolated_llm_env(FORMA_DEV_MODE="false"):
            with self.assertRaisesRegex(LLMProviderPreflightError, "did not perform a live model-availability check"):
                enforce_production_llm_preflight(validation)

    def test_production_preflight_accepts_selected_model_after_live_check(self) -> None:
        validation = LLMProviderValidation(
            provider="vertex",
            requested_model="gemini-test",
            actual_model="gemini-test",
            requested_model_available=True,
            strict_mode=True,
            fallback_active=False,
            model_availability_checked=True,
        )

        with isolated_llm_env(FORMA_DEV_MODE="false"):
            self.assertIs(validation, enforce_production_llm_preflight(validation))

    def test_production_preflight_rejects_model_fallback(self) -> None:
        validation = LLMProviderValidation(
            provider="vertex",
            requested_model="gemini-primary",
            actual_model="gemini-fallback",
            requested_model_available=False,
            strict_mode=False,
            fallback_active=True,
            fallback_model="gemini-fallback",
            model_availability_checked=True,
        )

        with isolated_llm_env(FORMA_DEV_MODE="false"):
            with self.assertRaisesRegex(LLMProviderPreflightError, "fell back to a different model"):
                enforce_production_llm_preflight(validation)

    def test_development_preflight_allows_unchecked_provider(self) -> None:
        validation = LLMProviderValidation(
            provider="openai",
            requested_model="gpt-test",
            actual_model="gpt-test",
            requested_model_available=True,
            strict_mode=True,
            fallback_active=False,
            model_availability_checked=False,
        )

        with isolated_llm_env(FORMA_DEV_MODE="true"):
            self.assertIs(validation, enforce_production_llm_preflight(validation))

    def test_production_forces_openai_compatible_model_validation(self) -> None:
        with isolated_llm_env(
            FORMA_DEV_MODE="false",
            LLM_PROVIDER="openai",
            OPENAI_API_KEY="test-key",
            OPENAI_MODEL="gpt-test",
        ):
            provider = OpenAICompatibleProvider(provider_name="openai")

        self.assertTrue(provider.validate_models)

    def test_local_ollama_uses_native_schema_api_with_request_context(self) -> None:
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "model": "qwen3:8b",
                    "message": {"role": "assistant", "content": json.dumps({
                        "title": "Test",
                        "description": "Test project",
                        "difficulty": "Beginner",
                        "estimated_cost": 0,
                        "category": "IoT",
                    })},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 10,
                    "eval_count": 20,
                }).encode()

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        with isolated_llm_env(
            LLM_BASE_URL="http://127.0.0.1:11434/v1",
            LLM_MODEL="qwen3:8b",
            LLM_RESPONSE_FORMAT="json_schema",
            OLLAMA_CONTEXT_LENGTH="16384",
        ), patch("forma_core.llm_providers.urllib.request.urlopen", side_effect=fake_urlopen):
            provider = OpenAICompatibleProvider("openai-compatible", "qwen3:8b")
            result = provider.generate_structured("Return a project overview.", ProjectOverview)

        self.assertEqual("Test", result.title)
        self.assertEqual("http://127.0.0.1:11434/api/chat", captured["url"])
        self.assertEqual(16384, captured["payload"]["options"]["num_ctx"])
        self.assertFalse(captured["payload"]["think"])
        self.assertIsInstance(captured["payload"]["format"], dict)

    def test_image_input_capability_identifies_known_model_types(self) -> None:
        self.assertFalse(model_image_input_support("cloudflare", "nvidia/nemotron-3-super-120b-a12b"))
        self.assertTrue(model_image_input_support("cloudflare", "@cf/google/gemma-4-26b-a4b-it"))
        self.assertTrue(model_image_input_support("openai", "gpt-5.5"))
        self.assertIsNone(model_image_input_support("cloudflare", "some-new-model"))

    def test_vertex_is_primary_google_cloud_provider_and_uses_adc_client(self) -> None:
        client_calls = []

        class FakeModel:
            name = "publishers/google/models/gemini-3.5-flash"
            supported_actions = None

        class FakeClient:
            def __init__(self):
                self.models = self

            def list(self):
                return [FakeModel()]

        class FakeGenAI:
            @staticmethod
            def Client(**kwargs):
                client_calls.append(kwargs)
                return FakeClient()

        with isolated_llm_env(
            GOOGLE_CLOUD_PROJECT="forma-vertex-test",
            GOOGLE_CLOUD_LOCATION="us-central1",
            VERTEX_AI_MODEL="gemini-3.5-flash",
        ), patch("forma_core.llm_providers.genai", FakeGenAI):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)
            validation = provider.validate_configured_model()

        self.assertEqual("vertex", runtime.provider)
        self.assertEqual("gemini-3.5-flash", runtime.model)
        self.assertEqual(1, len(client_calls))
        vertex_client_config = client_calls[0]
        self.assertTrue(vertex_client_config.pop("vertexai"))
        self.assertEqual("forma-vertex-test", vertex_client_config.pop("project"))
        self.assertEqual("us-central1", vertex_client_config.pop("location"))
        self.assertEqual(90_000, vertex_client_config.pop("http_options").timeout)
        self.assertEqual({}, vertex_client_config)
        self.assertTrue(provider.is_configured)
        self.assertTrue(validation.requested_model_available)
        self.assertEqual("vertex", validation.provider)

    def test_vertex_alias_and_provider_specific_allowlist_are_supported(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="google-vertex-ai",
            VERTEX_AI_PROJECT="forma-vertex-test",
            VERTEX_AI_ALLOWED_MODELS="gemini-2.5-flash,gemini-3.5-flash",
        ):
            runtime = resolve_llm_runtime_config(model_name="gemini-2.5-flash")

        self.assertEqual("vertex", runtime.provider)
        self.assertEqual("gemini-2.5-flash", runtime.model)
        self.assertEqual(["gemini-2.5-flash", "gemini-3.5-flash"], runtime.allowed_models)

    def test_vertex_passes_vercel_workload_identity_credentials_to_client(self) -> None:
        credentials = object()
        client_calls = []

        class FakeGenAI:
            @staticmethod
            def Client(**kwargs):
                client_calls.append(kwargs)
                return object()

        with isolated_llm_env(
            GOOGLE_CLOUD_PROJECT="forma-vertex-test",
            VERTEX_AI_MODEL="gemini-3.5-flash",
        ), patch("forma_core.llm_providers.genai", FakeGenAI), patch(
            "forma_core.llm_providers.build_vertex_credentials",
            return_value=credentials,
        ):
            provider = build_llm_provider()

        self.assertTrue(provider.is_configured)
        self.assertIs(credentials, client_calls[0]["credentials"])

    def test_parse_provider_model_selector(self) -> None:
        selector = parse_llm_selector("runpod/caid-technologies/parti-base")

        self.assertIsNotNone(selector)
        assert selector is not None
        self.assertEqual("runpod", selector.provider)
        self.assertEqual("caid-technologies/parti-base", selector.model)
        self.assertEqual("runpod/caid-technologies/parti-base", selector.key)
        hf_selector = parse_llm_selector("huggingface/Qwen/Qwen2.5-Coder-3B-Instruct:nscale")
        self.assertIsNotNone(hf_selector)
        assert hf_selector is not None
        self.assertEqual("huggingface", hf_selector.provider)
        self.assertEqual("Qwen/Qwen2.5-Coder-3B-Instruct:nscale", hf_selector.model)
        self.assertEqual(("openai", "gpt-5.5"), split_llm_selector("openai/gpt-5.5"))
        self.assertEqual((None, None), split_llm_selector(None))

    def test_invalid_selector_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_llm_selector("gpt-5.5")

    def test_runtime_allows_configured_openai_model_override(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="simulation",
            LLM_ALLOWED_PROVIDERS="simulation,openai,runpod",
            OPENAI_ALLOWED_MODELS="gpt-5.5",
        ):
            runtime = resolve_llm_runtime_config("openai", "gpt-5.5")

        self.assertEqual("openai", runtime.provider)
        self.assertEqual("gpt-5.5", runtime.model)
        self.assertTrue(runtime.model_overridden)
        self.assertEqual(["gpt-5.5"], runtime.allowed_models)

    def test_explicit_anthropic_request_extends_stale_provider_allowlist(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="simulation",
            LLM_ALLOWED_PROVIDERS="openai,simulation",
            ANTHROPIC_ALLOWED_MODELS="claude-old",
            ANTHROPIC_API_KEY="anthropic-test-key",
        ):
            runtime = resolve_llm_runtime_config("anthropic", "claude-sonnet-5")

        self.assertEqual("anthropic", runtime.provider)
        self.assertEqual("claude-sonnet-5", runtime.model)
        self.assertIn("anthropic", runtime.allowed_providers or [])
        self.assertIn("claude-sonnet-5", runtime.allowed_models or [])

    def test_anthropic_defaults_to_opus_5(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="anthropic-test-key",
        ):
            runtime = resolve_llm_runtime_config()

        self.assertEqual("anthropic", runtime.provider)
        self.assertEqual("claude-opus-5", runtime.model)

    def test_gemini_defaults_to_flash_3_7(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="gemini",
            GEMINI_API_KEY="gemini-test-key",
        ):
            runtime = resolve_llm_runtime_config()

        self.assertEqual("gemini", runtime.provider)
        self.assertEqual("gemini-3.7-flash", runtime.model)

    def test_vertex_defaults_to_flash_3_7(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="vertex",
            GOOGLE_CLOUD_PROJECT="forma-vertex-test",
        ):
            runtime = resolve_llm_runtime_config()

        self.assertEqual("vertex", runtime.provider)
        self.assertEqual("gemini-3.7-flash", runtime.model)

    def test_local_runtime_allows_configured_provider_model_override(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="baseten",
            LLM_ALLOWED_PROVIDERS="baseten,simulation",
            BASETEN_API_KEY="baseten-secret",
        ):
            runtime = resolve_llm_runtime_config("baseten", "zai-org/GLM-5.2")

        self.assertEqual("baseten", runtime.provider)
        self.assertEqual("zai-org/GLM-5.2", runtime.model)
        self.assertIn("zai-org/GLM-5.2", runtime.allowed_models or [])

    def test_env_default_provider_still_respects_allowlist(self) -> None:
        with isolated_llm_env(
            FORMA_DEPLOYMENT="true",
            LLM_PROVIDER="openai",
            LLM_ALLOWED_PROVIDERS="simulation",
        ):
            with self.assertRaises(LLMProviderConfigError):
                resolve_llm_runtime_config()

    def test_runpod_serverless_model_endpoint_map_extends_allowed_models(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="runpod-serverless",
            LLM_ALLOWED_PROVIDERS="runpod-serverless,simulation",
            RUNPOD_API_KEY="rpa_test",
            RUNPOD_MODEL_ENDPOINTS='{"caid-technologies/parti-base": "endpoint-test"}',
        ):
            runtime = resolve_llm_runtime_config("runpod-serverless", "caid-technologies/parti-base")

        self.assertEqual("runpod-serverless", runtime.provider)
        self.assertEqual("caid-technologies/parti-base", runtime.model)
        self.assertIn("caid-technologies/parti-base", runtime.allowed_models or [])

    def test_runpod_queue_base_url_autodetects_serverless_provider(self) -> None:
        with isolated_llm_env(
            RUNPOD_API_KEY="rpa_test",
            RUNPOD_BASE_URL="https://api.runpod.ai/v2/endpoint-test",
            RUNPOD_OPENAI_MODEL="caid-technologies/parti-base",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("runpod-serverless", runtime.provider)
        self.assertEqual("caid-technologies/parti-base", runtime.model)
        self.assertEqual("https://api.runpod.ai/v2/endpoint-test", provider.endpoint_base_url)
        self.assertTrue(provider.is_configured)

    def test_runpod_queue_url_is_not_reported_as_configured_openai_runpod(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="runpod-serverless",
            LLM_ALLOWED_PROVIDERS="openai,runpod,runpod-serverless,simulation",
            RUNPOD_API_KEY="rpa_test",
            RUNPOD_OPENAI_BASE_URL="https://api.runpod.ai/v2/endpoint-test",
            RUNPOD_ENDPOINT_ID="endpoint-test",
            RUNPOD_MODEL="caid-technologies/parti-base",
        ):
            runtime = resolve_llm_runtime_config()

        self.assertEqual("runpod-serverless", runtime.provider)
        self.assertIn("runpod-serverless", runtime.configured_providers or [])
        self.assertNotIn("runpod", runtime.configured_providers or [])
        self.assertIn("runpod", runtime.allowed_providers or [])

    def test_explicit_runpod_rejects_queue_endpoint_base_url(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="runpod",
            LLM_ALLOWED_PROVIDERS="runpod,simulation",
            RUNPOD_API_KEY="rpa_test",
            RUNPOD_BASE_URL="https://api.runpod.ai/v2/endpoint-test",
            RUNPOD_OPENAI_MODEL="caid-technologies/parti-base",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)
            validation = provider.validate_configured_model(raise_on_strict=False)

            with self.assertRaises(LLMProviderConfigError):
                provider.validate_configured_model()

        self.assertEqual("runpod", runtime.provider)
        self.assertEqual("runpod", provider.provider_name)
        self.assertFalse(validation.live_generation_enabled)
        self.assertIn("LLM_PROVIDER=runpod-serverless", validation.validation_error or "")

    def test_huggingface_runtime_uses_qwen_default(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="huggingface",
            LLM_ALLOWED_PROVIDERS="huggingface,simulation",
            HF_TOKEN="hf_test",
        ):
            runtime = resolve_llm_runtime_config()

        self.assertEqual("huggingface", runtime.provider)
        self.assertEqual("Qwen/Qwen2.5-Coder-3B-Instruct:nscale", runtime.model)
        self.assertIn("Qwen/Qwen2.5-Coder-3B-Instruct:nscale", runtime.allowed_models or [])

    def test_huggingface_provider_uses_router_defaults(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="huggingface",
            LLM_ALLOWED_PROVIDERS="huggingface,simulation",
            HF_TOKEN="hf_test",
            HUGGINGFACE_MODEL="Qwen/Qwen2.5-Coder-3B-Instruct:nscale",
            LLM_BASE_URL="https://api.runpod.ai/v2/not-huggingface/openai/v1",
            LLM_API_KEY="sk_not_huggingface",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("huggingface", provider.provider_name)
        self.assertEqual("Qwen/Qwen2.5-Coder-3B-Instruct:nscale", provider.requested_model)
        self.assertEqual("https://router.huggingface.co/v1", provider.base_url)
        self.assertTrue(provider.is_configured)

    def test_nvidia_runtime_uses_glm_52_default(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="nvidia",
            LLM_ALLOWED_PROVIDERS="nvidia,simulation",
            NVIDIA_API_KEY="nvapi_test",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("nvidia", runtime.provider)
        self.assertEqual("nvidia/z-ai/glm-5.2", runtime.model)
        self.assertIn("nvidia/z-ai/glm-5.2", runtime.allowed_models or [])
        self.assertEqual("nvidia/z-ai/glm-5.2", provider.requested_model)
        self.assertEqual("https://integrate.api.nvidia.com/v1", provider.base_url)
        self.assertTrue(provider.is_configured)

    def test_xai_runtime_uses_grok_4_default(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="grok",
            LLM_ALLOWED_PROVIDERS="xai,simulation",
            XAI_API_KEY="xai_test",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("xai", runtime.provider)
        self.assertEqual("grok-4", runtime.model)
        self.assertIn("grok-4", runtime.allowed_models or [])
        self.assertEqual("grok-4", provider.requested_model)
        self.assertEqual("https://api.x.ai/v1", provider.base_url)
        self.assertTrue(provider.is_configured)

    def test_together_runtime_uses_llama_default(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="together-ai",
            LLM_ALLOWED_PROVIDERS="together,simulation",
            TOGETHER_API_KEY="together_test",
            TOGETHER_MODEL="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("together", runtime.provider)
        self.assertEqual("meta-llama/Llama-3.3-70B-Instruct-Turbo", runtime.model)
        self.assertEqual("https://api.together.xyz/v1", provider.base_url)
        self.assertTrue(provider.is_configured)

    def test_cloudflare_runtime_uses_workers_ai_defaults(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="workers-ai",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
        ):
            runtime = resolve_llm_runtime_config()
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("cloudflare", runtime.provider)
        self.assertEqual("@cf/google/gemma-4-26b-a4b-it", runtime.model)
        self.assertIn("@cf/google/gemma-4-26b-a4b-it", runtime.allowed_models or [])
        self.assertEqual("cloudflare", provider.provider_name)
        self.assertEqual("@cf/google/gemma-4-26b-a4b-it", provider.requested_model)
        self.assertEqual("https://api.cloudflare.com/client/v4/accounts/test-account/ai/v1", provider.base_url)
        self.assertEqual("json_schema", provider.response_format)
        self.assertTrue(provider.is_configured)

    def test_cloudflare_api_token_autodetects_provider(self) -> None:
        with isolated_llm_env(CLOUDFLARE_API_TOKEN="cloudflare_test", CLOUDFLARE_ACCOUNT_ID="test-account"):
            runtime = resolve_llm_runtime_config()

        self.assertEqual("cloudflare", runtime.provider)
        self.assertIn("cloudflare", runtime.configured_providers or [])

    def test_cloudflare_structured_request_uses_json_schema_and_max_tokens(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="cloudflare",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
            CLOUDFLARE_MODEL="@cf/google/gemma-4-26b-a4b-it",
            CLOUDFLARE_MAX_TOKENS="321",
            CLOUDFLARE_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("cloudflare", "@cf/google/gemma-4-26b-a4b-it")
            provider = build_llm_provider(runtime_config=runtime)

        payloads = []

        def fake_request(path, method="GET", payload=None):
            payloads.append(copy.deepcopy(payload or {}))
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Test Project",
                                    "description": "A test project.",
                                    "difficulty": "Beginner",
                                    "estimated_cost": 1.0,
                                    "category": "IoT",
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

        provider._request_json = fake_request
        provider.generate_structured("Return a project overview.", ProjectOverview)

        self.assertEqual("json_schema", payloads[0]["response_format"]["type"])
        self.assertEqual(321, payloads[0]["max_tokens"])
        self.assertNotIn("max_completion_tokens", payloads[0])

    def test_known_text_only_model_rejects_image_before_request(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="cloudflare",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
            CLOUDFLARE_MODEL="nvidia/nemotron-3-super-120b-a12b",
        ):
            runtime = resolve_llm_runtime_config("cloudflare", "nvidia/nemotron-3-super-120b-a12b")
            provider = build_llm_provider(runtime_config=runtime)

        with self.assertRaisesRegex(LLMProviderInputError, "vision-capable model"):
            provider.validate_image_input()

    def test_web_research_rejects_text_only_image_input_before_research(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="cloudflare",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
            CLOUDFLARE_MODEL="nvidia/nemotron-3-super-120b-a12b",
            CLOUDFLARE_VALIDATE_MODELS="false",
        ):
            pipeline = WebResearchHardwarePipeline(
                provider_name="cloudflare",
                model_name="nvidia/nemotron-3-super-120b-a12b",
            )
            with patch.object(pipeline, "_research") as research:
                with self.assertRaises(LLMProviderInputError):
                    pipeline.generate_project(
                        "Build the safe low-voltage circuit shown in this image.",
                        image_bytes=b"fake-image",
                        image_mime_type="image/png",
                    )

        research.assert_not_called()

    def test_provider_normalizes_unknown_image_modality_error(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="cloudflare",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
            CLOUDFLARE_MODEL="some-new-model",
            CLOUDFLARE_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("cloudflare", "some-new-model")
            provider = build_llm_provider(runtime_config=runtime)

        def reject_image(*_args, **_kwargs):
            raise RuntimeError('cloudflare request failed with HTTP 400: {"detail":"Unknown modality: image"}')

        provider._request_json = reject_image
        with self.assertRaisesRegex(LLMProviderInputError, "cannot read reference images"):
            provider.generate_structured(
                "Infer the hardware project.",
                ProjectOverview,
                image_bytes=b"fake-image",
                image_mime_type="image/png",
            )

    def test_cloudflare_retries_when_reasoning_uses_budget_before_visible_content(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="cloudflare",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
            CLOUDFLARE_MODEL="@cf/google/gemma-4-26b-a4b-it",
            CLOUDFLARE_MAX_TOKENS="7000",
            CLOUDFLARE_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("cloudflare", "@cf/google/gemma-4-26b-a4b-it")
            provider = build_llm_provider(runtime_config=runtime)

        payloads = []
        responses = iter(
            [
                {
                    "choices": [
                        {
                            "message": {"content": None, "reasoning_content": "internal reasoning"},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"completion_tokens": 7000},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "title": "Recovered Project",
                                        "description": "A valid response after retry.",
                                        "difficulty": "Beginner",
                                        "estimated_cost": 5.0,
                                        "category": "IoT",
                                    }
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            ]
        )

        def fake_request(_path, method="GET", payload=None):
            payloads.append(copy.deepcopy(payload or {}))
            return next(responses)

        provider._request_json = fake_request
        result = provider.generate_structured("Return a project overview.", ProjectOverview)

        self.assertEqual("Recovered Project", result.title)
        self.assertEqual(7000, payloads[0]["max_tokens"])
        self.assertEqual(14000, payloads[1]["max_tokens"])
        self.assertEqual({"enable_thinking": False}, payloads[0]["chat_template_kwargs"])
        self.assertEqual({"enable_thinking": False}, payloads[1]["chat_template_kwargs"])
        self.assertNotIn("reasoning_effort", payloads[0])
        self.assertEqual("low", payloads[1]["reasoning_effort"])

    def test_cloudflare_empty_retry_reports_safe_response_metadata(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="cloudflare",
            LLM_ALLOWED_PROVIDERS="cloudflare,simulation",
            CLOUDFLARE_API_TOKEN="cloudflare_test",
            CLOUDFLARE_ACCOUNT_ID="test-account",
            CLOUDFLARE_MODEL="some-new-model",
            CLOUDFLARE_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("cloudflare", "some-new-model")
            provider = build_llm_provider(runtime_config=runtime)

        provider._request_json = lambda *_args, **_kwargs: {
            "choices": [
                {
                    "message": {"content": None, "reasoning_content": "internal reasoning"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"completion_tokens": 8192},
        }

        with self.assertRaisesRegex(
            LLMProviderOutputError,
            r"finish_reason=length.*completion_tokens=8192.*reasoning_content=True",
        ):
            provider.generate_structured("Return a project overview.", ProjectOverview)

    def test_nvidia_runtime_allows_qwen_coder_32b_instruct_override(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="nvidia",
            LLM_ALLOWED_PROVIDERS="nvidia,simulation",
            NVIDIA_API_KEY="nvapi_test",
            NVIDIA_ALLOWED_MODELS="qwen/qwen2.5-coder-32b-instruct,nvidia/z-ai/glm-5.2",
        ):
            runtime = resolve_llm_runtime_config("nvidia", "qwen/qwen2.5-coder-32b-instruct")
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("nvidia", runtime.provider)
        self.assertEqual("qwen/qwen2.5-coder-32b-instruct", runtime.model)
        self.assertIn("qwen/qwen2.5-coder-32b-instruct", runtime.allowed_models or [])
        self.assertEqual("qwen/qwen2.5-coder-32b-instruct", provider.requested_model)

    def test_gmi_runtime_uses_fable_default_and_aliases(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="gemicloud",
            LLM_ALLOWED_PROVIDERS="gmi,simulation",
            LLM_BASE_URL="https://api.runpod.ai/v2/not-gmi/openai/v1",
            LLM_TEMPERATURE="0.9",
            GMI_API_KEY="gmi_test",
            GMI_ALLOWED_MODELS="fable",
        ):
            runtime = resolve_llm_runtime_config("gmi", "gmi/fable")
            provider = build_llm_provider(runtime_config=runtime)

        self.assertEqual("gmi", runtime.provider)
        self.assertEqual("anthropic/claude-fable-5", runtime.model)
        self.assertEqual(["anthropic/claude-fable-5"], runtime.allowed_models)
        self.assertEqual("gmi", provider.provider_name)
        self.assertEqual("anthropic/claude-fable-5", provider.requested_model)
        self.assertEqual("https://api.gmi-serving.com/v1", provider.base_url)
        self.assertIsNone(provider.temperature)
        self.assertTrue(provider.is_configured)

    def test_gmi_json_schema_closes_object_schema(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="gmi",
            LLM_ALLOWED_PROVIDERS="gmi,simulation",
            GMI_API_KEY="gmi_test",
            GMI_MODEL="anthropic/claude-fable-5",
            GMI_RESPONSE_FORMAT="json_schema",
            GMI_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("gmi", "anthropic/claude-fable-5")
            provider = build_llm_provider(runtime_config=runtime)

        payloads = []

        def fake_request(path, method="GET", payload=None):
            payloads.append(copy.deepcopy(payload or {}))
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Test Project",
                                    "description": "A test project.",
                                    "difficulty": "Beginner",
                                    "estimated_cost": 1.0,
                                    "category": "IoT",
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

        provider._request_json = fake_request
        provider.generate_structured("Return a project overview.", ProjectOverview)

        schema = payloads[0]["response_format"]["json_schema"]["schema"]
        self.assertEqual(False, schema["additionalProperties"])

    def test_openai_provider_uses_max_completion_tokens(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="openai",
            LLM_ALLOWED_PROVIDERS="openai,simulation",
            OPENAI_API_KEY="sk_test",
            OPENAI_MODEL="gpt-5.6-sol",
            OPENAI_REASONING_EFFORT="low",
            OPENAI_MAX_TOKENS="123",
            OPENAI_RESPONSE_FORMAT="json_object",
            OPENAI_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("openai", "gpt-5.6-sol")
            provider = build_llm_provider(runtime_config=runtime)

        payloads = []

        def fake_request(path, method="GET", payload=None):
            payloads.append(payload or {})
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Test Project",
                                    "description": "A test project.",
                                    "difficulty": "Beginner",
                                    "estimated_cost": 1.0,
                                    "category": "IoT",
                                }
                            )
                        }
                    }
                ]
            }

        provider._request_json = fake_request
        provider.generate_structured("Return a project overview.", ProjectOverview)

        self.assertEqual(123, payloads[0]["max_completion_tokens"])
        self.assertEqual("low", payloads[0]["reasoning_effort"])
        self.assertNotIn("max_tokens", payloads[0])

    def test_anthropic_output_config_closes_object_schemas(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="anthropic-test-key",
            ANTHROPIC_MODEL="claude-test",
            ANTHROPIC_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("anthropic", "claude-test")
            provider = build_llm_provider(runtime_config=runtime)

        payloads = []

        def fake_request(path, method="GET", payload=None):
            payloads.append(payload or {})
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "title": "Test Project",
                                "description": "A test project.",
                                "difficulty": "Beginner",
                                "estimated_cost": 1.0,
                                "category": "IoT",
                            }
                        ),
                    }
                ],
                "stop_reason": "end_turn",
            }

        provider._request_json = fake_request
        provider.generate_structured("Return a project overview.", ProjectOverview)

        schema = payloads[0]["output_config"]["format"]["schema"]
        self.assertEqual(False, schema["additionalProperties"])

    def test_anthropic_grammar_timeout_falls_back_to_prompt_schema(self) -> None:
        with isolated_llm_env(
            LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="anthropic-test-key",
            ANTHROPIC_MODEL="claude-test",
            ANTHROPIC_VALIDATE_MODELS="false",
        ):
            runtime = resolve_llm_runtime_config("anthropic", "claude-test")
            provider = build_llm_provider(runtime_config=runtime)

        payloads = []

        def fake_request(path, method="GET", payload=None):
            payloads.append(copy.deepcopy(payload or {}))
            if len(payloads) == 1:
                raise RuntimeError(
                    'anthropic request failed with HTTP 400: {"type":"error","error":{"message":"Grammar compilation timed out."}}'
                )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "title": "Test Project",
                                "description": "A test project.",
                                "difficulty": "Beginner",
                                "estimated_cost": 1.0,
                                "category": "IoT",
                            }
                        ),
                    }
                ],
                "stop_reason": "end_turn",
            }

        provider._request_json = fake_request
        result = provider.generate_structured("Return a project overview.", ProjectOverview)

        self.assertEqual("Test Project", result.title)
        self.assertIn("output_config", payloads[0])
        self.assertNotIn("output_config", payloads[1])
        prompt_text = payloads[1]["messages"][0]["content"][-1]["text"]
        self.assertIn("Return only valid JSON", prompt_text)
        self.assertIn('"title"', prompt_text)


if __name__ == "__main__":
    unittest.main()
