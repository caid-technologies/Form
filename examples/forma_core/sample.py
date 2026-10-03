"""Generate a Forma project object from a frozen design brief."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from forma_core.workers.generation import HardwareIntermediateRepresentationGenerationEngine
from forma_core.workspaces.design_briefs import (
    DESIGN_BRIEF_SCHEMA_VERSION,
    DesignBrief,
    DesignBriefReadiness,
)
from forma_core.workspaces.projects.objects import (
    FormaProjectObject,
    attach_project_object_metadata,
    build_project_object,
)


def generate_project() -> FormaProjectObject:
    """Generate and return a complete Forma project object."""
    project_id = uuid4()

    design_brief = DesignBrief(
        schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
        design_brief_id=uuid4(),
        project_id=project_id,
        conversation_id="manual-generation-example",
        brief_version=1,
        previous_version=None,
        created_at=datetime.now(timezone.utc),
        intent="Create a compact desktop environmental monitor.",
        summary=(
            "A compact environmental monitor with an OLED display, "
            "temperature and humidity sensing, and USB-C power."
        ),
        requirements=[
            "Measure temperature and relative humidity.",
            "Display live measurements on an OLED screen.",
            "Use USB-C for power.",
        ],
        constraints=[
            "The enclosure should fit on a desktop.",
            "Use commonly available components.",
        ],
        references=[],
        requested_outputs=[
            "hardware design",
            "bill of materials",
            "wiring plan",
            "mechanical plan",
            "assembly guide",
        ],
        validation_criteria=[
            "All electrical components must have valid connections.",
            "The selected power supply must support the complete system.",
        ],
        unresolved_questions=[],
        assumptions=[
            "The device will be used indoors.",
            "Wi-Fi connectivity is not required.",
        ],
        readiness=DesignBriefReadiness.READY,
    )

    engine = HardwareIntermediateRepresentationGenerationEngine(
        use_simulation=True,
        generate_image=False,
    )

    revision_draft = engine.generate(design_brief)

    hardware_ir = attach_project_object_metadata(
        revision_draft.state,
    )

    project = build_project_object(hardware_ir)

    return project


def main() -> None:
    """Generate a project and print its serialized representation."""
    project_1 = generate_project()

    print(project_1.model_dump_json(indent=2))

    mechanical = project_1.get_namespace("product.mech")
    electrical = project_1.get_namespace("product.electrical")
    assembly = project_1.get_namespace("product.assembly")

    print("\nMechanical:")
    print(mechanical.model_dump_json(indent=2) if mechanical else "Not generated")

    print("\nElectrical:")
    print(electrical.model_dump_json(indent=2) if electrical else "Not generated")

    print("\nAssembly:")
    print(assembly.model_dump_json(indent=2) if assembly else "Not generated")


if __name__ == "__main__":
    main()
