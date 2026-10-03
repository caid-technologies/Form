from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "forma-hardware"
CAD_SCRIPT = SKILL_ROOT / "scripts" / "cad.py"


def load_cad_module():
    spec = importlib.util.spec_from_file_location("forma_cad_skill", CAD_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the Forma CAD adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CadSkillTests(unittest.TestCase):
    def test_compatible_runtime_is_reused_without_pip(self) -> None:
        module = load_cad_module()
        with patch.object(module, "_inspect_runtime") as inspect_runtime, patch.object(module, "_install") as install:
            inspect_runtime.return_value = (
                module.OpenCADRuntime("0.2.4", module.DEFAULT_OPENCAD_REQUIREMENT),
                "",
            )

            runtime = module.ensure_opencad()

        self.assertEqual("0.2.4", runtime.version)
        install.assert_not_called()

    def test_missing_runtime_is_installed_and_verified(self) -> None:
        module = load_cad_module()
        runtime = module.OpenCADRuntime("0.2.4", module.DEFAULT_OPENCAD_REQUIREMENT)
        with patch.object(module, "_inspect_runtime", side_effect=[(None, "package is missing"), (runtime, "")]) as inspect_runtime, patch.object(module, "_install") as install, patch.object(module, "_clear_opencad_modules"):
            result = module.ensure_opencad()

        self.assertEqual(runtime, result)
        install.assert_called_once_with(module.DEFAULT_OPENCAD_REQUIREMENT)
        self.assertEqual(2, inspect_runtime.call_count)

    def test_failed_setup_contains_one_exact_recovery_command(self) -> None:
        module = load_cad_module()
        with patch.object(module, "_inspect_runtime", return_value=(None, "OCCT is missing")), patch.object(
            module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="pip failed"
            ),
        ):
            with self.assertRaises(module.OpenCADError) as raised:
                module.ensure_opencad()

        message = str(raised.exception)
        command = f'python -m pip install "{module.DEFAULT_OPENCAD_REQUIREMENT}"'
        self.assertIn(command, message)
        self.assertEqual(1, message.count(command))

    def test_clean_skill_copy_exports_minimal_step_when_occt_is_available(self) -> None:
        module = load_cad_module()
        try:
            module.ensure_opencad(install=False)
        except module.OpenCADError as exc:
            self.skipTest(f"OpenCAD OCCT integration dependency unavailable: {exc}")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            installed_skill = workspace / ".agents" / "skills" / "forma-hardware"
            shutil.copytree(SKILL_ROOT, installed_skill)
            model = workspace / "assembly.py"
            model.write_text(
                "from opencad import Part\n\nresult = Part(name='Integration box').box(12, 10, 4)\n",
                encoding="utf-8",
            )
            output = workspace / "outputs" / "assembly.step"
            tree = workspace / "outputs" / "assembly.tree.json"
            environment = os.environ.copy()
            environment.pop("FORMA_OPENCAD_REQUIREMENT", None)

            setup = subprocess.run(
                [sys.executable, str(installed_skill / "scripts" / "cad.py"), "setup"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(0, setup.returncode, setup.stderr)

            build = subprocess.run(
                [
                    sys.executable,
                    str(installed_skill / "scripts" / "cad.py"),
                    "build",
                    str(model),
                    str(output),
                    "--tree-output",
                    str(tree),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(0, build.returncode, build.stderr)
            summary = json.loads(build.stdout)
            self.assertTrue(summary["valid"])
            self.assertEqual("0.2.4", summary["opencad_version"])
            self.assertTrue(output.is_file())
            self.assertTrue(tree.is_file())


if __name__ == "__main__":
    unittest.main()
