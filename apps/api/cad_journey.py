"""Preserved CAD originals and bounded native-result uploads for project journeys."""
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import math
from typing import Literal
from uuid import UUID, uuid4
from zipfile import ZipFile, BadZipFile

from fastapi import Depends, Request, Query
from starlette.concurrency import run_in_threadpool

from apps.api.auth import UserContext, require_user_context
from apps.api.cad_workflows import router, _owned, _runs, _error, _snapshot, _fingerprint, _store, _load
from forma_core import database as db
from forma_core.cad_migrations import MigrationModel
from forma_core.cad_migrations.cli import _json
from forma_core.cad_migrations.evidence import NativeEvidence, compare_evidence
from forma_core.persistence.project_artifacts import ProjectArtifactStorageError
from forma_core.workspaces.projects.state import ProjectArtifact, ProjectStateError

LIMIT = 50 * 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,140}$")
EXTENSIONS = {"solidworks": (".sldprt", ".sldasm"), "inventor": (".ipt", ".iam"),
              "creo": (".prt", ".asm"), "form": (".step", ".stp")}


async def bounded_body(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > LIMIT:
            raise _error(413, "CAD files must not exceed 50 MiB")
    if not body:
        raise _error(422, "Choose a non-empty CAD file")
    return bytes(body)


def filename(value):
    if not NAME.fullmatch(value) or PurePosixPath(value).name != value:
        raise ValueError("Use a simple CAD filename without path separators")
    return value


def step_envelope(content):
    if not content.lstrip().startswith(b"ISO-10303-21;") or not content.rstrip().endswith(b"END-ISO-10303-21;"):
        raise ValueError("The STEP preview has no valid STEP envelope")


def validate_mesh(content):
    mesh = json.loads(content)
    if not isinstance(mesh, dict):
        raise ValueError("Native preview mesh must be an object")
    if mesh.get("format") != "forma-cad-mesh" or mesh.get("units") != "mm":
        raise ValueError("Native preview mesh must declare millimeters")
    vertices, faces = mesh.get("vertices"), mesh.get("faces")
    if not isinstance(vertices, list) or not isinstance(faces, list) or not 9 <= len(vertices) <= 300000 or not 3 <= len(faces) <= 600000 or len(vertices) % 3 or len(faces) % 3:
        raise ValueError("Invalid or oversized native preview mesh")
    if any(type(v) not in {int, float} or not math.isfinite(v) or abs(v) > 1e7 for v in vertices) or any(type(i) is not int or not 0 <= i < len(vertices)//3 for i in faces):
        raise ValueError("Native mesh coordinates or face indices are invalid")


def save(project, owner, parent, state, run, job):
    runs = _runs(parent)
    state.assembly_metadata = {**(state.assembly_metadata or {}), "cad_workflows": [r for r in runs if r["id"] != run["id"]] + [run]}
    artifacts = [ProjectArtifact(artifact_id="cad-" + a["sha256"], kind="cad-workflow", uri="artifact://" + a["sha256"],
                 media_type=a["media_type"], checksum="sha256:" + a["sha256"], metadata={"filename": a["filename"], "run_id": run["id"]}) for a in run["artifacts"].values()]
    revision = db.append_project_revision(project, owner, state, source_job_id=job, expected_parent_revision=parent.revision, artifacts=artifacts)
    return _snapshot(project, revision)


def import_source(project, user, content, name, system, expected, request_id, preview_of=None):
    owner = _owned(project, user)
    try:
        filename(name)
        extension = PurePosixPath(name).suffix.lower()
        if extension not in (*EXTENSIONS[system], ".step", ".stp"):
            raise ValueError("The file extension does not match the source CAD system")
        if extension in {".step", ".stp"}: step_envelope(content)
        digest = hashlib.sha256(content).hexdigest()
        request_hash = hashlib.sha256(_json({"filename": name, "sha256": digest, "system": system, "revision": expected, "preview_of": preview_of})).hexdigest()
        job = "cad-workflow-import-" + str(request_id)
        replay = db.get_project_revision_by_source_job(project, owner, job)
        if replay:
            if not any(r.get("request_hash") == request_hash for r in _runs(replay)):
                raise _error(409, "This import request ID already identifies different content")
            return _snapshot(project, db.get_latest_project_revision(project, owner))
        parent = db.get_latest_project_revision(project, owner)
        if parent.revision != expected: raise _error(409, "The project changed. Refresh before importing")
        if len(_runs(parent)) >= 50: raise ValueError("The project CAD workflow limit is reached")
        state = parent.state.model_copy(deep=True)
        original = (state.assembly_metadata or {}).get("cad_source")
        if preview_of and (not original or original["sha256"] != preview_of or extension not in {".step", ".stp"} or original["system"] != system):
            raise ValueError("The source STEP preview must identify the current preserved original")
        artifact = _store(project, name, content, "model/step" if extension in {".step", ".stp"} else "application/octet-stream")
        source = {**artifact, "system": system, "actor": owner, "imported_at": datetime.now(timezone.utc).isoformat(),
                  "origin": "owner_uploaded_original", "source_revision_id": str(parent.revision_id)}
        if preview_of: source = {**original, "preview_sha256": digest}
        state.assembly_metadata = {**(state.assembly_metadata or {}), "cad_source": source}
        # A proprietary original does not silently replace an unrelated preview.
        if extension in {".step", ".stp"}:
            state.cad_model = {"adapter": "forma-opencad", "format": "step", "project_id": project,
                "stored_sha256": digest, "filename": name, "url": f"/api/opencode/projects/{project}/cad/{digest}"}
        else:
            state.cad_model = None
        run = {"id": str(uuid4()), "kind": "source-preview" if preview_of else "source", "status": "original_preserved", "source_system": system,
               "source_sha256": digest, "source_revision": parent.revision, "source_revision_id": str(parent.revision_id),
               "design_fingerprint": _fingerprint(state), "created_at": source["imported_at"], "artifacts": {"source_step" if preview_of else "source": artifact},
               "reviews": [], "request_hash": request_hash}
        return save(project, owner, parent, state, run, job)
    except (ValueError, BadZipFile) as exc: raise _error(422, str(exc)) from exc
    except ProjectStateError as exc: raise _error(409, "The project changed. Refresh and retry") from exc
    except (ProjectArtifactStorageError, OSError) as exc: raise _error(503, "The private CAD artifact store is unavailable") from exc


@router.post("/projects/{project_id}/cad-workflows/source")
async def upload_source(project_id: UUID, request: Request, filename_: str = Query(alias="filename"),
    system: Literal["solidworks", "inventor", "creo", "form"] = Query(),
    expected_revision: int = Query(ge=1), request_id: UUID = Query(), preview_of: str | None = Query(None, pattern="^[0-9a-f]{64}$"), user: UserContext = Depends(require_user_context)):
    project = str(project_id)
    _owned(project, user)
    content = await bounded_body(request)
    return await run_in_threadpool(import_source, project, user, content, filename_, system, expected_revision, request_id, preview_of)


def parse_result(content, target):
    with ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(entries) > 6 or len(set(names)) != len(names) or sum(i.file_size for i in entries) > LIMIT:
            raise ValueError("Native result bundle exceeds file/size limits")
        for item in entries:
            filename(item.filename)
            if item.is_dir() or item.flag_bits & 1 or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("Directories, encrypted files and symlinks are not native outputs")
        if "evidence.json" not in names or archive.getinfo("evidence.json").file_size > 2*1024*1024:
            raise ValueError("Include one bounded evidence.json report")
        report = NativeEvidence.model_validate_json(archive.read("evidence.json"))
        if set(names) != {"evidence.json", *report.native_artifacts}:
            raise ValueError("Every returned model must be named and hashed by its evidence report")
        native_extension = {"fusion360": ".f3d", "nx": ".prt", "onshape": ".onshape.json"}[target]
        if not any(n.lower().endswith(native_extension) for n in report.native_artifacts) or not any(n.lower().endswith((".step", ".stp")) for n in report.native_artifacts):
            raise ValueError("Include the target native model (or Onshape document reference) and its STEP preview")
        files = {}
        for name, digest in report.native_artifacts.items():
            if not name.lower().endswith((native_extension, ".step", ".stp")) and name not in {"source.mesh.json", "target.mesh.json"}:
                raise ValueError("Unexpected native output format")
            data = archive.read(name)
            if not data or hashlib.sha256(data).hexdigest() != digest: raise ValueError("Native file checksum failed")
            if name.lower().endswith((".step", ".stp")): step_envelope(data)
            if name in {"source.mesh.json", "target.mesh.json"}: validate_mesh(data)
            if name.lower().endswith(".onshape.json"):
                document = json.loads(data)
                if not isinstance(document, dict): raise ValueError("Onshape document reference must be an object")
                reference = document.get("document_url", "")
                if not re.fullmatch(r"https://cad\.onshape\.com/documents/[a-zA-Z0-9]+/w/[a-zA-Z0-9]+/e/[a-zA-Z0-9]+", reference):
                    raise ValueError("Invalid Onshape document reference")
            files[name] = data
        return report, files


def store_result(project, user, run_id, content, attempt):
    owner = _owned(project, user)
    try:
        digest = hashlib.sha256(content).hexdigest()
        job = "cad-workflow-result-" + run_id + "-" + digest
        replay = db.get_project_revision_by_source_job(project, owner, job)
        if replay: return _snapshot(project, db.get_latest_project_revision(project, owner))
        parent = db.get_latest_project_revision(project, owner)
        run = next((dict(r) for r in _runs(parent) if r["id"] == run_id and r["kind"] == "migration"), None)
        if not run: raise _error(404, "Migration run not found")
        if run["design_fingerprint"] != _fingerprint(parent.state): raise _error(409, "The design changed. Replan before attaching results")
        if "package" not in run["artifacts"]: raise ValueError("Build a reviewed package before attaching native results")
        if attempt:
            execution = run.get("execution", {})
            if run["status"] != "running_in_cad" or str(attempt) != execution.get("attempt_id") or datetime.fromisoformat(execution["expires_at"]) < datetime.now(timezone.utc):
                raise _error(409, "This worker execution attempt was cancelled, expired or replaced")
        elif run["status"] not in {"awaiting_native_execution", "worker_failed", "needs_repair", "checks_passed", "cancelled", "accepted_by_reviewer", "rejected_by_reviewer"}:
            raise _error(409, "Wait for the worker or cancel its attempt before submitting manual results")
        report, files = parse_result(content, run["target"])
        history = MigrationModel.model_validate_json(_load(project, run["artifacts"]["history"]))
        comparison = compare_evidence(history, run["target"], report, source_step_sha256=run["artifacts"].get("source_step", {}).get("sha256"))
        run["artifacts"] = {k: v for k, v in run["artifacts"].items() if not k.startswith("native_") and k not in {"target_step", "source_mesh", "target_mesh"}}
        for index, (name, data) in enumerate(files.items()):
            is_step = name.lower().endswith((".step", ".stp"))
            key = "source_mesh" if name == "source.mesh.json" else "target_mesh" if name == "target.mesh.json" else "target_step" if is_step else "native_" + str(index)
            run["artifacts"][key] = _store(project, name, data, "application/json" if key.endswith("_mesh") else "model/step" if is_step else "application/octet-stream")
        run["artifacts"]["evidence"] = _store(project, "native-evidence.json", _json(report.model_dump(mode="json")), "application/json")
        run["artifacts"]["comparison"] = _store(project, "comparison.json", _json(comparison), "application/json")
        run.update(status=comparison["status"], comparison=comparison, reviews=[], updated_at=datetime.now(timezone.utc).isoformat())
        if attempt: run["execution"] = {**run["execution"], "finished_at": run["updated_at"]}
        return save(project, owner, parent, parent.state.model_copy(deep=True), run, job)
    except (ValueError, BadZipFile, KeyError, RuntimeError) as exc: raise _error(422, "Invalid native result bundle: " + str(exc)) from exc
    except ProjectStateError as exc: raise _error(409, "The project changed. Refresh and retry the same result bundle") from exc
    except (ProjectArtifactStorageError, OSError) as exc: raise _error(503, "The private CAD artifact store is unavailable") from exc


@router.post("/projects/{project_id}/cad-workflows/runs/{run_id}/result")
async def upload_result(project_id: UUID, run_id: UUID, request: Request, attempt: UUID | None = Query(None), user: UserContext = Depends(require_user_context)):
    project = str(project_id)
    _owned(project, user)
    content = await bounded_body(request)
    return await run_in_threadpool(store_result, project, user, str(run_id), content, attempt)
