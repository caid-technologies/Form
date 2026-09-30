"""Explicit protocol models for the hosted OpenCode project agent."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationInfo, field_validator

from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ValidationIssue
from forma_core.workspaces.projects.outcomes import DesignOutcome


# OpenCode model IDs can themselves contain slashes (e.g. OpenRouter).
OpenCodeModel = Annotated[str, Field(max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_./:-]*$")]


class OpenCodeSessionStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class OpenCodeCommandStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpenCodeOperation(str, Enum):
    PROJECT_MESSAGE = "project_message"
    COMPILE_PROJECT = "compile_project"
    VALIDATE_PROJECT = "validate_project"


class OpenCodeEventKind(str, Enum):
    ASSISTANT_MESSAGE = "assistant_message"
    QUEUED = "queued"
    WORKING = "working"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONNECTOR_UNAVAILABLE = "connector_unavailable"
    PROGRESS = "progress"


class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_valid: bool
    critical_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)


class ProjectValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_valid: bool
    issues: tuple[ValidationIssue, ...] = ()


_DIAGNOSTIC_CODES = frozenset({
    'EACCES',
    'EBUSY',
    'ECONNREFUSED',
    'ECONNRESET',
    'EINVAL',
    'EIO',
    'EMFILE',
    'ENFILE',
    'ENOENT',
    'ENOSPC',
    'EPERM',
    'EROFS',
    'ETIMEDOUT',
    'OPENCODE_COMMAND_TIMEOUT',
    'OPENCODE_EVENT_INVALID',
    'OPENCODE_EVENT_STREAM_CLOSED',
    'OPENCODE_HTTP_ERROR',
    'OPENCODE_MESSAGE_INVALID',
    'OPENCODE_MODEL_UNAVAILABLE',
    'OPENCODE_PROMPT_FAILED',
    'OPENCODE_PROVIDER_AUTH',
    'OPENCODE_PROVIDER_TIMEOUT',
    'OPENCODE_RATE_LIMIT',
    'OPENCODE_REQUEST_TIMEOUT',
    'UND_ERR_BODY_TIMEOUT',
    'UND_ERR_CONNECT_TIMEOUT',
    'UND_ERR_HEADERS_TIMEOUT',
    'UND_ERR_SOCKET',
    'aggregate_error',
    'cancelled',
    'cancelled_lease_expired',
    'command_driver_not_configured',
    'command_failed',
    'opencode_capability_expired',
    'opencode_capability_invalid',
    'opencode_capability_required',
    'opencode_command_closed',
    'opencode_connector_auth_required',
    'opencode_lease_expired',
    'opencode_lease_invalid',
    'opencode_lease_required',
    'opencode_request_rejected',
    'opencode_scope_mismatch',
    'opencode_session_closed',
    'opencode_session_response_invalid',
    'opencode_transport_unavailable',
    'syntax_error',
    'unknown',
})


class SanitizedFailureDiagnostic(BaseModel):
    """Bounded mini-PC failure metadata. Raw provider/OpenCode content is forbidden."""

    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "provider_authentication",
        "model_unavailable",
        "rate_limit",
        "provider_timeout",
        "opencode_request_stream_failure",
        "connector_cloud_connectivity",
        "cancellation",
        "unknown",
    ]
    code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    phase: Literal["preparing", "authoring", "compiling", "validating", "finalizing"]
    retryable: bool
    provider: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    model: str | None = Field(default=None, max_length=160, pattern=r"^[A-Za-z0-9_./:-]+$")

    @field_validator("code")
    @classmethod
    def fixed_diagnostic_code(cls, value: str) -> str:
        # Identifier grammar alone would allow a token or exception text encoded
        # without spaces. Only known codes may cross the public boundary.
        if value in _DIAGNOSTIC_CODES or re.fullmatch(r"opencode_session_http_[45]\d{2}", value):
            return value
        return "unknown"


class PublicError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)
    correlation_id: str = Field(min_length=1, max_length=100)


class PublicEvent(BaseModel):
    """The only event shape that may cross the browser gateway."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(ge=1)
    session_id: str
    project_id: UUID
    kind: OpenCodeEventKind
    status: OpenCodeCommandStatus | None = None
    message: str | None = Field(default=None, max_length=2000)
    revision_id: str | None = None
    validation: ValidationSummary | None = None
    artifact_ids: tuple[str, ...] = ()
    design_outcome: DesignOutcome | None = None
    error: PublicError | None = None
    diagnostic: SanitizedFailureDiagnostic | None = None
    command_id: str | None = None
    created_at: datetime


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=120)
    project_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class SubmitCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=12_000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    model: OpenCodeModel | None = None

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    connector_id: str
    project_id: UUID
    owner_user_id: str
    status: OpenCodeSessionStatus
    created_at: datetime
    updated_at: datetime


class ProjectHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: Literal["user", "assistant"]
    content: str
    status: Literal["idle", "success", "error", "cancelled"]
    timestamp: str
    projectId: str
    revisionId: str | None = None


class ProjectHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    messages: tuple[ProjectHistoryMessage, ...]


class OperatorFailureDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    connector_id: str
    session_id: str
    command_id: str
    correlation_id: str
    category: str
    code: str
    phase: str
    retryable: bool
    provider: str | None = None
    model: str | None = None


class SessionDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    session_id: str
    project_id: UUID
    status: OpenCodeSessionStatus
    last_successful_poll_at: datetime | None = None
    latest_failure: OperatorFailureDiagnostic | None = None


class CommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    model: OpenCodeModel | None = None
    session_id: str
    project_id: UUID
    operation: OpenCodeOperation
    status: OpenCodeCommandStatus
    attempt_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class CommandContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=12_000)
    status: OpenCodeCommandStatus


class ConnectorCommand(BaseModel):
    """A leased command returned to the trusted mini-PC connector."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    model: OpenCodeModel | None = None
    session_id: str
    connector_id: str
    owner_user_id: str
    project_id: UUID
    operation: OpenCodeOperation
    message: str | None = None
    conversation_context: tuple[CommandContext, ...] = Field(default=(), max_length=16)
    attempt_count: int = Field(ge=1)
    lease_expires_at: datetime
    lease_token: str = Field(min_length=1)


class ConnectorSession(BaseModel):
    """A session the authenticated connector is allowed to poll."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    connector_id: str
    project_id: UUID


class ConnectorSessionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: tuple[ConnectorSession, ...]


class ConnectorCapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1)


class ConnectorEventInput(BaseModel):
    """Allowlisted connector fields; all internal OpenCode fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=80)
    message: str | None = Field(default=None, max_length=10_000)
    status: OpenCodeCommandStatus | None = None
    project_id: UUID | None = None
    revision_id: str | None = Field(default=None, max_length=200)
    validation: ValidationSummary | None = None
    artifact_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=300)
    correlation_id: str | None = Field(default=None, max_length=100)
    diagnostic: SanitizedFailureDiagnostic | None = None


class ConnectorHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: str = Field(min_length=1)


class ConnectorLeaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    status: OpenCodeCommandStatus
    lease_expires_at: datetime


class ConnectorCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_token: str = Field(min_length=1)
    status: Literal[OpenCodeCommandStatus.SUCCEEDED, OpenCodeCommandStatus.FAILED, OpenCodeCommandStatus.CANCELLED]
    error_code: str | None = Field(default=None, max_length=80)
    diagnostic: SanitizedFailureDiagnostic | None = None


class McpToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Raw JSON is confined to the MCP boundary; the authorized handler validates IR.
    project_ir: JsonValue = None


class GenerateImageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class AuthoringFieldError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: tuple[str | int, ...]
    type: str


class AuthoringToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["hardware_ir_invalid"] = "hardware_ir_invalid"
    message: str = "Correct these fields using the tool inputSchema, then retry."
    errors: tuple[AuthoringFieldError, ...]


class McpRequestParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    meta: dict[str, JsonValue] | None = Field(default=None, alias="_meta")


class McpIcon(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    src: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    sizes: list[str] | None = None
    theme: Literal["light", "dark"] | None = None


class McpClientInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    version: str
    title: str | None = None
    description: str | None = None
    website_url: str | None = Field(default=None, alias="websiteUrl")
    icons: list[McpIcon] | None = None


class McpRootsCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    list_changed: bool | None = Field(default=None, alias="listChanged")


class McpClientCapabilities(BaseModel):
    """Known client capabilities plus the protocol's open extension boundary."""

    model_config = ConfigDict(extra="allow", strict=True)

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)
    roots: McpRootsCapability | None = None
    sampling: dict[str, JsonValue] | None = None
    elicitation: dict[str, JsonValue] | None = None
    experimental: dict[str, dict[str, JsonValue]] | None = None


class McpInitializeParams(McpRequestParams):
    protocol_version: str = Field(alias="protocolVersion")
    capabilities: McpClientCapabilities
    client_info: McpClientInfo = Field(alias="clientInfo")


class McpToolsListParams(McpRequestParams):
    cursor: str | None = None


class McpToolCallParams(McpRequestParams):
    name: str | None = None
    arguments: McpToolArguments | GenerateImageArguments = Field(default_factory=McpToolArguments)

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_tool_arguments(cls, value: object, info: ValidationInfo) -> McpToolArguments | GenerateImageArguments:
        model = GenerateImageArguments if info.data.get("name") == "forma.opencode.generate_image" else McpToolArguments
        if isinstance(value, BaseModel):
            value = value.model_dump()
        return model.model_validate(value)


class McpJsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str
    params: McpInitializeParams | McpToolsListParams | McpToolCallParams | McpRequestParams = Field(
        default_factory=dict, validate_default=True,
    )

    @field_validator("params", mode="before")
    @classmethod
    def normalize_params(cls, value: object, info: ValidationInfo) -> McpRequestParams:
        # Select by method before union validation, including when params is omitted.
        params_model = {
            "initialize": McpInitializeParams,
            "tools/list": McpToolsListParams,
            "tools/call": McpToolCallParams,
        }.get(info.data.get("method"), McpRequestParams)
        if isinstance(value, McpRequestParams):
            value = value.model_dump(by_alias=True)
        return params_model.model_validate(value)


class ProjectToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    revision_id: str | None = None
    project_ir: HardwareIntermediateRepresentation
    validation: ProjectValidation
    mermaid_code: str
    svg_schematic: str
    design_outcome: DesignOutcome


class ProjectScopeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    owner_user_id: str
    created: bool


class EventPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: tuple[PublicEvent, ...]
    next_cursor: int


class ConnectorCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=120)
