"""Loopback-only browser fixture: real routes/storage, explicitly synthetic CAD evidence.

Run from repository root: .venv/bin/python -m tests.projects.cad_workflows_fixture
"""
from contextlib import asynccontextmanager, ExitStack
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apps.api import cad_workflows, project_history_api
from apps.api.auth import UserContext, require_user_context
from forma_core import database as db
from forma_core.persistence.project_artifacts import ProjectArtifactStorage
from forma_core.workspaces.design_briefs import DesignBriefCreate
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ProjectOverview
from forma_core.workspaces.projects.state import ProjectRevisionDraft, ProjectStateService
from tests.integrations.test_cad_native_evidence import sample, evidence
from tests.persistence.test_design_briefs import sqlite_repository

PROJECT = "ca000000-0000-4000-8000-000000000001"
EMPTY = "ca000000-0000-4000-8000-000000000002"
OWNER = "cad-browser-fixture"
USER = UserContext(provider="test", subject=OWNER, owner_user_id=OWNER, is_authenticated=True, is_admin=False)


@asynccontextmanager
async def lifespan(app):
    import hashlib
    with ExitStack() as stack:
        stack.enter_context(sqlite_repository())
        storage = stack.enter_context(TemporaryDirectory())
        stack.enter_context(patch("forma_core.persistence.project_artifacts.get_project_artifact_storage_config", return_value={
            "backend": "local", "enabled": True, "directory": storage, "max_bytes": 50*1024*1024}))
        for project in (PROJECT, EMPTY):
            brief = db.create_design_brief_version(project, OWNER, DesignBriefCreate(schema_version="1.0", conversation_id="fixture",
                intent="CAD browser test", summary="CAD browser test"))
            content = b"ISO-10303-21;\n/* synthetic browser fixture, not native geometry */\nEND-ISO-10303-21;"
            digest = hashlib.sha256(content).hexdigest()
            ProjectArtifactStorage().put(project, digest, content, "model/step")
            state = HardwareIntermediateRepresentation(overview=ProjectOverview(title="Browser test bracket", description="Synthetic fixture", difficulty="Beginner", category="Mechanical"),
                cad_model={"format": "step", "stored_sha256": digest} if project == PROJECT else None,
                mechanical={"enclosure_type": "Test part", "mounting_guidance": "Synthetic fixture", "manufacturability_rating": "Easy",
                            "cad_operations": [{"shape": "box", "size": {"x_mm": 40, "y_mm": 20, "z_mm": 4}}]})
            ProjectStateService(db._DATABASE_REPOSITORY).create_initial_revision(ProjectRevisionDraft(state=state), project_id=project,
                owner_user_id=OWNER, source_job_id="fixture-initial", design_brief_id=brief.design_brief_id, design_brief_version=1)
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(cad_workflows.router, prefix="/api")
app.include_router(project_history_api.router, prefix="/api")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:3016"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.dependency_overrides[require_user_context] = lambda: USER


@app.get("/api/fixture/{name}")
def fixture_file(name: str):
    if name not in {"history.json", "evidence.json"}:
        raise HTTPException(404)
    model = sample()
    payload = model.model_dump(mode="json") if name == "history.json" else evidence(model).model_dump(mode="json")
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/fixture/change-design")
def change_design():
    state = db.get_latest_project_revision(PROJECT, OWNER).state.model_copy(deep=True)
    state.overview.description += " updated"
    from uuid import uuid4
    revision = db.append_project_revision(PROJECT, OWNER, state, source_job_id="fixture-edit-"+str(uuid4()))
    return {"revision": revision.revision}


@app.get("/api/projects/{project}")
def read_project(project: str):
    if project not in {PROJECT, EMPTY}: raise HTTPException(404)
    revision = db.get_latest_project_revision(project, OWNER)
    ir = revision.state.model_dump(mode="json")
    ir["assembly_metadata"] = {**(ir.get("assembly_metadata") or {}), "project_id": project, "can_chat": True,
                              "project_revision": revision.revision, "canonical_revision_id": str(revision.revision_id)}
    return {"project_ir": ir, "project_id": project, "revision_id": str(revision.revision_id), "can_chat": True}


@app.get("/api/projects")
@app.get("/api/my/projects")
def project_list():
    return {"items": [], "total": 0, "limit": 20, "offset": 0}


@app.get("/api/")
@app.get("/api/health")
def health(): return {"status": "ok"}


@app.get("/api/chats")
def chats(): return {"items": [], "total": 0}


@app.get("/api/pipeline/steps")
def steps(): return {"steps": []}


@app.get("/api/runtime/config")
def runtime():
    return {"contract_version": 1, "authority": "backend", "forma_dev_mode": False,
        "generation": {"ready": False, "available": False, "reason": "Synthetic CAD test fixture", "selected_llm": None, "llm_options": []},
        "images": {"enabled": False, "configured": False, "request_capable": False, "provider": None, "model": None, "generate_by_default": False, "reason": "Synthetic test"},
        "workflow": {"default_id": "standard", "options": []},
        "provider_setup": {"required": False, "llm_required": False, "image_required": False},
        "deployment": {"hosted_chat_enabled": False, "authoring_mode_enabled": False, "authoring_access": False}}


@app.get("/api/projects/{project}/exports")
def exports(project: str):
    revision = db.get_latest_project_revision(project, OWNER)
    cad = revision.state.cad_model or {}
    if not cad.get("stored_sha256"): raise HTTPException(404, {"code": "step_not_found", "message": "No saved STEP"})
    return {"project_id": project, "step": {"filename": "synthetic.step", "sha256": cad["stored_sha256"], "size_bytes": 98,
            "download_url": f"/projects/{project}/history/{revision.revision_id}/cad/{cad['stored_sha256']}"}, "printers": []}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8016)
