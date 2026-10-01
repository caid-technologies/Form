import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from forma_core.config.contract import resolve_runtime_contract
from forma_core.agents.orchestrator import HardwarePipelineOrchestrator
from forma_core.llm import LLMProviderPreflightError, LLMProviderValidation


class RuntimeContractTests(unittest.TestCase):
    def test_contract_describes_rejected_production_provider_without_enabling_it(self) -> None:
        for rejection in ("billing", "unchecked", "fallback"):
            with self.subTest(rejection=rejection):
                validation = LLMProviderValidation(
                    provider="vertex", requested_model="gemini-3.6-flash",
                    actual_model="gemini-2.5-flash" if rejection == "fallback" else None,
                    requested_model_available=rejection == "unchecked", strict_mode=True,
                    fallback_active=rejection == "fallback",
                    model_availability_checked=rejection != "unchecked",
                    validation_error="BILLING_DISABLED" if rejection == "billing" else None,
                )
                provider = SimpleNamespace(
                    is_configured=True, model_name=validation.requested_model,
                    validate_configured_model=Mock(return_value=validation),
                )
                with patch.dict(os.environ, {
                    "FORMA_DEV_MODE": "false", "LLM_PROVIDER": "vertex",
                    "LLM_MODEL": validation.requested_model, "VERTEX_AI_PROJECT": "test-project",
                }, clear=True), patch("forma_core.agents.orchestrator.build_llm_provider", return_value=provider):
                    contract = resolve_runtime_contract(
                        image_config={"request_capable": False},
                        workflows=[{"id": "default", "label": "Catalog", "description": "Catalog"}],
                    )
                    # All other debug-config callers retain the strict default.
                    with self.assertRaises(LLMProviderPreflightError):
                        HardwarePipelineOrchestrator().get_debug_config()
                self.assertFalse(contract["generation"]["ready"])
                self.assertFalse(contract["generation"]["available"])
                self.assertTrue(contract["generation"]["reason"])

    def test_contract_owns_runtime_image_workflow_and_setup_decisions(self) -> None:
        environment = {
            "FORMA_DEV_MODE": "true",
            "LLM_PROVIDER": "cloudflare",
            "LLM_MODEL": "@cf/google/gemma-4-26b-a4b-it",
            "LLM_ALLOWED_PROVIDERS": "cloudflare,openai",
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "account-id",
            "CLOUDFLARE_MODEL": "@cf/google/gemma-4-26b-a4b-it",
            "OPENAI_API_KEY": "openai-token",
            "OPENAI_MODEL": "gpt-provider-specific",
        }
        with patch.dict(os.environ, environment, clear=True):
            contract = resolve_runtime_contract(
                llm_config={"live_generation_enabled": True, "validation_error": None},
                image_config={
                    "default_enabled": True,
                    "request_capable": True,
                    "request_provider": "gmi",
                    "request_model_name": "gpt-image-2",
                    "reason": None,
                },
                workflows=[
                    {"id": "default", "label": "Catalog", "description": "Catalog"},
                    {"id": "web_research", "label": "Web Research", "description": "Research"},
                ],
            )

        self.assertEqual(1, contract["contract_version"])
        self.assertEqual("backend", contract["authority"])
        self.assertEqual("web_research", contract["workflow"]["default_id"])
        self.assertTrue(contract["images"]["generate_by_default"])
        self.assertFalse(contract["provider_setup"]["required"])
        self.assertTrue(contract["deployment"]["hosted_chat_enabled"])
        self.assertFalse(contract["deployment"]["authoring_mode_enabled"])
        self.assertFalse(contract["deployment"]["authoring_access"])
        self.assertEqual(
            ("cloudflare", "@cf/google/gemma-4-26b-a4b-it"),
            (
                contract["generation"]["selected_llm"]["provider"],
                contract["generation"]["selected_llm"]["model"],
            ),
        )
        openai_models = [
            option["model"]
            for option in contract["generation"]["llm_options"]
            if option["provider"] == "openai"
        ]
        self.assertIn("gpt-provider-specific", openai_models)
        self.assertNotIn("@cf/google/gemma-4-26b-a4b-it", openai_models)

    def test_contract_reports_provider_setup_requirements(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            contract = resolve_runtime_contract(
                llm_config={
                    "live_generation_enabled": False,
                    "validation_error": "No live provider is configured.",
                },
                image_config={
                    "default_enabled": False,
                    "request_capable": False,
                    "provider": "none",
                    "reason": "Image provider API key is missing.",
                },
                workflows=[{"id": "default", "label": "Catalog", "description": "Catalog"}],
            )

        self.assertFalse(contract["generation"]["ready"])
        self.assertFalse(contract["generation"]["available"])
        self.assertTrue(contract["provider_setup"]["required"])
        self.assertTrue(contract["provider_setup"]["llm_required"])
        self.assertTrue(contract["provider_setup"]["image_required"])
        self.assertTrue(contract["deployment"]["hosted_chat_enabled"])

    def test_hosted_deployment_disables_chat_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORMA_DEPLOYMENT_MODE": "hosted",
                "FORMA_DEVELOPMENT_MODE": "false",
            },
            clear=True,
        ):
            contract = resolve_runtime_contract(
                llm_config={"live_generation_enabled": False, "validation_error": "Unavailable."},
                image_config={"request_capable": False},
                workflows=[{"id": "default", "label": "Catalog", "description": "Catalog"}],
            )

        self.assertFalse(contract["deployment"]["hosted_chat_enabled"])
        self.assertFalse(contract["deployment"]["authoring_mode_enabled"])
        self.assertFalse(contract["deployment"]["authoring_access"])

    def test_authoring_mode_surfaces_in_the_runtime_contract_without_granting_user_access(self) -> None:
        with patch.dict(os.environ, {"FORMA_AUTHORING_MODE_ENABLED": "true"}, clear=True):
            contract = resolve_runtime_contract(
                llm_config={"live_generation_enabled": False, "validation_error": "Unavailable."},
                image_config={"request_capable": False},
                workflows=[{"id": "default", "label": "Catalog", "description": "Catalog"}],
            )

        self.assertTrue(contract["deployment"]["authoring_mode_enabled"])
        self.assertFalse(contract["deployment"]["authoring_access"])

    def test_config_can_select_catalog_as_the_default_workflow(self) -> None:
        with patch.dict(os.environ, {"FORMA_DEFAULT_GENERATION_WORKFLOW": "default"}, clear=True):
            contract = resolve_runtime_contract(
                llm_config={"live_generation_enabled": True, "validation_error": None},
                image_config={"request_capable": True},
                workflows=[
                    {"id": "default", "label": "Catalog", "description": "Catalog"},
                    {"id": "web_research", "label": "Web Research", "description": "Research"},
                ],
            )

        self.assertEqual("default", contract["workflow"]["default_id"])


if __name__ == "__main__":
    unittest.main()
