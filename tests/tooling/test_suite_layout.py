from __future__ import annotations

from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT_DIR / "tests"
CONCERNS = {
    "agents",
    "api",
    "integrations",
    "jobs",
    "observability",
    "opencode",
    "persistence",
    "projects",
    "providers",
    "terminal",
    "tooling",
}


class TestSuiteLayoutTests(unittest.TestCase):
    def test_test_root_is_an_index_not_a_flat_module_directory(self) -> None:
        self.assertEqual([], sorted(path.name for path in TESTS_DIR.glob("test_*.py")))

    def test_concern_directories_are_discoverable_packages(self) -> None:
        discovered = {
            path.name
            for path in TESTS_DIR.iterdir()
            if path.is_dir() and not path.name.startswith("__")
        }

        self.assertEqual(CONCERNS, discovered)
        for concern in CONCERNS:
            self.assertTrue((TESTS_DIR / concern / "__init__.py").is_file())
            self.assertTrue(any((TESTS_DIR / concern).glob("test_*.py")))

    def test_test_index_describes_every_concern(self) -> None:
        index = (TESTS_DIR / "README.md").read_text(encoding="utf-8")

        for concern in CONCERNS:
            self.assertIn(f"`{concern}/`", index)


if __name__ == "__main__":
    unittest.main()
