"""Hosted OpenCode gateway and connector protocol endpoints."""

from __future__ import annotations

import hmac
import hashlib
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status

from apps.api.auth import UserContext, require_opencode_authoring_access
from apps.api.opencode_mcp import handle_opencode_mcp_json_rpc
from forma_core.config import config
from forma_core.debug import new_error_correlation_id
from forma_core.opencode.capabilities import CapabilityError, ConnectorCapability, issue_capability, verify_capability
from forma_core.opencode.models import (
    CommandResponse,
    ConnectorCapabilityResponse,
    ConnectorCapabilityRequest,
    ConnectorCommand,
    ConnectorCompletion,
    ConnectorEventInput,
    ConnectorHeartbeat,
    ConnectorLeaseResponse,
    ConnectorSession,
    ConnectorSessionPage,
    CreateSessionRequest,
    EventPage,
    OpenCodeCommandStatus,
    OpenCodeEventKind,
    OpenCodeOperation,
    OpenCodeSessionStatus,
    PublicEvent,
    ProjectHistoryResponse,
    SessionResponse,
    SubmitCommandRequest,
    McpJsonRpcRequest,
    ValidationSummary,
)
from forma_core.opencode.architecture import architecture_turn_context
from forma_core.workspaces.projects.state import ProjectStateError
from forma_core.opencode.public_events import project_public_event
from forma_core.opencode.store import CommandConflictError, OpenCodeStore, StoredCommand, StoredSession, SessionClosedError, LeaseError
from forma_core.database import get_project_identity, get_latest_project_revision
from forma_core.workspaces.projects.outcomes import evaluate_design_outcome


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opencode", tags=["opencode"])
OPENCODE_STORE = OpenCodeStore()
OPENCODE_SESSION_DISCOVERY_IDLE_SECONDS = 15 * 60


@router.get("/projects/{project_id}/history", response_model=ProjectHistoryResponse)
def get_opencode_project_history(
    project_id: UUID,
    response: Response,
    user: UserContext = Depends(require_opencode_authoring_access),
) -> ProjectHistoryResponse:
    owner = _owner(user)
    _require_owned_project(str(project_id), owner)
    response.headers["Cache-Control"] = "private, no-store"
    return ProjectHistoryResponse(
        project_id=project_id, messages=OPENCODE_STORE.project_history(str(project_id), owner),
    )


@router.get("/projects/{project_id}/cad/{sha256}")
def download_opencode_cad(
    project_id: UUID,
    sha256: str,
    user: UserContext = Depends(require_opencode_authoring_access),
) -> Response:
    from forma_core.persistence.project_artifacts import ProjectArtifactStorage, ProjectArtifactStorageError
    from forma_core.workspaces.projects.state import ProjectStateError
    owner = _owner(user)
    _require_owned_project(str(project_id), owner)
    try:
        revision = get_latest_project_revision(str(project_id), owner)
    except ProjectStateError:
        raise _http_error(404, "cad_not_found", "The project has no saved STEP artifact.")
    cad = revision.state.cad_model if revision else None
    if not isinstance(cad, dict) or cad.get("stored_sha256") != sha256 or len(sha256) != 64:
        raise _http_error(404, "cad_not_found", "The requested STEP artifact is not attached to this project.")
    try:
        stored = ProjectArtifactStorage().get(str(project_id), sha256, "model/step")
    except FileNotFoundError:
        raise _http_error(404, "cad_not_found", "The STEP artifact is missing.")
    except (ProjectArtifactStorageError, OSError, ValueError):
        raise _http_error(503, "cad_storage_unavailable", "The STEP artifact could not be retrieved.")
    content = stored.content or b""
    if hashlib.sha256(content).hexdigest() != sha256:
        raise _http_error(503, "cad_integrity_failed", "The STEP artifact failed its integrity check.")
    return Response(content, media_type="model/step", headers={
        "Content-Disposition": 'attachment; filename="assembly.step"', "Cache-Control": "private, no-store",
    })


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_opencode_session(
    request: CreateSessionRequest,
    user: UserContext = Depends(require_opencode_authoring_access),
) -> SessionResponse:
    owner = _owner(user)
    project_id = request.project_id or UUID(str(uuid4()))
    if request.project_id:
        _require_owned_project(str(project_id), owner)
    session = OPENCODE_STORE.create_session(
        session_id=f"ocs_{uuid4().hex}",
        connector_id=request.connector_id.strip(),
        owner_user_id=owner,
        project_id=str(project_id),
    )
    return _session_response(session)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_opencode_session(
    session_id: str,
    user: UserContext = Depends(require_opencode_authoring_access),
) -> SessionResponse:
    session = _owned_session(session_id, _owner(user))
    session = OPENCODE_STORE.reconcile_session(session.session_id) or session
    return _session_response(session)


