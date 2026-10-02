"""Deterministic STEP + metadata packages with explicit target capabilities."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from typing import Annotated, Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .importers import FUSION, ONSHAPE, SOLIDWORKS

Text = Annotated[str, StringConstraints(max_length=4096)]
MAX_STEP_BYTES = 100 * 1024 * 1024
TARGETS = {
    "solidworks": ("SOLIDWORKS", "import-solidworks.ps1", SOLIDWORKS,
                   "On Windows with SOLIDWORKS installed, run ./import-solidworks.ps1 in PowerShell. "
                   "Use -LegacyImport if 3D Interconnect is disabled. Inspect the new document and Save As a native part/assembly."),
    "onshape": ("Onshape", "import-onshape.py", ONSHAPE,
                "Import geometry.step from the Onshape Documents page, or set ONSHAPE_ACCESS_TOKEN to an OAuth bearer token "
                "with document write scope and run python import-onshape.py --document DOCUMENT_ID --workspace WORKSPACE_ID. "
                "The script uploads to that existing workspace and waits for translation. Metadata stays in metadata.json; "
                "map it to your organization's Onshape properties after import."),
    "fusion360": ("Autodesk Fusion 360", "import-fusion.py", FUSION,
                  "In Fusion, create a Python script in Utilities > Scripts and Add-Ins. Replace its Python file with "
                  "import-fusion.py and copy geometry.step, metadata.json and manifest.json beside it. Run the script, "
                  "inspect the new document, and save it to your chosen project."),
}


class CadMetadata(BaseModel):
    """Explicit CAD properties; arbitrary project state and credentials are not exported."""

    model_config = ConfigDict(extra="forbid")
    title: Text = "Forma model"
    part_number: Text = ""
    revision: Text = ""
    description: Text = ""
    material: Text = ""
    source_project_id: Text = ""
    source_revision_id: Text = ""
    properties: dict[Annotated[str, StringConstraints(min_length=1, max_length=120)], Text] = Field(default_factory=dict, max_length=100)

    @field_validator("properties")
    @classmethod
    def reject_secret_properties(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            compact = "".join(c for c in key.lower() if c.isalnum())
            if any(marker in compact for marker in ("password", "secret", "token", "apikey", "authorization")):
                raise ValueError("Credential-like property names are not CAD metadata.")
        return value


def export_capabilities() -> list[dict[str, Any]]:
    """Report supported handoffs without claiming native file authoring or history transfer."""
    return [{"id": key, "name": value[0], "format": "step", "handoff": "import_script",
             "native_history_preserved": False, "metadata": "sidecar",
             "requires_target_application": True} for key, value in TARGETS.items()]


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def build_export(step: bytes, target: str, metadata: CadMetadata | None = None) -> bytes:
    """Package existing STEP bytes; never tessellate, invent geometry, or execute target code."""
    if target not in TARGETS:
        raise ValueError(f"Unsupported CAD target: {target}")
    if not step or len(step) > MAX_STEP_BYTES:
        raise ValueError("STEP input must be between 1 byte and 100 MiB.")
    # This is a format-envelope check, not a B-rep validation claim.
    if not step.lstrip().startswith(b"ISO-10303-21;") or not step.rstrip().endswith(b"END-ISO-10303-21;"):
        raise ValueError("Expected an ISO-10303-21 STEP file, including its end marker.")
    name, script_name, script, instructions = TARGETS[target]
    properties = (metadata or CadMetadata()).model_dump(mode="json")
    files = {"geometry.step": step, "metadata.json": _json(properties), script_name: script.encode("utf-8")}
    manifest = {
        "format": "forma-cad-export", "version": 1, "target": target,
        "geometry": {"format": "step", "filename": "geometry.step", "units": "declared_in_step", "validation": "envelope_only"},
        "native_history_preserved": False, "native_application_tested": False,
        "metadata": {"filename": "metadata.json", "material_assignment": "reference_only"},
        "files": {path: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)} for path, content in files.items()},
    }
    files["manifest.json"] = _json(manifest)
    files["README.md"] = (
        f"# Forma Export → {name}\n\n{instructions}\n\n"
        "This package contains the original STEP bytes, explicit CAD metadata, and an import helper. "
        "STEP imports geometry; it does not recreate the source application's parametric history. "
        "Units are carried by STEP. Review scale, solid/body count, orientation and assembly structure in the target. "
        "Only the STEP envelope was checked when packaging; solid validity has not been verified.\n\n"
        "Review the helper before running it. It creates a new target document/element and never saves over a source file. "
        "Onshape upload sends geometry to your selected cloud workspace only when you run the helper. "
        "SOLIDWORKS writes Forma-prefixed custom properties. Fusion applies name, part number, description and "
        "Forma attributes. A material name is a reference, not a physical material assignment. "
        "All metadata is preserved in metadata.json; unsupported native mappings remain in that sidecar.\n\n"
        "Import helpers require validation against your licensed CAD version. Package generation does not claim "
        "that a native import or engineering validation has succeeded.\n"
    ).encode("utf-8")
    result = BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            entry = ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, content)
    return result.getvalue()
