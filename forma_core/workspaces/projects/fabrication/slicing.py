"""Slicer discovery, orchestration, provenance, and project-level isolation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from forma_core.workspaces.projects.fabrication.models import (
    FabricationError,
    SliceRequest,
    SliceResult,
    SliceValidationResult,
    SlicerUnavailableError,
)
from forma_core.workspaces.projects.fabrication.slicers.base import SlicerAdapter
from forma_core.workspaces.projects.fabrication.slicers.cura import CuraSlicerAdapter
from forma_core.workspaces.projects.fabrication.slicers.orca import OrcaSlicerAdapter
from forma_core.workspaces.projects.fabrication.validation import validate_gcode
from forma_core.workspaces.projects.state import ProjectArtifact


class SlicerCapability(BaseModel):
    """Availability metadata for one configured slicer backend."""

    model_config = ConfigDict(extra="forbid")

    name: str
    available: bool
    executable: str | None = None


def _adapters() -> list[SlicerAdapter]:
    """Return the built-in slicer adapter implementations."""
    return [CuraSlicerAdapter(), OrcaSlicerAdapter()]


def discover_slicers() -> list[SlicerCapability]:
    """Return structured availability information without requiring a slicer binary."""
    result = []
    for adapter in _adapters():
        executable = getattr(adapter, "executable_path", lambda: None)()
        result.append(
            SlicerCapability(
                name=adapter.name,
                available=bool(executable),
                executable=str(executable) if executable else None,
            )
        )
    return result


def get_slicer_adapter(backend: str, *, adapters: list[SlicerAdapter] | None = None) -> SlicerAdapter:
    """Resolve an available slicer adapter by stable backend name."""
    name = str(backend or "").strip().lower()
    selected = next((adapter for adapter in (adapters or _adapters()) if adapter.name == name), None)
    if selected is None:
        raise SlicerUnavailableError(f"Unsupported slicer backend: {backend}")
    if not selected.is_available():
        raise SlicerUnavailableError(f"Slicer backend {name!r} is not available.")
    return selected


def _report_artifact(
    result: SliceResult,
    request: SliceRequest,
    validation: SliceValidationResult,
    *,
    elapsed_s: float,
) -> ProjectArtifact:
    """Create a JSON provenance report beside generated G-code."""
    gcode = Path(result.gcode_artifact.uri)
    report_path = gcode.with_suffix(".report.json")
    gcode_checksum = hashlib.sha256(gcode.read_bytes()).hexdigest()
    input_uri = request.mesh_artifact.uri
    input_path = request.mesh_artifact.metadata.get("path") or input_uri
    input_path_obj = Path(str(input_path)).expanduser()
    input_checksum = request.mesh_artifact.checksum
    if input_path_obj.is_file():
        input_checksum = hashlib.sha256(input_path_obj.read_bytes()).hexdigest()
    payload = {
        "project_id": request.project_id,
        "backend": result.backend,
        "profile_name": result.profile_name,
        "printer_name": result.printer_name,
        "native_config": request.profile.native_config,
        "native_settings": request.profile.native_settings,
        "native_filaments": request.profile.native_filaments,
        "material": request.profile.material,
        "nozzle_diameter_mm": request.profile.nozzle_diameter_mm,
        "bed_size_mm": request.profile.bed_size_mm,
        "z_height_mm": request.profile.z_height_mm,
        "input_mesh": str(input_uri),
        "input_mesh_kind": request.mesh_artifact.kind,
        "input_mesh_sha256": str(input_checksum or "").removeprefix("sha256:"),
        "gcode_sha256": gcode_checksum,
        "elapsed_s": round(elapsed_s, 3),
        "warnings": [*result.warnings, *validation.warnings],
        "validation": validation.model_dump(mode="json"),
    }
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_checksum = hashlib.sha256(report_path.read_bytes()).hexdigest()
    return ProjectArtifact(
        artifact_id=f"slice-report-{report_checksum[:16]}",
        kind="slice.report.json",
        uri=str(report_path),
        media_type="application/json",
        checksum=f"sha256:{report_checksum}",
        metadata={"path": str(report_path), "gcode_sha256": gcode_checksum},
    )


def slice_project(request: SliceRequest, *, adapter: SlicerAdapter | None = None) -> SliceResult:
    """Slice an existing mesh, validate it, and emit a reproducibility report."""
    selected = adapter or get_slicer_adapter(request.profile.backend)
    mesh_raw = request.mesh_artifact.metadata.get("path") or request.mesh_artifact.uri
    mesh_path = Path(str(mesh_raw)).expanduser()
    inspection = selected.inspect(mesh_path)
    inspection_valid = inspection.valid if hasattr(inspection, "valid") else inspection.get("valid")
    if inspection_valid is False:
        inspection_errors = inspection.errors if hasattr(inspection, "errors") else [inspection.get("error", "")]
        raise FabricationError("Mesh inspection failed: " + "; ".join(str(error) for error in inspection_errors if error))
    started = time.monotonic()
    result = selected.slice(request)
    output = Path(result.gcode_artifact.uri).expanduser()
    if not output.is_file() or output.stat().st_size == 0:
        raise FabricationError("Slicer returned no G-code output.")
    actual_output_checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    declared_output_checksum = str(result.gcode_artifact.checksum or "").removeprefix("sha256:").lower()
    if declared_output_checksum and declared_output_checksum != actual_output_checksum:
        raise FabricationError("Slicer returned G-code with an invalid checksum.")
    adapter_validation = selected.validate(result)
    generic_validation = validate_gcode(output, request.profile)
    validation = SliceValidationResult(
        valid=adapter_validation.valid and generic_validation.valid,
        errors=[*adapter_validation.errors, *generic_validation.errors],
        warnings=[*adapter_validation.warnings, *generic_validation.warnings],
        movement_commands=generic_validation.movement_commands,
        extrusion_commands=generic_validation.extrusion_commands,
        temperature_commands=generic_validation.temperature_commands,
        min_position=generic_validation.min_position,
        max_position=generic_validation.max_position,
    )
    if not validation.valid:
        raise FabricationError("Generated G-code failed validation: " + "; ".join(validation.errors))
    result.validation = validation
    result.warnings = [*result.warnings, *validation.warnings]
    elapsed_s = time.monotonic() - started
    result.report_artifact = _report_artifact(result, request, validation, elapsed_s=elapsed_s)
    result.gcode_artifact.metadata = {
        **result.gcode_artifact.metadata,
        "elapsed_s": round(elapsed_s, 3),
        "validation": validation.model_dump(mode="json"),
        "report_uri": result.report_artifact.uri,
    }
    return result


def ensure_slice_artifact(
    project: Any,
    *,
    fabrication_request: SliceRequest,
    required: bool,
    adapter: SlicerAdapter | None = None,
) -> SliceResult | None:
    """Run slicing downstream of CAD while isolating optional failures in metadata."""
    metadata = dict(project.assembly_metadata or {})
    try:
        result = slice_project(fabrication_request, adapter=adapter)
    except Exception as exc:
        metadata["fabrication"] = {"status": "failed", "required": required, "error": str(exc)[:500]}
        project.assembly_metadata = metadata
        if required:
            raise
        return None
    metadata["fabrication"] = {
        "status": "succeeded",
        "required": required,
        "backend": result.backend,
        "printer_name": result.printer_name,
        "profile_name": result.profile_name,
        "artifacts": [
            artifact.model_dump(mode="json")
            for artifact in (result.gcode_artifact, result.report_artifact)
            if artifact is not None
        ],
    }
    project.assembly_metadata = metadata
    return result


__all__ = [
    "SlicerCapability",
    "discover_slicers",
    "ensure_slice_artifact",
    "get_slicer_adapter",
    "slice_project",
]
