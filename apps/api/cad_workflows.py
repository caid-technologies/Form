"""Owner-scoped, revision-bound CAD handoffs and migration review actions."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from copy import deepcopy
import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.auth import UserContext, require_user_context
from forma_core import database as db
from forma_core.cad_export import CadMetadata, build_export, export_capabilities
from forma_core.cad_migrations import MigrationModel, plan_migration
from forma_core.cad_migrations.cli import _json, build_migration
from forma_core.cad_migrations.evidence import NativeEvidence, compare_evidence
from forma_core.cad_migrations.form_history import from_project
from forma_core.cad_migrations.planner import ROUTES, history_bytes
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, ProjectArtifactStorageError
from forma_core.workspaces.projects.state import ProjectArtifact, ProjectStateError

router = APIRouter(tags=["cad-workflows"])
MAX_BODY = 2 * 1024 * 1024


class CadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["export", "plan", "form-history", "build", "evidence", "review", "queue", "claim", "cancel", "worker-failed"]
    request_id: UUID
    expected_revision: int = Field(strict=True, ge=1)
    target: Literal["solidworks", "onshape", "fusion360", "nx"] | None = None
    run_id: UUID | None = None
    history: MigrationModel | None = None
    metadata: CadMetadata | None = None
    evidence: NativeEvidence | None = None
    approve_inferred: bool = False
    decision: Literal["accept", "reject"] | None = None
    note: str = Field(default="", max_length=2000)
    worker_id: str = Field(default="customer-worker", max_length=120)
    attempt_id: UUID | None = None


def _error(code: int, message: str):
    return HTTPException(code, detail={"message": message})


def _owned(project_id: str, user: UserContext):
    owner = str(user.owner_user_id or "").strip()
    if not owner:
        raise _error(401, "Sign in to use CAD workflows.")
    identity = db.get_project_identity(project_id)
    if not identity or identity.get("owner_user_id") != owner or identity.get("status", "active") != "active":
        raise _error(404, "Project not found.")
    return owner


def _fingerprint(state):
    value = state.model_dump(mode="json")
    metadata = value.get("assembly_metadata") or {}
    for key in ("cad_workflows", "revision", "project_revision", "canonical_revision_id", "source_job_id"):
        metadata.pop(key, None)
    value["assembly_metadata"] = metadata
    return hashlib.sha256(_json(value)).hexdigest()


def _runs(revision):
    value = (revision.state.assembly_metadata or {}).get("cad_workflows", [])
    if not isinstance(value, list) or len(value) > 50 or any(not isinstance(item, dict) for item in value):
        raise _error(409, "Stored CAD workflow records require repair.")
    return value


def capabilities():
    return {"exports": export_capabilities(), "routes": [{"source": a, "target": b} for a, b in ROUTES],
            "history_schema": MigrationModel.model_json_schema(), "evidence_schema": NativeEvidence.model_json_schema(),
            "native_execution": "external_licensed_application", "native_certification": False}


def _snapshot(project_id, revision):
    fingerprint = _fingerprint(revision.state)
    runs = [{**item, "stale": item.get("design_fingerprint") != fingerprint} for item in _runs(revision)]
    cad = revision.state.cad_model or {}
    return {"project_id": project_id, "revision": revision.revision, "revision_id": str(revision.revision_id),
            "step_available": cad.get("format", "").lower() in {"step", "stp"} and bool(cad.get("stored_sha256") or cad.get("sha256")),
            "form_history_available": bool(revision.state.mechanical and revision.state.mechanical.cad_operations),
            "source": (revision.state.assembly_metadata or {}).get("cad_source"),
            "exports": export_capabilities(), "routes": [{"source": a, "target": b} for a, b in ROUTES], "runs": runs}


def get_workflows(project_id: str, user: UserContext):
    owner = _owned(project_id, user)
    try:
        return _snapshot(project_id, db.get_latest_project_revision(project_id, owner))
    except ProjectStateError as exc:
        raise _error(404, "Save the project before using CAD workflows.") from exc


def _store(project_id, name, content, media_type):
    digest = hashlib.sha256(content).hexdigest()
    ProjectArtifactStorage().put(project_id, digest, content, media_type)
    return {"filename": name, "sha256": digest, "size_bytes": len(content), "media_type": media_type}


def _load(project_id, artifact):
    result = ProjectArtifactStorage().get(project_id, artifact["sha256"], artifact["media_type"])
    content = result.content or b""
    if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
        raise ValueError("Stored artifact failed its checksum")
    return content


def _plan_summary(plan):
    return {key: plan[key] for key in ("status", "blockers", "limitations", "history_sha256", "features")}


def execute_action(project_id: str, user: UserContext, command: CadAction):
    owner = _owned(project_id, user)
    job = "cad-workflow-" + str(command.request_id)
    request_hash = hashlib.sha256(_json(command.model_dump(mode="json"))).hexdigest()
    try:
        replay = db.get_project_revision_by_source_job(project_id, owner, job)
        if replay is not None:
            if not any(item.get("request_hash") == request_hash for item in _runs(replay)):
                raise _error(409, "This request ID was already used for different content.")
            return _snapshot(project_id, db.get_latest_project_revision(project_id, owner))
        parent = db.get_latest_project_revision(project_id, owner)
        if parent.revision != command.expected_revision:
            raise _error(409, "The project changed. Refresh and review the latest revision.")
        state = parent.state.model_copy(deep=True)
        runs = list(_runs(parent))
        now = datetime.now(timezone.utc).isoformat()
        if command.action in {"export", "plan", "form-history"}:
            if len(runs) >= 50:
                raise _error(409, "This project has reached its 50 CAD workflow limit. Use a new project for additional runs.")
            run = {"id": str(uuid4()), "kind": "export" if command.action == "export" else "migration",
                   "target": command.target, "source_revision": parent.revision, "source_revision_id": str(parent.revision_id),
                   "design_fingerprint": _fingerprint(state), "created_at": now, "artifacts": {}, "reviews": []}
            if command.action == "export":
                cad = state.cad_model or {}
                digest = cad.get("stored_sha256") or cad.get("sha256")
                if cad.get("format", "").lower() not in {"step", "stp"} or not digest:
                    raise ValueError("This revision has no saved STEP artifact")
                step = _load(project_id, {"sha256": digest, "media_type": "model/step"})
                metadata = CadMetadata.model_validate({"title": state.overview.title,
                    **(command.metadata.model_dump(exclude_unset=True) if command.metadata else {}),
                    "source_project_id": project_id, "source_revision_id": str(parent.revision_id)})
                bundle = build_export(step, command.target, metadata)
                run.update(status="package_created", source_sha256=digest)
                run["artifacts"]["package"] = _store(project_id, "cad-export.zip", bundle, "application/zip")
            else:
                history = from_project(state) if command.action == "form-history" else command.history
                if history is None:
                    raise ValueError("A source history is required")
                source = (state.assembly_metadata or {}).get("cad_source")
                if command.action == "form-history" and source:
                    raise ValueError("An imported CAD model requires its own source history, not the previous Form operation list")
                if command.action == "plan" and source and (history.source.sha256 != source["sha256"] or history.source.system != source["system"]):
                    raise ValueError("The history must identify the imported original CAD file and source system")
                if command.target is None:
                    raise ValueError("Choose a migration target")
                plan = plan_migration(history, command.target)
                run.update(status=plan["status"], source_system=history.source.system,
                           source_sha256=history.source.sha256, plan=_plan_summary(plan))
                run["artifacts"]["history"] = _store(project_id, "source-history.json", history_bytes(history), "application/json")
                run["artifacts"]["plan"] = _store(project_id, "plan.json", _json(plan), "application/json")
                if history.source_geometry:
                    run["source_geometry"] = history.source_geometry.model_dump(mode="json")
                cad = state.cad_model or {}
                step_digest = cad.get("stored_sha256") or cad.get("sha256")
                if cad.get("format") in {"step", "stp"} and step_digest:
                    content = _load(project_id, {"sha256": step_digest, "media_type": "model/step"})
                    run["artifacts"]["source_step"] = _store(project_id, "source.step", content, "model/step")
                if source:
                    run["artifacts"]["source"] = source
            runs.append(run)
        else:
            run = next((deepcopy(item) for item in runs if item.get("id") == str(command.run_id)), None)
            if run is None or run.get("kind") != "migration":
                raise _error(404, "Migration run not found.")
            if run["design_fingerprint"] != _fingerprint(state):
                raise _error(409, "This migration belongs to an older design. Start a new plan from the current revision.")
            history = MigrationModel.model_validate_json(_load(project_id, run["artifacts"]["history"]))
            if command.action == "build":
                if run["status"] not in {"blocked", "ready_for_rebuild"}:
                    raise ValueError("This run already has a package. Start a new plan to change its history")
                if command.approve_inferred and not command.note.strip():
                    raise ValueError("Record what you reviewed before approving inferred features")
                plan = plan_migration(history, run["target"], approve_inferred=command.approve_inferred)
                bundle = build_migration(history, run["target"], approve_inferred=command.approve_inferred)
                run.update(status="awaiting_native_execution", plan=_plan_summary(plan),
                           inference_review={"approved": command.approve_inferred, "actor": owner, "note": command.note, "at": now})
                run["artifacts"]["plan"] = _store(project_id, "plan.json", _json(plan), "application/json")
                run["artifacts"]["package"] = _store(project_id, "cad-rebuild.zip", bundle, "application/zip")
            elif command.action == "queue":
                if "package" not in run["artifacts"] or run["status"] not in {"awaiting_native_execution", "worker_failed", "cancelled"}:
                    raise ValueError("Build a package before queuing a new native execution attempt")
                run.update(status="queued_for_worker", execution={"attempt_id": str(uuid4()), "queued_at": now})
            elif command.action == "claim":
                if run["status"] != "queued_for_worker":
                    raise _error(409, "This execution attempt is no longer queued")
                run.update(status="running_in_cad", execution={**run["execution"], "worker_id": command.worker_id,
                    "claimed_at": now, "expires_at": (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()})
            elif command.action == "cancel":
                if run["status"] not in {"queued_for_worker", "running_in_cad"}:
                    raise ValueError("There is no active execution to cancel")
                run.update(status="cancelled")
            elif command.action == "worker-failed":
                if run["status"] != "running_in_cad" or str(command.attempt_id) != run.get("execution", {}).get("attempt_id"):
                    raise _error(409, "The worker attempt is no longer active")
                run.update(status="worker_failed", execution={**run["execution"], "failure": command.note, "finished_at": now})
            elif command.action == "evidence":
                if "package" not in run["artifacts"] or command.evidence is None:
                    raise ValueError("Build the package and supply a native evidence report first")
                comparison = compare_evidence(history, run["target"], command.evidence, source_step_sha256=run["artifacts"].get("source_step", {}).get("sha256"))
                run.update(status=comparison["status"], comparison=comparison, reviews=[])
                # Report-only replacement invalidates previously uploaded model bytes.
                run["artifacts"] = {key: value for key, value in run["artifacts"].items() if not key.startswith("native_") and key not in {"target_step", "source_mesh", "target_mesh"}}
                run["artifacts"]["evidence"] = _store(project_id, "native-evidence.json", _json(command.evidence.model_dump(mode="json")), "application/json")
                run["artifacts"]["comparison"] = _store(project_id, "comparison.json", _json(comparison), "application/json")
            else:
                if command.decision is None or not command.note.strip():
                    raise ValueError("A review decision and note are required")
                if command.decision == "accept" and run.get("comparison", {}).get("status") != "checks_passed":
                    raise ValueError("Resolve the evidence checks before accepting the native result")
                if command.decision == "accept" and ("target_step" not in run["artifacts"] or not any(key.startswith("native_") for key in run["artifacts"])):
                    raise ValueError("Attach the rebuilt native model and its STEP preview before accepting")
                if len(run["reviews"]) >= 10:
                    raise ValueError("Review limit reached; start a new migration run")
                run["reviews"] = [*run["reviews"], {"decision": command.decision, "actor": owner, "note": command.note, "at": now,
                    "history_sha256": run["artifacts"]["history"]["sha256"],
                    "evidence_sha256": run["artifacts"].get("evidence", {}).get("sha256")}]
                run["status"] = "accepted_by_reviewer" if command.decision == "accept" else "rejected_by_reviewer"
                if command.decision == "accept":
                    digest = run["artifacts"]["target_step"]["sha256"]
                    state.cad_model = {"adapter": "forma-opencad", "format": "step", "project_id": project_id,
                                       "stored_sha256": digest, "filename": "rebuilt.step",
                                       "url": f"/api/opencode/projects/{project_id}/cad/{digest}"}
                    run["design_fingerprint"] = _fingerprint(state)
            runs = [run if item["id"] == run["id"] else item for item in runs]
        run.update(updated_at=now, request_hash=request_hash)
        state.assembly_metadata = {**(state.assembly_metadata or {}), "cad_workflows": runs}
        artifacts = [ProjectArtifact(artifact_id="cad-" + item["sha256"], kind="cad-workflow",
            uri="artifact://" + item["sha256"], media_type=item["media_type"], checksum="sha256:"+item["sha256"],
            metadata={"filename": item["filename"], "run_id": run["id"]}) for item in run["artifacts"].values()]
        revision = db.append_project_revision(project_id, owner, state, source_job_id=job,
                                              expected_parent_revision=parent.revision, artifacts=artifacts)
        return _snapshot(project_id, revision)
    except HTTPException:
        raise
    except ProjectStateError as exc:
        raise _error(409, "The project changed or has no saved revision. Refresh and retry.") from exc
    except (ProjectArtifactStorageError, OSError) as exc:
        raise _error(503, "The private artifact store is unavailable. Retry using the same request ID.") from exc
    except ValueError as exc:
        raise _error(422, str(exc)) from exc


@router.get("/cad-capabilities")
def get_capabilities(user: UserContext = Depends(require_user_context)):
    return capabilities()


@router.get("/projects/{project_id}/cad-workflows")
def list_workflows(project_id: UUID, response: Response, user: UserContext = Depends(require_user_context)):
    response.headers["Cache-Control"] = "private, no-store"
    return get_workflows(str(project_id), user)


@router.post("/projects/{project_id}/cad-workflows")
async def post_action(project_id: UUID, request: Request, response: Response, user: UserContext = Depends(require_user_context)):
    response.headers["Cache-Control"] = "private, no-store"
    # Bound bytes before parsing nested evidence/history, including chunked uploads.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise _error(413, "CAD workflow requests must not exceed 2 MiB.")
    try:
        command = CadAction.model_validate_json(body)
    except ValidationError as exc:
        raise _error(422, "Invalid CAD action, history or evidence. Use the published CAD schemas.") from exc
    return await run_in_threadpool(execute_action, str(project_id), user, command)


@router.get("/projects/{project_id}/cad-workflows/artifacts/{revision_id}/{digest}")
def download_artifact(project_id: UUID, revision_id: UUID, digest: str, user: UserContext = Depends(require_user_context)):
    project = str(project_id)
    owner = _owned(project, user)
    try:
        revision = db.get_project_revision_by_id(project, owner, str(revision_id))
        artifact = next((item for run in _runs(revision) for item in run.get("artifacts", {}).values() if item.get("sha256") == digest), None)
        if artifact is None:
            raise _error(404, "Artifact is not part of this project revision.")
        content = _load(project, artifact)
        # File names and MIME are selected from a fixed set, never echoed from project metadata.
        return Response(content, media_type=artifact["media_type"],
                        headers={"Content-Disposition": 'attachment; filename="' + artifact["filename"] + '"',
                                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    except HTTPException:
        raise
    except (ProjectStateError, FileNotFoundError) as exc:
        raise _error(404, "Artifact not found.") from exc
    except (ProjectArtifactStorageError, OSError, ValueError) as exc:
        raise _error(503, "Artifact failed its storage or integrity check.") from exc


# Additional binary import/result routes share the same router and owner checks.
from apps.api import cad_journey as _cad_journey  # noqa: E402,F401
