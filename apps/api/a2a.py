import asyncio
import contextlib
import json
import logging
import math
from forma_core.config import config
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from apps.api.auth import (
    LOCAL_USER_ID,
    UserContext,
    a2a_local_development_allowed,
    a2a_service_credentials_configured,
    authenticated_principal,
    mcp_context_is_authorized,
    resolve_a2a_service_context,
)
from forma_core.agents.workflows import (
    generate_project_with_workflow,
    get_workflow_debug_config,
    list_workflows,
    normalize_workflow_id,
)
from forma_core.agents.orchestrator import HardwarePipelineOrchestrator
from forma_core.database import (
    ensure_project_action_allowed,
    get_latest_project_revision,
    get_project_identity,
    persist_chat_project_revision,
    persist_legacy_project_projection,
    refresh_legacy_project_projection,
    resolve_project_for_read,
)
from forma_core.images import build_image_provider, build_project_visual_spec, get_image_output_debug_config
from apps.api.auth_mode import clerk_auth_required
from forma_core.jobs.store import JOB_STORE
from forma_core.jobs.context import (
    PAST_JOBS_DATA_SOURCE,
    PastJobContext,
    PastJobContextSource,
    compose_prompt_with_past_jobs,
    list_generation_data_sources,
    normalize_generation_data_sources,
)
from forma_core.jobs.source_usage import normalize_source_usage
from forma_core.llm import get_llm_runtime_debug_config
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    HardwareIntermediateRepresentation,
)
from forma_core.workspaces.projects import ProjectStateError
from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.workspaces.projects.generation_mode import GenerationMode
from forma_core.workspaces.projects.output import attach_product_image as attach_project_product_image
from forma_core.observability import (
    get_langfuse_debug_config,
    propagate_observation_attributes,
    serialize_for_langfuse,
    start_observation,
    update_observation,
)
from forma_core.agents.pipeline import emit_agent_pipeline_event, ensure_agent_pipeline_active, observe_agent_pipeline
from forma_core.debug import (
    api_error_detail,
    exception_debug_payload,
    log_exception,
    new_error_correlation_id,
    public_error_message,
    redact_error_value,
)
from forma_core.runtime import (
    AlphaGenerationUnavailableError,
    HostedChatUnavailableError,
    deployment_runtime_config,
    ensure_hosted_chat_enabled,
    generation_unavailable_message,
)
from forma_core.user_integrations import UserIntegrationStore, default_integration_store, resolve_user_integration_settings, ResolvedIntegrationSettings
from apps.api.storage import get_image_storage_config, upload_image_to_supabase_s3
from apps.api.security import MAX_IMAGE_ENCODED_CHARS, consume_operation_limit, security_config, validate_image_limits
from forma_core.utils import generate_mermaid_chart, generate_svg_schematic
from forma_core.validation import build_validation_summary, validate_circuit


logger = logging.getLogger(__name__)

FORMA_AGENT_ID = "forma"
SERVER_RECIPIENTS = {FORMA_AGENT_ID, "server", "hardware_pipeline", "hardware-compiler"}
MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_DEFAULT_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[0]
TCP_AUTH_TIMEOUT_SECONDS = 10.0
TCP_MAX_LINE_BYTES = 64 * 1024
TCP_MAX_AGENT_ID_LENGTH = 200


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _env_bool(name: str, default: bool = False) -> bool:
    value = config.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _payload_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class A2AAgentRegistration(BaseModel):
    agent_id: Optional[str] = Field(None, description="Stable agent identifier")
    name: Optional[str] = Field(None, description="Human-readable agent name")
    capabilities: List[str] = Field(default_factory=list, description="Capability labels this agent provides")
    transports: List[str] = Field(default_factory=list, description="Transports the agent can use")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional agent metadata")


class A2AMessage(BaseModel):
    job_id: str = Field(default_factory=lambda: f"job_{uuid.uuid4().hex}")
    message_id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    type: str = Field("task", description="Message type such as task, event, result, error, or ping")
    action: str = Field("forma.generate_project", description="Action name or tool name")
    sender: str = Field("anonymous", description="Sending agent id")
    recipient: str = Field(FORMA_AGENT_ID, description="Recipient agent id")
    correlation_id: Optional[str] = Field(None, description="Optional id used to correlate request/result pairs")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Message-specific JSON payload")


class A2AEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    job_id: Optional[str] = None
    message_id: Optional[str] = None
    correlation_id: Optional[str] = None
    type: str = "event"
    action: str
    sender: str = FORMA_AGENT_ID
    recipient: str
    created_at: str = Field(default_factory=_utc_now)
    payload: Dict[str, Any] = Field(default_factory=dict)


