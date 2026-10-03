"""Typed inputs and outputs for project fabrication."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forma_core.workspaces.projects.state import ProjectArtifact


class FabricationError(RuntimeError):
    """Raised when a fabrication operation cannot produce a safe result."""


class PrinterConfigurationError(FabricationError):
    """Raised when a real printer/profile was not resolved."""


class SlicerUnavailableError(FabricationError):
    """Raised when the requested slicer executable is unavailable."""


class SliceInspection(BaseModel):
    """Inspection result for a mesh before it reaches a slicer."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    path: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SliceProfile(BaseModel):
    """Resolved printer and process context for one slicing operation."""

    model_config = ConfigDict(extra="forbid")

    backend: str = Field(min_length=1)
    printer_name: str = Field(min_length=1)
    profile_name: str | None = None
    native_config: str | None = None
    native_settings: list[str] = Field(default_factory=list)
    native_filaments: list[str] = Field(default_factory=list)
    material: str | None = None
    nozzle_diameter_mm: float | None = Field(default=None, gt=0)
    bed_size_mm: tuple[float, float] | None = None
    z_height_mm: float | None = Field(default=None, gt=0)
    require_temperatures: bool = False
    require_extrusion: bool = True

    @field_validator("backend", mode="before")
    @classmethod
    def normalize_backend(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @field_validator("bed_size_mm")
    @classmethod
    def validate_bed_size(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is not None and (value[0] <= 0 or value[1] <= 0):
            raise ValueError("bed_size_mm values must be positive.")
        return value


class SliceRequest(BaseModel):
    """A mesh artifact and an already-resolved fabrication profile."""

    model_config = ConfigDict(extra="forbid")

    mesh_artifact: ProjectArtifact
    profile: SliceProfile
    output_name: str | None = None
    output_path: str | None = None
    project_id: str | None = None


class SliceValidationResult(BaseModel):
    """Deterministic checks applied to generated G-code."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    movement_commands: int = 0
    extrusion_commands: int = 0
    temperature_commands: int = 0
    min_position: tuple[float, float, float] | None = None
    max_position: tuple[float, float, float] | None = None


class SliceResult(BaseModel):
    """G-code plus normalized provenance from one slicer invocation."""

    model_config = ConfigDict(extra="forbid")

    gcode_artifact: ProjectArtifact
    backend: str
    profile_name: str | None = None
    printer_name: str | None = None
    estimated_print_time_s: float | None = None
    filament_length_mm: float | None = None
    filament_mass_g: float | None = None
    layer_count: int | None = None
    warnings: list[str] = Field(default_factory=list)
    validation: SliceValidationResult | None = None
    report_artifact: ProjectArtifact | None = None


__all__ = [
    "FabricationError",
    "PrinterConfigurationError",
    "SliceInspection",
    "SliceProfile",
    "SliceRequest",
    "SliceResult",
    "SliceValidationResult",
    "SlicerUnavailableError",
]
