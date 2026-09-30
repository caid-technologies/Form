"""Evidence-based saved design outcome, independent of agent execution status."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from forma_core.validation import validate_circuit
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


class DesignOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_readiness: Literal["draft", "partial", "complete"]
    component_count: int
    pin_count: int
    net_count: int
    is_valid: bool
    critical_count: int
    warning_count: int


def evaluate_design_outcome(project: HardwareIntermediateRepresentation) -> DesignOutcome:
    """Complete means populated and deterministically valid, not physically verified."""
    issues = validate_circuit(project.components, project.nets, project.requirements)
    critical = sum(issue.severity.upper() == "CRITICAL" for issue in issues)
    warnings = sum(issue.severity.upper() == "WARNING" for issue in issues)
    pins = sum(len(component.pins) for component in project.components)
    readiness: Literal["draft", "partial", "complete"] = "partial"
    if not project.components and not project.nets:
        readiness = "draft"
        if project.mechanical and project.mechanical.cad_operations:
            cad = project.cad_model if isinstance(project.cad_model, dict) else {}
            readiness = "complete" if (cad.get("stored_sha256") and cad.get("meshes")
                and project.assembly_metadata.get("cad_generation", {}).get("status") == "succeeded") else "partial"
    elif project.components and pins and project.nets and not critical:
        readiness = "complete"
    # Generation-stage incompleteness remains blocking even with a valid circuit.
    if readiness == "complete" and project.assembly_metadata.get("project_readiness") in {"draft", "partial"}:
        readiness = "partial"
    return DesignOutcome(
        project_readiness=readiness, component_count=len(project.components),
        pin_count=pins, net_count=len(project.nets), is_valid=not critical,
        critical_count=critical, warning_count=warnings,
    )
