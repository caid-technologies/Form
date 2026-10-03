from __future__ import annotations

import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import UserContext, require_user_context
from apps.api.hosted_chat import require_hosted_chat_enabled
from apps.api import ender_exports
from forma_core.database import (
    evaluate_project_readiness,
    get_latest_project_build,
    get_latest_project_revision,
    get_project_identity,
    initiate_project_build,
)
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, ProjectArtifactStorageError
from forma_core.workspaces.projects.cad_generation import (
    CadGenerationError,
    _obj_mesh_bytes,
    _stl_mesh_bytes,
    _three_mf_mesh_bytes,
)
from forma_core.workspaces.projects.fabrication.demo_printers import (
    demo_printer_capabilities,
    get_demo_printer,
    profile_fingerprint,
    resolve_demo_slice_profile,
)
from forma_core.workspaces.projects.fabrication.models import (
    FabricationError,
    PrinterConfigurationError,
    SliceRequest as FabricationSliceRequest,
)
from forma_core.workspaces.projects.fabrication.slicing import slice_project
from forma_core.workspaces.projects.state import ProjectArtifact, ProjectStateError
from forma_core.workspaces.readiness import (
    BuildAnywayRequest,
    BuildInitiationOutcome,
    BuildMode,
    BuildRequest,
    ProjectBuild,
    ReadinessError,
    ReadinessResult,
)
from forma_core.workspaces.workflow import WorkflowStateError


router = APIRouter(prefix="/projects/{project_id}", tags=["project-readiness"])
GCODE_MEDIA_TYPE = "text/x.gcode"
STEP_MEDIA_TYPE = "model/step"
MESH_EXPORT_MEDIA_TYPES = {
    "stl": "model/stl",
    "3mf": "model/3mf",
    "obj": "model/obj",
}
_GCODE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ProjectSliceRequest(BaseModel):
    """Request printer-specific G-code for a stored project STEP artifact."""

    model_config = ConfigDict(extra="forbid")

    printer_id: str = Field(min_length=1, max_length=120)


def _owner(user: UserContext) -> str:
    owner = str(user.owner_user_id or "").strip()
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required", "message": "Sign in to evaluate or build a project."},
        )
    return owner


def _domain_error(exc: ReadinessError | WorkflowStateError) -> HTTPException:
    not_found_codes = {"design_brief_not_found", "project_build_not_found", "workflow_not_found"}
    status_code = status.HTTP_404_NOT_FOUND if exc.code in not_found_codes else status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail=exc.as_dict())


