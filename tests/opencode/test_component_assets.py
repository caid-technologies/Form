"""Exercise the HTTP tool boundary, canonical project pins and authenticated bytes."""
import base64
import json
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.api.opencode_api import router
from forma_core import database
from forma_core.assets.models import AssetToolArguments
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.opencode.models import McpJsonRpcRequest
from forma_core.persistence.repositories.sqlite import SqlAlchemyRepository
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from tests.integrations.test_component_assets import DATA, VALIDATION, make_library, registration


@pytest.fixture
def client(tmp_path, monkeypatch):
    library=make_library(tmp_path, workspace="")
    provider=library.repository.provider
    monkeypatch.setattr(database,"_DATABASE_PROVIDER",provider)
    monkeypatch.setattr(database,"_DATABASE_REPOSITORY",SqlAlchemyRepository(provider.session_factory))
    monkeypatch.setattr("apps.api.component_assets.ProjectArtifactStorage",lambda:library.storage)
    project_id=str(uuid4())
    database.persist_chat_project_revision(project_id,"alice",HardwareIntermediateRepresentation(),
        source_job_id="initial",prompt="Arm",visibility="private")
    cap=ConnectorCapability("mini","session",project_id,"alice",2_000_000_000,"nonce",frozenset({"mcp"}))
    monkeypatch.setattr("apps.api.opencode_api._connector_capability",lambda *args,**kwargs:cap)
    monkeypatch.setattr("apps.api.opencode_api._scoped_connector_session",lambda *args,**kwargs:None)
    monkeypatch.setattr("forma_core.assets.service.inspect_step",lambda *args:VALIDATION)
    app=FastAPI(); app.include_router(router,prefix="/api")
    with TestClient(app) as http:
        yield http,cap,library


def call(http,operation,request):
    response=http.post("/api/opencode/mcp",json={"jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"forma.opencode.asset_"+operation,"arguments":{"request_json":json.dumps(request)}}})
    assert response.status_code==200,response.text
    return response.json()


def test_register_attach_replay_and_download_preserve_pinned_revision(client):
    http,cap,library=client
    reg=registration()
    result=call(http,"register",dict(registration=reg.model_dump(mode="json"),content_base64={"original":base64.b64encode(DATA).decode()}))
    asset=result["result"]["structuredContent"]["asset"]
    pin=dict(asset_id=asset["asset_id"],version=asset["version"],instance_id="S1",position_mm=[1,2,3])
    attached=call(http,"attach",pin)["result"]["structuredContent"]
    replay=call(http,"attach",pin)["result"]["structuredContent"]
    assert replay["revision_id"]==attached["revision_id"]
    first=database.get_latest_project_revision(cap.project_id,"alice")
    assert first.state.assembly_metadata["component_asset_refs"]["S1"]["position_mm"]==[1,2,3]
    assert any(a.kind=="component.cad" and a.checksum.endswith(reg.representations[0].sha256) for a in first.artifacts)
    # Refresh adds another model; the old project's reference and served bytes remain reproducible.
    newer=registration(DATA+b"new")
    library.register(newer,{"original":DATA+b"new"})
    response=http.get(f"/api/opencode/component-assets/{asset['asset_id']}/{asset['version']}/original")
    assert response.status_code==200 and response.content==DATA
    assert database.get_latest_project_revision(cap.project_id,"alice").revision_id==first.revision_id


def test_authorization_precedes_asset_parsing_and_download(client,monkeypatch):
    http,cap,library=client
    def denied(*a,**kw): raise HTTPException(403,"denied")
    monkeypatch.setattr("apps.api.opencode_api._connector_capability",denied)
    response=http.post("/api/opencode/mcp",json={"jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"forma.opencode.asset_register","arguments":{"request_json":"{bad"}}})
    assert response.status_code==403
    assert http.get("/api/opencode/component-assets/x/y/original").status_code==403


def test_json_boundary_rejects_spoofed_scope_and_preserves_typed_arguments(client):
    http,cap,library=client
    request={"identity":registration().identity.model_dump(),"owner_user_id":"victim"}
    result=call(http,"search",request)["result"]["structuredContent"]
    assert result["status"]=="invalid_request"
    parsed=McpJsonRpcRequest.model_validate({"jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"forma.opencode.asset_search","arguments":{"request_json":"{}"}}})
    assert isinstance(parsed.params.arguments,AssetToolArguments)


def test_stale_parent_revision_rejects_pin_write(client):
    http,cap,library=client
    from forma_core.workspaces.projects.state import ProjectRevisionDraft, ProjectStateError, ProjectStateService
    parent=database.get_latest_project_revision(cap.project_id,"alice")
    with pytest.raises(ProjectStateError,match="Project changed"):
        ProjectStateService(database._DATABASE_REPOSITORY).create_revision(
            ProjectRevisionDraft(state=parent.state),project_id=cap.project_id,owner_user_id="alice",
            source_job_id="stale",expected_parent_revision=parent.revision-1)