class A2AHub:
    """In-memory event broker for lightweight agent-to-agent handoffs."""

    def __init__(self) -> None:
        self._queues: Dict[str, asyncio.Queue[A2AEvent]] = {}
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._history: Dict[str, List[A2AEvent]] = {}
        self._principals: Dict[str, str] = {}
        self._owners: Dict[str, Optional[str]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        agent_id: str,
        registration: Optional[Dict[str, Any]] = None,
        *,
        principal: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            raise ValueError("agent_id is required.")
        if len(normalized_agent_id) > 200:
            raise ValueError("agent_id is too long.")
        normalized_principal = str(principal or "").strip() or None
        normalized_owner = str(owner_user_id or "").strip() or None
        async with self._lock:
            current_principal = self._principals.get(normalized_agent_id)
            current_owner = self._owners.get(normalized_agent_id)
            if current_principal and current_principal != normalized_principal:
                raise PermissionError("The agent is owned by another authenticated principal.")
            if current_owner and current_owner != normalized_owner:
                raise PermissionError("The agent is owned by another authenticated user.")

            if normalized_agent_id not in self._queues:
                self._queues[normalized_agent_id] = asyncio.Queue()
            current = self._agents.get(normalized_agent_id, {})
            public_registration = dict(registration or {})
            # Ownership is assigned from the authenticated context, never from
            # caller-controlled registration metadata.
            public_registration.pop("principal", None)
            public_registration.pop("auth_principal", None)
            public_registration.pop("owner_user_id", None)
            self._agents[normalized_agent_id] = {
                **current,
                **public_registration,
                "agent_id": normalized_agent_id,
                "last_seen_at": _utc_now(),
            }
            if normalized_principal:
                self._principals[normalized_agent_id] = normalized_principal
            if normalized_owner:
                self._owners[normalized_agent_id] = normalized_owner
            self._history.setdefault(normalized_agent_id, [])
            return self._agents[normalized_agent_id]

    async def authorize(self, agent_id: str, principal: Optional[str]) -> None:
        normalized_agent_id = str(agent_id or "").strip()
        normalized_principal = str(principal or "").strip() or None
        async with self._lock:
            if normalized_agent_id not in self._queues:
                raise KeyError("A2A agent not found.")
            current_principal = self._principals.get(normalized_agent_id)
            if current_principal and current_principal != normalized_principal:
                raise PermissionError("The agent is owned by another authenticated principal.")
            if normalized_principal and current_principal is None:
                raise PermissionError("The agent has no authenticated owner.")

    async def publish(
        self,
        event: A2AEvent,
        *,
        principal: Optional[str] = None,
        owner_user_id: Optional[str] = None,
    ) -> A2AEvent:
        recipient = str(event.recipient or "").strip()
        if not recipient:
            raise ValueError("event recipient is required.")
        if recipient != event.recipient:
            event = event.model_copy(update={"recipient": recipient})
        await self.register(recipient, principal=principal, owner_user_id=owner_user_id)
        queue = self._queues[recipient]
        await queue.put(event)
        history = self._history.setdefault(recipient, [])
        history.append(event)
        del history[:-100]
        return event

    async def poll(
        self,
        agent_id: str,
        timeout: float = 25.0,
        limit: int = 10,
        *,
        principal: Optional[str] = None,
        create_if_missing: bool = True,
    ) -> List[A2AEvent]:
        normalized_agent_id = str(agent_id or "").strip()
        if normalized_agent_id not in self._queues:
            if not create_if_missing:
                raise KeyError("A2A agent not found.")
            await self.register(normalized_agent_id, principal=principal)
        else:
            await self.authorize(normalized_agent_id, principal)
        queue = self._queues[normalized_agent_id]
        events: List[A2AEvent] = []

        if limit <= 0:
            return events

        if queue.empty() and timeout > 0:
            try:
                events.append(await asyncio.wait_for(queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                return events

        while len(events) < limit:
            try:
                events.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        return events

    def snapshot(self) -> Dict[str, Any]:
        return {
            "agents": list(self._agents.values()),
            "queued_events": {agent_id: queue.qsize() for agent_id, queue in self._queues.items()},
        }


A2A_HUB = A2AHub()


def _lattice_registry():
    from forma_core.agents.contracts import LatticeRegistry
    from forma_core.agents.lattice import default_namespace_agent_cards
    from forma_core.fabricator import fabricator_lattice_card

    return LatticeRegistry([*default_namespace_agent_cards(), fabricator_lattice_card()])


def get_a2a_capabilities() -> Dict[str, Any]:
    try:
        llm_runtime = get_llm_runtime_debug_config()
    except Exception as exc:
        correlation_id = new_error_correlation_id()
        log_exception(
            logger,
            "A2A capability runtime lookup failed",
            exc,
            correlation_id=correlation_id,
        )
        llm_runtime = {
            "error": api_error_detail(
                code="runtime_config_failed",
                message="Runtime configuration could not be resolved.",
                correlation_id=correlation_id,
                public=True,
            )
        }

    return {
        "agent_id": FORMA_AGENT_ID,
        "name": "Forma OSS Hardware Compiler",
        "transports": {
            "rest": {
                "capabilities": "/api/a2a/capabilities",
                "register": "/api/a2a/agents/{agent_id}",
                "send_message": "/api/a2a/messages",
                "listen": "/api/a2a/agents/{agent_id}/events",
            },
            "websocket": {"listen": "/api/a2a/socket/{agent_id}"},
            "tcp_jsonl": {
                "enabled_env": "A2A_SOCKET_ENABLED=true",
                "host_env": "A2A_SOCKET_HOST",
                "port_env": "A2A_SOCKET_PORT",
            },
            "mcp": {
                "endpoint": "/api/mcp",
                "alias": "/api/a2a/mcp",
                "tools": [
                    "forma.generate_project",
                    "forma.compile_project",
                    "forma.debug_config",
                    "forma.validate_circuit",
                    "forma.a2a.send_message",
                    "forma.a2a.poll_events",
                    "forma.a2a.get_job",
                    "forma.a2a.list_jobs",
                    "forma.lattice.list_agents",
                    "forma.lattice.get_agent_card",
                ],
            },
        },
        "job_metadata": JOB_STORE.get_config(),
        "llm_runtime": llm_runtime,
        "image_output": get_image_output_debug_config(),
        "image_storage": get_image_storage_config(),
        "security": security_config(),
        "observability": get_langfuse_debug_config(),
        "workflows": list_workflows(),
        "data_sources": list_generation_data_sources(),
        "actions": [
            "forma.generate_project",
            "forma.compile_project",
            "forma.debug_config",
            "forma.validate_circuit",
            "forma.a2a.capabilities",
            "forma.a2a.get_job",
            "forma.a2a.list_jobs",
            "forma.lattice.list_agents",
            "forma.lattice.get_agent_card",
            "a2a.ping",
        ],
        "lattice": _lattice_registry().manifest(),
    }


def _decode_image_data(image_data: Optional[str]) -> Tuple[Optional[bytes], Optional[str]]:
    if not image_data:
        return None, None
    if len(image_data) > MAX_IMAGE_ENCODED_CHARS:
        raise ValueError("Reference image exceeds the configured encoded size limit.")
    return validate_image_limits(image_data)


def _attach_stored_image_metadata(
    ir: Any,
    *,
    image_data: str,
    metadata_prefix: str,
    object_prefix: str,
    fallback_content_type: str = "image/png",
    allow_remote_url: bool = False,
) -> Dict[str, Any]:
    metadata = ir.assembly_metadata or {}
    project_id = metadata.get("project_id")
    try:
        stored = upload_image_to_supabase_s3(
            image_data,
            prefix=object_prefix,
            project_id=project_id,
            fallback_content_type=fallback_content_type,
            allow_remote_url=allow_remote_url,
        )
    except Exception as exc:
        correlation_id = new_error_correlation_id()
        log_exception(
            logger,
            "Image upload to Supabase Storage failed",
            exc,
            correlation_id=correlation_id,
            context={"metadata_prefix": metadata_prefix, "project_id": project_id},
        )
        return {
            f"{metadata_prefix}_storage_error": public_error_message("storage_failed"),
            f"{metadata_prefix}_storage_error_code": "storage_failed",
            f"{metadata_prefix}_storage_error_correlation_id": correlation_id,
            f"{metadata_prefix}_storage_bucket": get_image_storage_config().get("bucket"),
        }

    if not stored:
        storage_config = get_image_storage_config()
        logger.info(
            "Image storage skipped for %s: storage_enabled=%s bucket=%s",
            metadata_prefix,
            storage_config.get("enabled"),
            storage_config.get("bucket"),
        )
        return {
            f"{metadata_prefix}_storage_enabled": False,
            f"{metadata_prefix}_storage_bucket": storage_config.get("bucket"),
        }
    logger.info(
        "Image stored for %s: method=%s bucket=%s key=%s url_present=%s",
        metadata_prefix,
        stored.storage_method,
        stored.bucket,
        stored.key,
        bool(stored.url),
    )
    return {
        **stored.metadata(metadata_prefix),
        f"{metadata_prefix}_storage_enabled": True,
    }


def _operation_summary(operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"succeeded": 0, "failed": 0, "pending": 0, "not_requested": 0}
    for operation in operations:
        status = str(operation.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(operations),
        "failed": counts.get("failed", 0),
        "succeeded": counts.get("succeeded", 0),
        "pending": counts.get("pending", 0),
        "not_requested": counts.get("not_requested", 0),
        "ok": counts.get("failed", 0) == 0,
    }


def _set_operation_status(
    ir: Any,
    operation_id: str,
    *,
    label: str,
    status: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    requested: Optional[bool] = None,
    enabled: Optional[bool] = None,
    configured: Optional[bool] = None,
    reason: Optional[str] = None,
    error: Optional[str] = None,
    error_type: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    metadata = dict(ir.assembly_metadata or {})
    operations = [
        item for item in metadata.get("operation_statuses", [])
        if isinstance(item, dict) and item.get("id") != operation_id
    ]
    record: Dict[str, Any] = {
        "id": operation_id,
        "label": label,
        "status": status,
    }
    optional_values = {
        "provider": provider,
        "model": model,
        "requested": requested,
        "enabled": enabled,
        "configured": configured,
        "reason": reason,
        "error": error,
        "error_type": error_type,
        "details": details,
    }
    for key, value in optional_values.items():
        if value is not None:
            record[key] = value

    operations.append(record)
    metadata["operation_statuses"] = operations
    metadata["operation_summary"] = _operation_summary(operations)
    ir.assembly_metadata = metadata


def _safe_image_config(config: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key, value in (config or {}).items():
        key_text = str(key)
        if any(token in key_text.lower() for token in ("key", "token", "secret", "authorization")):
            safe[key_text] = "<redacted>"
        else:
            safe[key_text] = value
    return safe


def _attach_product_image(prompt_text: str, ir: Any, generate_image: bool = False, settings: Optional[ResolvedIntegrationSettings] = None) -> None:
    image_provider = build_image_provider(force_enabled=generate_image, settings=settings)
    image_config = _safe_image_config(image_provider.get_debug_config())
    visual_spec = build_project_visual_spec(prompt_text, ir)
    image_status = "pending" if generate_image else "not_requested"
    metadata = {
        **(ir.assembly_metadata or {}),
        "image_output_requested": generate_image,
        "image_output_enabled": image_config.get("enabled", False),
        "image_output_provider": image_config.get("provider"),
        "image_output_model": image_config.get("model_name"),
        "image_output_configured": image_config.get("configured", False),
        "image_output_status": image_status,
        "image_output_reason": image_config.get("reason"),
        "image_output_debug": image_config,
        "product_visual_spec": visual_spec,
    }
    ir.assembly_metadata = metadata
    _set_operation_status(
        ir,
        "image_generation",
        label="Image generation",
        status=image_status,
        provider=image_config.get("provider"),
        model=image_config.get("model_name"),
        requested=generate_image,
        enabled=image_config.get("enabled", False),
        configured=image_config.get("configured", False),
        reason=image_config.get("reason"),
        details={"image_output_debug": image_config},
    )

    if not generate_image:
        return

    if not image_config.get("configured", False):
        error_detail = api_error_detail(
            code="image_generation_failed",
            message="Image output is not configured.",
            correlation_id=new_error_correlation_id(),
            public=True,
        )
        logger.warning(
            "Image generation operation failed before request: provider=%s model=%s code=%s",
            image_config.get("provider"),
            image_config.get("model_name"),
            error_detail["code"],
        )
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": error_detail["message"],
            "image_output_error_code": error_detail["code"],
            "image_output_error_correlation_id": error_detail["correlation_id"],
            "image_output_error_type": "configuration",
            "image_output_debug": image_config,
            "product_image_error": error_detail["message"],
        }
        _set_operation_status(
            ir,
            "image_generation",
            label="Image generation",
            status="failed",
            provider=image_config.get("provider"),
            model=image_config.get("model_name"),
            requested=True,
            enabled=image_config.get("enabled", False),
            configured=False,
            reason=image_config.get("reason"),
            error=error_detail["message"],
            error_type="configuration",
            details={"image_output_debug": image_config},
        )
        return

    logger.info(
        "Image generation operation starting: provider=%s model=%s size=%s request_capable=%s reason=%s",
        image_config.get("provider"),
        image_config.get("model_name"),
        image_config.get("size"),
        image_config.get("configured", False),
        image_config.get("reason"),
    )
    try:
        generated_images = image_provider.generate_project_image_sequence(prompt_text, ir)
    except Exception as exc:
        correlation_id = new_error_correlation_id()
        log_exception(
            logger,
            "Image generation operation failed",
            exc,
            correlation_id=correlation_id,
            context={
                "provider": image_config.get("provider"),
                "model": image_config.get("model_name"),
            },
        )
        error_detail = api_error_detail(
            code="image_generation_failed",
            message="Image generation could not be completed.",
            correlation_id=correlation_id,
            public=True,
        )
        error_message = error_detail["message"]
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": error_message,
            "image_output_error_code": error_detail["code"],
            "image_output_error_correlation_id": correlation_id,
            "image_output_error_type": "provider",
            "image_output_debug": image_config,
            "product_image_error": error_message,
        }
        _set_operation_status(
            ir,
            "image_generation",
            label="Image generation",
            status="failed",
            provider=image_config.get("provider"),
            model=image_config.get("model_name"),
            requested=True,
            enabled=image_config.get("enabled", False),
            configured=image_config.get("configured", False),
            error=error_message,
            error_type="provider",
            details={"image_output_debug": image_config},
        )
        return

    logger.info(
        "Image generation provider returned: provider=%s model=%s generated_count=%s",
        image_config.get("provider"),
        image_config.get("model_name"),
        len(generated_images),
    )
    if not generated_images:
        error_detail = api_error_detail(
            code="image_generation_failed",
            message="Image output was requested, but no images were returned.",
            correlation_id=new_error_correlation_id(),
            public=True,
        )
        error_message = error_detail["message"]
        logger.warning(
            "Image generation operation failed: provider=%s model=%s error_type=empty_response error=%s",
            image_config.get("provider"),
            image_config.get("model_name"),
            error_message,
        )
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": error_message,
            "image_output_error_code": error_detail["code"],
            "image_output_error_correlation_id": error_detail["correlation_id"],
            "image_output_error_type": "empty_response",
            "image_output_debug": image_config,
            "product_image_error": error_message,
            "product_visual_sequence_count": 0,
        }
        _set_operation_status(
            ir,
            "image_generation",
            label="Image generation",
            status="failed",
            provider=image_config.get("provider"),
            model=image_config.get("model_name"),
            requested=True,
            enabled=image_config.get("enabled", False),
            configured=image_config.get("configured", False),
            error=error_message,
            error_type="empty_response",
            details={"image_output_debug": image_config},
        )
        return

    product_metadata: Dict[str, Any] = {
        "image_output_status": "succeeded",
        "image_output_failed": False,
        "image_output_error": None,
        "image_output_error_type": None,
        "image_output_generated_count": len(generated_images),
        "product_image_error": None,
        "product_visual_sequence_count": len(generated_images),
    }
    product_visual_sequence: List[Dict[str, Any]] = []

    for index, generated_image in enumerate(generated_images):
        view_id = generated_image.view_id or f"view_{index + 1}"
        metadata_prefix = f"product_{view_id}_image"
        object_prefix = f"product-{view_id}"
        logger.info(
            "Image generation view ready: index=%s view=%s provider=%s model=%s format=%s prompt_chars=%s compacted=%s",
            index,
            view_id,
            generated_image.provider,
            generated_image.model,
            generated_image.output_format,
            generated_image.prompt_final_length,
            generated_image.prompt_compacted,
        )
        storage_metadata = _attach_stored_image_metadata(
            ir,
            image_data=generated_image.data_url,
            metadata_prefix=metadata_prefix,
            object_prefix=object_prefix,
            fallback_content_type=f"image/{generated_image.output_format or 'png'}",
            allow_remote_url=True,
        )
        image_url = storage_metadata.get(f"{metadata_prefix}_url")
        if storage_metadata.get(f"{metadata_prefix}_storage_error"):
            logger.warning(
                "Image generation view storage failed: view=%s provider=%s model=%s error=%s",
                view_id,
                generated_image.provider,
                generated_image.model,
                storage_metadata.get(f"{metadata_prefix}_storage_error"),
            )
        elif image_url:
            logger.info(
                "Image generation view stored: view=%s provider=%s model=%s url_present=true",
                view_id,
                generated_image.provider,
                generated_image.model,
            )
        else:
            logger.info(
                "Image generation view kept inline: view=%s provider=%s model=%s data_url_chars=%s",
                view_id,
                generated_image.provider,
                generated_image.model,
                len(generated_image.data_url or ""),
            )
        image_record: Dict[str, Any] = {
            "view_id": view_id,
            "label": generated_image.label,
            "provider": generated_image.provider,
            "model": generated_image.model,
            "size": generated_image.size,
            "output_format": generated_image.output_format,
            "model_revision": generated_image.model_revision,
            "inference_provider": generated_image.inference_provider,
            "model_license": generated_image.model_license,
            "prompt": generated_image.prompt,
            "prompt_original_length": generated_image.prompt_original_length,
            "prompt_final_length": generated_image.prompt_final_length,
            "prompt_compacted": generated_image.prompt_compacted,
            "prompt_compaction_strategy": generated_image.prompt_compaction_strategy,
            "reference_view_id": generated_image.reference_view_id,
            "url": image_url,
            "content_type": storage_metadata.get(f"{metadata_prefix}_content_type"),
            "s3_bucket": storage_metadata.get(f"{metadata_prefix}_s3_bucket"),
            "s3_key": storage_metadata.get(f"{metadata_prefix}_s3_key"),
            "storage_method": storage_metadata.get(f"{metadata_prefix}_storage_method"),
            "storage_error": storage_metadata.get(f"{metadata_prefix}_storage_error"),
        }
        if not image_url:
            image_record["data"] = generated_image.data_url
            product_metadata[f"{metadata_prefix}_data"] = generated_image.data_url

        product_visual_sequence.append(image_record)
        product_metadata.update(storage_metadata)

        if index == 0:
            product_metadata.update(
                {
                    "product_image_provider": generated_image.provider,
                    "product_image_model": generated_image.model,
                    "product_image_size": generated_image.size,
                    "product_image_output_format": generated_image.output_format,
                    "product_image_model_revision": generated_image.model_revision,
                    "product_image_inference_provider": generated_image.inference_provider,
                    "product_image_model_license": generated_image.model_license,
                    "product_image_prompt": generated_image.prompt,
                    "product_image_prompt_original_length": generated_image.prompt_original_length,
                    "product_image_prompt_final_length": generated_image.prompt_final_length,
                    "product_image_prompt_compacted": generated_image.prompt_compacted,
                    "product_image_prompt_compaction_strategy": generated_image.prompt_compaction_strategy,
                    "product_image_url": image_url,
                    "product_image_content_type": storage_metadata.get(f"{metadata_prefix}_content_type"),
                    "product_image_s3_bucket": storage_metadata.get(f"{metadata_prefix}_s3_bucket"),
                    "product_image_s3_key": storage_metadata.get(f"{metadata_prefix}_s3_key"),
                    "product_image_storage_method": storage_metadata.get(f"{metadata_prefix}_storage_method"),
                }
            )
            if not image_url:
                product_metadata["product_image_data"] = generated_image.data_url

    product_metadata["product_visual_sequence"] = product_visual_sequence

    ir.assembly_metadata = {
        **(ir.assembly_metadata or {}),
        **product_metadata,
    }
    storage_errors = [
        record.get("storage_error")
        for record in product_visual_sequence
        if isinstance(record, dict) and record.get("storage_error")
    ]
    logger.info(
        "Image generation operation completed: provider=%s model=%s generated_count=%s stored_count=%s storage_errors=%s",
        image_config.get("provider"),
        image_config.get("model_name"),
        len(generated_images),
        len([record for record in product_visual_sequence if isinstance(record, dict) and record.get("url")]),
        len(storage_errors),
    )
    _set_operation_status(
        ir,
        "image_generation",
        label="Image generation",
        status="succeeded",
        provider=image_config.get("provider"),
        model=image_config.get("model_name"),
        requested=True,
        enabled=image_config.get("enabled", False),
        configured=image_config.get("configured", False),
        details={"generated_count": len(generated_images)},
    )
    _set_operation_status(
        ir,
        "image_storage",
        label="Image storage",
        status="failed" if storage_errors else "succeeded",
        requested=True,
        enabled=True,
        configured=True,
        error=str(storage_errors[0])[:500] if storage_errors else None,
        error_type="storage_upload" if storage_errors else None,
        details={"stored_count": len([record for record in product_visual_sequence if isinstance(record, dict) and record.get("url")])},
    )


def _persist_updated_project_ir(
    ir: Any,
    *,
    prompt_text: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> None:
    metadata = ir.assembly_metadata or {}
    project_id = metadata.get("project_id")
    if not project_id:
        return

    if owner_user_id:
        # Authenticated generation is committed canonically by the caller.
        return

    try:
        hardware_ir = ir.model_dump()
        updated = refresh_legacy_project_projection(
            project_id,
            hardware_ir,
            owner_user_id=owner_user_id,
        )
        if updated:
            return

        title = (
            getattr(getattr(ir, "overview", None), "title", None)
            or (prompt_text or "").strip()
            or "Untitled Forma Project"
        )
        created_at = metadata.get("created_at") if isinstance(metadata.get("created_at"), str) else _utc_now()
        persist_legacy_project_projection(
            project_id=project_id,
            title=title,
            prompt=(prompt_text or metadata.get("source_prompt") or "").strip(),
            hardware_ir=hardware_ir,
            created_at=created_at,
            chat_id=metadata.get("chat_id"),
            owner_user_id=owner_user_id,
            visibility="public",
        )
    except Exception as exc:
        logger.warning("Failed to persist updated project metadata for %s: %s", project_id, exc)


def _resolve_owner_user_integrations(owner_user_id: Optional[str]) -> ResolvedIntegrationSettings:
    if not clerk_auth_required():
        return resolve_user_integration_settings(default_integration_store())
    if isinstance(owner_user_id, str) and owner_user_id.strip():
        return resolve_user_integration_settings(UserIntegrationStore.for_user(owner_user_id.strip()))
    return resolve_user_integration_settings()


def _apply_owner_user_integrations(owner_user_id: Optional[str]) -> ResolvedIntegrationSettings:
    """Compatibility shim for callers that previously requested environment mutation."""
    return _resolve_owner_user_integrations(owner_user_id)


def _context_owner_user_id(user_context: Optional[UserContext]) -> Optional[str]:
    if user_context is None or not user_context.is_authenticated:
        return None
    owner_user_id = str(user_context.owner_user_id or "").strip()
    return owner_user_id or None


def a2a_principal_for_user(user_context: Optional[UserContext]) -> Optional[str]:
    """Expose the stable ownership key used by all A2A transports."""
    return authenticated_principal(user_context)


def _message_for_user_context(message: A2AMessage, user_context: Optional[UserContext]) -> A2AMessage:
    """Replace caller-supplied ownership metadata with authenticated identity."""
    principal = a2a_principal_for_user(user_context)
    if not message.action.startswith("forma.") and principal is None:
        return message

    payload = dict(message.payload)
    payload.pop("owner_user_id", None)
    payload.pop("_forma_a2a_principal", None)
    owner_user_id = _context_owner_user_id(user_context)
    if message.action.startswith("forma.") and owner_user_id:
        payload["owner_user_id"] = owner_user_id
    if principal:
        payload["_forma_a2a_principal"] = principal
    return message.model_copy(update={"payload": payload})


def build_generation_response(
    prompt: str,
    image_data: Optional[str] = None,
    generate_image: bool = False,
    workflow: str = "default",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    external_source_provider: Optional[str] = None,
    chat_id: Optional[str] = None,
    source_project_id: Optional[str] = None,
    frontend_job_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    data_sources: Optional[List[str]] = None,
    past_job_context: Optional[PastJobContext] = None,
    project_id: Optional[str] = None,
    retry_stage: Optional[str] = None,
    generation_mode: str = "regular",
    settings: Optional[ResolvedIntegrationSettings] = None,
) -> Dict[str, Any]:
    ensure_hosted_chat_enabled()
    settings = settings or _resolve_owner_user_integrations(owner_user_id)

    prompt_text = (prompt or "").strip()
    try:
        max_prompt_chars = max(1, int(config.get("FORMA_MAX_PROMPT_CHARS", "12000")))
    except ValueError:
        max_prompt_chars = 12000
    if len(prompt_text) > max_prompt_chars:
        raise ValueError("Prompt exceeds the configured length limit.")
    workflow_id = normalize_workflow_id(workflow)
    try:
        normalized_generation_mode = GenerationMode(str(generation_mode or "regular").strip().lower())
    except ValueError as exc:
        raise ValueError("generation_mode must be regular or progressive.") from exc
    progressive_generation = normalized_generation_mode == GenerationMode.PROGRESSIVE
    if progressive_generation:
        # Progressive generation requires the visual artifact used by its review gate.
        generate_image = True
    normalized_retry_stage = str(retry_stage or "").strip() or None
    prior_generation_run: Optional[Dict[str, Any]] = None
    retry_stage_replay = False
    if normalized_retry_stage:
        if workflow_id not in {"default", "web_research"}:
            raise ValueError("Named generation-stage retry is not supported by this workflow.")
        if not project_id:
            raise ValueError("project_id is required when retry_stage is provided.")
        if not owner_user_id:
            raise ValueError("owner_user_id is required when retry_stage is provided.")
        try:
            existing_revision = get_latest_project_revision(project_id, owner_user_id)
        except Exception as exc:
            raise ValueError("The project containing the failed generation stage was not found.") from exc
        if existing_revision is None:
            raise ValueError("The project containing the failed generation stage was not found.")
        if str(getattr(existing_revision, "owner_user_id", "") or "") != str(owner_user_id):
            raise ValueError("The failed generation stage is not owned by the requesting user.")
        existing_ir = existing_revision.state
        if hasattr(existing_ir, "model_dump"):
            existing_ir = existing_ir.model_dump(mode="json")
        existing_ir = existing_ir if isinstance(existing_ir, dict) else {}
        existing_metadata = existing_ir.get("assembly_metadata") or {}
        candidate_run = existing_metadata.get("generation_run")
        if not isinstance(candidate_run, dict):
            raise ValueError("The project does not contain persisted generation-stage artifacts.")
        candidate_record = (candidate_run.get("records") or {}).get(normalized_retry_stage)
        if not isinstance(candidate_record, dict):
            raise ValueError(f"Generation stage '{normalized_retry_stage}' is not failed and cannot be retried.")
        if candidate_record.get("status") == "succeeded":
            retry_stage_replay = bool(
                frontend_job_id
                and existing_metadata.get("frontend_job_id") == frontend_job_id
                and candidate_run.get("retry_stage") == normalized_retry_stage
            )
            if not retry_stage_replay:
                raise ValueError(f"Generation stage '{normalized_retry_stage}' is not failed and cannot be retried.")
        elif candidate_record.get("status") != "failed":
            raise ValueError(f"Generation stage '{normalized_retry_stage}' is not failed and cannot be retried.")
        prior_generation_run = candidate_run
    has_prompt = bool(prompt_text)
    if not has_prompt and not image_data:
        raise ValueError("Provide a prompt or reference image.")
    if not has_prompt:
        prompt_text = "Infer a buildable hardware project from the uploaded reference image."
    cad_required = False
    normalized_data_sources = normalize_generation_data_sources(data_sources)
    context_requested = PAST_JOBS_DATA_SOURCE in normalized_data_sources
    resolved_past_job_context = past_job_context or PastJobContext(
        reason="No past-job context was retrieved." if context_requested else None
    )
    generation_prompt = compose_prompt_with_past_jobs(prompt_text, resolved_past_job_context)
    past_jobs_metadata = resolved_past_job_context.metadata() if context_requested else None

    try:
        image_bytes, image_mime_type = _decode_image_data(image_data)
    except Exception as exc:
        raise ValueError("Reference image could not be decoded or exceeds the configured limits.") from exc

    llm_config = get_workflow_debug_config(
        workflow_id,
        provider_name=provider,
        model_name=model,
        external_source_provider=external_source_provider,
        settings=settings,
    )
    if deployment_runtime_config(llm_config)["alpha_generation_gate_active"]:
        raise AlphaGenerationUnavailableError(generation_unavailable_message(llm_config))

    trace_metadata = {
        "workflow": workflow_id,
        "chat_id": chat_id,
        "source_project_id": source_project_id,
        "project_id": project_id,
        "owner_user_id": owner_user_id,
        "requested_provider": provider,
        "requested_model": model,
        "runtime_provider": (llm_config.get("runtime") or {}).get("runtime_provider"),
        "runtime_model": (llm_config.get("runtime") or {}).get("runtime_model"),
        "has_reference_image": bool(image_data),
        "image_mime_type": image_mime_type,
        "generate_image": generate_image,
        "generation_mode": normalized_generation_mode.value,
        "frontend_job_id": frontend_job_id,
        "external_source_provider": external_source_provider,
        "data_sources": normalized_data_sources,
        "past_jobs_context": past_jobs_metadata,
    }
    with start_observation(
        name="forma.generate_project",
        as_type="span",
        input={
            "prompt": prompt_text,
            "workflow": workflow_id,
            "provider": provider,
            "model": model,
            "external_source_provider": external_source_provider,
            "has_reference_image": bool(image_data),
            "generate_image": generate_image,
        },
        metadata=trace_metadata,
    ) as root_observation:
        with propagate_observation_attributes(
            trace_name="forma.generate_project",
            metadata=trace_metadata,
            tags=["forma", f"workflow:{workflow_id}"],
        ):
            ir = generate_project_with_workflow(
                workflow_id,
                generation_prompt,
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
                provider_name=provider,
                model_name=model,
                external_source_provider=external_source_provider,
                settings=settings,
                generation_metadata={
                    "project_id": project_id,
                    "chat_id": chat_id,
                    "source_project_id": source_project_id,
                    "frontend_job_id": frontend_job_id,
                    "owner_user_id": owner_user_id,
                    "external_source_provider": external_source_provider,
                    "data_sources": normalized_data_sources,
                    "past_jobs_context": past_jobs_metadata,
                    "project_prompt": prompt_text,
                    "retry_stage": normalized_retry_stage,
                    "prior_generation_run": prior_generation_run,
                    "retry_stage_replay": retry_stage_replay,
                    "cad_required": cad_required,
                    "generation_mode": normalized_generation_mode.value,
                    **(
                        {"visual_approval_policy": "require_approval"}
                        if progressive_generation
                        else {}
                    ),
                },
                persist_project=False,
            )
            ensure_agent_pipeline_active()
            ir.assembly_metadata = {
                **(ir.assembly_metadata or {}),
                "project_id": project_id or (ir.assembly_metadata or {}).get("project_id"),
                "chat_id": chat_id or (ir.assembly_metadata or {}).get("chat_id"),
                "source_project_id": source_project_id or (ir.assembly_metadata or {}).get("source_project_id"),
                "frontend_job_id": frontend_job_id or (ir.assembly_metadata or {}).get("frontend_job_id"),
                "workflow": workflow_id,
                "generation_mode": normalized_generation_mode.value,
                "external_source_provider": external_source_provider or (ir.assembly_metadata or {}).get("external_source_provider"),
                "data_sources": normalized_data_sources,
                "past_jobs_context": past_jobs_metadata,
                "source_usage": normalize_source_usage(
                    {
                        **((ir.assembly_metadata or {}).get("source_usage") or {}),
                        "past_jobs": resolved_past_job_context.used,
                    },
                    fallback_workflow=workflow_id,
                ),
            }

            if image_data:
                metadata = ir.assembly_metadata or {}
                storage_metadata = _attach_stored_image_metadata(
                    ir,
                    image_data=image_data,
                    metadata_prefix="reference_image",
                    object_prefix="reference",
                    fallback_content_type=image_mime_type or "image/png",
                )
                reference_metadata: Dict[str, Any] = {
                    **storage_metadata,
                    "image_features": metadata.get("image_features") or ir.constraints[:12],
                    "input_mode": "prompt_image",
                }
                if not storage_metadata.get("reference_image_url"):
                    reference_metadata["reference_image_data"] = image_data
                ir.assembly_metadata = {
                    **metadata,
                    **reference_metadata,
                }

            if generate_image:
                emit_agent_pipeline_event(workflow_id, "image_generation", "started")
            if progressive_generation:
                attach_project_product_image(
                    prompt_text,
                    ir,
                    generate_image=generate_image,
                    settings=settings,
                )
            else:
                _attach_product_image(prompt_text, ir, generate_image=generate_image, settings=settings)
            if generate_image:
                image_status = (ir.assembly_metadata or {}).get("image_output_status")
                emit_agent_pipeline_event(
                    workflow_id,
                    "image_generation",
                    "failed" if image_status == "failed" else "completed",
                    details={"image_output_status": image_status},
                )
            ensure_agent_pipeline_active()
            if owner_user_id and (ir.assembly_metadata or {}).get("project_id"):
                persisted_revision = persist_chat_project_revision(
                    str((ir.assembly_metadata or {}).get("project_id")),
                    owner_user_id,
                    ir,
                    source_job_id=f"generation-{frontend_job_id or uuid.uuid4().hex}",
                    prompt=prompt_text,
                    chat_id=chat_id,
                )
                ir = persisted_revision.state
            _persist_updated_project_ir(ir, prompt_text=prompt_text, owner_user_id=owner_user_id)

            response = {
                "project_id": (ir.assembly_metadata or {}).get("project_id"),
                "chat_id": (ir.assembly_metadata or {}).get("chat_id"),
                "project_ir": ir.model_dump(),
                "mermaid_code": generate_mermaid_chart(ir),
                "svg_schematic": generate_svg_schematic(ir),
                "generation_status": (ir.assembly_metadata or {}).get("generation_status", "succeeded"),
                "project_readiness": (ir.assembly_metadata or {}).get("project_readiness", "complete"),
                "generation_stages": ((ir.assembly_metadata or {}).get("generation_run") or {}).get("records", {}),
                "idempotent_stage_replay": bool((ir.assembly_metadata or {}).get("retry_stage_replay")),
            }
            update_observation(
                root_observation,
                output={
                    "project_id": (ir.assembly_metadata or {}).get("project_id"),
                    "chat_id": (ir.assembly_metadata or {}).get("chat_id"),
                    "title": ir.overview.title if ir.overview else None,
                    "is_valid": ir.is_valid,
                    "component_count": len(ir.components),
                    "net_count": len(ir.nets),
                    "workflow": workflow_id,
                },
                metadata={
                    **trace_metadata,
                    "project_id": (ir.assembly_metadata or {}).get("project_id"),
                    "chat_id": (ir.assembly_metadata or {}).get("chat_id"),
                    "llm_provider": (ir.assembly_metadata or {}).get("llm_provider"),
                    "model_name": (ir.assembly_metadata or {}).get("model_name"),
                    "runtime_provider": (ir.assembly_metadata or {}).get("runtime_provider"),
                    "runtime_model": (ir.assembly_metadata or {}).get("runtime_model"),
                    "response_summary": serialize_for_langfuse(
                        {
                            "has_mermaid": bool(response["mermaid_code"]),
                            "has_svg_schematic": bool(response["svg_schematic"]),
                        }
                    ),
                },
            )
            return response


async def call_forma_action(
    action: str,
    payload: Dict[str, Any],
    user_context: Optional[UserContext] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = action.removeprefix("forma.")
    if normalized == "generate_project":
        ensure_hosted_chat_enabled()
    owner_user_id = _context_owner_user_id(user_context)
    if normalized in {"generate_project", "validate_circuit"}:
        identity = a2a_principal_for_user(user_context) or "anonymous"
        consume_operation_limit("generation" if normalized == "generate_project" else "validation", identity)
    project_id = payload.get("project_id")

    if project_id and not owner_user_id:
        raise ValueError("An authenticated user context is required for project actions.")
    if project_id:
        ensure_project_action_allowed(
            str(project_id),
            owner_user_id,
            action,
            require_workflow=normalized == "generate_project",
        )

    settings = _resolve_owner_user_integrations(owner_user_id if isinstance(owner_user_id, str) else None)

    if normalized == "generate_project":
        data_sources = normalize_generation_data_sources(payload.get("data_sources") or [])
        past_job_context = None
        if PAST_JOBS_DATA_SOURCE in data_sources:
            def load_project_for_context(context_project_id: str) -> Any:
                try:
                    return resolve_project_for_read(context_project_id, owner_user_id).project
                except Exception:
                    return None

            past_job_context = await PastJobContextSource(JOB_STORE, load_project_for_context).retrieve(
                str(payload.get("prompt") or ""),
                owner_user_id=owner_user_id if isinstance(owner_user_id, str) else None,
                limit=int(payload.get("past_jobs_limit") or 3),
                exclude_job_id=payload.get("client_job_id") or payload.get("frontend_job_id"),
            )
        return await asyncio.to_thread(
            build_generation_response,
            payload.get("prompt", ""),
            payload.get("image_data"),
            _payload_bool(payload.get("generate_image"), default=False),
            payload.get("workflow", "default"),
            payload.get("provider"),
            payload.get("model"),
            payload.get("external_source_provider"),
            payload.get("chat_id"),
            payload.get("source_project_id"),
            payload.get("client_job_id") or payload.get("frontend_job_id") or job_id,
            owner_user_id,
            data_sources,
            past_job_context,
            payload.get("project_id"),
            payload.get("retry_stage"),
            generation_mode=payload.get("generation_mode", "regular"),
            settings=settings,
        )

    if normalized == "debug_config":
        orchestrator = HardwarePipelineOrchestrator(
            provider_name=payload.get("provider"),
            model_name=payload.get("model"),
            settings=settings,
        )
        return {
            **orchestrator.get_debug_config(),
            "image_output": get_image_output_debug_config(settings=settings),
            "image_storage": get_image_storage_config(),
            "observability": get_langfuse_debug_config(),
            "workflows": list_workflows(),
            "data_sources": list_generation_data_sources(),
        }

    if normalized == "validate_circuit":
        components = [ComponentInstance.model_validate(component) for component in payload.get("components", [])]
        nets = [ConnectionNet.model_validate(net) for net in payload.get("nets", [])]
        issues = validate_circuit(components, nets)
        return {
            "is_valid": not any(issue.severity.upper() == "CRITICAL" for issue in issues),
            "issues": [issue.model_dump() for issue in issues],
        }

    if normalized in {"a2a.capabilities", "capabilities"}:
        return get_a2a_capabilities()

    if action == "a2a.ping" or normalized == "ping":
        return {"pong": True, "server_time": _utc_now()}

    raise ValueError(f"Unsupported Forma A2A action: {action}")


def _is_server_message(message: A2AMessage) -> bool:
    return message.recipient in SERVER_RECIPIENTS or message.action.startswith("forma.")


def _normalize_message_agent_id(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required.")
    if len(normalized) > 200:
        raise ValueError(f"{field_name} is too long.")
    return normalized


def _job_identity_matches(
    job: Dict[str, Any],
    message: A2AMessage,
    principal: Optional[str],
    owner_user_id: Optional[str],
) -> bool:
    if (
        str(job.get("message_id") or "") != message.message_id
        or str(job.get("action") or "") != message.action
        or str(job.get("sender") or "") != message.sender
        or str(job.get("recipient") or "") != message.recipient
    ):
        return False
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    stored_principal = str(payload.get("_forma_a2a_principal") or "").strip() or None
    stored_owner = str(payload.get("owner_user_id") or "").strip() or None
    return bool(
        (principal and stored_principal == principal)
        or (owner_user_id and stored_owner == owner_user_id)
    )


def _idempotent_job_ack(
    message: A2AMessage,
    existing_job: Dict[str, Any],
    server_owned: bool,
) -> A2AEvent:
    return A2AEvent(
        job_id=message.job_id,
        message_id=message.message_id,
        correlation_id=existing_job.get("correlation_id") or message.correlation_id,
        type="ack",
        action=message.action,
        sender=FORMA_AGENT_ID,
        recipient=message.sender,
        payload={
            "accepted": True,
            "server_owned": server_owned,
            "job_id": message.job_id,
            "job": existing_job,
            "idempotent": True,
        },
    )


async def submit_a2a_message(
    message: A2AMessage,
    user_context: Optional[UserContext] = None,
) -> A2AEvent:
    if user_context is None or not user_context.is_authenticated:
        raise PermissionError("An authenticated context is required for A2A messages.")
    message = message.model_copy(
        update={
            "job_id": _normalize_message_agent_id(message.job_id, "job_id"),
            "message_id": _normalize_message_agent_id(message.message_id, "message_id"),
            "sender": _normalize_message_agent_id(message.sender, "sender"),
            "recipient": _normalize_message_agent_id(message.recipient, "recipient"),
        }
    )
    message = _message_for_user_context(message, user_context)
    message = message.model_copy(update={"correlation_id": message.correlation_id or new_error_correlation_id()})
    server_owned = _is_server_message(message)
    if server_owned and message.action.removeprefix("forma.") == "generate_project":
        ensure_hosted_chat_enabled()
    principal = a2a_principal_for_user(user_context)
    owner_user_id = _context_owner_user_id(user_context)
    await A2A_HUB.register(
        message.sender,
        principal=principal,
        owner_user_id=owner_user_id,
    )
    if not server_owned and principal:
        try:
            await A2A_HUB.authorize(message.recipient, principal)
        except KeyError as exc:
            raise PermissionError("The recipient agent is not registered for this principal.") from exc
    project_id = message.payload.get("project_id")
    owner_user_id = _context_owner_user_id(user_context)
    if server_owned and project_id and not owner_user_id:
        raise ValueError("An authenticated user context is required for project actions.")
    if server_owned and project_id:
        ensure_project_action_allowed(
            str(project_id),
            owner_user_id,
            message.action,
            require_workflow=message.action.removeprefix("forma.") == "generate_project",
        )
    existing_job = JOB_STORE.get_job(message.job_id)
    if existing_job:
        if _job_identity_matches(existing_job, message, principal, owner_user_id):
            ack = _idempotent_job_ack(message, existing_job, server_owned)
            await A2A_HUB.publish(ack, principal=principal, owner_user_id=owner_user_id)
            return ack
        raise PermissionError("The A2A job id is already owned by another message.")
    try:
        job = JOB_STORE.create_job(
            job_id=message.job_id,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            action=message.action,
            sender=message.sender,
            recipient=message.recipient,
            payload=message.payload,
            server_owned=server_owned,
            status="queued" if server_owned else "accepted",
            replace_existing=False,
        )
    except Exception as create_error:
        # A concurrent request may have won the unique job-id insert between
        # the preflight lookup and creation. Reconcile only when the persisted
        # record proves this is the same authenticated request.
        try:
            existing_job = JOB_STORE.get_job(message.job_id)
        except Exception:
            raise create_error
        if existing_job is None:
            raise create_error
        if _job_identity_matches(existing_job, message, principal, owner_user_id):
            ack = _idempotent_job_ack(message, existing_job, server_owned)
            await A2A_HUB.publish(ack, principal=principal, owner_user_id=owner_user_id)
            return ack
        raise PermissionError("The A2A job id is already owned by another message.") from create_error

    ack = A2AEvent(
        job_id=message.job_id,
        message_id=message.message_id,
        correlation_id=message.correlation_id,
        type="ack",
        action=message.action,
        sender=FORMA_AGENT_ID,
        recipient=message.sender,
        payload={"accepted": True, "server_owned": server_owned, "job_id": message.job_id, "job": job},
    )
    await A2A_HUB.publish(ack, principal=principal, owner_user_id=owner_user_id)

    if server_owned:
        asyncio.create_task(_process_server_message(message, user_context))
    else:
        JOB_STORE.mark_routed(message.job_id)
        await A2A_HUB.publish(
            A2AEvent(
                job_id=message.job_id,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                type=message.type,
                action=message.action,
                sender=message.sender,
                recipient=message.recipient,
                payload=message.payload,
            ),
            principal=principal,
            owner_user_id=owner_user_id,
        )

    return ack


async def _process_server_message(
    message: A2AMessage,
    user_context: Optional[UserContext] = None,
) -> None:
    JOB_STORE.mark_running(message.job_id)
    try:
        with observe_agent_pipeline(
            lambda event: JOB_STORE.append_progress_event(message.job_id, event.as_dict()),
            cancellation_check=lambda: JOB_STORE.is_cancelled(message.job_id),
        ):
            result = await call_forma_action(message.action, message.payload, user_context, message.job_id)
        generation_status = str(result.get("generation_status") or "succeeded").lower()
        if generation_status == "partial":
            JOB_STORE.mark_partial(message.job_id, result)
        elif generation_status == "failed":
            correlation_id = new_error_correlation_id()
            error_detail = api_error_detail(
                code="generation_root_failed",
                message="A required root generation stage failed.",
                job_id=message.job_id,
                correlation_id=correlation_id,
                public=True,
            )
            JOB_STORE.mark_failed(
                message.job_id,
                error_detail["message"],
                error_code=error_detail["code"],
                correlation_id=correlation_id,
            )
        else:
            JOB_STORE.mark_succeeded(message.job_id, result)
        transport_result = redact_error_value(result) if generation_status in {"partial", "failed"} else result
        event_type = "error" if generation_status == "failed" else "result"
        event_payload = (
            {"error": error_detail, "result": transport_result}
            if generation_status == "failed"
            else transport_result
        )
        event = A2AEvent(
            job_id=message.job_id,
            message_id=message.message_id,
            correlation_id=error_detail["correlation_id"] if generation_status == "failed" else message.correlation_id,
            type=event_type,
            action=message.action,
            sender=FORMA_AGENT_ID,
            recipient=message.sender,
            payload=event_payload,
        )
    except Exception as exc:
        correlation_id = new_error_correlation_id()
        error_detail = api_error_detail(
            code="a2a_action_failed",
            message="A2A action failed.",
            job_id=message.job_id,
            correlation_id=correlation_id,
            public=True,
        )
        log_exception(
            logger,
            "A2A action failed",
            exc,
            correlation_id=correlation_id,
            context={"action": message.action, "job_id": message.job_id, "payload": message.payload},
        )
        JOB_STORE.mark_failed(
            message.job_id,
            error_detail["message"],
            exception_debug_payload(exc, correlation_id=correlation_id),
            error_code=error_detail["code"],
            correlation_id=correlation_id,
        )
        event = A2AEvent(
            job_id=message.job_id,
            message_id=message.message_id,
            correlation_id=correlation_id,
            type="error",
            action=message.action,
            sender=FORMA_AGENT_ID,
            recipient=message.sender,
            payload={"error": error_detail},
        )

    await A2A_HUB.publish(
        event,
        principal=a2a_principal_for_user(user_context),
        owner_user_id=_context_owner_user_id(user_context),
    )


async def handle_a2a_websocket(
    websocket: WebSocket,
    agent_id: str,
    user_context: Optional[UserContext] = None,
) -> None:
    if user_context is None or not user_context.is_authenticated:
        await websocket.close(code=4401, reason="Authentication is required for A2A transports.")
        return

    principal = a2a_principal_for_user(user_context)
    owner_user_id = _context_owner_user_id(user_context)
    try:
        await A2A_HUB.register(
            agent_id,
            {"transports": ["websocket"]},
            principal=principal,
            owner_user_id=_context_owner_user_id(user_context),
        )
    except PermissionError:
        await websocket.close(code=4403, reason="The agent is owned by another principal.")
        return

    await websocket.accept()

    sender_task = asyncio.create_task(_websocket_sender(websocket, agent_id, principal))
    try:
        await A2A_HUB.publish(
            A2AEvent(
                type="ready",
                action="a2a.connected",
                sender=FORMA_AGENT_ID,
                recipient=agent_id,
                payload=get_a2a_capabilities(),
            ),
            principal=principal,
            owner_user_id=owner_user_id,
        )
        while True:
            raw_message = await websocket.receive_json()
            if isinstance(raw_message, dict) and raw_message.get("jsonrpc") == "2.0":
                if not mcp_context_is_authorized(user_context):
                    if "id" in raw_message:
                        await websocket.send_json(
                            _jsonrpc_error(
                                raw_message.get("id"),
                                -32003,
                                "You are not authorized to use this MCP tool.",
                                error_code="authorization_required",
                            )
                        )
                    continue
                response = await handle_mcp_json_rpc(raw_message, user_context)
                if response is not None:
                    await websocket.send_json(response)
                continue

            if not isinstance(raw_message, dict):
                raise ValueError("A2A messages must be JSON objects.")
            supplied_sender = raw_message.get("sender")
            if supplied_sender and supplied_sender != agent_id:
                raise PermissionError("The WebSocket sender must match its authenticated agent.")
            raw_message = {**raw_message, "sender": agent_id}
            await submit_a2a_message(A2AMessage.model_validate(raw_message), user_context)
    except WebSocketDisconnect:
        logger.info("A2A websocket disconnected: %s", agent_id)
    except PermissionError as exc:
        correlation_id = new_error_correlation_id()
        error_detail = api_error_detail(
            code="authorization_required",
            message="You are not authorized to use this A2A connection.",
            correlation_id=correlation_id,
            public=True,
        )
        log_exception(
            logger,
            "A2A WebSocket authorization failed",
            exc,
            correlation_id=correlation_id,
            context={"agent_id": agent_id},
            level=logging.WARNING,
        )
        await websocket.send_json({"type": "error", "error": error_detail})
    except Exception as exc:
        correlation_id = new_error_correlation_id()
        error_detail = api_error_detail(
            code="a2a_protocol_error",
            message="The A2A message could not be processed.",
            correlation_id=correlation_id,
            public=True,
        )
        log_exception(
            logger,
            "A2A WebSocket message failed",
            exc,
            correlation_id=correlation_id,
            context={"agent_id": agent_id},
        )
        await websocket.send_json({"type": "error", "error": error_detail})
    finally:
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task


async def _websocket_sender(
    websocket: WebSocket,
    agent_id: str,
    principal: Optional[str],
) -> None:
    while True:
        events = await A2A_HUB.poll(
            agent_id,
            timeout=30.0,
            limit=10,
            principal=principal,
            create_if_missing=False,
        )
        for event in events:
            await websocket.send_json(event.model_dump())


_tcp_server: Optional[asyncio.AbstractServer] = None
_tcp_auth_policy: Optional[bool] = None


async def start_a2a_tcp_server() -> Optional[asyncio.AbstractServer]:
    global _tcp_auth_policy, _tcp_server
    if _tcp_server is not None or not _env_bool("A2A_SOCKET_ENABLED", default=False):
        return _tcp_server

    host = config.get("A2A_SOCKET_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(config.get("A2A_SOCKET_PORT", "8766"))
    auth_required = _tcp_authentication_required(host)
    if auth_required and not a2a_service_credentials_configured():
        logger.error(
            "A2A TCP JSONL transport is disabled because it is not bound to an "
            "explicitly local loopback runtime and no A2A service credential is configured."
        )
        return None
    _tcp_auth_policy = auth_required
    try:
        _tcp_server = await asyncio.start_server(
            _handle_tcp_client,
            host,
            port,
            limit=TCP_MAX_LINE_BYTES,
        )
    except Exception:
        _tcp_auth_policy = None
        raise
    logger.info("A2A TCP JSONL socket listening on %s:%s", host, port)
    return _tcp_server


async def stop_a2a_tcp_server() -> None:
    global _tcp_auth_policy, _tcp_server
    if _tcp_server is None:
        _tcp_auth_policy = None
        return
    _tcp_server.close()
    await _tcp_server.wait_closed()
    _tcp_server = None
    _tcp_auth_policy = None


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _tcp_authentication_required(host: str) -> bool:
    return not (a2a_local_development_allowed() and _is_loopback_host(host))


async def _send_tcp_error(writer: asyncio.StreamWriter, code: str, message: str) -> None:
    correlation_id = new_error_correlation_id()
    error_detail = api_error_detail(
        code=code,
        message=message,
        correlation_id=correlation_id,
        public=True,
    )
    writer.write(json.dumps({"type": "error", "error": error_detail}).encode("utf-8") + b"\n")
    with contextlib.suppress(Exception):
        await writer.drain()


async def _authenticate_tcp_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> Optional[Tuple[Optional[str], UserContext]]:
    auth_required = _tcp_auth_policy
    host = config.get("A2A_SOCKET_HOST", "127.0.0.1") or "127.0.0.1"
    if auth_required is None:
        auth_required = _tcp_authentication_required(host)
    if not auth_required:
        return (
            None,
            UserContext(
                provider="local",
                subject=LOCAL_USER_ID,
                owner_user_id=LOCAL_USER_ID,
                is_authenticated=True,
                is_admin=True,
            ),
        )

    try:
        line = await asyncio.wait_for(reader.readline(), timeout=TCP_AUTH_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, ValueError):
        await _send_tcp_error(
            writer,
            "a2a_authentication_required",
            "The first TCP message must authenticate the connection.",
        )
        return None
    if not line:
        return None
    if len(line) > TCP_MAX_LINE_BYTES:
        await _send_tcp_error(writer, "a2a_invalid_request", "The TCP message is too large.")
        return None
    try:
        envelope = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await _send_tcp_error(
            writer,
            "a2a_authentication_required",
            "The first TCP message must authenticate the connection.",
        )
        return None

    if not isinstance(envelope, dict) or (
        envelope.get("type") not in {"auth", "authenticate"}
        and envelope.get("action") not in {"a2a.authenticate", "authenticate"}
    ):
        await _send_tcp_error(
            writer,
            "a2a_authentication_required",
            "The first TCP message must authenticate the connection.",
        )
        return None

    nested_payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    token = (
        envelope.get("token")
        or envelope.get("api_key")
        or envelope.get("authorization")
        or nested_payload.get("token")
        or nested_payload.get("api_key")
        or nested_payload.get("authorization")
    )
    context = resolve_a2a_service_context(token if isinstance(token, str) else None)
    if context is None:
        await _send_tcp_error(
            writer,
            "authorization_required",
            "The TCP A2A credential is invalid or missing.",
        )
        return None

    requested_agent_id = (
        envelope.get("agent_id")
        or envelope.get("agentId")
        or nested_payload.get("agent_id")
        or nested_payload.get("agentId")
    )
    agent_id = str(requested_agent_id or f"tcp_{uuid.uuid4().hex[:12]}").strip()
    if not agent_id or len(agent_id) > TCP_MAX_AGENT_ID_LENGTH:
        await _send_tcp_error(writer, "a2a_invalid_request", "The TCP agent id is invalid.")
        return None
    return agent_id, context


async def _handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    authenticated = await _authenticate_tcp_client(reader, writer)
    if authenticated is None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    agent_id, user_context = authenticated
    principal = a2a_principal_for_user(user_context)
    owner_user_id = _context_owner_user_id(user_context)
    sender_task: Optional[asyncio.Task[None]] = None

    async def establish_agent(normalized_agent_id: str) -> None:
        nonlocal agent_id, sender_task
        await A2A_HUB.register(
            normalized_agent_id,
            {"transports": ["tcp_jsonl"], "metadata": {"peer": str(peer)}},
            principal=principal,
            owner_user_id=owner_user_id,
        )
        agent_id = normalized_agent_id
        sender_task = asyncio.create_task(_tcp_sender(writer, normalized_agent_id, principal))
        await A2A_HUB.publish(
            A2AEvent(
                type="ready",
                action="a2a.connected",
                sender=FORMA_AGENT_ID,
                recipient=normalized_agent_id,
                payload={**get_a2a_capabilities(), "connection_agent_id": normalized_agent_id},
            ),
            principal=principal,
            owner_user_id=owner_user_id,
        )

    try:
        if agent_id is not None:
            try:
                await establish_agent(agent_id)
            except PermissionError:
                await _send_tcp_error(writer, "authorization_required", "The agent is owned by another principal.")
                return
        while not reader.at_eof():
            try:
                line = await reader.readline()
            except ValueError:
                await _send_tcp_error(writer, "a2a_invalid_request", "The TCP message is too large.")
                break
            if not line:
                break
            try:
                raw_message = json.loads(line.decode("utf-8"))
                if not isinstance(raw_message, dict):
                    raise ValueError("A2A messages must be JSON objects.")
                supplied_sender = raw_message.get("sender")
                if agent_id is None:
                    await establish_agent(
                        _normalize_message_agent_id(supplied_sender or "anonymous", "sender")
                    )
                elif supplied_sender and supplied_sender != agent_id:
                    raise PermissionError("The TCP sender must match its authenticated agent.")
                raw_message = {**raw_message, "sender": agent_id}
                await submit_a2a_message(A2AMessage.model_validate(raw_message), user_context)
            except Exception as exc:
                correlation_id = new_error_correlation_id()
                error_code = "authorization_required" if isinstance(exc, PermissionError) else "a2a_protocol_error"
                error_message = (
                    "You are not authorized to use this A2A connection."
                    if isinstance(exc, PermissionError)
                    else "The A2A message could not be processed."
                )
                error_detail = api_error_detail(
                    code=error_code,
                    message=error_message,
                    correlation_id=correlation_id,
                    public=True,
                )
                log_exception(
                    logger,
                    "A2A TCP message failed",
                    exc,
                    correlation_id=correlation_id,
                    context={"agent_id": agent_id},
                )
                writer.write(json.dumps({"type": "error", "error": error_detail}).encode("utf-8") + b"\n")
                await writer.drain()
    finally:
        if sender_task is not None:
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
        writer.close()
        await writer.wait_closed()


async def _tcp_sender(
    writer: asyncio.StreamWriter,
    agent_id: str,
    principal: Optional[str],
) -> None:
    while not writer.is_closing():
        events = await A2A_HUB.poll(
            agent_id,
            timeout=30.0,
            limit=10,
            principal=principal,
            create_if_missing=False,
        )
        for event in events:
            writer.write(json.dumps(event.model_dump()).encode("utf-8") + b"\n")
            await writer.drain()


def _jsonrpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Optional[Any] = None,
    *,
    error_code: str = "mcp_request_failed",
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    detail = api_error_detail(
        code=error_code,
        message=message,
        correlation_id=correlation_id or new_error_correlation_id(),
        public=True,
    )
    error: Dict[str, Any] = {"code": code, "message": detail["message"]}
    error_data: Dict[str, Any] = {
        "code": detail["code"],
        "correlation_id": detail["correlation_id"],
    }
    if data is not None:
        error_data["details"] = redact_error_value(data)
    error["data"] = error_data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _mcp_tool_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "structuredContent": result,
    }


def _mcp_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "forma.compile_project",
            "description": (
                "Normalize, electrically validate, and render host-agent-authored Forma Hardware IR "
                "without invoking a server-side LLM, then persist the result for gallery/workspace use."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_ir": {
                        "type": "object",
                        "description": "Forma Hardware IR authored by the calling agent.",
                    },
                    "project_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Optional existing project UUID to update when owned by the caller.",
                    },
                    "authoring_agent": {
                        "type": "string",
                        "enum": ["openclaw", "opencode", "nemoclaw", "claude", "codex", "other"],
                        "default": "other",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional source prompt stored with the project.",
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["public", "private"],
                        "default": "public",
                    },
                },
                "required": ["project_ir"],
            },
        },
        {
            "name": "forma.generate_project",
            "description": "Generate a Forma Hardware IR package, Mermaid diagram, and SVG schematic.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "project_id": {
                        "type": "string",
                        "description": "Existing project ID when retrying a failed generation stage.",
                    },
                    "retry_stage": {
                        "type": "string",
                        "description": "Failed generation stage to retry while reusing successful artifacts.",
                    },
                    "workflow": {
                        "type": "string",
                        "enum": ["default", "web_research"],
                        "default": "default",
                    },
                    "image_data": {"type": "string", "description": "Optional data URL or base64 image"},
                    "generate_image": {"type": "boolean", "default": False},
                    "generation_mode": {
                        "type": "string",
                        "enum": ["regular", "progressive"],
                        "default": "regular",
                        "description": "Regular runs one-shot generation; progressive uses the staged hierarchical lifecycle.",
                    },
                    "external_source_provider": {
                        "type": "string",
                        "enum": ["firecrawl"],
                        "description": "Optional provider for web_research workflow.",
                    },
                    "data_sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["past_jobs"]},
                        "description": "Optional lightweight context sources.",
                    },
                    "past_jobs_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "default": 3,
                    },
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "forma.debug_config",
            "description": "Return configured LLM provider and model resolution details.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "forma.validate_circuit",
            "description": "Validate a list of components and nets against Forma electrical rules.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "components": {"type": "array", "items": {"type": "object"}},
                    "nets": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["components", "nets"],
            },
        },
        {
            "name": "forma.a2a.send_message",
            "description": "Send an A2A message through the Forma in-memory broker.",
            "inputSchema": {"type": "object", "properties": A2AMessage.model_json_schema()["properties"]},
        },
        {
            "name": "forma.a2a.poll_events",
            "description": "Long-poll queued A2A events for an agent id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "timeout": {"type": "number", "minimum": 0, "maximum": 60, "default": 25},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "forma.a2a.get_job",
            "description": "Fetch persisted metadata for one A2A job.",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
        {
            "name": "forma.a2a.list_jobs",
            "description": "List persisted A2A job metadata.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "forma.lattice.list_agents",
            "description": "List Lattice domain-agent cards registered with Forma.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Optional namespace filter, such as product.mech."},
                    "domain": {"type": "string", "description": "Optional domain text filter."},
                    "capability": {"type": "string", "description": "Optional capability id or label filter."},
                    "tool": {"type": "string", "description": "Optional needed-tool text filter."},
                },
            },
        },
        {
            "name": "forma.lattice.get_agent_card",
            "description": "Fetch one Lattice domain-agent card by agent id.",
            "inputSchema": {
                "type": "object",
                "properties": {"agent_id": {"type": "string", "default": "fabricator"}},
                "required": ["agent_id"],
            },
        },
    ]