@router.post("/sessions/{session_id}/commands", response_model=CommandResponse, status_code=status.HTTP_201_CREATED)
def submit_opencode_command(
    session_id: str,
    request: SubmitCommandRequest,
    user: UserContext = Depends(require_opencode_authoring_access),
) -> CommandResponse:
    session = _owned_session(session_id, _owner(user))
    session = OPENCODE_STORE.reconcile_session(session.session_id) or session
    if session.status != OpenCodeSessionStatus.ACTIVE:
        raise _http_error(409, "opencode_session_closed", "The OpenCode session is no longer active.")
    try:
        command = OPENCODE_STORE.create_command(
            command_id=f"occ_{uuid4().hex}",
            session=session,
            operation=OpenCodeOperation.PROJECT_MESSAGE,
            idempotency_key=request.idempotency_key,
            message=request.message,
            model=request.model,
        )
    except SessionClosedError:
        raise _http_error(409, "opencode_session_closed", "The OpenCode session is no longer active.")
    except CommandConflictError:
        raise _http_error(409, "opencode_command_conflict", "This request key was already used with a different message or model.")
    _store_event(
        session,
        ConnectorEventInput(
            event_id=f"queued_{command.command_id}",
            kind=OpenCodeEventKind.QUEUED.value,
            status=OpenCodeCommandStatus.QUEUED,
        ),
    )
    return _command_response(command)


@router.get("/sessions/{session_id}/events", response_model=EventPage)
def list_opencode_events(
    session_id: str,
    cursor: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_opencode_authoring_access),
) -> EventPage:
    session = _owned_session(session_id, _owner(user))
    _record_connector_unavailable_if_stale(session)
    events = tuple(OPENCODE_STORE.list_events(session.session_id, cursor, limit))
    for event in events:
        if event.kind == OpenCodeEventKind.FAILED:
            logger.warning("opencode_terminal_failure event_id=%s code=%s", event.event_id, event.error.code if event.error else "command_failed", extra={
                "event": "opencode_terminal_failure", "event_id": event.event_id,
                "error_code": event.error.code if event.error else "command_failed",
            })
    next_cursor = events[-1].sequence if events else cursor
    return EventPage(events=events, next_cursor=next_cursor)


@router.post("/sessions/{session_id}/cancel", response_model=SessionResponse)
def cancel_opencode_session(
    session_id: str,
    user: UserContext = Depends(require_opencode_authoring_access),
) -> SessionResponse:
    session = _owned_session(session_id, _owner(user))
    session = OPENCODE_STORE.reconcile_session(session.session_id, cancel=True) or session
    return _session_response(session)


@router.post("/connector/capability")
def issue_opencode_connector_capability(
    request: ConnectorCapabilityRequest,
    bootstrap: str | None = Header(default=None, alias="X-Forma-OpenCode-Bootstrap"),
) -> ConnectorCapabilityResponse:
    _require_connector_bootstrap(bootstrap)
    session = OPENCODE_STORE.reconcile_session(request.session_id)
    if session is None or session.connector_id != request.connector_id or session.status != OpenCodeSessionStatus.ACTIVE:
        raise _http_error(404, "opencode_session_not_found", "The OpenCode session was not found.")
    token = issue_capability(
        connector_id=session.connector_id,
        session_id=session.session_id,
        project_id=session.project_id,
        owner_user_id=session.owner_user_id,
        scopes=frozenset({"poll", "heartbeat", "events", "complete", "mcp"}),
    )
    return ConnectorCapabilityResponse(capability=token)


