"""Component library operations scoped to the authenticated OpenCode project."""
from __future__ import annotations

import base64
import json
from uuid import uuid4

from pydantic import ValidationError

from forma_core.assets.models import (AssetScope, AssetToolArguments, AttachRequest, InspectRequest,
                                      RegisterRequest, SearchRequest)
from forma_core.assets.repository import AssetRepository
from forma_core.assets.project import pin_artifacts
from forma_core.assets.service import ComponentAssetLibrary, AssetRecoveryRequired
from forma_core.persistence.project_artifacts import ProjectArtifactStorage
from forma_core.workspaces.projects.state import ProjectArtifact, ProjectRevisionDraft, ProjectStateService

REQUEST_MODELS = {"search": SearchRequest, "inspect": InspectRequest, "register": RegisterRequest,
                  "attach": AttachRequest, "revalidate": InspectRequest}


def library_tools():
    descriptions = {
        "search": "Search the private component library FIRST using exact manufacturer/part/revision/variants. A hit requires no source download or conversion. A miss hands off to CAD sourcing (#530); do not substitute another variant.",
        "inspect": "Inspect a pinned component asset version, including provenance, license, validation and representations.",
        "register": "Register uploaded, sourced or generated CAD and derived files. Supply base64 bytes keyed by representation name. STEP is inspected by OCCT; generated approximations must be labeled approximate. Ownership is assigned by the server.",
        "attach": "Attach a verified pinned component version to this project as a durable artifact reference with independent placement. Explicitly accept approximate models. This does not automatically merge geometry into the assembly; use the authenticated representation endpoint for CAD import.",
        "revalidate": "Explicitly recheck stored geometry. Missing/corrupt bytes require re-registering original bytes. Updated validation creates a new version; existing pins do not change.",
    }
    return [{"name": "forma.opencode.asset_" + operation, "description": description,
             "inputSchema": {"type": "object", "properties": {"request_json": {"type": "string",
                 "description": "JSON request, without fences, conforming to: " + json.dumps(model.model_json_schema(), separators=(",", ":"))}},
                 "required": ["request_json"], "additionalProperties": False}}
            for operation, model in REQUEST_MODELS.items() for description in [descriptions[operation]]]


def project_library(project_id: str, owner: str):
    from forma_core import database
    identity = database.get_project_identity(project_id)
    if not identity or identity.get("owner_user_id") != owner or identity.get("status", "active") != "active":
        raise PermissionError("The project is unavailable.")
    return ComponentAssetLibrary(AssetRepository(database.get_database_provider()), ProjectArtifactStorage(),
                                 AssetScope(owner_user_id=owner, workspace_id=identity.get("workspace_id") or ""))



def call_asset_tool(name: str, arguments: AssetToolArguments, capability, before_save):
    operation = name.removeprefix("forma.opencode.asset_")
    model = REQUEST_MODELS[operation]
    try:
        request = model.model_validate_json(arguments.request_json)
    except ValidationError:
        return {"status": "invalid_request", "message": "Correct request_json using the tool schema."}
    before_save()
    library = project_library(capability.project_id, capability.owner_user_id)
    try:
        if operation == "search":
            return library.search(request.identity, allow_approximate=request.allow_approximate)
        if operation == "inspect":
            return {"asset": library.inspect(request.asset_id, request.version).model_dump(mode="json")}
        if operation == "register":
            try:
                content = {name: base64.b64decode(data, validate=True) for name, data in request.content_base64.items()}
            except ValueError:
                return {"status": "invalid_request", "message": "Representation data must be valid base64."}
            before_save()
            return {"asset": library.register(request.registration, content).model_dump(mode="json")}
        if operation == "revalidate":
            before_save()
            return {"asset": library.revalidate(request.asset_id, request.version).model_dump(mode="json")}
        pin = library.pin(request)
        from forma_core import database
        service = ProjectStateService(database._DATABASE_REPOSITORY)
        parent = service.get_latest(capability.project_id, capability.owner_user_id)
        state = parent.state.model_copy(deep=True)
        metadata = dict(state.assembly_metadata or {})
        pins = dict(metadata.get("component_asset_refs") or {})
        if pins.get(request.instance_id) == pin:
            return {"status": "attached", "pin": pin, "revision_id": str(parent.revision_id), "reused": True}
        pins[request.instance_id] = pin
        metadata["component_asset_refs"] = pins
        state.assembly_metadata = metadata
        artifacts = [a for a in parent.artifacts if not (a.kind == "component.cad" and a.metadata.get("instance_id") == request.instance_id)]
        artifacts.extend(pin_artifacts(pin))
        draft = ProjectRevisionDraft(state=state, components=state.components, systems=parent.systems,
                                     artifacts=artifacts, assumptions=parent.assumptions)
        before_save()
        result = service.create_revision(draft, project_id=capability.project_id,
            owner_user_id=capability.owner_user_id, source_job_id="asset-attach-" + uuid4().hex,
            expected_parent_revision=parent.revision)
        return {"status": "attached", "pin": pin, "revision_id": str(result.revision.revision_id), "reused": True}
    except AssetRecoveryRequired as exc:
        return {"status": "recovery_required", "message": str(exc)}
    except ValueError as exc:
        return {"status": "invalid_asset", "message": str(exc)}