def _export_error(status_code: int, code: str, message: str) -> HTTPException:
    """Create a stable project-export API error payload."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _require_owned_project(project_id: str, owner_user_id: str) -> None:
    """Require an active project owned by the authenticated account."""
    identity = get_project_identity(project_id)
    if (
        identity is None
        or str(identity.get("owner_user_id") or "") != owner_user_id
        or identity.get("status", "active") != "active"
    ):
        raise _export_error(status.HTTP_404_NOT_FOUND, "project_not_found", "The project is not available to this account.")


def _project_cad(project_id: str, owner_user_id: str) -> dict[str, Any]:
    """Return the latest saved CAD descriptor for an owned project."""
    _require_owned_project(project_id, owner_user_id)
    try:
        revision = get_latest_project_revision(project_id, owner_user_id)
    except ProjectStateError as exc:
        raise _export_error(status.HTTP_404_NOT_FOUND, "step_not_found", "The project has no saved STEP artifact.") from exc
    cad = revision.state.cad_model if revision else None
    if not isinstance(cad, dict) or str(cad.get("format") or "").lower() not in {"step", "stp"}:
        raise _export_error(status.HTTP_404_NOT_FOUND, "step_not_found", "The project has no saved STEP artifact.")
    return cad


def _step_digest(cad: dict[str, Any]) -> str:
    """Return a validated SHA-256 digest from a CAD descriptor."""
    digest = str(cad.get("stored_sha256") or cad.get("sha256") or "").strip().lower()
    if not _GCODE_DIGEST_RE.fullmatch(digest):
        raise _export_error(status.HTTP_404_NOT_FOUND, "step_not_found", "The STEP artifact has no valid checksum.")
    return digest


def _load_step_bytes(project_id: str, cad: dict[str, Any]) -> tuple[str, bytes]:
    """Load persisted STEP bytes, with a one-time migration from a surviving worker path."""
    digest = _step_digest(cad)
    storage = ProjectArtifactStorage()
    try:
        stored = storage.get(project_id, digest, STEP_MEDIA_TYPE)
        content = stored.content or b""
    except FileNotFoundError:
        source = Path(str(cad.get("path") or "")).expanduser()
        if not source.is_file():
            raise _export_error(status.HTTP_404_NOT_FOUND, "step_not_found", "The stored STEP artifact is missing.")
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "step_integrity_failed", "The STEP artifact failed its integrity check.")
        try:
            storage.put(project_id, digest, content, STEP_MEDIA_TYPE)
        except (ProjectArtifactStorageError, OSError, ValueError) as exc:
            raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "artifact_storage_unavailable", "The STEP artifact could not be persisted.") from exc
    except (ProjectArtifactStorageError, OSError, ValueError) as exc:
        raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "artifact_storage_unavailable", "The STEP artifact could not be retrieved.") from exc
    if hashlib.sha256(content).hexdigest() != digest:
        raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "step_integrity_failed", "The STEP artifact failed its integrity check.")
    return digest, content


def _mesh_export_descriptor(cad: dict[str, Any], format_name: str) -> dict[str, Any] | None:
    """Resolve a stored mesh export, or derive one from the revision preview mesh."""

    media_type = MESH_EXPORT_MEDIA_TYPES.get(format_name)
    if not media_type:
        return None

    exports = cad.get("exports")
    descriptor = exports.get(format_name) if isinstance(exports, dict) else None
    if isinstance(descriptor, dict):
        digest = str(descriptor.get("sha256") or "").strip().lower()
        if (
            _GCODE_DIGEST_RE.fullmatch(digest)
            and str(descriptor.get("media_type") or "").strip().lower() == media_type
        ):
            size_bytes = descriptor.get("bytes")
            return {
                "filename": f"assembly.{format_name}",
                "sha256": digest,
                "size_bytes": size_bytes if isinstance(size_bytes, int) and size_bytes >= 0 else None,
                "media_type": media_type,
            }

    meshes = cad.get("meshes")
    mesh = meshes[0] if isinstance(meshes, list) and meshes and isinstance(meshes[0], dict) else None
    serializer = {
        "stl": _stl_mesh_bytes,
        "3mf": _three_mf_mesh_bytes,
        "obj": _obj_mesh_bytes,
    }.get(format_name)
    if mesh is None or serializer is None:
        return None
    try:
        content = serializer(mesh)
    except CadGenerationError:
        return None
    digest = hashlib.sha256(content).hexdigest()
    return {
        "filename": f"assembly.{format_name}",
        "sha256": digest,
        "size_bytes": len(content),
        "media_type": media_type,
        "_content": content,
    }


def _step_to_stl(step_content: bytes, directory: Path) -> ProjectArtifact:
    """Tessellate trusted STEP bytes into a temporary STL for the slicer."""
    step_path = directory / "assembly.step"
    stl_path = directory / "assembly.stl"
    step_path.write_bytes(step_content)
    try:
        from cadquery import exporters, importers
    except ImportError as exc:
        raise _export_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "cad_runtime_unavailable",
            "The fabrication worker does not have the native CAD runtime needed to prepare this STEP file.",
        ) from exc
    try:
        model = importers.importStep(str(step_path))
        exporters.export(model, str(stl_path))
    except Exception as exc:
        raise _export_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "step_mesh_failed", "The STEP artifact could not be tessellated for slicing.") from exc
    if not stl_path.is_file() or stl_path.stat().st_size == 0:
        raise _export_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "step_mesh_failed", "The STEP artifact produced no printable mesh.")
    content = stl_path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    return ProjectArtifact(
        artifact_id=f"fabrication-mesh-{checksum[:16]}",
        kind="mesh.stl",
        uri=str(stl_path),
        media_type="model/stl",
        checksum=f"sha256:{checksum}",
        metadata={"path": str(stl_path), "source": "stored-project-step"},
    )


def _gcode_preview(content: bytes) -> str:
    """Return a bounded text preview suitable for the demo UI."""
    text = content.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[:100])[:12000]


@router.get("/readiness", response_model=ReadinessResult)
def evaluate_readiness_endpoint(
    project_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> ReadinessResult:
    try:
        return evaluate_project_readiness(str(project_id), _owner(user))
    except (ReadinessError, WorkflowStateError) as exc:
        raise _domain_error(exc) from exc


@router.post("/build", response_model=BuildInitiationOutcome)
def build_project_endpoint(
    project_id: UUID,
    request: BuildRequest,
    user: UserContext = Depends(require_user_context),
) -> BuildInitiationOutcome:
    require_hosted_chat_enabled(user)
    owner = _owner(user)
    try:
        return initiate_project_build(
            str(project_id),
            owner,
            mode=BuildMode.BUILD,
            actor_id=owner,
            idempotency_key=request.idempotency_key,
        )
    except (ReadinessError, WorkflowStateError) as exc:
        raise _domain_error(exc) from exc


@router.post("/build-anyway", response_model=BuildInitiationOutcome)
def build_project_anyway_endpoint(
    project_id: UUID,
    request: BuildAnywayRequest,
    user: UserContext = Depends(require_user_context),
) -> BuildInitiationOutcome:
    require_hosted_chat_enabled(user)
    owner = _owner(user)
    try:
        return initiate_project_build(
            str(project_id),
            owner,
            mode=BuildMode.BUILD_ANYWAY,
            actor_id=owner,
            assumptions=request.assumptions,
            idempotency_key=request.idempotency_key,
        )
    except (ReadinessError, WorkflowStateError) as exc:
        raise _domain_error(exc) from exc


@router.get("/build", response_model=ProjectBuild)
def get_frozen_build_endpoint(
    project_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> ProjectBuild:
    try:
        return get_latest_project_build(str(project_id), _owner(user))
    except (ReadinessError, WorkflowStateError) as exc:
        raise _domain_error(exc) from exc


@router.get("/exports")
def list_project_exports_endpoint(
    project_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    """Describe downloadable CAD formats and supported printer-specific G-code targets."""
    project_key = str(project_id)
    cad = _project_cad(project_key, _owner(user))
    digest = _step_digest(cad)
    mesh_exports = {}
    for format_name in MESH_EXPORT_MEDIA_TYPES:
        descriptor = _mesh_export_descriptor(cad, format_name)
        if descriptor:
            mesh_exports[format_name] = {
                key: value for key, value in descriptor.items() if key != "_content"
            }
            mesh_exports[format_name]["download_url"] = (
                f"/projects/{project_key}/exports/mesh/{format_name}/{descriptor['sha256']}"
            )
    return {
        "project_id": project_key,
        "step": {
            "filename": str(cad.get("filename") or "assembly.step"),
            "sha256": digest,
            "size_bytes": cad.get("bytes"),
            "download_url": f"/projects/{project_key}/exports/step/{digest}",
        },
        "mesh_exports": mesh_exports,
        "printers": ender_exports.capabilities() if ender_exports.enabled() else demo_printer_capabilities(),
    }


@router.get("/exports/step/{sha256}")
def download_project_step_endpoint(
    project_id: UUID,
    sha256: str,
    user: UserContext = Depends(require_user_context),
) -> Response:
    """Download the authenticated project's canonical STEP artifact."""
    project_key = str(project_id)
    cad = _project_cad(project_key, _owner(user))
    expected, content = _load_step_bytes(project_key, cad)
    if sha256.strip().lower() != expected:
        raise _export_error(status.HTTP_404_NOT_FOUND, "step_not_found", "The requested STEP artifact is not attached to this project.")
    return Response(
        content,
        media_type=STEP_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="assembly.step"', "Cache-Control": "private, no-store"},
    )


