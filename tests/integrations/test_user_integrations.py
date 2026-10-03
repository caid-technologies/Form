from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from forma_core import user_integrations
from forma_core.user_integrations import (
    EncryptedFileIntegrationStore,
    SupabaseUserIntegrationStore,
    SupabaseWorkspaceIntegrationStore,
    UserIntegrationStore,
    apply_user_integrations_to_environment,
    default_integration_store,
    integration_status_payload,
    resolve_user_integration_settings,
)
from forma_core.llm_providers import build_llm_provider, resolve_llm_runtime_config


TEST_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_IMAGE_API_KEY",
    "OPENAI_IMAGE_MODEL",
    "OPENAI_MODEL",
    "OPENAI_STREAM_MODEL",
    "OPENAI_ALLOWED_MODELS",
    "BASETEN_API_KEY",
    "BASETEN_BASE_URL",
    "BASETEN_MODEL",
    "BASETEN_STREAM_MODEL",
    "BASETEN_TIMEOUT_SECONDS",
    "BASETEN_STREAM_TIMEOUT_SECONDS",
    "BASETEN_MAX_TOKENS",
    "BASETEN_STREAM_MAX_OUTPUT_TOKENS",
    "BASETEN_ALLOWED_MODELS",
    "NVIDIA_API_KEY",
    "NVIDIA_MODEL",
    "CLOUDFLARE_ALLOWED_MODELS",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_AI_API_KEY",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_BASE_URL",
    "CLOUDFLARE_MODEL",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_ALLOWED_PROVIDERS",
    "FORMA_WORKSPACE_INTEGRATIONS_BACKEND",
    "FORMA_USER_INTEGRATIONS_BACKEND",
    "FORMA_INTEGRATIONS_BACKEND",
    "FORMA_DEV_MODE",
    "DATABASE_BACKEND",
    "DATABASE_PROVIDER",
    "DB_BACKEND",
    "DB_PROVIDER",
    "SUPABASE_URL",
    "NEXT_PUBLIC_SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SECRET_KEY",
    "FORMA_USER_SECRETS_KEY",
    "FORMA_DEPLOYMENT",
    "FORMA_DEPLOYMENT_MODE",
    "DEPLOYMENT",
    "DEPLOYMENT_MODE",
    "NEXT_PUBLIC_FORMA_DEPLOYMENT",
    "IMAGE_PROVIDER",
    "IMAGE_OUTPUT_ENABLED",
    "OPENAI_IMAGE_OUTPUT_ENABLED",
    "OPENAI_IMAGE_SIZE",
    "IMAGE_API_KEY",
    "IMAGE_BASE_URL",
    "IMAGE_MODEL",
    "IMAGE_SIZE",
    "IMAGE_QUALITY",
    "IMAGE_OUTPUT_FORMAT",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "VERTEX_AI_PROJECT",
    "VERTEX_AI_LOCATION",
    "VERTEX_AI_MODEL",
    "VERTEX_AI_IMAGE_MODEL",
    "VERTEX_AI_IMAGE_RESOLUTION",
    "VERTEX_AI_IMAGE_ASPECT_RATIO",
    "VERTEX_AI_IMAGE_OUTPUT_FORMAT",
    "GMI_API_KEY",
    "GMI_CLOUD_API_KEY",
    "GMICLOUD_API_KEY",
    "GMI_IMAGE_BASE_URL",
    "GMI_CLOUD_IMAGE_BASE_URL",
    "GMICLOUD_IMAGE_BASE_URL",
    "GMI_IMAGE_MODEL",
    "GMI_CLOUD_IMAGE_MODEL",
    "GMICLOUD_IMAGE_MODEL",
    "GMI_IMAGE_SIZE",
    "GMI_IMAGE_OUTPUT_FORMAT",
    "TOGETHER_API_KEY",
    "TOGETHER_IMAGE_API_KEY",
    "TOGETHER_IMAGE_BASE_URL",
    "TOGETHER_BASE_URL",
    "TOGETHER_IMAGE_MODEL",
    "TOGETHER_IMAGE_SIZE",
    "TOGETHER_IMAGE_STEPS",
    "TOGETHER_IMAGE_OUTPUT_FORMAT",
    "TOGETHER_MODEL",
    "TOGETHER_LLM_BASE_URL",
    "XAI_API_KEY",
    "XAI_BASE_URL",
    "XAI_MODEL",
    "GROK_API_KEY",
    "TAVILY_API_KEY",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_SEARCH_LIMIT",
    "TAVILY_CRAWL_MAX_DEPTH",
    "TAVILY_RESEARCH_MODEL",
    "HF_TOKEN",
    "HUGGINGFACE_API_KEY",
    "HUGGINGFACE_IMAGE_MODEL",
    "HUGGINGFACE_IMAGE_INFERENCE_PROVIDER",
)


@contextmanager
def isolated_integration_env() -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in TEST_ENV_KEYS}
    user_integrations._APPLIED_ENV_VALUES.clear()
    user_integrations._ORIGINAL_ENV_VALUES.clear()
    user_integrations._WORKSPACE_CONFIG_CACHE.clear()
    user_integrations._WORKSPACE_CONFIG_FAILURE_CACHE.clear()
    user_integrations._google_cloud_auth_available.cache_clear()
    try:
        for key in TEST_ENV_KEYS:
            os.environ.pop(key, None)
        with patch.object(user_integrations, "_google_cloud_auth_available", return_value=False):
            yield
    finally:
        for key in TEST_ENV_KEYS:
            os.environ.pop(key, None)
            if old_values[key] is not None:
                os.environ[key] = old_values[key] or ""
        user_integrations._APPLIED_ENV_VALUES.clear()
        user_integrations._ORIGINAL_ENV_VALUES.clear()
        user_integrations._WORKSPACE_CONFIG_CACHE.clear()
        user_integrations._WORKSPACE_CONFIG_FAILURE_CACHE.clear()
        user_integrations._google_cloud_auth_available.cache_clear()


