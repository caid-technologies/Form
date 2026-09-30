"""Restricted MCP tools for one authenticated OpenCode project session."""

from __future__ import annotations

import json
import hashlib
import logging
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from apps.api.a2a import _persist_mcp_compile
from apps.api.auth import UserContext
from apps.api.opencode_images import ImageToolError, generate_project_image, image_metadata_for_agent, is_image_metadata
from forma_core.workspaces.projects.cad_generation import CadGenerationError, ensure_native_cad_model
from forma_core.debug import new_error_correlation_id
from forma_core.opencode.architecture import ArchitectureContinuityError, reconcile_architecture
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.opencode.models import (
    McpJsonRpcRequest,
    McpToolArguments,
    GenerateImageArguments,
    McpToolCallParams,
    ProjectToolResult,
    AuthoringFieldError,
    AuthoringToolError,
)
from forma_core.database import get_latest_project_revision, get_project_revision_by_source_job
from forma_core.validation import build_validation_summary, validate_circuit
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.outcomes import evaluate_design_outcome
from forma_core.utils import generate_mermaid_chart, generate_svg_schematic


logger = logging.getLogger(__name__)


def opencode_mcp_tools() -> list[dict[str, object]]:
    """Return only project authoring tools, never the broad Forma MCP registry."""
    # OpenCode's Vertex adapter drops $ref/$defs, leaving untyped parameters
    # that reject the entire request, even when it only needs the image tool.
    # HardwareIntermediateRepresentation has recursive and arbitrary JSON fields, so flattening its
    # schema would narrow the contract. Carry it as JSON text instead, retain
    # the full schema as authoring guidance, and validate after authorization.
    authoring_schema = {
        "type": "object",
        "properties": {
            "project_ir": {
                "type": "string",
                "description": (
                    "The complete HardwareIntermediateRepresentation object serialized as a JSON string, "
                    "without Markdown fences. The decoded object must follow "
                    "this JSON Schema: "
                    + json.dumps(HardwareIntermediateRepresentation.model_json_schema(), separators=(",", ":"))
                ),
            },
        },
        "required": ["project_ir"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "forma.opencode.create_project",
            "description": "Initialize a missing private project. If a project already exists, return it unchanged. Never use this to reset or resume a design.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "forma.opencode.read_project",
            "description": "Read the latest saved session project, revision, current validation, and design_outcome before reporting results.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "forma.opencode.update_project",
            "description": "Validate and persist a revision. Include system_architecture with stable IDs and updated interfaces for topology changes; omitted or null hierarchy preserves the saved tree. Follow this schema and correct field errors before retrying. A saved draft is not a produced design; report project_readiness and validation, not HTTP success.",
            "inputSchema": authoring_schema,
        },
        {
            "name": "forma.opencode.compile_project",
            "description": "Compile and persist Hardware Intermediate Representation. For solid CAD, author mechanical.cad_operations (exact mm box/cylinder add/cut operations; optional engraved axis labels). For two meshing gears, set mechanical.mechanism_benchmark.kind to spur_gear_pair, with driver_teeth/driven_teeth, module_mm, pressure_angle_deg, face_width_mm, bore_diameter_mm, backlash_mm and cycle_seconds as needed (defaults: 20/40 teeth, module 2 mm). This generates real separate gear meshes and synchronized OpenCAD motion; do not approximate gears as boxes or independently driven placements. CAD-only projects need no components or nets. This exports real STEP plus preview meshes and stores the STEP artifact; check cad_generation and cad_model before claiming availability. Correct schema errors and critical validation findings. Report the saved revision and project_readiness; draft/partial is not a completed design or physical verification.",
            "inputSchema": authoring_schema,
        },
        {
            "name": "forma.opencode.validate_project",
            "description": "Run deterministic Forma electrical validation for the session project.",
            "inputSchema": authoring_schema,
        },
        {
            "name": "forma.opencode.generate_image",
            "description": "Generate one concept image of the saved design using Forma's configured image provider and persist it in the project's preview. Use only when the user requests an image. Read the project first; preserve its geometry, part count, materials, and constraints. Do not redesign it or add electronics or other absent features. Reuse request_id for a retry; use a new ID for a new image. Images are not CAD or physical verification.",
            "inputSchema": GenerateImageArguments.model_json_schema(),
        },
    ]


async def handle_opencode_mcp_json_rpc(
    payload: McpJsonRpcRequest | list[McpJsonRpcRequest],
    capability: ConnectorCapability,
) -> dict[str, object] | list[dict[str, object]] | None:
    if isinstance(payload, list):
        responses = [await _handle_request(item, capability) for item in payload]
        return [response for response in responses if response is not None] or None
    return await _handle_request(payload, capability)