@router.get("/connector/sessions", response_model=ConnectorSessionPage)
def list_opencode_connector_sessions(
    connector_id: str = Query(..., min_length=1, max_length=120),
    bootstrap: str | None = Header(default=None, alias="X-Forma-OpenCode-Bootstrap"),
) -> ConnectorSessionPage:
    _require_connector_bootstrap(bootstrap)
    sessions = OPENCODE_STORE.list_connector_sessions(
        connector_id.strip(), idle_after_seconds=OPENCODE_SESSION_DISCOVERY_IDLE_SECONDS,
    )
    return ConnectorSessionPage(
        sessions=tuple(
            ConnectorSession(session_id=session.session_id, connector_id=session.connector_id, project_id=UUID(session.project_id))
            for candidate in sessions
            if (session := OPENCODE_STORE.reconcile_session(candidate.session_id)) is not None
            and session.status == OpenCodeSessionStatus.ACTIVE
        )
    )


@router.get("/connector/commands", response_model=ConnectorCommand | None)
def poll_opencode_command(
    session_id: str,
    capability: str | None = Header(default=None, alias="X-Forma-OpenCode-Capability"),
    authorization: str | None = Header(default=None),
    lease_seconds: int = Query(60, ge=15, le=300),
) -> ConnectorCommand | Response:
    cap = _connector_capability(capability or _bearer(authorization), session_id=session_id, scope="poll")
    session = _scoped_connector_session(cap, require_active=False)
    configured_lease_seconds = lease_seconds if isinstance(lease_seconds, int) else 60
    if session.status != OpenCodeSessionStatus.ACTIVE:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    OPENCODE_STORE.touch_session(session.session_id)
    command = OPENCODE_STORE.claim_next(connector_id=session.connector_id, session_id=session.session_id, lease_seconds=configured_lease_seconds)
    if command is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if command.message is not None:
        try:
            revision = get_latest_project_revision(session.project_id, session.owner_user_id)
        except ProjectStateError as exc:
            if exc.code != "project_revision_not_found":
                raise
            revision = None
        command = command.model_copy(update={"message": architecture_turn_context(
            command.message,
            revision.state if revision else None,
            str(revision.revision_id) if revision else None,
        )})
    return command


