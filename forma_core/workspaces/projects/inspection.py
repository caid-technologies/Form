"""Shared deterministic project inspection and explicit engineering review.

Inspection reads declared bytes through a caller-owned resolver. Hosted callers
can therefore use authorized storage without accepting local file paths.
Validation and review are separate: a review receipt is an attributable local
declaration, not authentication, certification, or evidence of physical testing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forma_core.validation import build_validation_summary, validate_circuit
from .manifest import ProjectArtifactReference, ProjectManifest, redact_project_secrets, validate_artifact_references
from .models import HardwareIntermediateRepresentation
from .outcomes import evaluate_design_outcome


ArtifactReader = Callable[[ProjectArtifactReference], bytes]


class InspectionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["pass", "fail", "unknown"]
    message: str


class ReviewDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal["forma.engineering-review"] = "forma.engineering-review"
    version: Literal[1] = 1
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=4000)
    recorded_at: str
    authority: Literal["declaration_not_authenticated"] = "declaration_not_authenticated"


class HumanReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "approved", "rejected", "stale", "invalid"]
    authority: Literal["declaration_not_authenticated"] = "declaration_not_authenticated"
    declaration: ReviewDeclaration | None = None


class ProjectInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["forma.project-inspection"] = "forma.project-inspection"
    version: Literal[1] = 1
    project_id: str
    manifest_sha256: str
    is_valid: bool
    project_readiness: Literal["draft", "partial", "complete"]
    checks: list[InspectionCheck]
    validation: dict[str, Any]
    human_review: HumanReview
    assumptions: list[str]
    limitations: list[str]


def manifest_digest(manifest: ProjectManifest) -> str:
    """Pin the full portable design and declarations, excluding its review itself."""
    payload = redact_project_secrets(manifest.model_dump(mode="json"))
    payload.pop("engineering_review", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def cad_intent_digest(project: HardwareIntermediateRepresentation) -> str:
    """Conservatively bind generated geometry to its declared design inputs."""
    payload = {name: getattr(project, name, None) for name in
               ("components", "part_definitions", "mechanical", "system_architecture")}
    payload = {name: ([item.model_dump(mode="json") for item in value] if isinstance(value, list)
                      else value.model_dump(mode="json") if isinstance(value, BaseModel) else value)
               for name, value in payload.items()}
    payload["component_asset_refs"] = project.assembly_metadata.get("component_asset_refs", {})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _human_review(manifest: ProjectManifest, digest: str) -> HumanReview:
    raw = (manifest.model_extra or {}).get("engineering_review")
    if raw is None:
        return HumanReview(status="pending")
    try:
        declaration = ReviewDeclaration.model_validate(raw)
    except (ValueError, TypeError):
        return HumanReview(status="invalid")
    return HumanReview(status=declaration.decision if declaration.manifest_sha256 == digest else "stale",
                       declaration=declaration)


def _inspect_step(content: bytes) -> InspectionCheck:
    """Check actual geometry bytes; do not execute bundled Python model source."""
    try:
        import cadquery as cq
    except ImportError:
        return InspectionCheck(name="cad_geometry", status="unknown", message="CadQuery/OCCT is required to inspect native STEP solids.")
    try:
        with tempfile.TemporaryDirectory(prefix="forma-step-inspection-") as directory:
            source = Path(directory) / "assembly.step"
            source.write_bytes(content)
            solids = cq.importers.importStep(str(source)).solids().vals()
            valid = bool(solids) and all(solid.isValid() and solid.Volume() > 0 for solid in solids)
    except Exception:
        valid = False
    return InspectionCheck(name="cad_geometry", status="pass" if valid else "fail",
                            message="Native STEP import checks nonempty valid solids with positive volume; dimensional intent and fabrication remain engineering review obligations.")


def inspect_project(manifest: ProjectManifest, *, read_artifact: ArtifactReader | None = None) -> ProjectInspection:
    """Recompute supported facts; never trust agent-supplied validation or approval.

    is_valid requires every declared artifact to have matching available bytes.
    Empty/draft designs can be schema-valid; readiness and human review remain
    independent so callers cannot infer production approval from this boolean.
    """
    checks: list[InspectionCheck] = []
    checks.append(InspectionCheck(name="manifest_version",
        status="pass" if manifest.format == "forma-project" and type(manifest.version) is int and manifest.version == 1 else "fail",
        message="Supported forma-project v1 contract required."))
    digest = manifest_digest(manifest)
    validation: dict[str, Any] = {"critical": [], "warning": [], "info": []}
    readiness: Literal["draft", "partial", "complete"] = "draft"
    assumptions: list[str] = []
    declared = (manifest.model_extra or {}).get("assumptions", [])
    if isinstance(declared, list):
        assumptions.extend(item for item in declared if isinstance(item, str))
    cad: dict[str, Any] = {}
    cad_required = False
    try:
        from forma_core.config.compatibility import ensure_supported_hardware_ir_version
        ensure_supported_hardware_ir_version(manifest.project_ir)
        project = HardwareIntermediateRepresentation.model_validate(manifest.project_ir)
        issues = validate_circuit(project.components, project.nets, project.requirements)
        summary = build_validation_summary(issues)
        validation = summary.model_dump(mode="json")
        readiness = evaluate_design_outcome(project).project_readiness
        checks.append(InspectionCheck(name="hardware_ir", status="pass", message="HardwareIR schema parsed."))
        checks.append(InspectionCheck(name="electrical_rules", status="fail" if summary.critical else "pass",
                                      message=f"{len(summary.critical)} critical findings; {len(summary.warning)} warnings."))
        cad = project.cad_model if isinstance(project.cad_model, dict) else {}
        cad_required = bool(project.mechanical and (project.mechanical.cad_operations or project.mechanical.mechanism_benchmark))
        if cad:
            pinned = cad.get("intent_sha256")
            checks.append(InspectionCheck(name="cad_source", status=("unknown" if not pinned else "pass"
                if pinned == cad_intent_digest(project) else "fail"),
                message="Generated CAD must be bound to the current declared geometry inputs; recompile missing or stale bindings."))
        elif cad_required:
            checks.append(InspectionCheck(name="cad_source", status="fail", message="Declared CAD operations have no compiled geometry."))
        declared_assumptions = (project.assembly_metadata or {}).get("assumptions", [])
        for value in declared_assumptions if isinstance(declared_assumptions, list) else []:
            if isinstance(value, str):
                if value not in assumptions:
                    assumptions.append(value)
    except (ValidationError, TypeError, ValueError):
        checks.append(InspectionCheck(name="hardware_ir", status="fail", message="HardwareIR schema or deterministic validation failed."))
    try:
        references = validate_artifact_references([item.model_dump(mode="json") for item in manifest.artifacts])
        checks.append(InspectionCheck(name="artifact_declarations", status="pass", message="Artifact paths and integrity declarations parsed."))
    except (ValueError, TypeError):
        references = []
        checks.append(InspectionCheck(name="artifact_declarations", status="fail", message="Artifact declarations are invalid, ambiguous, or missing integrity metadata."))
    verified_contents: dict[str, bytes] = {}
    for raw in references:
        reference = ProjectArtifactReference.model_validate(raw)
        name = f"artifact:{reference.path}"
        if read_artifact is None:
            checks.append(InspectionCheck(name=name, status="unknown", message="Artifact bytes were not supplied for verification."))
            continue
        try:
            content = read_artifact(reference)
            if not isinstance(content, bytes):
                raise TypeError("Artifact resolver must return bytes.")
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise ValueError("Checksum mismatch.")
            if reference.size_bytes is not None and len(content) != reference.size_bytes:
                raise ValueError("Size mismatch.")
        except (OSError, ValueError, TypeError, RuntimeError):
            checks.append(InspectionCheck(name=name, status="fail", message="Artifact is missing, inaccessible, or differs from its declared bytes."))
        else:
            verified_contents[reference.path] = content
            checks.append(InspectionCheck(name=name, status="pass", message="SHA-256 and declared size match available bytes."))
    if cad_required:
        by_path = {item["path"]: item for item in references}
        evidence_ok = cad.get("adapter") == "forma-opencad" and cad.get("generated") is True
        for field in ("path", "preview_path", "model_source_path", "feature_tree_path"):
            evidence_ok = evidence_ok and isinstance(cad.get(field), str) and cad[field] in by_path
        if evidence_ok:
            evidence_ok = by_path[cad["path"]].get("media_type") == "model/step" and by_path[cad["path"]]["sha256"] == cad.get("sha256")
        checks.append(InspectionCheck(name="cad_artifacts", status="pass" if evidence_ok else "fail",
            message="Native CAD requires matching STEP, preview, editable source and feature-tree artifact declarations; intent metadata alone does not establish geometry."))
        if evidence_ok and cad["path"] in verified_contents:
            checks.append(_inspect_step(verified_contents[cad["path"]]))
        elif evidence_ok:
            checks.append(InspectionCheck(name="cad_geometry", status="unknown", message="Native STEP bytes are unavailable for geometric inspection."))
    return ProjectInspection(
        project_id=manifest.project_id, manifest_sha256=digest,
        is_valid=all(item.status == "pass" for item in checks), project_readiness=readiness,
        checks=checks, validation=validation, human_review=_human_review(manifest, digest), assumptions=assumptions,
        limitations=[
            "Electrical checks cover implemented rules and supplied component facts; they do not establish physical correctness.",
            "Artifact hashes establish byte integrity, not geometric validity, manufacturability, or persistent face identity.",
            "Generated descriptions, assumptions and declared measurements require engineering review.",
            "A review receipt records a caller declaration; reviewer identity is not authenticated by this portable format.",
        ],
    )


def record_project_review(manifest: ProjectManifest, *, decision: Literal["approved", "rejected"],
                          reviewer: str, note: str = "") -> ProjectManifest:
    """Record a human's explicit declaration against exactly this manifest digest."""
    declaration = ReviewDeclaration(manifest_sha256=manifest_digest(manifest), decision=decision,
                                    reviewer=reviewer.strip(), note=note,
                                    recorded_at=datetime.now(timezone.utc).isoformat())
    reviewed = manifest.model_copy(deep=True)
    reviewed.engineering_review = declaration.model_dump(mode="json")
    return reviewed


