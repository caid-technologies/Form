"""Generate a Forma project object using a local Ollama model."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from forma_core.config import config


OLLAMA_MODEL = config.get("OLLAMA_MODEL", "qwen3:8b") or "qwen3:8b"
OLLAMA_BASE_URL = (
    config.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    or "http://127.0.0.1:11434/v1"
)

# Configure the runtime before importing generation modules.
for name, value in {
    "FORMA_DEV_MODE": "true",
    "FORMA_DISABLE_GENERATION_FALLBACK": "true",
    "FORMA_STRICT_GENERATION": "true",
    "LLM_DISABLE_FALLBACK": "true",
    "LLM_PROVIDER": "openai-compatible",
    "LLM_ALLOWED_PROVIDERS": "openai-compatible",
    "LLM_BASE_URL": OLLAMA_BASE_URL,
    "LLM_MODEL": OLLAMA_MODEL,
    "OPENAI_COMPATIBLE_ALLOWED_MODELS": OLLAMA_MODEL,
    "LLM_ALLOW_NO_API_KEY": "true",
    "LLM_RESPONSE_FORMAT": "json_object",
    "OPENAI_FALLBACK_MODEL": "",
    "LLM_FALLBACK_MODEL": "",
    "LLM_TIMEOUT_SECONDS": "1200",
    "IMAGE_OUTPUT_ENABLED": "false",
}.items():
    config.set_default(name, value)

database_path = Path(__file__).resolve().with_name("forma-local-test.db")
config.update(
    {
        "DATABASE_BACKEND": "sqlite",
        "SQLITE_DATABASE_URL": f"sqlite:///{database_path}",
    }
)

from forma_core.workers.generation import HardwareIntermediateRepresentationGenerationEngine  # noqa: E402
from forma_core.workspaces.design_briefs import (  # noqa: E402
    DESIGN_BRIEF_SCHEMA_VERSION,
    DesignBrief,
    DesignBriefReadiness,
)
from forma_core.workspaces.projects.objects import (  # noqa: E402
    FormaProjectObject,
    attach_project_object_metadata,
    build_project_object,
)


def generate_project() -> FormaProjectObject:
    """Generate a Forma project object from a manually constructed design brief."""
    project_id = uuid4()
    design_brief = DesignBrief(
        schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
        conversation_id="manual-generation-example",
        intent="Create a compact desktop environmental monitor.",
        summary="A compact environmental monitor with an OLED display and USB-C power.",
        requirements=[
            "Measure temperature and relative humidity.",
            "Display live measurements on an OLED screen.",
            "Use USB-C for power.",
        ],
        constraints=["Use commonly available components."],
        references=[],
        requested_outputs=["bill of materials", "wiring plan", "assembly guide"],
        validation_criteria=["All electrical components must have valid connections."],
        unresolved_questions=[],
        assumptions=["The device will be used indoors."],
        readiness=DesignBriefReadiness.READY,
        design_brief_id=uuid4(),
        project_id=project_id,
        brief_version=1,
        previous_version=None,
        created_at=datetime.now(timezone.utc),
    )

    engine = HardwareIntermediateRepresentationGenerationEngine(
        provider_name="openai-compatible",
        model_name=OLLAMA_MODEL,
        use_simulation=False,
        generate_image=False,
    )
    revision_draft = engine.generate(design_brief)
    hardware_ir = attach_project_object_metadata(revision_draft.state)
    return build_project_object(hardware_ir)


def main() -> None:
    """Generate a project and print its JSON representation."""
    print(f"Using Ollama model: {OLLAMA_MODEL}")
    print(f"Ollama endpoint: {OLLAMA_BASE_URL}")
    project = generate_project()
    print(project.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
