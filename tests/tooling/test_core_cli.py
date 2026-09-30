from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forma_core.cli.main import CLI_LIVE_GENERATION_ENVIRONMENT, build_parser, main
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


class CoreCliTests(unittest.TestCase):
    def test_parser_describes_a_direct_core_cli(self) -> None:
        parser = build_parser()
        self.assertEqual("forma-core", parser.prog)
        self.assertIn("without a backend server", parser.description)

    def test_generate_supports_one_run_image_persistence_and_terminal_output(self) -> None:
        args = build_parser().parse_args([
            "generate",
            "make a sensor",
            "--generate-image",
            "--image-provider",
            "gmi",
            "--show-image",
        ])

        self.assertTrue(args.generate_image)
        self.assertEqual("gmi", args.image_provider)
        self.assertTrue(args.show_image)

    def test_live_generate_disables_fallbacks_and_returns_provider_error(self) -> None:
        observed_environment = {}

        def fail_generation(*args, **kwargs):
            observed_environment.update(
                {name: os.environ.get(name) for name in CLI_LIVE_GENERATION_ENVIRONMENT}
            )
            raise RuntimeError("provider request failed")

        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {name: "false" for name in CLI_LIVE_GENERATION_ENVIRONMENT},
            clear=False,
        ), patch(
            "forma_core.generation.generate_project_with_workflow",
            side_effect=fail_generation,
        ), patch("sys.stderr", stderr):
            exit_code = main(["generate", "make a sensor", "--llm", "openai/test-model"])

            self.assertTrue(all(value == "true" for value in observed_environment.values()))
            self.assertTrue(
                all(os.environ.get(name) == "false" for name in CLI_LIVE_GENERATION_ENVIRONMENT)
            )

        self.assertEqual(2, exit_code)
        self.assertIn("provider request failed", stderr.getvalue())

    def test_unconfigured_live_generate_does_not_persist_simulated_output(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch(
            "forma_core.workspaces.projects.output.persist_project_output"
        ) as persist_project, patch("sys.stderr", stderr):
            exit_code = main(
                ["generate", "make a sensor", "--llm", "openai/unavailable-test-model"]
            )

        self.assertEqual(2, exit_code)
        persist_project.assert_not_called()
        self.assertIn("provider is not configured", stderr.getvalue())

    def test_live_generate_rejects_fallback_metadata_before_persistence(self) -> None:
        fallback_project = HardwareIntermediateRepresentation(
            assembly_metadata={"fallback_mode": True, "workflow_fallback": "simulation"}
        )
        stderr = io.StringIO()
        with patch(
            "forma_core.generation.generate_project_with_workflow",
            return_value=fallback_project,
        ), patch(
            "forma_core.workspaces.projects.output.attach_product_image"
        ) as attach_image, patch(
            "forma_core.workspaces.projects.output.persist_project_output"
        ) as persist_project, patch("sys.stderr", stderr):
            exit_code = main(["generate", "make a sensor", "--llm", "openai/test-model"])

        self.assertEqual(2, exit_code)
        attach_image.assert_not_called()
        persist_project.assert_not_called()
        self.assertIn("CLI fallback output is disabled", stderr.getvalue())

    def test_simulation_explicitly_allows_simulated_generator(self) -> None:
        observed_environment = {}

        def fail_after_observing(*args, **kwargs):
            for name in (
                "FORMA_DISABLE_GENERATION_FALLBACK",
                "FORMA_STRICT_GENERATION",
                "LLM_DISABLE_FALLBACK",
            ):
                observed_environment[name] = os.environ.get(name)
            raise RuntimeError("stop after environment check")

        with patch.dict(
            os.environ,
            {
                "FORMA_DISABLE_GENERATION_FALLBACK": "true",
                "FORMA_STRICT_GENERATION": "true",
                "LLM_DISABLE_FALLBACK": "true",
            },
            clear=False,
        ), patch(
            "forma_core.generation.generate_project_with_workflow",
            side_effect=fail_after_observing,
        ), patch("sys.stderr", io.StringIO()):
            exit_code = main(["generate", "make a sensor", "--simulation"])

        self.assertEqual(2, exit_code)
        self.assertEqual(
            {
                "FORMA_DISABLE_GENERATION_FALLBACK": "false",
                "FORMA_STRICT_GENERATION": "false",
                "LLM_DISABLE_FALLBACK": "false",
            },
            observed_environment,
        )

    def test_workflows_lists_core_workflows_as_json(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["workflows", "--json"])

        self.assertEqual(0, exit_code)
        self.assertIn("default", [item["id"] for item in json.loads(stdout.getvalue())])

    def test_validate_reads_hardware_ir_without_a_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project.json"
            project_path.write_text(json.dumps({"components": [], "nets": []}), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["validate", str(project_path)])

        self.assertEqual(0, exit_code)
        self.assertEqual({"critical": [], "info": [], "warning": []}, json.loads(stdout.getvalue())["validation"])

    def test_core_cli_is_a_package_not_another_flat_module(self) -> None:
        core_root = Path(__file__).resolve().parents[2] / "forma_core"
        self.assertTrue((core_root / "cli" / "main.py").exists())
        self.assertFalse((core_root / "cli.py").exists())


if __name__ == "__main__":
    unittest.main()
