"""Owner-scoped application adapters for the shared portable project contract."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from apps.api.auth import UserContext, require_user_context
from apps.api.project_foundation import inspect_revision, revision_project
from forma_core.database import get_project_identity, get_latest_project_revision, get_project_revision_by_id
from forma_core.workspaces.projects.inspection import ProjectInspection
from forma_core.workspaces.projects.portable import export_project_bundle
from forma_core.workspaces.projects.state import ProjectStateError


router = APIRouter(prefix="/projects/{project_id}/foundation", tags=["project-foundation"])


def _revision(project_id: UUID, revision_id: UUID | None, user: UserContext):
    owner = str(user.owner_user_id or "").strip()
    if not owner:
        raise HTTPException(status_code=401, detail="Sign in to inspect project artifacts.")
    identity = get_project_identity(str(project_id))
    if not identity or identity.get("owner_user_id") != owner or identity.get("status", "active") != "active":
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        revision = (get_project_revision_by_id(str(project_id), owner, str(revision_id)) if revision_id
                    else get_latest_project_revision(str(project_id), owner))
    except ProjectStateError as exc:
        if exc.code == "project_revision_not_found":
            raise HTTPException(status_code=404, detail="Project revision not found.") from exc
        raise
    if revision is None:
        raise HTTPException(status_code=404, detail="Project revision not found.")
    return revision, owner


@router.get("/inspection", response_model=ProjectInspection)
def get_inspection(project_id: UUID, response: Response, revision_id: UUID | None = None,
                   user: UserContext = Depends(require_user_context)):
    revision, owner = _revision(project_id, revision_id, user)
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return inspect_revision(revision, str(project_id), owner)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail="Project revision evidence could not be verified for inspection.") from exc


@router.get("/bundle")
def get_bundle(project_id: UUID, revision_id: UUID | None = None,
               user: UserContext = Depends(require_user_context)):
    revision, owner = _revision(project_id, revision_id, user)
    try:
        manifest, reader = revision_project(revision, str(project_id), owner)
        with TemporaryDirectory(prefix="forma-project-export-") as temporary:
            archive = export_project_bundle(manifest, Path(temporary) / "project.zip", read_artifact=reader)
            content = archive.read_bytes()
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail="Project artifacts could not be verified for export.") from exc
    return Response(content=content, media_type="application/zip", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{project_id}-{revision.revision_id}.zip"',
    })