@router.get("/exports/mesh/{format_name}/{sha256}")
def download_project_mesh_endpoint(
    project_id: UUID,
    format_name: str,
    sha256: str,
    user: UserContext = Depends(require_user_context),
) -> Response:
    """Download an authenticated project's stored STL, 3MF, or OBJ derivative."""

    project_key = str(project_id)
    cad = _project_cad(project_key, _owner(user))
    normalized_format = format_name.strip().lower()
    descriptor = _mesh_export_descriptor(cad, normalized_format)
    digest = sha256.strip().lower()
    if descriptor is None or digest != descriptor["sha256"]:
        raise _export_error(
            status.HTTP_404_NOT_FOUND,
            "mesh_not_found",
            "The requested mesh artifact is not attached to this project.",
        )
    derived_content = descriptor.get("_content")
    if isinstance(derived_content, bytes):
        content = derived_content
        try:
            ProjectArtifactStorage().put(project_key, digest, content, descriptor["media_type"])
        except (ProjectArtifactStorageError, OSError, ValueError):
            # Legacy revisions remain downloadable from their persisted preview mesh
            # even when the optional artifact cache is unavailable.
            pass
    else:
        try:
            stored = ProjectArtifactStorage().get(project_key, digest, descriptor["media_type"])
        except FileNotFoundError as exc:
            raise _export_error(
                status.HTTP_404_NOT_FOUND,
                "mesh_not_found",
                "The requested mesh artifact was not found.",
            ) from exc
        except (ProjectArtifactStorageError, OSError, ValueError) as exc:
            raise _export_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "artifact_storage_unavailable",
                "The mesh artifact could not be retrieved.",
            ) from exc
        content = stored.content or b""
    if hashlib.sha256(content).hexdigest() != digest:
        raise _export_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "mesh_integrity_failed",
            "The mesh artifact failed its integrity check.",
        )
    return Response(
        content,
        media_type=descriptor["media_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{descriptor["filename"]}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/exports/gcode")
def create_project_gcode_endpoint(
    project_id: UUID,
    request: ProjectSliceRequest,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    """Slice the stored STEP for one fixed demo printer and persist the resulting G-code."""
    project_key = str(project_id)
    owner = _owner(user)
    cad = _project_cad(project_key, owner)
    source_sha256, step_content = _load_step_bytes(project_key, cad)
    if ender_exports.enabled():
        return ender_exports.start(project_key, source_sha256, step_content, request.printer_id)
    printer = get_demo_printer(request.printer_id)
    try:
        profile = resolve_demo_slice_profile(printer.printer_id)
    except PrinterConfigurationError as exc:
        raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "printer_profile_unavailable", str(exc)) from exc

    with TemporaryDirectory(prefix=f"forma-slice-{project_key[:8]}-") as temporary:
        workdir = Path(temporary)
        mesh_artifact = _step_to_stl(step_content, workdir)
        output_name = f"assembly-{printer.printer_id}.gcode"
        fabrication_request = FabricationSliceRequest(
            mesh_artifact=mesh_artifact,
            profile=profile,
            output_name=output_name,
            project_id=project_key,
        )
        try:
            result = slice_project(fabrication_request)
        except FabricationError as exc:
            raise _export_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "slice_validation_failed", str(exc)) from exc
        except Exception as exc:
            raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "slicer_failed", f"Printer slicing failed: {exc}") from exc

        gcode_path = Path(result.gcode_artifact.uri)
        gcode_content = gcode_path.read_bytes()
        gcode_sha256 = hashlib.sha256(gcode_content).hexdigest()
        report_content = Path(result.report_artifact.uri).read_bytes() if result.report_artifact else b""
        report_sha256 = hashlib.sha256(report_content).hexdigest() if report_content else None
        storage = ProjectArtifactStorage()
        try:
            storage.put(project_key, gcode_sha256, gcode_content, GCODE_MEDIA_TYPE)
            if report_content and report_sha256:
                storage.put(project_key, report_sha256, report_content, "application/json")
        except (ProjectArtifactStorageError, OSError, ValueError) as exc:
            raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "artifact_storage_unavailable", "Generated G-code could not be persisted.") from exc

        return {
            "status": "completed",
            "project_id": project_key,
            "source_step_sha256": source_sha256,
            "printer_id": printer.printer_id,
            "printer": printer.display_name,
            "material": profile.material,
            "nozzle_mm": profile.nozzle_diameter_mm,
            "layer_height_mm": 0.2,
            "slicer": result.backend,
            "profile_name": result.profile_name,
            "profile_sha256": profile_fingerprint(profile),
            "gcode": {
                "filename": output_name,
                "sha256": gcode_sha256,
                "size_bytes": len(gcode_content),
                "download_url": f"/projects/{project_key}/exports/gcode/{gcode_sha256}",
                "preview": _gcode_preview(gcode_content),
            },
            "report_sha256": report_sha256,
            "estimated_print_time_seconds": result.estimated_print_time_s,
            "estimated_filament_grams": result.filament_mass_g,
            "layer_count": result.layer_count,
            "warnings": result.warnings,
        }


