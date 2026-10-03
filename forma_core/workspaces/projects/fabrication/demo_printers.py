"""Trusted demo printer targets used by the hosted STEP-to-G-code export flow."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from forma_core.config import config
from forma_core.workspaces.projects.fabrication.models import PrinterConfigurationError, SliceProfile
from forma_core.workspaces.projects.fabrication.slicers.orca import OrcaSlicerAdapter


@dataclass(frozen=True)
class DemoPrinterSpec:
    """Stable CAID printer identifier mapped to an OrcaSlicer system preset."""

    printer_id: str
    display_name: str
    vendor_directory: str
    machine_filename: str
    bed_size_mm: tuple[float, float]
    z_height_mm: float
    fallback_process_name: str
    fallback_filament_name: str


DEMO_PRINTERS: tuple[DemoPrinterSpec, ...] = (
    DemoPrinterSpec(
        printer_id="creality_ender_3_04",
        display_name="Creality Ender-3",
        vendor_directory="Creality",
        machine_filename="Creality Ender-3 0.4 nozzle.json",
        bed_size_mm=(220.0, 220.0),
        z_height_mm=250.0,
        fallback_process_name="0.20mm Standard @Creality Ender3",
        fallback_filament_name="Creality Generic PLA",
    ),
    DemoPrinterSpec(
        printer_id="bambu_a1_04",
        display_name="Bambu Lab A1",
        vendor_directory="BBL",
        machine_filename="Bambu Lab A1 0.4 nozzle.json",
        bed_size_mm=(256.0, 256.0),
        z_height_mm=256.0,
        fallback_process_name="0.20mm Standard @BBL A1",
        fallback_filament_name="Bambu PLA Basic @BBL A1",
    ),
)


def get_demo_printer(printer_id: str) -> DemoPrinterSpec:
    """Return a supported demo printer or raise a configuration error."""
    normalized = str(printer_id or "").strip().lower()
    selected = next((printer for printer in DEMO_PRINTERS if printer.printer_id == normalized), None)
    if selected is None:
        supported = ", ".join(printer.printer_id for printer in DEMO_PRINTERS)
        raise PrinterConfigurationError(f"Unsupported printer {printer_id!r}. Supported printers: {supported}.")
    return selected


def _candidate_profile_roots(adapter: OrcaSlicerAdapter) -> list[Path]:
    """Return explicit and conventional OrcaSlicer system-profile locations."""
    candidates: list[Path] = []
    configured = str(config.optional("FORMA_ORCA_PROFILE_ROOT") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    executable = adapter.executable_path()
    if executable is not None:
        executable = executable.resolve()
        candidates.extend(
            [
                executable.parent / "resources" / "profiles",
                executable.parent.parent / "resources" / "profiles",
                executable.parent.parent / "Resources" / "profiles",
                executable.parent.parent / "share" / "OrcaSlicer" / "resources" / "profiles",
            ]
        )
    candidates.extend(
        [
            Path("/usr/share/OrcaSlicer/resources/profiles"),
            Path("/usr/share/orca-slicer/resources/profiles"),
            Path("/opt/OrcaSlicer/resources/profiles"),
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key not in seen:
            seen.add(key)
            unique.append(candidate.expanduser())
    return unique


def orca_profile_root(adapter: OrcaSlicerAdapter | None = None) -> Path:
    """Resolve the OrcaSlicer system profile root used for trusted presets."""
    selected_adapter = adapter or OrcaSlicerAdapter()
    for candidate in _candidate_profile_roots(selected_adapter):
        if candidate.is_dir():
            return candidate.resolve()
    raise PrinterConfigurationError(
        "OrcaSlicer system profiles are unavailable. Set FORMA_ORCA_PROFILE_ROOT to OrcaSlicer's resources/profiles directory."
    )


def _profile_payload(path: Path) -> dict[str, Any]:
    """Read one OrcaSlicer JSON profile and require an object payload."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrinterConfigurationError(f"Could not read OrcaSlicer profile: {path}") from exc
    if not isinstance(payload, dict):
        raise PrinterConfigurationError(f"OrcaSlicer profile is not a JSON object: {path}")
    return payload


