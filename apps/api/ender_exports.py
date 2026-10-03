"""Opt-in Ender-only project exports via the already-running slicing worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import HTTPException

from apps.api.slicing_worker import PRINTER_ID, SETTINGS, SlicingWorker, WorkerError
from forma_core.config import config
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, ProjectArtifactStorageError
from forma_core.workspaces.projects.fabrication.models import SliceProfile
from forma_core.workspaces.projects.fabrication.validation import validate_gcode


def enabled() -> bool:
    return bool(config.optional("FORMA_ENDER_WORKER_URL"))


def _worker() -> SlicingWorker:
    return SlicingWorker(str(config.optional("FORMA_ENDER_WORKER_URL") or ""),
                         str(config.optional("FORMA_ENDER_WORKER_TOKEN") or ""))


def capabilities() -> list[dict[str, Any]]:
    try:
        return _worker().capabilities()
    except WorkerError as exc:
        return [{"printer_id": PRINTER_ID, "display_name": "Creality Ender-3", "nozzle_mm": 0.4,
                 "material": "PLA", "layer_height_mm": 0.2, "available": False,
                 "unavailable_reason": str(exc)}]


def _response(worker: SlicingWorker, project_id: str, digest: str, job: dict[str, Any]) -> dict[str, Any]:
    state = job["status"]
    if state in {"failed", "cancelled", "awaiting_upload"}:
        code = (job.get("error") or {}).get("code", state)
        messages = {
            "conversion_failed": "The mini-PC could not convert this STEP file to STL.",
            "slicer_failed": "PrusaSlicer could not slice this model. Check the mini-PC job log.",
            "worker_restarted": "The slicing worker restarted. Generate G-code again.",
            "cancelled": "Slicing was cancelled. Generate G-code again.",
        }
        raise WorkerError("slice_failed", messages.get(code, "The mini-PC could not finish slicing. Generate G-code again."), 422)
    if state != "completed":
        return {"status": state, "job_id": job["job_id"], "project_id": project_id,
                "source_step_sha256": digest, "printer_id": PRINTER_ID}

    content, gcode_digest = worker.artifact(job)
    profile = SliceProfile(backend="prusaslicer", printer_name="Creality Ender-3",
                           profile_name="Ender 3 / PLA / 0.20 mm", material="PLA",
                           nozzle_diameter_mm=0.4, bed_size_mm=(220, 220), z_height_mm=250,
                           require_temperatures=True, require_extrusion=True)
    with TemporaryDirectory(prefix="forma-ender-") as temporary:
        path = Path(temporary) / "assembly-ender3.gcode"
        path.write_bytes(content)
        validation = validate_gcode(path, profile)
    if not validation.valid:
        raise WorkerError("slice_validation_failed", "Generated G-code failed Forma's movement, extrusion, or temperature checks.", 422)
    warning = "Inspect the Ender 3 toolpath in PrusaSlicer before printing. This export does not start a print."
    report = {"project_id": project_id, "source_step_sha256": digest, "job_id": job["job_id"],
              "printer_id": PRINTER_ID, "worker_profile": "ender3", "settings": SETTINGS,
              "slicer": "prusaslicer", "slicer_version": job.get("slicer_version"),
              "converter_version": job.get("converter_version"), "gcode_sha256": gcode_digest,
              "profile_sha256": None, "print_safety_certified": False,
              "validation": validation.model_dump(mode="json")}
    encoded = json.dumps(report, sort_keys=True).encode()
    report_digest = hashlib.sha256(encoded).hexdigest()
    try:
        storage = ProjectArtifactStorage()
        storage.put(project_id, gcode_digest, content, "text/x.gcode")
        storage.put(project_id, report_digest, encoded, "application/json")
    except (ProjectArtifactStorageError, OSError, ValueError) as exc:
        raise WorkerError("artifact_storage_unavailable", "Generated G-code could not be saved. Retry the export.") from exc
    return {"status": "completed", "job_id": job["job_id"], "project_id": project_id,
            "source_step_sha256": digest, "printer_id": PRINTER_ID, "printer": "Creality Ender-3",
            "material": "PLA", "nozzle_mm": 0.4, "layer_height_mm": 0.2,
            "slicer": "prusaslicer", "profile_name": profile.profile_name,
            # The current worker does not attest a profile-file fingerprint. Do not invent one.
            "profile_sha256": None, "report_sha256": report_digest,
            "warnings": [*validation.warnings, warning],
            "gcode": {"filename": "assembly-ender3.gcode", "sha256": gcode_digest,
                      "size_bytes": len(content), "preview": "\n".join(content.decode("utf-8", errors="replace").splitlines()[:100])[:12000],
                      "download_url": f"/projects/{project_id}/exports/gcode/{gcode_digest}"}}


def start(project_id: str, digest: str, content: bytes, printer_id: str) -> dict[str, Any]:
    try:
        if printer_id != PRINTER_ID:
            raise WorkerError("unsupported_printer", "This demo supports the Creality Ender-3 only.", 422)
        worker = _worker()
        return _response(worker, project_id, digest, worker.start(project_id, digest, content))
    except WorkerError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


def poll(project_id: str, digest: str, job_id: str) -> dict[str, Any]:
    try:
        worker = _worker()
        return _response(worker, project_id, digest, worker.status(job_id, project_id, digest))
    except WorkerError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