def compile_project(project: HardwareIntermediateRepresentation, *, project_id: str | None = None,
                    required_cad: bool = True, authoring_agent: str = "local", workflow: str = "default") -> HardwareIntermediateRepresentation:
    """Compile authored intent using the same validators and native CAD boundary.

    No inference or project-revision write occurs here. Authorization, revision
    guards and review decisions remain with the owning application boundary.
    """
    from .cad_generation import CAD_ADAPTER_NAME, ensure_native_cad_model
    compiled = project.model_copy(deep=True)
    issues = validate_circuit(compiled.components, compiled.nets, compiled.requirements)
    compiled.validation = build_validation_summary(issues)
    compiled.is_valid = not compiled.validation.critical
    cad = compiled.cad_model
    if (isinstance(cad, dict) and cad.get("generated") is True
            and cad.get("adapter") == CAD_ADAPTER_NAME
            and cad.get("intent_sha256") != cad_intent_digest(compiled)):
        # Legacy enclosure/component builds also have editable inputs. Retire
        # their derived geometry before the existing "provided CAD" fast path.
        compiled.cad_model = None
    generated = ensure_native_cad_model(compiled, project_id=project_id, required=required_cad,
                                        authoring_agent=authoring_agent, workflow=workflow)
    if generated is True and isinstance(compiled.cad_model, dict):
        compiled.cad_model["intent_sha256"] = cad_intent_digest(compiled)
    return compiled


__all__ = ["ArtifactReader", "ProjectInspection", "ReviewDeclaration", "compile_project", "inspect_project",
           "manifest_digest", "record_project_review"]
