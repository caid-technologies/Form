"""Private project/revision synchronization endpoints for the Forma CLI."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from apps.api.auth import UserContext, require_user_context
from forma_core.config.compatibility import (
    UnsupportedHardwareIntermediateRepresentationVersion,
    ensure_supported_hardware_ir_version,
    hosted_compatibility_metadata,
)
from forma_core.database import (
    CliProjectConflictError,
    get_cli_project_delivery,
    get_cli_project_delivery_by_id,
    get_cli_project_revision,
    insert_cli_project_delivery,
    insert_cli_project_revision,
    invalidate_project_lists,
    list_project_identities,
    list_project_publish_audits,
    publish_cli_project,
    update_cli_project_delivery,
    get_latest_project_revision,
)
from forma_core.persistence.project_artifacts import (
    ProjectArtifactStorage,
    ProjectArtifactStorageError,
)
from forma_core.workspaces.projects.manifest import (
    ProjectManifest,
    normalize_artifact_media_type,
    validate_artifact_references,
)
from forma_core.workspaces.projects.models import ProjectIdentityResponse
from forma_core.workspaces.projects import ProjectStateError

router = APIRouter(prefix="/cli/projects", tags=["cli"])


class ProjectPushRequest(BaseModel):
    manifest: dict[str, Any]
    parent_revision_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    visibility: str | None = Field(default=None)


class ProjectDeliverRequest(BaseModel):
    manifest: dict[str, Any]
    parent_revision_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    visibility: str | None = Field(default=None)


def _owner(user: UserContext) -> str:
    if not user.owner_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to use cloud projects.")
    return user.owner_user_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _revision_conflict_detail(project_id: str, owner: str, exc: BaseException) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": "PROJECT_REVISION_CONFLICT", "message": str(exc)}
    if project_id:
        latest = get_cli_project_revision(project_id, owner)
        if latest is not None:
            detail["current_revision_id"] = latest.get("revision_id")
            detail["current_revision"] = latest.get("revision")
    return detail


def _delivery_visibility(value: str | None) -> str:
    normalized = str(value or "").strip().lower() if value is not None else ""
    if normalized not in {"", "public", "private"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="deliver visibility must be public or private.",
        )
    return normalized or "private"


def _push_idempotency_key(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"push:{hashlib.sha256(canonical).hexdigest()}"


def _manifest_digest(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _delivery_manifest_digest(delivery: dict[str, Any]) -> str:
    return str(delivery.get("manifest_digest") or "").strip()


def _ensure_delivery_key_matches(delivery: dict[str, Any], manifest: dict[str, Any]) -> None:
    stored_digest = _delivery_manifest_digest(delivery)
    # Rows created before manifest_digest was introduced cannot be compared
    # safely after normalization, so preserve their existing replay behavior.
    if not stored_digest or stored_digest == _manifest_digest(manifest):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "IDEMPOTENCY_KEY_REUSE",
            "message": "The idempotency_key was already used for different project content.",
        },
    )


def _delivery_plan(project_id: str, owner: str) -> tuple[int, str]:
    latest = get_cli_project_revision(project_id, owner)
    latest_revision = int((latest or {}).get("revision") or 0)
    return latest_revision + 1, str(uuid.uuid4())


def _reserve_and_materialize_delivery(
    payload: dict[str, Any],
    owner: str,
    idempotency_key: str,
    expected_revision_id: str | None,
) -> dict[str, Any]:
    project_id = str(payload.get("project_id") or "").strip()
    digest = _manifest_digest(payload)
    delivery = get_cli_project_delivery(project_id, owner, idempotency_key)
    if delivery is None:
        planned_revision, planned_revision_id = _delivery_plan(project_id, owner)
        delivery = insert_cli_project_delivery(
            {
                "delivery_id": str(uuid.uuid4()),
                "project_id": project_id,
                "owner_user_id": owner,
                "idempotency_key": idempotency_key,
                "revision_id": planned_revision_id,
                "revision": planned_revision,
                "parent_revision_id": expected_revision_id,
                "manifest_json": payload,
                "manifest_digest": digest,
                "status": "pending",
                "receipt_json": None,
                "created_at": _now(),
                "completed_at": None,
            }
        )
    _ensure_delivery_key_matches(delivery, payload)
    if delivery.get("status") == "complete" and delivery.get("receipt"):
        return delivery
    if delivery.get("revision_id") and get_cli_project_revision(
        project_id,
        owner,
        delivery["revision_id"],
    ) is not None:
        return delivery

    try:
        saved = insert_cli_project_revision(
            payload,
            owner,
            expected_revision_id=delivery.get("parent_revision_id"),
            revision_id=delivery.get("revision_id"),
            revision=delivery.get("revision"),
        )
    except CliProjectConflictError as exc:
        current = get_cli_project_delivery(project_id, owner, idempotency_key)
        if current is not None:
            _ensure_delivery_key_matches(current, payload)
            if current.get("revision_id") and get_cli_project_revision(
                project_id,
                owner,
                current["revision_id"],
            ) is not None:
                return current
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_revision_conflict_detail(project_id, owner, exc),
        ) from exc
    if (
        saved.get("revision_id") != delivery.get("revision_id")
        or saved.get("revision") != delivery.get("revision")
    ):
        updated = update_cli_project_delivery(
            delivery["delivery_id"],
            owner,
            {"revision_id": saved["revision_id"], "revision": saved["revision"]},
        )
        if updated is not None:
            delivery = updated
    return delivery


def _artifact_declaration(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": artifact.get("path"),
        "sha256": artifact.get("sha256"),
        "media_type": artifact.get("media_type"),
        "size_bytes": artifact.get("size_bytes"),
    }


def _delivery_project_ref(delivery: dict[str, Any]) -> dict[str, Any]:
    manifest = delivery.get("manifest") or {}
    return {
        "project_id": delivery["project_id"],
        "revision_id": delivery["revision_id"],
        "revision": delivery["revision"],
        "parent_revision_id": delivery.get("parent_revision_id"),
        "visibility": manifest.get("visibility") or "private",
    }


def _pending_delivery_response(delivery: dict[str, Any]) -> dict[str, Any]:
    declaration = delivery.get("manifest") or {}
    artifacts = validate_artifact_references(declaration.get("artifacts") or [], require_integrity=False)
    return {
        "delivery_id": delivery["delivery_id"],
        "status": delivery["status"],
        "project": _delivery_project_ref(delivery),
        "artifacts": [{**_artifact_declaration(artifact), "status": "pending"} for artifact in artifacts],
        "artifact_summary": {"declared": len(artifacts), "present": 0},
        "created_at": delivery["created_at"],
        "completed_at": delivery.get("completed_at"),
    }


def _verify_delivery_artifacts(
    delivery: dict[str, Any],
    storage: ProjectArtifactStorage,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    declaration = delivery.get("manifest") or {}
    artifacts = validate_artifact_references(declaration.get("artifacts") or [], require_integrity=True)
    rows: list[dict[str, Any]] = []
    present = 0
    for artifact in artifacts:
        present_now = storage.contains(delivery["project_id"], artifact["sha256"])
        present += 1 if present_now else 0
        rows.append({**_artifact_declaration(artifact), "status": "present" if present_now else "missing"})
    summary = {"declared": len(artifacts), "present": present}
    return rows, summary, present == len(artifacts)


@router.get("")
async def list_cli_projects_endpoint(user: UserContext = Depends(require_user_context)) -> dict[str, object]:
    items = [
        ProjectIdentityResponse.model_validate(identity).model_dump(mode="json", exclude_unset=True)
        for identity in list_project_identities(_owner(user))
    ]
    return {"items": items}


@router.post("/push")
async def push_cli_project(
    request: ProjectPushRequest,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    owner = _owner(user)
    manifest_document = dict(request.manifest or {})
    if "owner_user_id" in manifest_document or "owner" in manifest_document:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project ownership is derived from the authenticated user and cannot be supplied.",
        )
    project_id = str(manifest_document.get("project_id") or "").strip()
    try:
        compatibility = hosted_compatibility_metadata()
        ensure_supported_hardware_ir_version(
            manifest_document,
            supported_versions=compatibility.supported_hardware_ir_versions,
        )
        manifest_document["visibility"] = _delivery_visibility(request.visibility)
        manifest = ProjectManifest.from_document(manifest_document)
        payload = manifest.upload_payload()
        payload["artifacts"] = validate_artifact_references(payload.get("artifacts"), require_integrity=True)
        if payload["artifacts"] and not ProjectArtifactStorage().config.get("enabled"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CLI project artifact storage is not configured.",
            )
    except UnsupportedHardwareIntermediateRepresentationVersion as exc:
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "code": "UNSUPPORTED_HARDWARE_IR_VERSION",
                "message": str(exc),
                "hardware_ir_version": exc.version,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    idempotency_key = request.idempotency_key or _push_idempotency_key(manifest_document)
    delivery = _reserve_and_materialize_delivery(payload, owner, idempotency_key, request.parent_revision_id)
    return _pending_delivery_response(delivery)


@router.post("/deliver")
async def deliver_cli_project(
    request: ProjectDeliverRequest,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    owner = _owner(user)
    idempotency_key = str(request.idempotency_key or "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An idempotency_key is required.")
    manifest_document = dict(request.manifest or {})
    if "owner_user_id" in manifest_document or "owner" in manifest_document:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project ownership is derived from the authenticated user and cannot be supplied.",
        )
    project_id = str(manifest_document.get("project_id") or "").strip()
    try:
        compatibility = hosted_compatibility_metadata()
        ensure_supported_hardware_ir_version(
            manifest_document,
            supported_versions=compatibility.supported_hardware_ir_versions,
        )
        manifest_document["visibility"] = _delivery_visibility(request.visibility)
        manifest = ProjectManifest.from_document(manifest_document)
        payload = manifest.upload_payload()
        payload["artifacts"] = validate_artifact_references(payload.get("artifacts"), require_integrity=True)
        if payload["artifacts"] and not ProjectArtifactStorage().config.get("enabled"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CLI project artifact storage is not configured.",
            )
    except UnsupportedHardwareIntermediateRepresentationVersion as exc:
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "code": "UNSUPPORTED_HARDWARE_IR_VERSION",
                "message": str(exc),
                "hardware_ir_version": exc.version,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    delivery = _reserve_and_materialize_delivery(payload, owner, idempotency_key, request.parent_revision_id)
    return _pending_delivery_response(delivery)


@router.post("/deliver/{delivery_id}/complete")
async def complete_cli_project_delivery(
    delivery_id: str,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    owner = _owner(user)
    delivery = get_cli_project_delivery_by_id(delivery_id)
    if delivery is None or delivery["owner_user_id"] != owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Delivery session not found or not authorized.",
        )
    if delivery["status"] == "complete" and delivery.get("receipt"):
        return delivery["receipt"]
    if delivery["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Delivery session is not pending.")
    try:
        rows, summary, all_present = _verify_delivery_artifacts(delivery, ProjectArtifactStorage())
    except ProjectArtifactStorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Project artifact storage is unavailable.",
        ) from exc
    if not all_present:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DELIVERY_ARTIFACTS_MISSING",
                "artifacts": rows,
                "artifact_summary": summary,
            },
        )
    completed_at = _now()
    receipt = {
        "delivery_id": delivery["delivery_id"],
        "status": "complete",
        "project": _delivery_project_ref(delivery),
        "artifacts": rows,
        "artifact_summary": summary,
        "created_at": delivery["created_at"],
        "completed_at": completed_at,
    }
    updated = update_cli_project_delivery(
        delivery["delivery_id"],
        owner,
        {"status": "complete", "receipt_json": receipt, "completed_at": completed_at},
    )
    if updated is None or updated.get("receipt") is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Delivery session could not be completed.")
    invalidate_project_lists()
    return updated["receipt"]


@router.post("/{project_id}/publish")
async def publish_cli_project_endpoint(
    project_id: str,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    owner = _owner(user)
    try:
        result = publish_cli_project(project_id, owner, acting_user_id=owner)
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message or "another user" in message else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message) from exc
    result["audits"] = [
        {
            "acting_user_id": audit["acting_user_id"],
            "visibility_before": audit["visibility_before"],
            "created_at": audit["created_at"],
        }
        for audit in list_project_publish_audits(project_id, owner)
    ]
    return result


def _revision_artifact(project_id: str, revision_id: str, owner: str, artifact_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    revision = get_cli_project_revision(project_id, owner, revision_id)
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cloud project artifact not found or not authorized.",
        )
    try:
        manifest = ProjectManifest.from_document(revision.get("manifest") or {})
        artifacts = validate_artifact_references(manifest.artifacts, require_integrity=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    normalized_sha256 = str(artifact_sha256 or "").strip().lower()
    for artifact in artifacts:
        if artifact.get("sha256") == normalized_sha256:
            return revision, artifact
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="The requested artifact is not declared by this cloud revision.",
    )


@router.put("/{project_id}/revisions/{revision_id}/artifacts/{artifact_sha256}")
async def upload_cli_project_artifact(
    project_id: str,
    revision_id: str,
    artifact_sha256: str,
    request: Request,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    _revision, artifact = _revision_artifact(project_id, revision_id, _owner(user), artifact_sha256)
    try:
        media_type = normalize_artifact_media_type(request.headers.get("content-type"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Artifact Content-Type is required.") from exc
    if media_type != artifact["media_type"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Artifact media type mismatch for {artifact['path']}: "
                f"expected {artifact['media_type']}, received {media_type}."
            ),
        )

    storage = ProjectArtifactStorage()
    max_bytes = int(storage.config["max_bytes"])
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None:
            declared_length = int(content_length)
            if declared_length < 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length header.")
            if declared_length > max_bytes:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Project artifact is too large.")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length header.") from exc
    content_parts: list[bytes] = []
    content_size = 0
    async for chunk in request.stream():
        content_size += len(chunk)
        if content_size > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Project artifact is too large.")
        content_parts.append(chunk)
    content = b"".join(content_parts)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != artifact["sha256"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Artifact hash mismatch for {artifact['path']}: "
                f"expected {artifact['sha256']}, received {actual_sha256}."
            ),
        )
    if artifact.get("size_bytes") is not None and len(content) != artifact["size_bytes"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Artifact size mismatch for {artifact['path']}.",
        )
    try:
        stored = storage.put(project_id, artifact["sha256"], content, artifact["media_type"])
    except ProjectArtifactStorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Project artifact storage is unavailable.") from exc
    return {
        "path": artifact["path"],
        "status": "uploaded",
        "sha256": stored.sha256,
        "media_type": stored.media_type,
        "size_bytes": stored.size_bytes,
    }


@router.get("/{project_id}/revisions/{revision_id}/artifacts/{artifact_sha256}")
async def download_cli_project_artifact(
    project_id: str,
    revision_id: str,
    artifact_sha256: str,
    user: UserContext = Depends(require_user_context),
) -> Response:
    _revision, artifact = _revision_artifact(project_id, revision_id, _owner(user), artifact_sha256)
    try:
        stored = ProjectArtifactStorage().get(project_id, artifact["sha256"], artifact["media_type"])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cloud project artifact is missing.") from exc
    except ProjectArtifactStorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cloud project artifact is missing.") from exc
    content = stored.content or b""
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != artifact["sha256"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stored cloud artifact {artifact['path']} failed its SHA-256 integrity check.",
        )
    if artifact.get("size_bytes") is not None and len(content) != artifact["size_bytes"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stored cloud artifact {artifact['path']} failed its size check.",
        )
    return Response(
        content=content,
        media_type=artifact["media_type"],
        headers={
            "X-Forma-Artifact-SHA256": artifact["sha256"],
            "X-Forma-Artifact-Size": str(len(content)),
        },
    )


@router.get("/{project_id}/revisions/{revision_id}")
async def get_cli_project_revision_endpoint(
    project_id: str,
    revision_id: str,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    revision = get_cli_project_revision(project_id, _owner(user), revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cloud project revision not found.")
    return revision


@router.get("/{project_id}")
async def get_latest_cli_project_revision(
    project_id: str,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    owner = _owner(user)

    cli_revision = get_cli_project_revision(project_id, owner)

    try:
        canonical_revision = get_latest_project_revision(project_id, owner)
    except ProjectStateError:
        canonical_revision = None

    if canonical_revision is None:
        if cli_revision is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cloud project not found.",
            )
        return cli_revision

    manifest = dict((cli_revision or {}).get("manifest") or {})

    manifest.update(
        {
            "project_id": str(canonical_revision.project_id),
            "project_ir": canonical_revision.state.model_dump(mode="json"),
        }
    )

    if canonical_revision.state.overview is not None:
        manifest["title"] = canonical_revision.state.overview.title

    parent_revision_id = None

    if canonical_revision.parent_revision is not None and cli_revision is not None:
        cli_revision_number = int(cli_revision.get("revision") or 0)

        if cli_revision_number == canonical_revision.parent_revision:
            parent_revision_id = (
                str(cli_revision.get("revision_id") or "").strip() or None
            )

    return {
        "revision_id": str(canonical_revision.revision_id),
        "project_id": str(canonical_revision.project_id),
        "revision": canonical_revision.revision,
        "parent_revision_id": parent_revision_id,
        "manifest": manifest,
        "created_at": canonical_revision.created_at.isoformat(),
    }


__all__ = ["router"]
