from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CursorPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))

    def test_manifest_discovers_skill_mcp_and_committed_logo(self) -> None:
        self.assertRegex(self.manifest["name"], r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual("MPL-2.0", self.manifest["license"])
        self.assertIn("Mozilla Public License", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        for field in ("skills", "mcpServers", "logo"):
            relative = Path(self.manifest[field])
            self.assertFalse(relative.is_absolute(), field)
            self.assertNotIn("..", relative.parts, field)
            self.assertTrue((ROOT / relative).exists(), field)

        skills = sorted((ROOT / self.manifest["skills"]).glob("*/SKILL.md"))
        self.assertEqual([ROOT / ".agents/skills/forma-hardware/SKILL.md"], skills)
        frontmatter = skills[0].read_text(encoding="utf-8").split("---", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^name: forma-hardware$")
        self.assertRegex(frontmatter, r"(?m)^description: \S.+")
        config = json.loads((ROOT / self.manifest["mcpServers"]).read_text(encoding="utf-8"))
        self.assertEqual({"forma": {"url": "http://127.0.0.1:8000/mcp"}}, config["mcpServers"])

    def test_cloud_example_uses_environment_credentials_and_is_opt_in(self) -> None:
        example = ROOT / ".cursor-plugin/mcp-cloud.example.json"
        config = json.loads(example.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "url": "${env:FORMA_MCP_URL}",
                "headers": {"Authorization": "Bearer ${env:FORMA_AUTH_TOKEN}"},
            },
            config["mcpServers"]["forma-cloud"],
        )
        self.assertNotEqual(example, ROOT / self.manifest["mcpServers"])

    def test_skill_resources_and_helpers_work_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory) / "forma"
            plugin.mkdir()
            # Resolve exactly the components exposed by the manifest, without
            # the app checkout or an editable Forma installation beside them.
            for relative in (".cursor-plugin/plugin.json", "LICENSE", self.manifest["mcpServers"], self.manifest["logo"]):
                target = plugin / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            shutil.copytree(
                ROOT / self.manifest["skills"],
                plugin / self.manifest["skills"],
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            skill = plugin / self.manifest["skills"] / "forma-hardware"
            for relative in (
                "references/cad.md", "references/configuration.md",
                "references/hardware-ir.md", "references/cli-only.md", "agents/openai.yaml",
            ):
                self.assertTrue((skill / relative).is_file(), relative)

            # A broken relative reference would leave an installed skill unable
            # to obtain its schema, setup, or CAD instructions.
            for document in skill.rglob("*.md"):
                for link in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                    if "://" not in link:
                        self.assertTrue((document.parent / link.split("#")[0]).is_file(), link)

            for script in ("forma.py", "cad.py", "create_project.py"):
                result = subprocess.run(
                    [sys.executable, "-I", str(skill / "scripts" / script), "--help"],
                    cwd=plugin, capture_output=True, text=True, timeout=15, check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)
            result = subprocess.run(
                [sys.executable, "-I", str(skill / "scripts/forma.py"), "compile", "--help"],
                cwd=plugin, capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("other", result.stdout)


if __name__ == "__main__":
    unittest.main()