@router.get("/exports/gcode/{sha256}")
def download_project_gcode_endpoint(
    project_id: UUID,
    sha256: str,
    user: UserContext = Depends(require_user_context),
) -> Response:
    """Download a project-scoped G-code artifact generated by the fabrication service."""
    project_key = str(project_id)
    _require_owned_project(project_key, _owner(user))
    digest = sha256.strip().lower()
    if not _GCODE_DIGEST_RE.fullmatch(digest):
        raise _export_error(status.HTTP_404_NOT_FOUND, "gcode_not_found", "The requested G-code artifact was not found.")
    try:
        stored = ProjectArtifactStorage().get(project_key, digest, GCODE_MEDIA_TYPE)
    except FileNotFoundError as exc:
        raise _export_error(status.HTTP_404_NOT_FOUND, "gcode_not_found", "The requested G-code artifact was not found.") from exc
    except (ProjectArtifactStorageError, OSError, ValueError) as exc:
        raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "artifact_storage_unavailable", "The G-code artifact could not be retrieved.") from exc
    content = stored.content or b""
    if hashlib.sha256(content).hexdigest() != digest:
        raise _export_error(status.HTTP_503_SERVICE_UNAVAILABLE, "gcode_integrity_failed", "The G-code artifact failed its integrity check.")
    return Response(
        content,
        media_type=GCODE_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="assembly.gcode"',
            "Cache-Control": "private, no-store",
        },
    )



@router.get("/exports/gcode/jobs/{job_id}")
def poll_project_gcode_endpoint(
    project_id: UUID,
    job_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    """Poll without holding a cloud request open for the entire native slice."""
    project_key = str(project_id)
    cad = _project_cad(project_key, _owner(user))
    if not ender_exports.enabled():
        raise _export_error(503, "slicing_worker_unavailable", "The Ender 3 worker is not configured.")
    return ender_exports.poll(project_key, _step_digest(cad), str(job_id))
