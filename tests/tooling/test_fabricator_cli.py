from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def run_module(module_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module_name, *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=True,
    )


class FabricatorCliTests(unittest.TestCase):
    def test_fabricator_default_plan_outputs_structured_json(self) -> None:
        completed = run_module(
            "forma_core.fabricator",
            "--material",
            "alumina ceramic powder",
            "--amount",
            "2 kg",
            "--available-equipment",
            "inventory database, furnace, microscope",
        )

        payload = json.loads(completed.stdout)

        self.assertEqual("local", payload["mode"])
        self.assertIn("fabricator_plan", payload)
        self.assertEqual("lattice.run_record", payload["lattice_run"]["record_type"])
        self.assertEqual("fabricator.plan.v0", payload["lattice_run"]["contract_id"])
        workflow = payload["fabricator_plan"]["candidate_workflows"][0]
        self.assertEqual("functional material formulations", workflow["product_family"])
        self.assertEqual("fabricator-initialize", payload["fabricator_plan"]["forma_mcp_handoff"][0]["id"])

    def test_fabricator_prompt_command_uses_correct_name(self) -> None:
        completed = run_module("forma_core.fabricator", "prompt", "--material", "cellulose acetate offcuts")

        self.assertIn("You are Fabricator", completed.stdout)
        self.assertNotIn("Fibricator", completed.stdout)

    def test_fabricator_root_help_lists_subcommands(self) -> None:
        completed = run_module("forma_core.fabricator", "--help")

        self.assertIn("plan", completed.stdout)
        self.assertIn("prompt", completed.stdout)
        self.assertIn("mcp-tools", completed.stdout)
        self.assertIn("card", completed.stdout)

    def test_fabricator_card_command_outputs_lattice_agent_card(self) -> None:
        completed = run_module("forma_core.fabricator", "card")

        payload = json.loads(completed.stdout)

        self.assertEqual("lattice.agent_card", payload["card_type"])
        self.assertEqual("fabricator", payload["agent_id"])
        self.assertEqual("product.fabricator", payload["namespace"])
        self.assertEqual("fabricator.plan.v0", payload["contracts"][0]["id"])
        self.assertIn("Primitive-to-product planning", [item["label"] for item in payload["capabilities"]])

    def test_fabricator_package_exports_schemas(self) -> None:
        import forma_core.fabricator as fabricator

        self.assertEqual("FabricatorPlan", fabricator.FabricatorPlan.__name__)
        self.assertTrue(callable(fabricator.main))

    def test_live_plan_raises_instead_of_returning_a_local_fallback(self) -> None:
        from forma_core.fabricator.main import main

        provider = Mock()
        provider.validate_configured_model.return_value = SimpleNamespace(
            as_debug_dict=lambda: {"provider": "openai"},
            provider="openai",
            requested_model="test-model",
            live_generation_enabled=False,
            validation_error="provider is not configured",
        )

        with patch("forma_core.fabricator.main.build_llm_provider", return_value=provider):
            with self.assertRaisesRegex(RuntimeError, "provider is not configured"):
                main(["plan", "--live", "--provider", "openai"])

        provider.generate_structured.assert_not_called()


if __name__ == "__main__":
    unittest.main()
