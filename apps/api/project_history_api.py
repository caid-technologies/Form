"""Owner-scoped access to immutable project snapshots and their saved artifacts."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from apps.api.auth import UserContext, require_user_context
from forma_core.database import get_project_identity, get_project_revision_by_id, list_project_revisions
from forma_core.persistence.images import hydrate_image_storage_metadata
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, ProjectArtifactStorageError
from forma_core.workspaces.projects.state import ProjectRevision, ProjectStateError

router = APIRouter(prefix="/projects/{project_id}/history", tags=["project-history"])


class RevisionSummary(BaseModel):
    revision_id: UUID
    revision: int
    parent_revision: int | None
    created_at: datetime
    title: str
    summary: str


class RevisionPage(BaseModel):
    project_id: UUID
    items: list[RevisionSummary]
    latest_revision: int | None
    next_before: int | None


class RevisionSnapshot(RevisionSummary):
    project_id: UUID
    project_ir: dict[str, Any]


def _owner(project_id: UUID, user: UserContext) -> str:
    owner = str(user.owner_user_id or "").strip()
    if not owner:
        raise HTTPException(status_code=401, detail="Sign in to view version history.")
    identity = get_project_identity(str(project_id))
    # History can contain material from before a project was made public.
    if (identity is None or identity.get("owner_user_id") != owner
            or identity.get("status", "active") != "active"):
        raise HTTPException(status_code=404, detail="Project not found.")
    return owner


def _summary(revision: ProjectRevision) -> RevisionSummary:
    metadata = revision.state.assembly_metadata or {}
    last_iteration = metadata.get("last_iteration") or {}
    if revision.source_job_id.startswith("opencode-image-"):
        summary = "Generated concept image"
    elif revision.parent_revision is None:
        summary = "Created project"
    elif revision.source_job_id.startswith("opencode-"):
        summary = "Updated design"
    else:
        summary = "Saved project update"
    if (isinstance(last_iteration, dict) and last_iteration.get("revision") == revision.revision
            and isinstance(last_iteration.get("instruction"), str)):
        summary = last_iteration["instruction"].strip()[:240] or summary
    return RevisionSummary(
        revision_id=revision.revision_id, revision=revision.revision,
        parent_revision=revision.parent_revision, created_at=revision.created_at,
        title=revision.state.overview.title, summary=summary,
    )


def _revision(project_id: UUID, revision_id: UUID, owner: str) -> ProjectRevision:
    try:
        return get_project_revision_by_id(str(project_id), owner, str(revision_id))
    except ProjectStateError as exc:
        if exc.code == "project_revision_not_found":
            raise HTTPException(status_code=404, detail="This saved version is unavailable.") from exc
        raise


@router.get("", response_model=RevisionPage)
def get_project_history(
    project_id: UUID,
    response: Response,
    limit: int = Query(20, ge=1, le=50),
    before: int | None = Query(None, ge=1),
    user: UserContext = Depends(require_user_context),
) -> RevisionPage:
    """List versions using a stable cursor even while new versions are saved."""
    owner = _owner(project_id, user)
    revisions = list_project_revisions(str(project_id), owner, limit=limit + 1, before=before)
    latest = revisions[:1] if before is None else list_project_revisions(str(project_id), owner, limit=1)
    response.headers["Cache-Control"] = "private, no-store"
    return RevisionPage(
        project_id=project_id, items=[_summary(item) for item in revisions[:limit]],
        latest_revision=latest[0].revision if latest else None,
        next_before=revisions[limit - 1].revision if len(revisions) > limit else None,
    )


@router.get("/{revision_id}", response_model=RevisionSnapshot)
def get_project_snapshot(
    project_id: UUID, revision_id: UUID, response: Response,
    user: UserContext = Depends(require_user_context),
) -> RevisionSnapshot:
    """Hydrate only assets recorded in this version; never discover newer images."""
    revision = _revision(project_id, revision_id, _owner(project_id, user))
    return _snapshot(project_id, revision_id, revision, response)


def _snapshot(project_id: UUID, revision_id: UUID, revision: ProjectRevision, response: Response) -> RevisionSnapshot:
    """Render an authorized immutable snapshot without discovering live assets."""
    ir = revision.state.model_dump(mode="json")
    ir["assembly_metadata"] = hydrate_image_storage_metadata(
        ir.get("assembly_metadata") or {}, str(project_id), discover_missing=False,
    )
    ir["assembly_metadata"].update({
        "project_id": str(project_id), "canonical_revision_id": str(revision_id),
        "project_revision": revision.revision,
    })
    cad = ir.get("cad_model")
    if isinstance(cad, dict) and cad.get("stored_sha256"):
        # The live CAD endpoint only authorizes the latest artifact. Bind this
        # download to the snapshot so old geometry remains addressable.
        cad["signed_url"] = f"/api/projects/{project_id}/history/{revision_id}/cad/{cad['stored_sha256']}"
    response.headers["Cache-Control"] = "private, no-store"
    return RevisionSnapshot(**_summary(revision).model_dump(), project_id=project_id, project_ir=ir)


@router.get("/{revision_id}/cad/{sha256}")
def get_snapshot_cad(
    project_id: UUID, revision_id: UUID, sha256: str,
    user: UserContext = Depends(require_user_context),
) -> Response:
    """Serve bytes only when the exact owned snapshot references their checksum."""
    revision = _revision(project_id, revision_id, _owner(project_id, user))
    return _cad_response(project_id, revision, sha256)


def _cad_response(project_id: UUID, revision: ProjectRevision, sha256: str) -> Response:
    """Return verified CAD bytes from an already authorized revision."""
    cad = revision.state.cad_model
    if not isinstance(cad, dict) or len(sha256) != 64 or cad.get("stored_sha256") != sha256:
        raise HTTPException(status_code=404, detail="This version has no matching STEP artifact.")
    try:
        stored = ProjectArtifactStorage().get(str(project_id), sha256, "model/step")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="The saved STEP artifact is unavailable.") from exc
    except (ProjectArtifactStorageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="The saved STEP artifact could not be retrieved.") from exc
    content = stored.content or b""
    if hashlib.sha256(content).hexdigest() != sha256:
        raise HTTPException(status_code=503, detail="The saved STEP artifact failed its integrity check.")
    return Response(content, media_type="model/step", headers={
        "Content-Disposition": f'attachment; filename="assembly-v{revision.revision}.step"',
        "Cache-Control": "private, no-store",
    })
