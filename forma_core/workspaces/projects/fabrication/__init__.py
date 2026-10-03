"""Deterministic fabrication services that operate downstream of project CAD."""

from forma_core.workspaces.projects.fabrication.models import (
    FabricationError,
    PrinterConfigurationError,
    SliceInspection,
    SliceProfile,
    SliceRequest,
    SliceResult,
    SliceValidationResult,
    SlicerUnavailableError,
)
from forma_core.workspaces.projects.fabrication.printer_config import (
    PrinterProfileReference,
    PrinterRegistry,
    UserPrinterConfig,
    default_printer_config_path,
    load_printer_registry,
    resolve_slice_profile,
    save_printer_registry,
)
from forma_core.workspaces.projects.fabrication.slicing import (
    SlicerCapability,
    discover_slicers,
    ensure_slice_artifact,
    get_slicer_adapter,
    slice_project,
)
from forma_core.workspaces.projects.fabrication.slicers import CuraSlicerAdapter, SlicerAdapter

__all__ = [
    "FabricationError",
    "PrinterConfigurationError",
    "PrinterProfileReference",
    "PrinterRegistry",
    "SliceInspection",
    "SliceProfile",
    "SliceRequest",
    "SliceResult",
    "SliceValidationResult",
    "SlicerCapability",
    "SlicerUnavailableError",
    "UserPrinterConfig",
    "default_printer_config_path",
    "CuraSlicerAdapter",
    "discover_slicers",
    "ensure_slice_artifact",
    "get_slicer_adapter",
    "load_printer_registry",
    "resolve_slice_profile",
    "save_printer_registry",
    "slice_project",
    "SlicerAdapter",
]