async def handle_mcp_json_rpc(payload: Any, user_context: Optional[UserContext] = None) -> Any:
    if isinstance(payload, list):
        if not payload:
            return _jsonrpc_error(
                None,
                -32600,
                "Invalid empty JSON-RPC batch.",
                error_code="mcp_invalid_request",
            )
        responses = [await _handle_mcp_request(item, user_context) for item in payload]
        filtered = [response for response in responses if response is not None]
        return filtered or None
    return await _handle_mcp_request(payload, user_context)


async def _handle_mcp_request(
    request: Dict[str, Any],
    user_context: Optional[UserContext] = None,
) -> Optional[Dict[str, Any]]:
    if (
        not isinstance(request, dict)
        or request.get("jsonrpc") != "2.0"
        or not isinstance(request.get("method"), str)
    ):
        return _jsonrpc_error(None, -32600, "Invalid JSON-RPC request.", error_code="mcp_invalid_request")

    is_notification = "id" not in request
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params")
    if params is None:
        params = {}
    correlation_id = new_error_correlation_id()

    # JSON-RPC notifications never receive a response. Forma currently has no
    # notification methods with server-side work beyond initialization.
    if is_notification:
        return None

    if not isinstance(params, dict):
        return _jsonrpc_error(
            request_id,
            -32602,
            "Request parameters are invalid.",
            error_code="mcp_invalid_params",
            correlation_id=correlation_id,
        )

    try:
        if method == "initialize":
            requested_version = params.get("protocolVersion")
            configured_version = config.get("MCP_PROTOCOL_VERSION", MCP_DEFAULT_PROTOCOL_VERSION)
            protocol_version = (
                requested_version
                if requested_version in MCP_PROTOCOL_VERSIONS
                else configured_version
                if configured_version in MCP_PROTOCOL_VERSIONS
                else MCP_DEFAULT_PROTOCOL_VERSION
            )
            return _jsonrpc_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "serverInfo": {"name": "forma-oss", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                },
            )

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return _jsonrpc_result(request_id, {})

        if method == "tools/list":
            return _jsonrpc_result(request_id, {"tools": _mcp_tools()})

        if method == "tools/call":
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise TypeError("tools/call requires a tool name.")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise TypeError("tools/call arguments must be an object.")
            result = await _call_mcp_tool(tool_name, arguments, user_context)
            return _jsonrpc_result(request_id, _mcp_tool_result(result))

        return _jsonrpc_error(
            request_id,
            -32601,
            "The requested MCP method was not found.",
            error_code="mcp_method_not_found",
            correlation_id=correlation_id,
        )
    except Exception as exc:
        log_exception(
            logger,
            "MCP request failed",
            exc,
            correlation_id=correlation_id,
            context={"method": method, "params": params},
        )
        if isinstance(exc, HostedChatUnavailableError):
            error_code = "hosted_chat_unavailable"
            rpc_code = -32004
            public_message = str(exc)
        elif isinstance(exc, PermissionError):
            error_code = "authorization_required"
            rpc_code = -32003
            public_message = "You are not authorized to use this MCP tool."
        elif isinstance(exc, (TypeError, KeyError)):
            error_code = "mcp_invalid_params"
            rpc_code = -32602
            public_message = "Request parameters are invalid."
        else:
            error_code = "mcp_tool_failed"
            rpc_code = -32000
            public_message = "The MCP request could not be completed."
        return _jsonrpc_error(
            request_id,
            rpc_code,
            public_message,
            error_code=error_code,
            correlation_id=correlation_id,
        )


