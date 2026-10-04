"""Loopback-only E2E fixture: real sharing/history routes with temporary SQLite.

Run with ``python -m tests.projects.project_sharing_browser_server``. The test
identity header exists only in this fixture, never in the production application.
"""
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from apps.api.auth import UserContext, require_user_context
from apps.api.project_history_api import router as history_router
from apps.api.project_share_api import router as share_router
from forma_core import database
from forma_core.workspaces.design_briefs import DesignBriefCreate
from forma_core.workspaces.projects.models import HardwareIR, ProjectOverview
from forma_core.workspaces.projects.state import ProjectRevisionDraft, ProjectStateService
from tests.persistence.test_design_briefs import sqlite_repository

OWNER = "sharing-browser-owner"
state = {}


@asynccontextmanager
async def lifespan(_app):
    with sqlite_repository():
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(history_router)
app.include_router(share_router)


def test_user(x_test_owner: str | None = Header(None)):
    owner = OWNER if x_test_owner == OWNER else ""
    return UserContext(provider="test", subject=owner, owner_user_id=owner, is_authenticated=bool(owner), is_admin=False)


app.dependency_overrides[require_user_context] = test_user


@app.get("/test/health")
def health():
    return {"status": "ok"}


@app.post("/test/reset")
def reset():
    project_id = str(uuid4())
    brief = database.create_design_brief_version(project_id, OWNER, DesignBriefCreate(
        schema_version="1.0", conversation_id="private-chat", intent="Private request", summary="Private instructions",
    ))
    state.update(project_id=project_id, brief=brief, revisions=[])
    for _ in range(3):
        save()
    return {"project_id": project_id, "revisions": [str(r.revision_id) for r in state["revisions"]]}


@app.post("/test/save")
def save():
    service = ProjectStateService(database._DATABASE_REPOSITORY)
    number = len(state["revisions"]) + 1
    title = f"Robot arm version {number}"
    ir = HardwareIR(overview=ProjectOverview(title=title, description=f"Saved arm design {number}.", difficulty="Beginner", category="Mechanical"),
                    assembly_metadata={"chat_id": "private-chat", "reference_image_url": "private-upload", "last_iteration": {"instruction": "Private instructions"}})
    draft = ProjectRevisionDraft(state=ir)
    kwargs = dict(project_id=state["project_id"], owner_user_id=OWNER, source_job_id=f"browser-{number}")
    if number == 1:
        result = service.create_initial_revision(draft, **kwargs, design_brief_id=state["brief"].design_brief_id, design_brief_version=1)
    else:
        result = service.create_revision(draft, **kwargs)
    state["revisions"].append(result.revision)
    return {"revision": number}


@app.get("/projects/{project_id}")
def latest(project_id: str, x_test_owner: str | None = Header(None)):
    if x_test_owner != OWNER or project_id != state.get("project_id"):
        raise HTTPException(404)
    latest = state["revisions"][-1]
    ir = latest.state.model_dump(mode="json")
    ir["assembly_metadata"].update(project_id=project_id, canonical_revision_id=str(latest.revision_id), project_revision=latest.revision)
    return {"project_id": project_id, "can_chat": True, "project_ir": ir}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=4178)