@router.post("/connector/sessions/{session_id}/heartbeat")
def heartbeat_opencode_session(
    session_id: str,
    capability: str | None = Header(default=None, alias="X-Forma-OpenCode-Capability"),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    cap = _connector_capability(capability or _bearer(authorization), session_id=session_id, scope="heartbeat")
    session = _scoped_connector_session(cap)
    OPENCODE_STORE.touch_session(session.session_id)
    return {"status": "ok"}


@router.post("/connector/commands/{command_id}/heartbeat", response_model=ConnectorLeaseResponse)
def heartbeat_opencode_command(
    command_id: str,
    request: ConnectorHeartbeat,
    capability: str | None = Header(default=None, alias="X-Forma-OpenCode-Capability"),
    authorization: str | None = Header(default=None),
) -> ConnectorLeaseResponse:
    command = _connector_command(command_id, capability or _bearer(authorization), "heartbeat")
    try:
        updated = OPENCODE_STORE.heartbeat(command, request.lease_token)
    except PermissionError as exc:
        raise _http_error(403, exc.code if isinstance(exc, LeaseError) else "opencode_lease_invalid",
                          "The command lease is no longer valid.") from exc
    return ConnectorLeaseResponse(
        command_id=updated.command_id,
        status=updated.status,
        lease_expires_at=_datetime(updated.lease_expires_at or ""),
    )


@router.post("/connector/commands/{command_id}/events", response_model=PublicEvent)
def ingest_opencode_event(
    command_id: str,
    event: ConnectorEventInput,
    capability: str | None = Header(default=None, alias="X-Forma-OpenCode-Capability"),
    authorization: str | None = Header(default=None),
    lease_token: str | None = Header(default=None, alias="X-Forma-OpenCode-Lease"),
) -> PublicEvent:
    resolved_capability = capability or _bearer(authorization)
    command = _connector_command(command_id, resolved_capability, "events")
    if not lease_token:
        raise _http_error(401, "opencode_lease_required", "The command lease is required for event delivery.")
    if event.project_id is not None and str(event.project_id) != command.project_id:
        raise _http_error(403, "opencode_scope_mismatch", "The event is outside the command project scope.")
    # A lost acknowledgement may be replayed after completion revoked the lease.
    # Return the durable original, never the new payload, within the verified scope.
    existing = OPENCODE_STORE.get_event(command.session_id, event.event_id)
    if existing is not None:
        return existing
    try:
        OPENCODE_STORE.validate_lease(command, lease_token)
    except PermissionError as exc:
        raise _http_error(403, exc.code if isinstance(exc, LeaseError) else "opencode_lease_invalid",
                          "The command lease is no longer valid.") from exc
    session = _scoped_connector_session(verify_capability(resolved_capability or "", session_id=command.session_id, project_id=command.project_id, owner_user_id=command.owner_user_id, scope="events"))
    return _store_event(session, event)


@router.post("/connector/commands/{command_id}/complete", response_model=CommandResponse)
def complete_opencode_command(
    command_id: str,
    request: ConnectorCompletion,
    capability: str | None = Header(default=None, alias="X-Forma-OpenCode-Capability"),
    authorization: str | None = Header(default=None),
) -> CommandResponse:
    command = _connector_command(command_id, capability or _bearer(authorization), "complete")
    try:
        updated = OPENCODE_STORE.complete(command, request.lease_token, request.status)
    except PermissionError as exc:
        raise _http_error(403, exc.code if isinstance(exc, LeaseError) else "opencode_lease_invalid",
                          "The command lease is no longer valid.") from exc
    terminal_kind = {
        OpenCodeCommandStatus.SUCCEEDED: OpenCodeEventKind.COMPLETED.value,
        OpenCodeCommandStatus.FAILED: OpenCodeEventKind.FAILED.value,
        OpenCodeCommandStatus.CANCELLED: OpenCodeEventKind.CANCELLED.value,
    }[request.status]
    _store_event(
        _scoped_connector_session(_connector_capability(capability or _bearer(authorization), scope="complete")),
        ConnectorEventInput(
            event_id=f"{command.command_id}:terminal",
            kind=terminal_kind,
            status=request.status,
            error_code=request.error_code if request.status == OpenCodeCommandStatus.FAILED else None,
        ),
    )
    return _command_response(updated)


@router.post("/mcp", response_model=None)
async def opencode_mcp_endpoint(
    payload: McpJsonRpcRequest | list[McpJsonRpcRequest] = Body(...),
    capability: str | None = Header(default=None, alias="X-Forma-OpenCode-Capability"),
    authorization: str | None = Header(default=None),
) -> dict[str, object] | list[dict[str, object]] | Response:
    cap = _connector_capability(capability or _bearer(authorization), scope="mcp")
    _scoped_connector_session(cap)
    response = await handle_opencode_mcp_json_rpc(payload, cap)
    return Response(status_code=status.HTTP_202_ACCEPTED) if response is None else response


def _owner(user: UserContext) -> str:
    owner = str(user.owner_user_id or "").strip()
    if not owner:
        raise _http_error(401, "authentication_required", "A signed-in owner is required.")
    return owner


def _require_owned_project(project_id: str, owner_user_id: str) -> None:
    identity = get_project_identity(project_id)
    if identity is None or str(identity.get("owner_user_id") or "") != owner_user_id or identity.get("status", "active") != "active":
        raise _http_error(404, "opencode_project_not_found", "The project is not available to this account.")


def _owned_session(session_id: str, owner_user_id: str) -> StoredSession:
    session = OPENCODE_STORE.get_session(session_id)
    if session is None or session.owner_user_id != owner_user_id:
        raise _http_error(404, "opencode_session_not_found", "The OpenCode session was not found.")
    return session


def _session_response(session: StoredSession) -> SessionResponse:
    return SessionResponse(
        session_id=session.session_id, connector_id=session.connector_id, project_id=session.project_id,
        owner_user_id=session.owner_user_id, status=session.status, created_at=_datetime(session.created_at), updated_at=_datetime(session.updated_at),
    )


def _command_response(command: StoredCommand) -> CommandResponse:
    return CommandResponse(
        command_id=command.command_id, session_id=command.session_id, project_id=command.project_id,
        operation=command.operation, status=command.status, attempt_count=command.attempt_count,
        model=command.model,
        created_at=_datetime(command.created_at), updated_at=_datetime(command.updated_at),
    )


def _datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _store_event(session: StoredSession, event: ConnectorEventInput) -> PublicEvent:
    public_event = project_public_event(
        event,
        sequence=OPENCODE_STORE.next_event_sequence(session.session_id),
        session_id=session.session_id,
        project_id=UUID(session.project_id),
    )
    if public_event.kind == OpenCodeEventKind.COMPLETED:
        # The gateway, not the worker's HTTP result, owns saved-output evidence.
        revision = get_latest_project_revision(session.project_id, session.owner_user_id)
        public_event.revision_id = str(revision.revision_id) if revision else None
        public_event.artifact_ids = tuple(item.artifact_id for item in revision.artifacts) if revision else ()
        public_event.validation = None
        if revision:
            outcome = evaluate_design_outcome(revision.state)
            public_event.design_outcome = outcome
            public_event.validation = ValidationSummary(
                is_valid=outcome.is_valid, critical_count=outcome.critical_count,
                warning_count=outcome.warning_count,
            )
    return OPENCODE_STORE.add_event(public_event)


def _record_connector_unavailable_if_stale(session: StoredSession) -> None:
    OPENCODE_STORE.reconcile_session(session.session_id)


def _connector_capability(token: str | None, *, session_id: str | None = None, scope: str) -> ConnectorCapability:
    if not token:
        raise _http_error(401, "opencode_capability_required", "A connector capability is required.")
    try:
        return verify_capability(token, session_id=session_id, scope=scope)
    except CapabilityError as exc:
        raise _http_error(403, exc.code, "The connector capability was rejected.") from exc


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _scoped_connector_session(capability: ConnectorCapability, *, require_active: bool = True) -> StoredSession:
    session = OPENCODE_STORE.get_session(capability.session_id)
    if session is None or session.connector_id != capability.connector_id or session.project_id != capability.project_id or session.owner_user_id != capability.owner_user_id:
        raise _http_error(403, "opencode_scope_mismatch", "The connector capability does not match the session.")
    session = OPENCODE_STORE.reconcile_session(session.session_id) or session
    if require_active and session.status != OpenCodeSessionStatus.ACTIVE:
        raise _http_error(409, "opencode_session_closed", "The OpenCode session is no longer active.")
    return session


def _connector_command(command_id: str, token: str | None, scope: str) -> StoredCommand:
    cap = _connector_capability(token, scope=scope)
    _scoped_connector_session(cap)
    command = OPENCODE_STORE.get_command(command_id)
    if command is None or command.session_id != cap.session_id or command.connector_id != cap.connector_id or command.project_id != cap.project_id or command.owner_user_id != cap.owner_user_id:
        raise _http_error(403, "opencode_scope_mismatch", "The connector capability does not match the command.")
    return command


def _require_connector_bootstrap(value: str | None) -> None:
    configured = (config.get("FORMA_OPENCODE_CONNECTOR_BOOTSTRAP_TOKEN") or "").strip()
    if len(configured) < 32 or not value or not hmac.compare_digest(value, configured):
        raise _http_error(401, "opencode_connector_auth_required", "The connector bootstrap credential is invalid.")


def _http_error(code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"code": error_code, "message": message, "correlation_id": new_error_correlation_id()},
    )