def _persist_mcp_compile(
    project: HardwareIntermediateRepresentation,
    arguments: Dict[str, Any],
    user_context: Optional[UserContext],
) -> Dict[str, Any]:
    """Persist a host-authored compilation without trusting caller ownership."""
    metadata = dict(project.assembly_metadata or {})
    requested_project_id = arguments.get("project_id") or metadata.get("project_id")
    try:
        project_id = (
            str(uuid.UUID(str(requested_project_id).strip()))
            if requested_project_id
            else str(uuid.uuid4())
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("project_id must be a UUID when supplied.") from exc

    owner_user_id = _context_owner_user_id(user_context)
    existing = get_project_identity(project_id)
    if existing is not None:
        if existing.get("status", "active") != "active":
            raise ValueError("A deleted compiled project cannot be restored by recompiling.")
        existing_owner = str(existing.get("owner_user_id") or "").strip()
        if not owner_user_id or existing_owner != owner_user_id:
            raise ValueError("An existing compiled project can only be updated by its owner.")
        chat_id = str(existing.get("chat_id") or "").strip() or None
        try:
            existing_revision = get_latest_project_revision(project_id, owner_user_id)
        except ProjectStateError as exc:
            if exc.code != "project_revision_not_found":
                raise
            existing_revision = None
        existing_ir = existing_revision.state.model_dump(mode="json") if existing_revision is not None else {}
        existing_metadata = existing_ir.get("assembly_metadata", {}) if isinstance(existing_ir, dict) else {}
        revision = int(existing_metadata.get("compile_revision") or 1) + 1
        created_at = str(existing.get("created_at") or _utc_now())
    else:
        chat_id = str(uuid.uuid4()) if owner_user_id else None
        revision = 1
        created_at = _utc_now()

    title = str((project.overview.title if project.overview else "") or "").strip() or "Untitled Forma Project"
    prompt = str(arguments.get("prompt") or metadata.get("source_prompt") or title).strip()
    if arguments.get("authoring_agent") == "opencode" and existing is not None:
        # Rendering or revising a design must not replace its originating brief
        # with a tool-operation label or an agent-authored metadata value.
        original_prompt = next((text for value in (existing_metadata.get("source_prompt"), existing.get("prompt"))
                                if (text := str(value or "").strip()) and text != "OpenCode project"), "")
        if original_prompt:
            prompt = original_prompt
        elif prompt == "OpenCode project":
            prompt = title
    visibility = str(
        arguments.get("visibility")
        or (existing.get("visibility") if isinstance(existing, dict) else None)
        or "public"
    ).strip().lower()
    if visibility not in {"public", "private"}:
        raise ValueError("visibility must be public or private.")
    if not owner_user_id:
        visibility = "public"

    project.assembly_metadata = {
        **metadata,
        "project_id": project_id,
        "chat_id": chat_id,
        "authoring_agent": arguments.get("authoring_agent", "other"),
        "compiled_by": "forma.compile_project",
        "compile_revision": revision,
        "created_at": created_at,
        "source_prompt": prompt,
    }
    hardware_ir = project.model_dump(mode="json")
    source_job_id = str(arguments.get("source_job_id") or f"compile-{uuid.uuid4().hex}")
    if existing is not None:
        if not owner_user_id:
            raise ValueError("An authenticated owner is required to update a compiled project.")
        persist_chat_project_revision(
            project_id,
            owner_user_id,
            project,
            source_job_id=source_job_id,
            prompt=prompt,
            chat_id=chat_id,
            visibility=visibility,
        )
    elif owner_user_id:
        persist_chat_project_revision(
            project_id,
            owner_user_id,
            project,
            source_job_id=source_job_id,
            prompt=prompt,
            chat_id=chat_id,
            visibility=visibility,
        )
    else:
        persist_legacy_project_projection(
            project_id=project_id,
            title=title,
            prompt=prompt,
            hardware_ir=hardware_ir,
            created_at=created_at,
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            visibility=visibility,
        )
    return {
        "project_id": project_id,
        "chat_id": chat_id,
        "persisted": True,
        "visibility": visibility,
    }


def _require_mcp_a2a_principal(user_context: Optional[UserContext]) -> str:
    if user_context is None or not user_context.is_authenticated:
        raise PermissionError("An authenticated context is required for A2A tools.")
    principal = a2a_principal_for_user(user_context)
    if not principal:
        raise PermissionError("An authenticated principal is required for A2A tools.")
    return principal


def _mcp_global_job_access(user_context: Optional[UserContext]) -> bool:
    return bool(
        user_context
        and user_context.is_admin
        and not user_context.provider.endswith("-api-key")
    )


def _mcp_job_is_accessible(
    job: Dict[str, Any],
    user_context: Optional[UserContext],
    *,
    principal: Optional[str] = None,
) -> bool:
    if _mcp_global_job_access(user_context):
        return True
    principal = principal or a2a_principal_for_user(user_context)
    payload = job.get("payload") if isinstance(job, dict) else None
    if not isinstance(payload, dict):
        return False
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    if owner_user_id and user_context and owner_user_id == user_context.owner_user_id:
        return True
    return bool(principal and payload.get("_forma_a2a_principal") == principal)


def _mcp_poll_bounds(arguments: Dict[str, Any]) -> Tuple[float, int]:
    try:
        timeout = float(arguments.get("timeout", 25))
        limit = int(arguments.get("limit", 10))
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("poll_events timeout and limit must be numeric.") from exc
    if not math.isfinite(timeout) or timeout < 0 or timeout > 60:
        raise TypeError("poll_events timeout must be between 0 and 60 seconds.")
    if limit < 1 or limit > 100:
        raise TypeError("poll_events limit must be between 1 and 100.")
    return timeout, limit


async def _call_mcp_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    user_context: Optional[UserContext] = None,
) -> Dict[str, Any]:
    if tool_name == "forma.compile_project":
        project = HardwareIntermediateRepresentation.model_validate(arguments.get("project_ir"))
        issues = validate_circuit(project.components, project.nets, project.requirements)
        project.validation = build_validation_summary(issues)
        project.is_valid = not project.validation.critical
        requested_project_id = arguments.get("project_id") or (project.assembly_metadata or {}).get("project_id")
        try:
            compile_project_id = (
                str(uuid.UUID(str(requested_project_id).strip()))
                if requested_project_id
                else str(uuid.uuid4())
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("project_id must be a UUID when supplied.") from exc
        ensure_native_cad_model(
            project,
            project_id=compile_project_id,
            required=False,
            authoring_agent=arguments.get("authoring_agent"),
            workflow="default",
        )
        persistence = _persist_mcp_compile(
            project,
            {**arguments, "project_id": compile_project_id},
            user_context,
        )
        return {
            **persistence,
            "project_ir": project.model_dump(mode="json"),
            "is_valid": project.is_valid,
            "validation": project.validation.model_dump(mode="json"),
            "mermaid_code": generate_mermaid_chart(project),
            "svg_schematic": generate_svg_schematic(project),
        }

    if tool_name == "forma.a2a.send_message":
        _require_mcp_a2a_principal(user_context)
        ack = await submit_a2a_message(A2AMessage.model_validate(arguments), user_context)
        return ack.model_dump()

    if tool_name == "forma.a2a.poll_events":
        principal = _require_mcp_a2a_principal(user_context)
        timeout, limit = _mcp_poll_bounds(arguments)
        try:
            events = await A2A_HUB.poll(
                arguments["agent_id"],
                timeout=timeout,
                limit=limit,
                principal=principal,
                create_if_missing=False,
            )
        except (KeyError, PermissionError) as exc:
            raise PermissionError("You are not authorized to poll this agent queue.") from exc
        return {"events": [event.model_dump() for event in events]}

    if tool_name == "forma.a2a.get_job":
        _require_mcp_a2a_principal(user_context)
        job = JOB_STORE.get_job(arguments["job_id"])
        if not job:
            raise ValueError("A2A job not found.")
        if not _mcp_job_is_accessible(job, user_context):
            raise PermissionError("You are not authorized to view this A2A job.")
        return job

    if tool_name == "forma.a2a.list_jobs":
        principal = _require_mcp_a2a_principal(user_context)
        requested_limit = int(arguments.get("limit", 50))
        if requested_limit < 1 or requested_limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        query_limit = requested_limit if _mcp_global_job_access(user_context) else 200
        jobs = JOB_STORE.list_jobs(
            sender=arguments.get("sender"),
            status=arguments.get("status"),
            limit=query_limit,
        )
        if not _mcp_global_job_access(user_context):
            jobs = [
                job
                for job in jobs
                if _mcp_job_is_accessible(job, user_context, principal=principal)
            ]
        return {"jobs": jobs[:requested_limit]}

    if tool_name == "forma.lattice.list_agents":
        registry = _lattice_registry()
        agents = registry.find(
            namespace=arguments.get("namespace"),
            domain=arguments.get("domain"),
            capability=arguments.get("capability"),
            tool=arguments.get("tool"),
        )
        return {
            "name": "Lattice",
            "agents": [agent.model_dump(mode="json") for agent in agents],
        }

    if tool_name == "forma.lattice.get_agent_card":
        registry = _lattice_registry()
        return {"agent": registry.get(arguments.get("agent_id", "fabricator")).model_dump(mode="json")}

    return await call_forma_action(tool_name, arguments, user_context)
