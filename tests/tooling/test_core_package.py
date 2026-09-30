from __future__ import annotations

import importlib.metadata
import json
import pathlib
import tomllib
import unittest

import forma_core


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
CORE_DIR = ROOT_DIR / "forma_core"
DIST_NAME = "caid-forma-core"


class CorePackageTests(unittest.TestCase):
    def test_forma_core_exports_package_version(self) -> None:
        self.assertRegex(forma_core.__version__, r"^\d+\.\d+\.\d+")
        self.assertEqual(["__version__"], forma_core.__all__)

    def test_pyproject_declares_installable_typed_core_package(self) -> None:
        pyproject = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(DIST_NAME, pyproject["project"]["name"])
        self.assertEqual(["version"], pyproject["project"]["dynamic"])
        self.assertEqual("apps.api.main:app", pyproject["tool"]["vercel"]["entrypoint"])
        self.assertEqual("forma_core._version.__version__", pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"])
        self.assertEqual("forma_core.cli.main:main", pyproject["project"]["scripts"]["forma-core"])
        self.assertEqual("forma_cli.app:app", pyproject["project"]["scripts"]["forma-oss"])
        self.assertEqual(
            "forma_core.fabricator.main:main",
            pyproject["project"]["scripts"]["fabricator"],
        )
        self.assertNotIn("fibricator", pyproject["project"]["scripts"])
        self.assertIn("forma_core", pyproject["tool"]["setuptools"]["packages"]["find"]["include"])
        self.assertIn("forma_core.*", pyproject["tool"]["setuptools"]["packages"]["find"]["include"])
        self.assertIn("py.typed", pyproject["tool"]["setuptools"]["package-data"]["forma_core"])
        self.assertTrue((CORE_DIR / "py.typed").exists())
        self.assertTrue((CORE_DIR / "fabricator" / "main.py").exists())
        self.assertFalse((ROOT_DIR / "fabricator").exists())

    def test_backend_requirements_use_third_party_dependencies_for_vercel(self) -> None:
        requirements_path = ROOT_DIR / "apps" / "api" / "requirements.txt"
        requirements = [
            line.split("#", 1)[0].strip()
            for line in requirements_path.read_text(encoding="utf-8").splitlines()
        ]
        active_requirements = [line for line in requirements if line]

        self.assertIn("fastapi>=0.124.1", active_requirements)
        self.assertIn("uvicorn", active_requirements)
        self.assertIn("websockets>=12.0", active_requirements)
        self.assertIn("pydantic>=2.12.0", active_requirements)
        self.assertIn("python-dotenv>=1.0.1", active_requirements)
        self.assertIn("sqlalchemy==2.0.31", active_requirements)
        self.assertIn("supabase==2.31.0", active_requirements)
        self.assertNotIn(".[backend]", active_requirements)
        for requirement in active_requirements:
            self.assertFalse(
                requirement.startswith((".", "-e", "file:")),
                "apps/api/requirements.txt must remain deployable without local project paths; "
                f"found {requirement!r}.",
            )
        self.assertFalse(any(line.startswith(f"{DIST_NAME}==") for line in active_requirements))

    def test_root_requirements_avoid_local_paths_for_vercel(self) -> None:
        root_requirements = [
            line.split("#", 1)[0].strip()
            for line in (ROOT_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        ]
        backend_requirements = [
            line.split("#", 1)[0].strip()
            for line in (ROOT_DIR / "apps" / "api" / "requirements.txt").read_text(encoding="utf-8").splitlines()
        ]

        root_active = [line for line in root_requirements if line]
        backend_active = [line for line in backend_requirements if line]

        self.assertEqual(backend_active, root_active)
        for requirement in root_active:
            self.assertFalse(requirement.startswith((".", "-e", "file:")))

    def test_pyproject_backend_extra_contains_fastapi_service_dependencies(self) -> None:
        pyproject = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        backend_deps = set(pyproject["project"]["optional-dependencies"]["backend"])

        self.assertIn("fastapi>=0.124.1", backend_deps)
        self.assertIn("Pillow>=10.0.0", backend_deps)
        self.assertIn("uvicorn", backend_deps)
        self.assertIn("websockets>=12.0", backend_deps)
        self.assertIn("supabase==2.31.0", backend_deps)

    def test_vercel_backend_uses_standard_dependency_discovery(self) -> None:
        vercel_config = json.loads((ROOT_DIR / "vercel.json").read_text(encoding="utf-8"))

        self.assertNotIn("experimentalServices", vercel_config)
        backend = vercel_config["services"]["backend"]
        self.assertEqual(".", backend["root"])
        self.assertEqual("fastapi", backend["framework"])
        self.assertEqual("apps.api.main:app", backend["entrypoint"])
        self.assertNotIn("installCommand", backend)
        for function_key in ("apps.api.main:app", "apps/api/main.py", "main.py"):
            function_config = backend["functions"][function_key]
            self.assertEqual("forma_core/**", function_config["includeFiles"])
            exclude_files = function_config["excludeFiles"]
            self.assertIn("apps/web/**", exclude_files)
            self.assertIn("*.db", exclude_files)
        self.assertNotIn("functions", vercel_config)
        self.assertIn(
            {"source": "/api/(.*)", "destination": {"service": "backend"}},
            vercel_config["rewrites"],
        )

    def test_deployable_applications_live_under_apps(self) -> None:
        self.assertTrue((ROOT_DIR / "apps" / "__init__.py").is_file())
        self.assertTrue((ROOT_DIR / "apps" / "api" / "main.py").is_file())
        self.assertTrue((ROOT_DIR / "apps" / "web" / "package.json").is_file())
        self.assertFalse((ROOT_DIR / "backend").exists())
        self.assertFalse((ROOT_DIR / "frontend").exists())

    def test_root_main_shim_exports_backend_app_for_legacy_vercel_detection(self) -> None:
        import apps.api.main as backend_main
        import main as root_main

        self.assertIs(root_main.app, backend_main.app)
        self.assertIs(root_main.application, backend_main.app)

    def test_installed_distribution_metadata_when_available(self) -> None:
        try:
            distribution = importlib.metadata.distribution(DIST_NAME)
        except importlib.metadata.PackageNotFoundError:
            self.skipTest(f"{DIST_NAME} is not installed in this interpreter")

        import_path = pathlib.Path(forma_core.__file__).resolve()
        distribution_root = pathlib.Path(distribution.locate_file("")).resolve()
        if not import_path.is_relative_to(distribution_root):
            self.skipTest(f"{DIST_NAME} distribution metadata belongs to {distribution_root}, but source import is {import_path}")

        self.assertEqual(forma_core.__version__, distribution.version)

    def test_forma_core_does_not_import_backend_modules(self) -> None:
        offenders: list[str] = []
        for path in CORE_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "from apps.api" in text or "import apps.api" in text or "apps.api." in text:
                offenders.append(str(path.relative_to(ROOT_DIR)))

        self.assertEqual([], offenders)

    def test_generation_package_imports(self) -> None:
        from forma_core.generation import HardwarePipelineOrchestrator, list_workflows
        from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

        self.assertEqual("HardwarePipelineOrchestrator", HardwarePipelineOrchestrator.__name__)
        self.assertEqual("HardwareIntermediateRepresentation", HardwareIntermediateRepresentation.__name__)
        self.assertIn("default", [item["id"] for item in list_workflows()])

    def test_backend_compatibility_wrappers_reexport_core_objects(self) -> None:
        import apps.api.agents.orchestrator as backend_orchestrator
        import apps.api.llm_providers as backend_llm
        import apps.api.models as backend_models
        import apps.api.validation as backend_validation
        from forma_core.agents import orchestrator as core_orchestrator
        from forma_core import validation as core_validation
        from forma_core import llm as core_llm
        from forma_core.workspaces.projects import models as core_models

        self.assertIs(backend_models.HardwareIntermediateRepresentation, core_models.HardwareIntermediateRepresentation)
        self.assertIs(backend_validation.validate_circuit, core_validation.validate_circuit)
        self.assertIs(backend_llm.resolve_llm_runtime_config, core_llm.resolve_llm_runtime_config)
        self.assertIs(
            backend_orchestrator.HardwarePipelineOrchestrator,
            core_orchestrator.HardwarePipelineOrchestrator,
        )


if __name__ == "__main__":
    unittest.main()