async def _handle_request(request: McpJsonRpcRequest, capability: ConnectorCapability) -> dict[str, object] | None:
    request_id = request.id
    if "id" not in request.model_fields_set:
        return None
    method = request.method
    params = request.params
    if method == "initialize":
        return _result(request_id, {"protocolVersion": "2025-06-18", "serverInfo": {"name": "forma-opencode", "version": "1.0.0"}, "capabilities": {"tools": {}}})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": opencode_mcp_tools()})
    if method != "tools/call":
        return _error(request_id, -32601, "The requested MCP method was not found.", "mcp_method_not_found")
    assert isinstance(params, McpToolCallParams)
    tool_name = params.name
    arguments = params.arguments
    if not tool_name:
        return _error(request_id, -32602, "Request parameters are invalid.", "mcp_invalid_params")
    try:
        result = await _call_tool(tool_name, arguments, capability)
    except PermissionError:
        return _error(request_id, -32003, "The OpenCode tool is outside the session scope.", "authorization_required")
    except ImageToolError as exc:
        result = {"code": exc.code, "message": exc.public_message}
        return _result(request_id, {"isError": True, "content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})
    except ValidationError as exc:
        # Never echo inputs, validator messages, context, or arbitrary mapping keys.
        schema = HardwareIntermediateRepresentation.model_json_schema()
        fields = {"project_ir", *schema.get("properties", {})}
        for definition in schema.get("$defs", {}).values():
            fields.update(definition.get("properties", {}))
        errors = tuple(AuthoringFieldError(path=("project_ir", *[part if isinstance(part, int) or part in fields else "<key>" for part in error["loc"]]), type=error["type"])
                       for error in exc.errors(include_input=False, include_context=False, include_url=False))
        result = AuthoringToolError(errors=errors).model_dump(mode="json")
        return _result(request_id, {"isError": True, "content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})
    except ArchitectureContinuityError as exc:
        result = {"code": "invalid_system_architecture", "message": str(exc)}
        return _result(request_id, {"isError": True, "content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})
    except CadGenerationError:
        result = {"code": "cad_generation_failed", "message": "STEP generation or artifact storage failed. The previous saved project is unchanged. Retry the CAD compile after checking the backend CAD runtime and storage; do not report STEP files as delivered."}
        return _result(request_id, {"isError": True, "content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})
    except (TypeError, ValueError, KeyError):
        return _error(request_id, -32602, "The project tool parameters are invalid.", "mcp_invalid_params")
    except Exception:
        logger.warning("Restricted OpenCode MCP tool failed")
        return _error(request_id, -32000, "The OpenCode project tool could not complete.", "mcp_tool_failed")
    return _result(request_id, {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})


async def _call_tool(name: str, arguments: McpToolArguments | GenerateImageArguments, capability: ConnectorCapability) -> dict[str, object]:
    allowed = {tool["name"] for tool in opencode_mcp_tools()}
    if name not in allowed:
        raise PermissionError("The requested tool is not part of the project-only surface.")
    if name == "forma.opencode.generate_image":
        if not isinstance(arguments, GenerateImageArguments):
            raise ValueError("Image arguments are required.")
        return await run_in_threadpool(generate_project_image, arguments, capability, before_save=lambda: _assert_session_active(capability))
    assert isinstance(arguments, McpToolArguments)
    project_id = capability.project_id
    owner_user_id = capability.owner_user_id
    user_context = UserContext(
        provider="opencode-connector",
        subject=owner_user_id,
        owner_user_id=owner_user_id,
        is_authenticated=True,
        is_admin=False,
    )
    if name == "forma.opencode.create_project":
        from forma_core.workspaces.projects.state import ProjectStateError
        try:
            existing = await run_in_threadpool(get_latest_project_revision, project_id, owner_user_id)
        except ProjectStateError as exc:
            if exc.code != "project_revision_not_found":
                raise
            existing = None
        if existing is not None:
            return _tool_result(existing.state, project_id, _revision_identifier(existing))
        project = HardwareIntermediateRepresentation.model_validate({"components": [], "nets": []})
        result = await run_in_threadpool(_compile, project, project_id, user_context, capability)
        return ProjectToolResult.model_validate(result).model_dump(mode="json")
    if name == "forma.opencode.read_project":
        revision = get_latest_project_revision(project_id, owner_user_id)
        if revision is None:
            raise ValueError("The session project has not been created.")
        project = revision.state
        return _tool_result(project, project_id, _revision_identifier(revision))
    try:
        # Accept object arguments from older clients and existing sessions too.
        project = (
            HardwareIntermediateRepresentation.model_validate_json(arguments.project_ir)
            if isinstance(arguments.project_ir, str)
            else HardwareIntermediateRepresentation.model_validate(arguments.project_ir)
        )
    except TypeError as exc:
        # Legacy normalization can raise TypeError before Pydantic wraps it.
        raise ValidationError.from_exception_data("HardwareIntermediateRepresentation", [{
            "type": "model_type", "loc": (), "input": None,
            "ctx": {"class_name": "HardwareIntermediateRepresentation"},
        }]) from exc
    if name == "forma.opencode.validate_project":
        return _validation_result(project)
    if name in {"forma.opencode.compile_project", "forma.opencode.update_project"}:
        result = await run_in_threadpool(_compile, project, project_id, user_context, capability)
        return ProjectToolResult.model_validate(result).model_dump(mode="json")
    raise PermissionError("The requested tool is not part of the project-only surface.")


def _assert_session_active(capability: ConnectorCapability) -> None:
    # Resolve at call time to avoid the gateway/router import cycle.
    from fastapi import HTTPException
    from apps.api.opencode_api import _scoped_connector_session
    try:
        _scoped_connector_session(capability)
    except HTTPException as exc:
        raise PermissionError("The authoring session is no longer active.") from exc


def _compile(project: HardwareIntermediateRepresentation, project_id: str, user_context: UserContext,
             capability: ConnectorCapability | None = None) -> dict[str, object]:
    from forma_core.workspaces.projects.state import ProjectStateError
    # Images are authored by the server tool; a subsequent IR edit cannot erase
    # them, replace their provenance or make the agent echo their base64 payloads.
    metadata = {key: value for key, value in (project.assembly_metadata or {}).items() if not is_image_metadata(key)}
    try:
        previous = get_latest_project_revision(project_id, user_context.owner_user_id or "")
    except ProjectStateError as exc:
        if exc.code != "project_revision_not_found":
            raise
        previous = None
    reconcile_architecture(project, previous.state if previous else None)
    if previous is not None and isinstance(previous.state.assembly_metadata, dict):
        metadata.update({key: value for key, value in previous.state.assembly_metadata.items() if is_image_metadata(key)})
    metadata.update({"project_id": project_id, "authoring_agent": "opencode"})
    project.assembly_metadata = metadata
    issues = validate_circuit(project.components, project.nets, project.requirements)
    project.validation = build_validation_summary(issues)
    project.is_valid = not project.validation.critical
    ensure_native_cad_model(project, project_id=project_id,
                            required=bool(project.mechanical and project.mechanical.cad_operations),
                            authoring_agent="opencode", workflow="default")
    source_job_id = "opencode-" + hashlib.sha256(
        json.dumps(project.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    existing = get_project_revision_by_source_job(project_id, user_context.owner_user_id or "", source_job_id)
    if existing is not None:
        return _tool_result(existing.state, project_id, _revision_identifier(existing))
    if capability is not None:
        _assert_session_active(capability)
    _persist_mcp_compile(
        project,
        {"project_id": project_id, "visibility": "private", "authoring_agent": "opencode", "source_job_id": source_job_id},
        user_context,
    )
    revision = get_latest_project_revision(project_id, user_context.owner_user_id or "")
    return _tool_result(project, project_id, _revision_identifier(revision))


def _revision_identifier(revision: object | None) -> str | None:
    if revision is None:
        return None
    value = getattr(revision, "revision_id", None) or getattr(revision, "id", None)
    return str(value) if value else None


def _tool_result(project: HardwareIntermediateRepresentation, project_id: str, revision_id: str | None) -> dict[str, object]:
    public_project = project.model_copy(deep=True)
    public_project.assembly_metadata = image_metadata_for_agent(dict(project.assembly_metadata or {}))
    return {
        "project_id": project_id,
        "revision_id": revision_id,
        "design_outcome": evaluate_design_outcome(project).model_dump(mode="json"),
        "project_ir": public_project.model_dump(mode="json"),
        "validation": _validation_result(project),
        "mermaid_code": generate_mermaid_chart(project),
        "svg_schematic": generate_svg_schematic(project),
    }


def _validation_result(project: HardwareIntermediateRepresentation) -> dict[str, object]:
    issues = validate_circuit(project.components, project.nets, project.requirements)
    return {"is_valid": not any(issue.severity.upper() == "CRITICAL" for issue in issues), "issues": [issue.model_dump(mode="json") for issue in issues]}


def _result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str, error_code: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message, "data": {"code": error_code, "correlation_id": new_error_correlation_id()}}}