def _find_named_profile(directory: Path, profile_name: str) -> Path:
    """Find a system profile by its canonical JSON `name` field."""
    expected = str(profile_name or "").strip()
    if not expected:
        raise PrinterConfigurationError(f"No profile name was supplied for {directory}.")
    if not directory.is_dir():
        raise PrinterConfigurationError(f"OrcaSlicer profile directory is missing: {directory}")
    for path in sorted(directory.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and str(payload.get("name") or "").strip() == expected:
            return path.resolve()
    raise PrinterConfigurationError(f"OrcaSlicer profile {expected!r} was not found under {directory}.")


def _first_profile_name(value: Any, fallback: str) -> str:
    """Normalize an Orca default profile value that may be a scalar or list."""
    if isinstance(value, list):
        for item in value:
            if str(item or "").strip():
                return str(item).strip()
    if str(value or "").strip():
        return str(value).strip()
    return fallback


def resolve_demo_slice_profile(
    printer_id: str,
    *,
    adapter: OrcaSlicerAdapter | None = None,
) -> SliceProfile:
    """Resolve one demo printer to real machine, process, and PLA system profiles."""
    printer = get_demo_printer(printer_id)
    selected_adapter = adapter or OrcaSlicerAdapter()
    if not selected_adapter.is_available():
        raise PrinterConfigurationError(
            "OrcaSlicer is unavailable. Install it on the fabrication worker or set FORMA_ORCA_SLICER_PATH."
        )
    root = orca_profile_root(selected_adapter)
    vendor = root / printer.vendor_directory
    machine = vendor / "machine" / printer.machine_filename
    if not machine.is_file():
        raise PrinterConfigurationError(f"OrcaSlicer machine profile is missing: {machine}")
    machine_payload = _profile_payload(machine)
    process_name = _first_profile_name(machine_payload.get("default_print_profile"), printer.fallback_process_name)
    filament_name = _first_profile_name(machine_payload.get("default_filament_profile"), printer.fallback_filament_name)
    process = _find_named_profile(vendor / "process", process_name)
    try:
        filament = _find_named_profile(vendor / "filament", filament_name)
    except PrinterConfigurationError:
        filament = _find_named_profile(root, filament_name)
    return SliceProfile(
        backend="orca",
        printer_name=printer.display_name,
        profile_name=process_name,
        native_config=str(machine.resolve()),
        native_settings=[str(process)],
        native_filaments=[str(filament)],
        material="PLA",
        nozzle_diameter_mm=0.4,
        bed_size_mm=printer.bed_size_mm,
        z_height_mm=printer.z_height_mm,
        require_temperatures=True,
        require_extrusion=True,
    )


def profile_fingerprint(profile: SliceProfile) -> str:
    """Hash the resolved system profile files for reproducibility metadata."""
    paths = [profile.native_config, *profile.native_settings, *profile.native_filaments]
    digest = hashlib.sha256()
    for value in paths:
        if not value:
            continue
        path = Path(value)
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def demo_printer_capabilities() -> list[dict[str, Any]]:
    """Return UI-safe availability information for the fixed demo printer set."""
    adapter = OrcaSlicerAdapter()
    capabilities: list[dict[str, Any]] = []
    for printer in DEMO_PRINTERS:
        available = False
        reason: str | None = None
        try:
            resolve_demo_slice_profile(printer.printer_id, adapter=adapter)
            available = True
        except PrinterConfigurationError as exc:
            reason = str(exc)
        capabilities.append(
            {
                "printer_id": printer.printer_id,
                "display_name": printer.display_name,
                "nozzle_mm": 0.4,
                "material": "PLA",
                "layer_height_mm": 0.2,
                "available": available,
                "unavailable_reason": reason,
            }
        )
    return capabilities


__all__ = [
    "DEMO_PRINTERS",
    "DemoPrinterSpec",
    "demo_printer_capabilities",
    "get_demo_printer",
    "orca_profile_root",
    "profile_fingerprint",
    "resolve_demo_slice_profile",
]
