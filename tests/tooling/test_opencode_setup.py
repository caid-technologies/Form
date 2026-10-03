from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class OpenCodeSetupTests(unittest.TestCase):
    def test_local_opencode_config_preserves_existing_settings(self) -> None:
        setup = load_module(
            REPO_ROOT / "scripts" / "development" / "setup-opencode.py",
            "forma_opencode_setup",
        )
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            config_path = workspace / "opencode.json"
            config_path.write_text(
                json.dumps(
                    {
                        "$schema": "https://example.test/schema.json",
                        "model": "provider/model",
                        "mcp": {"other": {"type": "remote", "url": "https://example.test/mcp"}},
                    }
                ),
                encoding="utf-8",
            )

            setup._configure_opencode(workspace)
            payload = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual("provider/model", payload["model"])
            self.assertIn("other", payload["mcp"])
            self.assertEqual("http://127.0.0.1:8000/mcp", payload["mcp"]["forma"]["url"])
            self.assertTrue(payload["mcp"]["forma"]["enabled"])
            self.assertFalse(payload["mcp"]["forma"]["oauth"])

    def test_skill_copy_includes_nested_helper_files(self) -> None:
        setup = load_module(
            REPO_ROOT / "scripts" / "development" / "setup-opencode.py",
            "forma_opencode_setup_skill",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "forma"
            source = root / setup.SKILL_RELATIVE_PATH
            (source / "scripts").mkdir(parents=True)
            (source / "references").mkdir()
            (source / "SKILL.md").write_text("skill", encoding="utf-8")
            (source / "scripts" / "forma.py").write_text("client", encoding="utf-8")
            (source / "references" / "configuration.md").write_text("configuration", encoding="utf-8")

            destination = setup._copy_skill(root, Path(temporary) / "workspace")

            self.assertEqual("skill", (destination / "SKILL.md").read_text(encoding="utf-8"))
            self.assertTrue((destination / "scripts" / "forma.py").is_file())
            self.assertTrue((destination / "references" / "configuration.md").is_file())

    def test_compiler_response_updates_uploadable_manifest(self) -> None:
        client = load_module(
            REPO_ROOT / ".agents" / "skills" / "forma-hardware" / "scripts" / "forma.py",
            "forma_skill_client_manifest",
        )
        with tempfile.TemporaryDirectory() as temporary:
            project_path = Path(temporary) / "forma-project.json"
            project_path.write_text(
                json.dumps(
                    {
                        "format": "forma-project",
                        "version": 1,
                        "project_id": "draft-id",
                        "title": "Draft project",
                        "prompt": "Build a monitor",
                        "project_ir": {"components": [], "nets": []},
                        "artifacts": [{"path": "notes.md"}],
                    }
                ),
                encoding="utf-8",
            )
            compiled = {
                "project_id": "compiled-id",
                "project_ir": {
                    "overview": {"title": "Compiled project"},
                    "assembly_metadata": {
                        "project_id": "compiled-id",
                        "source_prompt": "Build a monitor",
                    },
                    "components": [{"reference": "U1"}],
                    "nets": [],
                },
                "validation": {"critical": [], "warnings": []},
                "mermaid_code": "flowchart LR\n  U1 --> U2",
                "svg_schematic": "<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>",
            }

            client._update_project_manifest(str(project_path), compiled)
            payload = json.loads(project_path.read_text(encoding="utf-8"))

            self.assertEqual("compiled-id", payload["project_id"])
            self.assertEqual(compiled["project_ir"], payload["project_ir"])
            self.assertIn({"path": "notes.md"}, payload["artifacts"])
            self.assertEqual(
                {"path": "validation.json", "media_type": "application/json"},
                {key: value for key, value in payload["artifacts"][1].items() if key != "sha256"},
            )
            self.assertEqual("Draft project", payload["title"])
            self.assertTrue((Path(temporary) / "validation.json").is_file())
            self.assertTrue((Path(temporary) / "wiring.mmd").is_file())
            self.assertTrue((Path(temporary) / "schematic.svg").is_file())

    def test_compile_parser_exposes_manifest_update(self) -> None:
        client = load_module(
            REPO_ROOT / ".agents" / "skills" / "forma-hardware" / "scripts" / "forma.py",
            "forma_skill_client_parser",
        )
        args = client.build_parser().parse_args(
            [
                "compile",
                "forma-project.json",
                "--authoring-agent",
                "opencode",
                "--update-project",
            ]
        )
        self.assertTrue(args.update_project)


if __name__ == "__main__":
    unittest.main()
