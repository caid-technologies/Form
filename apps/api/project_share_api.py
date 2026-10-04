"""Capability links grant read-only access to one explicitly shared revision."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel

from apps.api.auth import UserContext, require_user_context
from apps.api.project_history_api import RevisionSnapshot, _cad_response, _owner, _revision, _snapshot
from forma_core.database import (
    get_active_project_share, get_project_identity, insert_project_share,
    list_project_shares, revoke_project_share,
)
from forma_core.workspaces.projects.state import ProjectRevision

router = APIRouter(prefix="/projects/{project_id}/shared", tags=["project-sharing"])


class ShareRecord(BaseModel):
    """Owner-visible link metadata; never includes the stored token hash."""

    id: UUID
    revision_id: UUID
    created_at: datetime
    revoked_at: datetime | None = None


class ShareLink(ShareRecord):
    """The raw token is returned only when its link is created."""

    token: str


class SharePage(BaseModel):
    items: list[ShareRecord]
    next_offset: int | None


def _shared_revision(project_id: UUID, revision_id: UUID, token: str | None) -> ProjectRevision:
    """Reject missing, altered, cross-version, and deleted-project capabilities."""
    identity = get_project_identity(str(project_id))
    owner = str((identity or {}).get("owner_user_id") or "")
    if (not identity or not owner or identity.get("status", "active") != "active"
            or not token or not re.fullmatch(r"[0-9a-f]{64}", token)):
        raise HTTPException(status_code=404, detail="Shared version unavailable.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if get_active_project_share(str(project_id), owner, str(revision_id), token_hash) is None:
        raise HTTPException(status_code=404, detail="Shared version unavailable.")
    return _revision(project_id, revision_id, owner)


@router.post("/{revision_id}", response_model=ShareLink)
def create_share_link(
    project_id: UUID, revision_id: UUID, response: Response,
    user: UserContext = Depends(require_user_context),
) -> ShareLink:
    """Only the owner may issue a link for an existing saved revision."""
    owner = _owner(project_id, user)
    _revision(project_id, revision_id, owner)
    token = secrets.token_hex(32)
    record = {
        "id": str(uuid4()), "project_id": str(project_id), "revision_id": str(revision_id),
        "owner_user_id": owner, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(), "revoked_at": None,
    }
    insert_project_share(record)
    response.headers["Cache-Control"] = "private, no-store"
    return ShareLink(**record, token=token)


@router.get("/{revision_id}/links", response_model=SharePage)
def get_share_links(
    project_id: UUID, revision_id: UUID, response: Response,
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_user_context),
) -> SharePage:
    """Let the owner manage links for this version, without exposing tokens."""
    owner = _owner(project_id, user)
    _revision(project_id, revision_id, owner)
    records = list_project_shares(str(project_id), owner, str(revision_id), limit=limit + 1, offset=offset)
    response.headers["Cache-Control"] = "private, no-store"
    return SharePage(
        items=[ShareRecord.model_validate(record, from_attributes=True) for record in records[:limit]],
        next_offset=offset + limit if len(records) > limit else None,
    )


@router.delete("/{revision_id}/links/{share_id}", status_code=204)
def revoke_share_link(
    project_id: UUID, revision_id: UUID, share_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> Response:
    """Idempotently revoke one owned link; all other links remain valid."""
    owner = _owner(project_id, user)
    _revision(project_id, revision_id, owner)
    revoke_project_share(str(project_id), owner, str(revision_id), str(share_id), datetime.now(timezone.utc).isoformat())
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.get("/{revision_id}", response_model=RevisionSnapshot)
def get_shared_snapshot(
    project_id: UUID, revision_id: UUID, response: Response,
    x_project_share: str | None = Header(None),
) -> RevisionSnapshot:
    """Expose the design only; omit conversation, provenance, and other history."""
    revision = _shared_revision(project_id, revision_id, x_project_share)
    snapshot = _snapshot(project_id, revision_id, revision, response)
    metadata = snapshot.project_ir.get("assembly_metadata") or {}
    # Generation metadata may contain chat IDs, prompts, and uploaded references.
    allowed = {"project_id", "canonical_revision_id", "project_revision", "image_features"}
    allowed.update(f"product{view}_image_{field}" for view in ("", "_case", "_hero", "_exploded", "_diagram")
                   for field in ("url", "data", "content_type"))
    public_metadata = {key: value for key, value in metadata.items() if key in allowed}
    sequence = metadata.get("product_visual_sequence")
    if isinstance(sequence, list):
        public_metadata["product_visual_sequence"] = [
            {key: item[key] for key in ("view_id", "label", "url", "data", "content_type") if key in item}
            for item in sequence if isinstance(item, dict)
        ]
    snapshot.project_ir["assembly_metadata"] = public_metadata
    snapshot.project_ir["project_version_history"] = []
    snapshot.summary = f"Shared project version {revision.revision}"
    snapshot.parent_revision = None
    cad = snapshot.project_ir.get("cad_model")
    if isinstance(cad, dict) and cad.get("stored_sha256"):
        cad["signed_url"] = f"/api/projects/{project_id}/shared/{revision_id}/cad/{cad['stored_sha256']}"
    return snapshot


@router.get("/{revision_id}/cad/{sha256}")
def get_shared_cad(
    project_id: UUID, revision_id: UUID, sha256: str,
    x_project_share: str | None = Header(None),
) -> Response:
    """Authorize the exact shared revision before serving its verified CAD."""
    return _cad_response(project_id, _shared_revision(project_id, revision_id, x_project_share), sha256)