def integration_by_id(payload: dict[str, object], integration_id: str) -> dict[str, object]:
    integrations = payload["integrations"]
    assert isinstance(integrations, list)
    for integration in integrations:
        assert isinstance(integration, dict)
        if integration["id"] == integration_id:
            return integration
    raise AssertionError(f"integration not found: {integration_id}")


def field_by_id(integration: dict[str, object], field_id: str) -> dict[str, object]:
    fields = integration["fields"]
    assert isinstance(fields, list)
    for field in fields:
        assert isinstance(field, dict)
        if field["id"] == field_id:
            return field
    raise AssertionError(f"field not found: {field_id}")


class UserIntegrationTests(unittest.TestCase):
    def test_supabase_user_store_reports_encryption_key_mismatch(self) -> None:
        class Response:
            data = [
                {
                    "encrypted_config": "encrypted-value",
                    "encryption_key_id": "old-key-id",
                    "version": 1,
                    "updated_at": "2026-07-21T18:51:40Z",
                }
            ]

        class Query:
            def select(self, *_args):
                return self

            def eq(self, *_args):
                return self

            def limit(self, *_args):
                return self

            def execute(self):
                return Response()

        class Client:
            def table(self, *_args):
                return Query()

        store = SupabaseUserIntegrationStore("user_test")
        with isolated_integration_env(), patch.object(store, "_client", return_value=Client()):
            os.environ["FORMA_USER_SECRETS_KEY"] = "current-server-key"
            with self.assertLogs("forma_core.user_integrations", level="ERROR") as logs:
                with self.assertRaisesRegex(RuntimeError, "different FORMA_USER_SECRETS_KEY"):
                    store.load()

        output = "\n".join(logs.output)
        self.assertIn("encryption key mismatch", output)
        self.assertIn("stored_key_id=old-key-id", output)
        self.assertNotIn("current-server-key", output)

    def test_saved_secret_is_masked_and_nonsecret_value_is_visible(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "openai",
                field_values={
                    "api_key": "sk-testsecret123456",
                    "model": "gpt-5.5",
                },
            )

            payload = integration_status_payload(store)
            openai = integration_by_id(payload, "openai")
            api_key = field_by_id(openai, "api_key")
            model = field_by_id(openai, "model")

            self.assertTrue(api_key["configured"])
            self.assertEqual("saved", api_key["source"])
            self.assertEqual("sk-t...3456", api_key["masked_value"])
            self.assertIsNone(api_key["value"])
            self.assertEqual("gpt-5.5", model["value"])
            self.assertEqual("saved", model["source"])
            self.assertNotIn("sk-testsecret123456", str(payload))
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_apply_overlays_saved_values_and_restores_environment_fallback(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENAI_API_KEY"] = "sk-env-original"
            os.environ["IMAGE_PROVIDER"] = "openai"
            os.environ["OPENAI_IMAGE_MODEL"] = "gpt-image-2"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "openai",
                field_values={
                    "api_key": "sk-user-managed",
                    "model": "gpt-5.5",
                },
            )

            apply_user_integrations_to_environment(store)
            self.assertEqual("sk-user-managed", os.environ["OPENAI_API_KEY"])
            self.assertNotIn("OPENAI_IMAGE_API_KEY", os.environ)
            self.assertEqual("gpt-5.5", os.environ["OPENAI_MODEL"])
            self.assertEqual("gpt-5.5", os.environ["OPENAI_STREAM_MODEL"])

            store.clear_integration("openai")
            apply_user_integrations_to_environment(store)
            self.assertEqual("sk-env-original", os.environ["OPENAI_API_KEY"])
            self.assertEqual("openai", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("gpt-image-2", os.environ["OPENAI_IMAGE_MODEL"])
            self.assertNotIn("OPENAI_IMAGE_API_KEY", os.environ)
            self.assertNotIn("OPENAI_MODEL", os.environ)
            self.assertNotIn("OPENAI_STREAM_MODEL", os.environ)

    def test_request_scoped_settings_isolate_concurrent_provider_credentials(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store_a = UserIntegrationStore(Path(tmpdir) / "user-a.json")
            store_b = UserIntegrationStore(Path(tmpdir) / "user-b.json")
            store_a.update_integration(
                "openai",
                field_values={"api_key": "sk-user-a", "model": "gpt-user-a"},
            )
            store_b.update_integration(
                "anthropic",
                field_values={"api_key": "sk-user-b", "model": "claude-user-b"},
            )
            environment_before = dict(os.environ)

            async def resolve_provider(store: UserIntegrationStore):
                settings = await asyncio.to_thread(resolve_user_integration_settings, store)
                runtime = resolve_llm_runtime_config(settings=settings)
                provider = build_llm_provider(runtime_config=runtime, settings=settings)
                return runtime.provider, runtime.model, provider.api_key

            async def resolve_both():
                return await asyncio.gather(resolve_provider(store_a), resolve_provider(store_b))

            resolved = asyncio.run(resolve_both())

            self.assertEqual(
                [("openai", "gpt-user-a", "sk-user-a"), ("anthropic", "claude-user-b", "sk-user-b")],
                resolved,
            )
            self.assertEqual(environment_before, dict(os.environ))

    def test_status_payload_reports_environment_as_configured_fallback(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENAI_API_KEY"] = "sk-env-original"
            os.environ["IMAGE_PROVIDER"] = "openai"
            os.environ["OPENAI_IMAGE_MODEL"] = "gpt-image-2"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            environment_before = dict(os.environ)

            payload = integration_status_payload(store)
            openai = integration_by_id(payload, "openai")
            runtime = integration_by_id(payload, "runtime")

            self.assertTrue(openai["configured"])
            self.assertEqual("environment", field_by_id(openai, "api_key")["source"])
            self.assertTrue(field_by_id(openai, "api_key")["configured"])
            self.assertEqual("environment", field_by_id(runtime, "image_provider")["source"])
            self.assertTrue(field_by_id(runtime, "image_provider")["configured"])
            self.assertEqual(environment_before, dict(os.environ))

    def test_image_env_defaults_do_not_mark_openai_or_custom_image_configured(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["IMAGE_OUTPUT_ENABLED"] = "false"
            os.environ["IMAGE_PROVIDER"] = "openai"
            os.environ["OPENAI_IMAGE_MODEL"] = "gpt-image-2"
            os.environ["OPENAI_IMAGE_SIZE"] = "1024x1024"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")

            payload = integration_status_payload(store)
            image = integration_by_id(payload, "image")
            openai = integration_by_id(payload, "openai")

            self.assertFalse(image["configured"])
            self.assertFalse(openai["configured"])

    def test_saved_openai_image_model_without_key_is_not_configured(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration("openai", field_values={"image_model": "gpt-image-2", "image_size": "1024x1024"})

            payload = integration_status_payload(store)
            openai = integration_by_id(payload, "openai")

            self.assertFalse(openai["configured"])
            self.assertTrue(field_by_id(openai, "image_model")["saved"])

    def test_tavily_tool_fields_are_saved_and_configure_the_integration(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "tavily",
                field_values={
                    "api_key": "tvly-test-key",
                    "search_depth": "advanced",
                    "search_limit": "8",
                    "crawl_max_depth": "2",
                    "research_model": "mini",
                },
            )

            payload = integration_status_payload(store)
            tavily = integration_by_id(payload, "tavily")

            self.assertTrue(tavily["configured"])
            self.assertEqual("saved", field_by_id(tavily, "api_key")["source"])
            self.assertEqual("advanced", field_by_id(tavily, "search_depth")["value"])
            self.assertEqual("8", field_by_id(tavily, "search_limit")["value"])
            self.assertEqual("2", field_by_id(tavily, "crawl_max_depth")["value"])
            self.assertEqual("mini", field_by_id(tavily, "research_model")["value"])
            self.assertNotIn("tvly-test-key", str(payload))

            apply_user_integrations_to_environment(store)
            self.assertEqual("tvly-test-key", os.environ["TAVILY_API_KEY"])
            self.assertEqual("advanced", os.environ["TAVILY_SEARCH_DEPTH"])
            self.assertEqual("8", os.environ["TAVILY_SEARCH_LIMIT"])
            self.assertEqual("2", os.environ["TAVILY_CRAWL_MAX_DEPTH"])
            self.assertEqual("mini", os.environ["TAVILY_RESEARCH_MODEL"])

    def test_saved_byok_is_request_scoped_without_hiding_other_env_providers(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENAI_API_KEY"] = "sk-platform"
            os.environ["OPENAI_MODEL"] = "gpt-platform"
            os.environ["LLM_ALLOWED_PROVIDERS"] = "openai"
            os.environ["OPENAI_ALLOWED_MODELS"] = "gpt-platform"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "openai",
                field_values={"api_key": "sk-user", "model": "gpt-user"},
            )
            store.update_integration(
                "baseten",
                field_values={"api_key": "baseten-user", "model": "byok/model"},
            )

            payload = integration_status_payload(store)

            self.assertEqual("sk-platform", os.environ["OPENAI_API_KEY"])
            self.assertEqual("gpt-platform", os.environ["OPENAI_MODEL"])
            self.assertEqual("openai", os.environ["LLM_ALLOWED_PROVIDERS"])
            self.assertEqual("gpt-platform", os.environ["OPENAI_ALLOWED_MODELS"])
            settings = resolve_user_integration_settings(store)
            runtime = resolve_llm_runtime_config(settings=settings)
            provider = build_llm_provider(runtime_config=runtime, settings=settings)
            self.assertEqual("openai", runtime.provider)
            self.assertEqual("gpt-user", runtime.model)
            self.assertEqual("sk-user", provider.api_key)
            self.assertIn("baseten", runtime.allowed_providers or [])
            openai = integration_by_id(payload, "openai")
            self.assertEqual("saved", field_by_id(openai, "api_key")["source"])
            self.assertEqual("saved", field_by_id(openai, "model")["source"])

            store.clear_integration("openai")
            payload = integration_status_payload(store)
            openai = integration_by_id(payload, "openai")

            self.assertEqual("sk-platform", os.environ["OPENAI_API_KEY"])
            self.assertEqual("gpt-platform", os.environ["OPENAI_MODEL"])
            self.assertEqual("environment", field_by_id(openai, "api_key")["source"])
            self.assertEqual("environment", field_by_id(openai, "model")["source"])

    def test_disabled_integration_is_saved_but_not_applied(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "baseten",
                enabled=False,
                field_values={"api_key": "baseten-secret"},
            )

            payload = integration_status_payload(store)
            baseten = integration_by_id(payload, "baseten")

            self.assertFalse(baseten["enabled"])
            self.assertTrue(baseten["configured"])
            self.assertFalse(baseten["available"])
            self.assertNotIn("BASETEN_API_KEY", os.environ)

    def test_environment_provider_remains_available_when_saved_byok_is_disabled(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["BASETEN_API_KEY"] = "baseten-platform"
            os.environ["BASETEN_MODEL"] = "platform/model"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "baseten",
                enabled=False,
                field_values={"api_key": "baseten-user", "model": "user/model"},
            )

            payload = integration_status_payload(store)
            baseten = integration_by_id(payload, "baseten")

            self.assertFalse(baseten["enabled"])
            self.assertTrue(baseten["environment_configured"])
            self.assertTrue(baseten["available"])
            self.assertEqual("environment", field_by_id(baseten, "api_key")["source"])
            self.assertEqual("environment", field_by_id(baseten, "model")["source"])
            self.assertEqual("baseten-platform", os.environ["BASETEN_API_KEY"])
            self.assertEqual("platform/model", os.environ["BASETEN_MODEL"])

    def test_runtime_defaults_apply_to_llm_selector(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "runtime",
                field_values={
                    "llm_provider": "gmi",
                    "llm_model": "anthropic/claude-fable-5",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("gmi", os.environ["LLM_PROVIDER"])
            self.assertEqual("anthropic/claude-fable-5", os.environ["LLM_MODEL"])

    def test_provider_model_saved_integration_updates_allowlist(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "openai",
                field_values={
                    "api_key": "sk-user-managed",
                    "model": "gpt-5.5",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("gpt-5.5", os.environ["OPENAI_MODEL"])
            self.assertEqual("gpt-5.5", os.environ["OPENAI_ALLOWED_MODELS"])

    def test_saved_provider_config_extends_environment_llm_allowed_providers(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["LLM_ALLOWED_PROVIDERS"] = "openai,simulation"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "baseten",
                enabled=True,
                field_values={
                    "api_key": "baseten-secret",
                    "base_url": "https://inference.baseten.co/v1",
                    "model": "deepseek-ai/DeepSeek-V4-Pro",
                },
            )

            apply_user_integrations_to_environment(store)
            runtime = resolve_llm_runtime_config("baseten", "deepseek-ai/DeepSeek-V4-Pro")

            self.assertEqual(["baseten", "openai", "simulation"], runtime.allowed_providers)
            self.assertEqual("baseten", runtime.provider)
            self.assertEqual("deepseek-ai/DeepSeek-V4-Pro", runtime.model)

    def test_deployed_user_store_rejects_openai_api_key(self) -> None:
        store = SupabaseUserIntegrationStore("user_policy_test")

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "does not accept user-supplied OpenAI API keys"):
                store.update_integration("openai", field_values={"api_key": "sk-user-owned"})

    def test_deployed_user_store_rejects_nvidia_build_api_key(self) -> None:
        store = SupabaseUserIntegrationStore("user_nvidia_policy_test")

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "does not accept user-supplied NVIDIA Build/API Catalog keys"):
                store.update_integration("nvidia", field_values={"api_key": "nvapi-user-owned"})

    def test_deployed_user_store_rejects_cloudflare_api_key(self) -> None:
        store = SupabaseUserIntegrationStore("user_cloudflare_policy_test")

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "does not accept user-supplied Cloudflare API tokens"):
                store.update_integration("cloudflare", field_values={"api_key": "cloudflare-user-owned"})

    def test_deployed_user_store_requires_gmi_key_delegation_confirmation(self) -> None:
        class FakeHostedGmiStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_gmi_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        store = FakeHostedGmiStore()

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "requires confirmation"):
                store.update_integration("gmi", field_values={"api_key": "gmi-user-owned"})

    def test_hosted_user_store_accepts_confirmed_gmi_key(self) -> None:
        class FakeHostedGmiStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_gmi_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        store = FakeHostedGmiStore()
        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            config = store.update_integration(
                "gmi",
                field_values={
                    "api_key": "gmi-user-owned",
                    "key_delegation_confirmation": "project-scoped",
                    "model": "anthropic/claude-fable-5",
                },
            )
        gmi = config.integration_by_id("gmi")

        self.assertIsNotNone(gmi)
        self.assertEqual("gmi-user-owned", gmi.field_value("api_key"))
        self.assertEqual("project-scoped", gmi.field_value("key_delegation_confirmation"))

    def test_deployed_user_store_requires_together_project_key_confirmation(self) -> None:
        class FakeHostedTogetherStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_together_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        store = FakeHostedTogetherStore()

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "project-scoped"):
                store.update_integration("together", field_values={"api_key": "together-user-owned"})

    def test_hosted_user_store_accepts_confirmed_together_project_key(self) -> None:
        class FakeHostedTogetherStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_together_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        store = FakeHostedTogetherStore()
        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            config = store.update_integration(
                "together",
                field_values={
                    "api_key": "together-user-owned",
                    "project_key_confirmation": "dedicated-to-forma",
                    "image_model": "openai/gpt-image-2",
                },
            )
        together = config.integration_by_id("together")

        self.assertIsNotNone(together)
        self.assertEqual("together-user-owned", together.field_value("api_key"))
        self.assertEqual("dedicated-to-forma", together.field_value("project_key_confirmation"))

    def test_local_supabase_user_store_allows_openai_api_key(self) -> None:
        class FakeLocalSupabaseStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_local_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        with isolated_integration_env():
            store = FakeLocalSupabaseStore()
            config = store.update_integration("openai", field_values={"api_key": "sk-user-owned"})
            openai = config.integration_by_id("openai")

            self.assertIsNotNone(openai)
            self.assertEqual("sk-user-owned", openai.field_value("api_key"))

            payload = integration_status_payload(store)
            openai_payload = integration_by_id(payload, "openai")
            api_key_payload = field_by_id(openai_payload, "api_key")

            self.assertEqual("enabled", openai_payload["policy_status"])
            self.assertEqual("", openai_payload["policy_notice"])
            self.assertTrue(api_key_payload["editable"])

    def test_local_file_store_allows_nvidia_for_development(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "nvidia",
                field_values={
                    "api_key": "nvapi-local-dev",
                    "model": "nvidia/z-ai/glm-5.2",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("nvapi-local-dev", os.environ["NVIDIA_API_KEY"])
            self.assertEqual("nvidia/z-ai/glm-5.2", os.environ["NVIDIA_MODEL"])

    def test_local_file_store_applies_cloudflare_provider_settings(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "cloudflare",
                field_values={
                    "api_key": "cloudflare-local-key",
                    "account_id": "test-account",
                    "model": "@cf/google/gemma-4-26b-a4b-it",
                },
            )

            apply_user_integrations_to_environment(store)
            runtime = resolve_llm_runtime_config("cloudflare", "@cf/google/gemma-4-26b-a4b-it")

            self.assertEqual("cloudflare-local-key", os.environ["CLOUDFLARE_API_TOKEN"])
            self.assertEqual("test-account", os.environ["CLOUDFLARE_ACCOUNT_ID"])
            self.assertEqual("@cf/google/gemma-4-26b-a4b-it", os.environ["CLOUDFLARE_MODEL"])
            self.assertEqual("@cf/google/gemma-4-26b-a4b-it", os.environ["CLOUDFLARE_ALLOWED_MODELS"])
            self.assertEqual("cloudflare", runtime.provider)
            self.assertEqual("@cf/google/gemma-4-26b-a4b-it", runtime.model)

    def test_local_image_integration_can_apply_openai_compatible_settings(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "image",
                field_values={
                    "provider": "openai-compatible",
                    "api_key": "image-provider-secret",
                    "base_url": "https://images.example.test/v1",
                    "model": "vendor/image-model",
                    "size": "1024x1024",
                    "quality": "medium",
                    "output_format": "png",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("openai-compatible", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("image-provider-secret", os.environ["IMAGE_API_KEY"])
            self.assertEqual("https://images.example.test/v1", os.environ["IMAGE_BASE_URL"])
            self.assertEqual("vendor/image-model", os.environ["IMAGE_MODEL"])
            self.assertEqual("1024x1024", os.environ["IMAGE_SIZE"])
            self.assertEqual("medium", os.environ["IMAGE_QUALITY"])
            self.assertEqual("png", os.environ["IMAGE_OUTPUT_FORMAT"])

    def test_huggingface_image_config_becomes_active_image_provider(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OPENAI_API_KEY"] = "sk-env-openai"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "openai",
                field_values={
                    "model": "gpt-5.5",
                    "image_model": "gpt-image-2",
                },
            )
            store.update_integration(
                "huggingface",
                field_values={
                    "api_key": "hf-scoped-token",
                    "image_model": "black-forest-labs/FLUX.1-schnell",
                    "image_inference_provider": "fal-ai",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("huggingface", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("black-forest-labs/FLUX.1-schnell", os.environ["HUGGINGFACE_IMAGE_MODEL"])
            self.assertEqual("fal-ai", os.environ["HUGGINGFACE_IMAGE_INFERENCE_PROVIDER"])
            self.assertNotIn("IMAGE_MODEL", os.environ)

    def test_gmi_image_config_becomes_active_image_provider(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "gmi",
                field_values={
                    "api_key": "gmi-secret",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("gmi", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("gmi-secret", os.environ["GMI_API_KEY"])
            self.assertNotIn("GMI_IMAGE_BASE_URL", os.environ)
            self.assertNotIn("GMI_IMAGE_MODEL", os.environ)
            self.assertNotIn("GMI_IMAGE_SIZE", os.environ)
            self.assertNotIn("GMI_IMAGE_OUTPUT_FORMAT", os.environ)

    def test_gmi_credentials_do_not_override_platform_vertex_image_provider(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["IMAGE_PROVIDER"] = "vertex"
            os.environ["VERTEX_AI_IMAGE_MODEL"] = "gemini-3.1-flash-image"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "gmi",
                field_values={
                    "api_key": "gmi-secret",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("vertex", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("gemini-3.1-flash-image", os.environ["VERTEX_AI_IMAGE_MODEL"])
            self.assertEqual("gmi-secret", os.environ["GMI_API_KEY"])

    def test_vertex_image_config_becomes_active_image_provider(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "vertex",
                field_values={
                    "project": "forma-vertex-project",
                    "location": "global",
                    "image_model": "gemini-3.1-flash-image",
                    "image_resolution": "2K",
                    "image_aspect_ratio": "16:9",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("vertex", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("forma-vertex-project", os.environ["VERTEX_AI_PROJECT"])
            self.assertEqual("gemini-3.1-flash-image", os.environ["VERTEX_AI_IMAGE_MODEL"])
            self.assertEqual("2K", os.environ["VERTEX_AI_IMAGE_RESOLUTION"])
            self.assertEqual("16:9", os.environ["VERTEX_AI_IMAGE_ASPECT_RATIO"])

    def test_google_cloud_adc_marks_gemini_and_vertex_configured(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            with patch.object(user_integrations, "_google_cloud_auth_available", return_value=True):
                payload = integration_status_payload(store)

            gemini = integration_by_id(payload, "gemini")
            vertex = integration_by_id(payload, "vertex")
            openai = integration_by_id(payload, "openai")

            self.assertTrue(gemini["configured"])
            self.assertTrue(gemini["environment_configured"])
            self.assertTrue(vertex["configured"])
            self.assertTrue(vertex["environment_configured"])
            self.assertFalse(openai["configured"])

    def test_vertex_project_env_marks_vertex_configured_without_adc(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["GOOGLE_CLOUD_PROJECT_ID"] = "forma-vertex-test"
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            payload = integration_status_payload(store)

            vertex = integration_by_id(payload, "vertex")
            gemini = integration_by_id(payload, "gemini")

            self.assertTrue(vertex["configured"])
            self.assertTrue(vertex["environment_configured"])
            self.assertEqual("environment", field_by_id(vertex, "project")["source"])
            self.assertEqual("forma-vertex-test", field_by_id(vertex, "project")["value"])
            self.assertFalse(gemini["configured"])

    def test_together_image_config_becomes_active_image_provider(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "together",
                field_values={
                    "api_key": "together-secret",
                    "image_model": "openai/gpt-image-2",
                    "image_size": "1024x1024",
                    "image_steps": "4",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("together", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("together-secret", os.environ["TOGETHER_API_KEY"])
            self.assertEqual("openai/gpt-image-2", os.environ["TOGETHER_IMAGE_MODEL"])
            self.assertEqual("1024x1024", os.environ["TOGETHER_IMAGE_SIZE"])
            self.assertEqual("4", os.environ["TOGETHER_IMAGE_STEPS"])

    def test_selected_image_provider_wins_over_newer_provider_specific_config(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration("image", field_values={"provider": "gmi"})
            store.update_integration(
                "together",
                field_values={
                    "api_key": "together-secret",
                    "image_model": "openai/gpt-image-2",
                },
            )
            config = store.load()
            image = config.integration_by_id("image")
            together = config.integration_by_id("together")
            assert image is not None
            assert together is not None
            image.updated_at = "2026-07-21T12:00:00Z"
            together.updated_at = "2026-07-21T12:01:00Z"
            store.save(config)

            apply_user_integrations_to_environment(store)

            self.assertEqual("gmi", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("together-secret", os.environ["TOGETHER_API_KEY"])

    def test_selected_image_provider_wins_over_stale_provider_specific_config(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "huggingface",
                field_values={
                    "api_key": "hf-scoped-token",
                    "image_model": "black-forest-labs/FLUX.1-Krea-dev",
                    "image_inference_provider": "fal-ai",
                },
            )
            store.update_integration("image", field_values={"provider": "gmi"})
            config = store.load()
            huggingface = config.integration_by_id("huggingface")
            image = config.integration_by_id("image")
            assert huggingface is not None
            assert image is not None
            huggingface.updated_at = "2026-07-21T12:00:00Z"
            image.updated_at = "2026-07-21T12:01:00Z"
            store.save(config)

            apply_user_integrations_to_environment(store)

            self.assertEqual("gmi", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("hf-scoped-token", os.environ["HF_TOKEN"])
            self.assertEqual("black-forest-labs/FLUX.1-Krea-dev", os.environ["HUGGINGFACE_IMAGE_MODEL"])

    def test_explicit_image_provider_override_wins_over_huggingface_image_config(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "runtime",
                field_values={
                    "image_provider": "openai",
                    "image_model": "gpt-image-2",
                },
            )
            store.update_integration(
                "huggingface",
                field_values={
                    "api_key": "hf-scoped-token",
                    "image_model": "black-forest-labs/FLUX.1-schnell",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("openai", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("gpt-image-2", os.environ["IMAGE_MODEL"])

    def test_enabled_image_generation_provider_applies_when_runtime_defaults_are_disabled(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            store = UserIntegrationStore(Path(tmpdir) / "integrations.json")
            store.update_integration(
                "runtime",
                enabled=False,
                field_values={
                    "image_provider": "openai",
                    "image_model": "gpt-image-2",
                },
            )
            store.update_integration(
                "image",
                enabled=True,
                field_values={"provider": "huggingface"},
            )
            store.update_integration(
                "huggingface",
                enabled=True,
                field_values={
                    "api_key": "hf-scoped-token",
                    "image_model": "black-forest-labs/FLUX.1-schnell",
                    "image_inference_provider": "fal-ai",
                },
            )

            apply_user_integrations_to_environment(store)

            self.assertEqual("huggingface", os.environ["IMAGE_PROVIDER"])
            self.assertEqual("black-forest-labs/FLUX.1-schnell", os.environ["HUGGINGFACE_IMAGE_MODEL"])
            self.assertEqual("fal-ai", os.environ["HUGGINGFACE_IMAGE_INFERENCE_PROVIDER"])
            self.assertNotIn("IMAGE_MODEL", os.environ)

    def test_deployed_user_store_rejects_generic_image_api_key(self) -> None:
        store = SupabaseUserIntegrationStore("user_image_policy_test")

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "does not accept generic user-supplied image provider API keys"):
                store.update_integration("image", field_values={"api_key": "image-provider-secret"})

    def test_deployed_user_store_requires_huggingface_token_scope_confirmation(self) -> None:
        class FakeHostedHuggingFaceStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_hf_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        store = FakeHostedHuggingFaceStore()

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            with self.assertRaisesRegex(ValueError, "requires confirmation"):
                store.update_integration("huggingface", field_values={"api_key": "hf_secret"})

    def test_hosted_user_store_accepts_confirmed_huggingface_token(self) -> None:
        class FakeHostedHuggingFaceStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_hf_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        store = FakeHostedHuggingFaceStore()
        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            config = store.update_integration(
                "huggingface",
                field_values={
                    "api_key": "hf_secret",
                    "token_scope_confirmation": "fine-grained",
                    "model": "Qwen/Qwen2.5-Coder-3B-Instruct:nscale",
                    "model_license": "apache-2.0",
                },
            )
        huggingface = config.integration_by_id("huggingface")

        self.assertIsNotNone(huggingface)
        self.assertEqual("hf_secret", huggingface.field_value("api_key"))
        self.assertEqual("fine-grained", huggingface.field_value("token_scope_confirmation"))

    def test_hosted_user_policy_sanitizes_saved_openai_api_key(self) -> None:
        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            config = user_integrations.UserIntegrationConfig()
            integration = config.ensure_integration("openai")
            integration.set_field("api_key", "sk-legacy-user-owned")
            integration.set_field("model", "gpt-5.5")

            sanitized = user_integrations._sanitize_hosted_user_config(config)
            sanitized_openai = sanitized.integration_by_id("openai")

            self.assertIsNotNone(sanitized_openai)
            self.assertIsNone(sanitized_openai.field_value("api_key"))
            self.assertEqual("gpt-5.5", sanitized_openai.field_value("model"))

    def test_hosted_user_policy_sanitizes_saved_nvidia_api_key(self) -> None:
        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            config = user_integrations.UserIntegrationConfig()
            integration = config.ensure_integration("nvidia")
            integration.set_field("api_key", "nvapi-legacy-user-owned")
            integration.set_field("model", "nvidia/z-ai/glm-5.2")

            sanitized = user_integrations._sanitize_hosted_user_config(config)
            sanitized_nvidia = sanitized.integration_by_id("nvidia")

            self.assertIsNotNone(sanitized_nvidia)
            self.assertIsNone(sanitized_nvidia.field_value("api_key"))
            self.assertEqual("nvidia/z-ai/glm-5.2", sanitized_nvidia.field_value("model"))

    def test_hosted_user_policy_sanitizes_saved_cloudflare_api_key(self) -> None:
        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            config = user_integrations.UserIntegrationConfig()
            integration = config.ensure_integration("cloudflare")
            integration.set_field("api_key", "cloudflare-legacy-user-owned")
            integration.set_field("model", "@cf/google/gemma-4-26b-a4b-it")

            sanitized = user_integrations._sanitize_hosted_user_config(config)
            sanitized_cloudflare = sanitized.integration_by_id("cloudflare")

            self.assertIsNotNone(sanitized_cloudflare)
            self.assertIsNone(sanitized_cloudflare.field_value("api_key"))
            self.assertEqual("@cf/google/gemma-4-26b-a4b-it", sanitized_cloudflare.field_value("model"))

    def test_hosted_nvidia_status_marks_api_key_blocked(self) -> None:
        class FakeHostedNvidiaStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_nvidia_status_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            store = FakeHostedNvidiaStore()
            nvidia = store.config.ensure_integration("nvidia")
            nvidia.set_field("api_key", "nvapi-legacy-user-owned")
            nvidia.set_field("model", "nvidia/z-ai/glm-5.2")

            payload = integration_status_payload(store)
            nvidia_payload = integration_by_id(payload, "nvidia")
            api_key_payload = field_by_id(nvidia_payload, "api_key")

            self.assertEqual("disabled", nvidia_payload["policy_status"])
            self.assertIn("does not accept user-supplied NVIDIA Build/API Catalog keys", nvidia_payload["policy_notice"])
            self.assertFalse(api_key_payload["editable"])
            self.assertTrue(api_key_payload["policy_blocked"])
            self.assertFalse(api_key_payload["configured"])

    def test_hosted_cloudflare_status_marks_api_key_blocked(self) -> None:
        class FakeHostedCloudflareStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_cloudflare_status_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            store = FakeHostedCloudflareStore()
            cloudflare = store.config.ensure_integration("cloudflare")
            cloudflare.set_field("api_key", "cloudflare-legacy-user-owned")
            cloudflare.set_field("model", "@cf/google/gemma-4-26b-a4b-it")

            payload = integration_status_payload(store)
            cloudflare_payload = integration_by_id(payload, "cloudflare")
            api_key_payload = field_by_id(cloudflare_payload, "api_key")

            self.assertEqual("disabled", cloudflare_payload["policy_status"])
            self.assertIn("does not accept user-supplied Cloudflare API tokens", cloudflare_payload["policy_notice"])
            self.assertFalse(api_key_payload["editable"])
            self.assertTrue(api_key_payload["policy_blocked"])
            self.assertFalse(api_key_payload["configured"])

    def test_hosted_gmi_status_marks_api_key_conditional(self) -> None:
        class FakeHostedGmiStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_gmi_status_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            store = FakeHostedGmiStore()
            gmi = store.config.ensure_integration("gmi")
            gmi.set_field("api_key", "gmi-user-owned")
            gmi.set_field("key_delegation_confirmation", "project-scoped")

            payload = integration_status_payload(store)
            gmi_payload = integration_by_id(payload, "gmi")
            api_key_payload = field_by_id(gmi_payload, "api_key")
            confirmation_payload = field_by_id(gmi_payload, "key_delegation_confirmation")

            self.assertEqual("conditional", gmi_payload["policy_status"])
            self.assertIn("Hosted GMI Cloud BYOK is conditional", gmi_payload["policy_notice"])
            self.assertTrue(api_key_payload["editable"])
            self.assertTrue(api_key_payload["policy_conditional"])
            self.assertTrue(api_key_payload["configured"])
            self.assertTrue(confirmation_payload["configured"])

    def test_hosted_together_status_marks_api_key_conditional(self) -> None:
        class FakeHostedTogetherStore(SupabaseUserIntegrationStore):
            def __init__(self) -> None:
                self.config = user_integrations.UserIntegrationConfig()
                self.user_id = "user_together_status_policy_test"
                self.path = Path(".forma/test")

            def load(self) -> user_integrations.UserIntegrationConfig:
                return self.config

            def save(self, config: user_integrations.UserIntegrationConfig) -> user_integrations.UserIntegrationConfig:
                self.config = config
                return config

        with isolated_integration_env():
            os.environ["FORMA_DEPLOYMENT"] = "true"
            store = FakeHostedTogetherStore()
            together = store.config.ensure_integration("together")
            together.set_field("api_key", "together-user-owned")
            together.set_field("project_key_confirmation", "dedicated-to-forma")

            payload = integration_status_payload(store)
            together_payload = integration_by_id(payload, "together")
            api_key_payload = field_by_id(together_payload, "api_key")
            confirmation_payload = field_by_id(together_payload, "project_key_confirmation")

            self.assertEqual("enabled", together_payload["policy_status"])
            self.assertIn("Hosted Together AI image BYOK requires a project-scoped API key", together_payload["policy_notice"])
            self.assertTrue(api_key_payload["editable"])
            self.assertTrue(api_key_payload["policy_conditional"])
            self.assertTrue(api_key_payload["configured"])
            self.assertTrue(confirmation_payload["configured"])

    def test_default_store_uses_encrypted_local_file_unless_workspace_backend_is_supabase(self) -> None:
        with isolated_integration_env():
            os.environ.pop("FORMA_WORKSPACE_INTEGRATIONS_BACKEND", None)
            self.assertIsInstance(default_integration_store(), EncryptedFileIntegrationStore)

    def test_user_store_uses_supabase_when_supabase_is_configured(self) -> None:
        with isolated_integration_env():
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            store = UserIntegrationStore.for_user("user_123")

            self.assertIsInstance(store, SupabaseUserIntegrationStore)
            self.assertEqual("supabase:user_integration_configs/user_123", store.storage_label)

    def test_user_store_file_backend_override_uses_encrypted_local_file(self) -> None:
        with isolated_integration_env():
            os.environ["FORMA_USER_INTEGRATIONS_BACKEND"] = "file"
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            store = UserIntegrationStore.for_user("user_123")

            self.assertIsInstance(store, EncryptedFileIntegrationStore)
            self.assertIn("user_123", str(store.path))

    def test_encrypted_file_store_never_writes_secret_plaintext(self) -> None:
        with isolated_integration_env(), tempfile.TemporaryDirectory() as tmpdir:
            os.environ["FORMA_USER_SECRETS_KEY"] = "test-encryption-key"
            store = EncryptedFileIntegrationStore(Path(tmpdir) / "workspace.enc.json")

            store.update_integration(
                "openai",
                field_values={"api_key": "sk-must-not-appear", "model": "gpt-5.5"},
            )

            contents = store.path.read_text(encoding="utf-8")
            self.assertNotIn("sk-must-not-appear", contents)
            self.assertNotIn("gpt-5.5", contents)
            self.assertIn("encrypted_config", contents)
            loaded = store.load().integration_by_id("openai")
            self.assertIsNotNone(loaded)
            self.assertEqual("sk-must-not-appear", loaded.field_value("api_key"))
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_default_store_uses_supabase_workspace_when_supabase_is_configured(self) -> None:
        with isolated_integration_env():
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            store = default_integration_store()

            self.assertIsInstance(store, SupabaseWorkspaceIntegrationStore)
            self.assertEqual("supabase:workspace_integration_configs/default", store.storage_label)

    def test_sqlite_database_keeps_workspace_integrations_local_with_supabase_credentials(self) -> None:
        with isolated_integration_env():
            os.environ["DATABASE_BACKEND"] = "sqlite"
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            self.assertIsInstance(default_integration_store(), EncryptedFileIntegrationStore)

    def test_workspace_supabase_override_is_explicit_opt_in_from_sqlite(self) -> None:
        with isolated_integration_env():
            os.environ["DATABASE_BACKEND"] = "sqlite"
            os.environ["FORMA_WORKSPACE_INTEGRATIONS_BACKEND"] = "supabase"
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            self.assertIsInstance(default_integration_store(), SupabaseWorkspaceIntegrationStore)

    def test_supabase_database_keeps_workspace_integrations_in_supabase(self) -> None:
        with isolated_integration_env():
            os.environ["DATABASE_BACKEND"] = "supabase"
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            self.assertIsInstance(default_integration_store(), SupabaseWorkspaceIntegrationStore)

    def test_dev_mode_keeps_workspace_integrations_local_despite_override(self) -> None:
        with isolated_integration_env():
            os.environ["FORMA_DEV_MODE"] = "true"
            os.environ["DATABASE_BACKEND"] = "supabase"
            os.environ["FORMA_WORKSPACE_INTEGRATIONS_BACKEND"] = "supabase"
            os.environ["SUPABASE_URL"] = "https://example.supabase.co"
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "service-role-secret"

            self.assertIsInstance(default_integration_store(), EncryptedFileIntegrationStore)

    def test_supabase_workspace_load_failure_only_uses_empty_fallback_locally(self) -> None:
        class BrokenWorkspaceStore(SupabaseWorkspaceIntegrationStore):
            calls = 0

            def _client(self):
                type(self).calls += 1

                class Execute:
                    @property
                    def data(self):
                        return []

                    def execute(self):
                        raise RuntimeError(
                            "{'message': 'Could not query the database for the schema cache. Retrying.', 'code': 'PGRST002'}"
                        )

                class Query:
                    def select(self, *_args):
                        return self

                    def eq(self, *_args):
                        return self

                    def limit(self, *_args):
                        return Execute()

                class Client:
                    def table(self, *_args):
                        return Query()

                return Client()

        with isolated_integration_env(), self.assertLogs("forma_core.user_integrations", level="WARNING") as logs:
            first = apply_user_integrations_to_environment(BrokenWorkspaceStore())
            second = apply_user_integrations_to_environment(BrokenWorkspaceStore())
            os.environ["FORMA_DEPLOYMENT_MODE"] = "hosted"
            user_integrations._WORKSPACE_CONFIG_FAILURE_CACHE.clear()
            with self.assertRaisesRegex(RuntimeError, "PGRST002"):
                apply_user_integrations_to_environment(BrokenWorkspaceStore())

        self.assertEqual([], first.integrations)
        self.assertEqual([], second.integrations)
        self.assertEqual(2, BrokenWorkspaceStore.calls)
        self.assertIn("PGRST002", "\n".join(logs.output))
        self.assertEqual(2, "\n".join(logs.output).count("PGRST002"))


if __name__ == "__main__":
    unittest.main()
