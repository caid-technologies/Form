from pathlib import Path
from datetime import datetime, timedelta, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import sys
import types
import urllib.parse
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4


def _ensure_api_package_imports() -> None:
    """Support Vercel loading apps/api/main.py as top-level main.py."""
    if "apps.api" in sys.modules:
        return

    current_dir = Path(__file__).resolve().parent
    if not (current_dir / "database.py").exists():
        return

    project_root = current_dir.parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    apps_package = types.ModuleType("apps")
    apps_package.__path__ = [str(current_dir.parent)]
    apps_package.__file__ = str(current_dir.parent / "__init__.py")
    apps_package.__package__ = "apps"
    sys.modules["apps"] = apps_package

    api_package = types.ModuleType("apps.api")
    api_package.__path__ = [str(current_dir)]
    api_package.__file__ = str(current_dir / "__init__.py")
    api_package.__package__ = "apps.api"
    sys.modules["apps.api"] = api_package
    sys.modules.setdefault("apps.api.main", sys.modules[__name__])
    setattr(apps_package, "api", api_package)
    setattr(api_package, "main", sys.modules[__name__])


_ensure_api_package_imports()

from forma_core.debug import (
    api_error_detail,
    debug_mode_enabled,
    exception_debug_payload,
    get_debug_mode_config,
    runtime_safe_error_message,
)
from forma_core._version import __version__
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from forma_core.user_integrations import UserIntegrationStore, require_user_secrets_key, resolve_user_integration_settings, ResolvedIntegrationSettings
from forma_core.vertex_auth import VercelOidcContextMiddleware

from apps.api.logging_config import configure_backend_logging

configure_backend_logging()

from forma_core.database import (
    append_project_revision,
    DesignBriefAccessError,
    DesignBriefNotFoundError,
    count_component_templates,
    delete_project_chat,
    ensure_project_action_allowed,
    get_database_config,
    get_latest_design_brief,
    get_latest_project_revision,
    get_latest_project_deletion_audit,
    get_project_contribution_consent,
    get_project_chat,
    get_project_identity,
    init_db,
    list_project_chats,
    list_component_templates,
    list_project_gallery_inventory,
    list_project_gallery_inventory_page,
    list_project_identities,
    list_latest_project_revisions,
    update_project_deletion_state,
    update_project_identity,
    list_project_deletion_audits,
    list_project_generation_jobs,
    project_engagement_for_ids,
    refresh_legacy_project_projection,
    ensure_chat_project,
    persist_chat_project_revision,
    resolve_project_for_read,
    remix_generated_project,
    save_alpha_signup,
    save_project_for_user,
    unsave_project_for_user,
    publish_project_revision,
    upsert_project_chat,
)
from forma_core.project_list_cache import (
    cache_project_list,
    cache_project_page,
    get_cached_project_list,
    get_cached_project_page,
    require_project_list_cache_config,
)
from apps.api.seed_db import seed_database
from forma_core.agents.workflows import get_workflow_debug_config, list_workflows
from forma_core.agents.clarification import ask_clarifying_questions
from forma_core.workspaces.chats.models import Chat, ChatUpsertRequest, ProjectChatUpsertRequest
from forma_core.workspaces.projects.models import (
    ClarifyingQuestionsRequest, ClarifyingQuestionsResponse, ComponentInstance,
    ConnectionNet, GenerateProjectRequest, HardwareIntermediateRepresentation, IterateProjectRequest,
    ProjectContributionConsentRequest, ProjectDetail, ProjectIdentityResponse, ProjectUpdateRequest, ProjectSummary, ValidationIssue, ValidationReport, VideoSelfCorrectRequest,
)
from forma_core.workspaces.projects import ProjectReadError, ProjectRevision, ProjectStateError
from forma_core.workspaces.projects.manifest import ProjectManifest
from forma_core.workspaces.workflow import WorkflowStateError
from forma_core.signups.models import AlphaSignupRequest, AlphaSignupResponse
from forma_core.agents.orchestrator import HardwarePipelineOrchestrator
from apps.api.a2a import (
    A2A_HUB,
    A2AAgentRegistration,
    A2AMessage,
    a2a_principal_for_user,
    build_generation_response,
    get_a2a_capabilities,
    handle_a2a_websocket,
    handle_mcp_json_rpc,
    start_a2a_tcp_server,
    stop_a2a_tcp_server,
    submit_a2a_message,
)
from forma_core.images import get_image_output_debug_config
from forma_core.config.contract import resolve_runtime_contract
from forma_core.workspaces.projects.iteration import ProjectIterator
from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.workspaces.projects.design_lifecycle import (
    VisualApprovalStatus,
    load_design_lifecycle,
    record_visual_decision,
)
from forma_core.workspaces.projects.generation_mode import is_progressive_generation
from forma_core.llm import LLMProviderConfigError
from forma_core.llm import LLMProviderOutputError
from forma_core.debug import api_error_detail, log_exception, new_error_correlation_id
from forma_core.workspaces.projects.objects import build_project_object, list_project_namespaces
from forma_core.agents.pipeline import PipelineCancelledError, list_agent_pipeline_steps, observe_agent_pipeline, pipeline_workflow_id
from forma_core.video_prompts import generate_image_to_video_prompt_from_namespaces
from forma_core.agents.video_correction import FireworksVideoSelfCorrectionAgent
from forma_core.video_review import FireworksVideoReviewClient
from apps.api.logs_api import router as logs_router
from apps.api.streams_api import router as streams_router
from apps.api.design_briefs_api import router as design_briefs_router
from apps.api.context_gathering_api import router as context_gathering_router
from apps.api.project_workflow_api import router as project_workflow_router
from apps.api.project_history_api import router as project_history_router
from apps.api.readiness_api import router as readiness_router
from forma_core.workspaces.projects.outcomes import evaluate_design_outcome
from apps.api.worker_plans_api import router as worker_plans_router
from apps.api.user_integrations_api import router as user_integrations_router
from apps.api.user_settings_api import router as user_settings_router
from apps.api.cli_auth_api import router as cli_auth_router
from apps.api.cli_projects_api import router as cli_projects_router
from apps.api.cli_credentials_api import router as cli_credentials_router
from apps.api.compatibility import require_client_compatibility
from apps.api.version_api import router as version_router
from apps.api.hosted_chat import require_hosted_chat_enabled
from apps.api.opencode_api import router as opencode_router
from apps.api.auth import (
    UserContext,
    require_a2a_admin_user_context,
    require_a2a_user_context,
    require_a2a_websocket_context,
    clerk_user_profile,
    deployed_auth_required,
    has_opencode_authoring_access,
    optional_user_context,
    require_admin_user_context,
    require_mcp_user_context,
    require_destructive_user_context,
    require_user_context,
)
from apps.api.security import SecurityLimitsMiddleware, cors_origins, security_config
from apps.api.project_deletion import (
    DELETION_POLICY_VERSION,
    PERMITTED_CONTRIBUTION_PURPOSES,
    deletion_metrics,
    grant_contribution_consent,
    purge_worker,
    request_project_deletion,
    restore_project,
    withdraw_contribution,
)
from forma_core.jobs.store import JOB_STORE, JobCancelledError
from forma_core.jobs.context import PAST_JOBS_DATA_SOURCE, PastJobContextSource, list_generation_data_sources
from forma_core.observability import flush_langfuse, get_langfuse_debug_config
from forma_core.runtime import (
    ALPHA_GENERATION_UNAVAILABLE_MESSAGE,
    AlphaGenerationUnavailableError,
    HostedChatUnavailableError,
    deployment_runtime_config,
    generation_unavailable_detail,
)
from forma_core.config.runtime import forma_dev_mode_enabled, validate_runtime_configuration
from apps.api.storage import get_image_storage_config, hydrate_image_storage_metadata
from forma_core.validation import validate_circuit
from forma_core.utils import generate_mermaid_chart, generate_svg_schematic
from apps.api.video_providers import (
    GMICloudProvider,
    VIDEO_MODE_IMAGE_TO_VIDEO,
    VIDEO_MODE_VIDEO_TO_VIDEO,
    get_available_video_aspect_ratios,
    get_available_video_model_options,
    get_available_video_models,
    get_default_video_model,
    normalize_video_aspect_ratio,
    normalize_video_mode,
)
from apps.api.video_storage import (
    ensure_video_storage_configured,
    get_video_storage_config,
    list_project_videos,
    upload_generated_videos_to_s3,
)

logger = logging.getLogger(__name__)
ROOT_DIR = REPO_ROOT
EXAMPLE_RESULTS_DIR = ROOT_DIR / "examples" / "results"
_CACHE_OWNER_DIGEST_FIELD = "_forma_cache_owner_digest"
_CACHE_OWNER_CHAT_FIELD = "_forma_cache_owner_chat_id"


def _parse_job_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _generation_duration_seconds(job: Optional[Dict[str, Any]]) -> Optional[int]:
    if not job:
        return None
    started_at = _parse_job_timestamp(job.get("started_at"))
    completed_at = _parse_job_timestamp(job.get("completed_at"))
    if not started_at or not completed_at or completed_at < started_at:
        return None
    return max(1, round((completed_at - started_at).total_seconds()))


def _attach_generation_timing_metadata(response: Dict[str, Any], job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    duration_seconds = _generation_duration_seconds(job)
    if duration_seconds is None:
        return response

    project_ir = response.get("project_ir")
    if not isinstance(project_ir, dict):
        return response

    metadata = project_ir.get("assembly_metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    project_ir["assembly_metadata"] = {
        **metadata,
        "total_generation_time_seconds": duration_seconds,
        "total_generation_started_at": job.get("started_at") if job else None,
        "total_generation_completed_at": job.get("completed_at") if job else None,
    }
    return response

app = FastAPI(
    title="Forma Open-Source API",
    description="AI-native prompt-to-hardware compilation, validation, and design generation platform.",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_oauth2_redirect_url="/docs/oauth2-redirect",
)


@app.exception_handler(RequestValidationError)
async def transport_validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path not in {
        "/a2a/messages",
        "/mcp",
        "/a2a/mcp",
        "/api/a2a/messages",
        "/api/mcp",
        "/api/a2a/mcp",
    }:
        return await request_validation_exception_handler(request, exc)
    correlation_id = new_error_correlation_id()
    logger.warning(
        "Transport request validation failed: path=%s correlation_id=%s",
        request.url.path,
        correlation_id,
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": api_error_detail(
                code="request_validation_failed",
                message="The request failed validation.",
                correlation_id=correlation_id,
                public=True,
            )
        },
    )

_project_purge_stop_event: Optional[asyncio.Event] = None
_project_purge_task: Optional[asyncio.Task[Any]] = None


class ApiPrefixCompatibilityMiddleware:
    """Accept /api-prefixed requests when the service receives the full public path."""

    def __init__(self, app: Any, prefix: str = "/api") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"}:
            path = scope.get("path", "")
            if path == self.prefix:
                scope = dict(scope)
                scope["path"] = "/"
                scope["root_path"] = f"{scope.get('root_path', '').rstrip('/')}{self.prefix}"
            elif path.startswith(f"{self.prefix}/"):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):] or "/"
                scope["root_path"] = f"{scope.get('root_path', '').rstrip('/')}{self.prefix}"

        await self.app(scope, receive, send)


app.add_middleware(ApiPrefixCompatibilityMiddleware)
app.add_middleware(VercelOidcContextMiddleware)
app.add_middleware(SecurityLimitsMiddleware)

# Credentialed CORS must be explicit. Hosted deployments with no configured
# origins remain API-only instead of accidentally exposing browser credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
)

app.include_router(logs_router, dependencies=[Depends(require_admin_user_context)])
app.include_router(streams_router, dependencies=[Depends(require_admin_user_context)])
app.include_router(design_briefs_router)
app.include_router(context_gathering_router)
app.include_router(project_workflow_router)
app.include_router(project_history_router)
app.include_router(readiness_router)
app.include_router(worker_plans_router)
app.include_router(user_integrations_router)
app.include_router(user_settings_router)
app.include_router(cli_auth_router, dependencies=[Depends(require_client_compatibility)])
app.include_router(cli_projects_router, dependencies=[Depends(require_client_compatibility)])
app.include_router(cli_credentials_router, dependencies=[Depends(require_client_compatibility)])
app.include_router(version_router)
app.include_router(opencode_router)


def _deployment_runtime_config(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    return deployment_runtime_config(llm_config, signup_storage=get_database_config()["client"])


def _resolved_client_runtime_config(
    settings: Optional[ResolvedIntegrationSettings] = None,
    user: Optional[UserContext] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    llm_config = HardwarePipelineOrchestrator(settings=settings).get_debug_config()
    image_config = get_image_output_debug_config(settings=settings)
    contract = resolve_runtime_contract(
        llm_config=llm_config,
        image_config=image_config,
        workflows=list_workflows(),
        signup_storage=get_database_config()["client"],
        settings=settings,
        authoring_access=has_opencode_authoring_access(user),
    )
    contract["video"] = {
        "generation": GMICloudProvider(settings=settings).get_debug_config(),
        "self_correction": FireworksVideoReviewClient().get_debug_config(),
    }
    return llm_config, contract


def _runtime_config_settings(user: Optional[UserContext]) -> Optional[ResolvedIntegrationSettings]:
    """Keep signed-in authoring available when stale BYOK data is unreadable."""
    try:
        return _resolve_user_integrations(user)
    except RuntimeError as exc:
        if not has_opencode_authoring_access(user):
            raise
        logger.warning(
            "OpenCode runtime config is using deployment defaults after user settings failed: %s",
            exc,
        )
        return None


def _resolve_user_integrations(user: Optional[UserContext]) -> ResolvedIntegrationSettings:
    """Snapshot provider settings for this request without mutating the process environment."""
    if user is None or user.provider == "local":
        return resolve_user_integration_settings()
    if user.owner_user_id:
        return resolve_user_integration_settings(UserIntegrationStore.for_user(user.owner_user_id))
    return resolve_user_integration_settings()


def _apply_user_integrations(user: UserContext) -> ResolvedIntegrationSettings:
    """Compatibility shim for callers that previously requested environment mutation."""
    return _resolve_user_integrations(user)


def _job_owner_user_id(job: Optional[Dict[str, Any]]) -> Optional[str]:
    payload = job.get("payload") if isinstance(job, dict) else None
    value = payload.get("owner_user_id") if isinstance(payload, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _job_a2a_principal(job: Optional[Dict[str, Any]]) -> Optional[str]:
    payload = job.get("payload") if isinstance(job, dict) else None
    value = payload.get("_forma_a2a_principal") if isinstance(payload, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _admin_job_records(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add admin-only owner labels without changing the persisted job shape."""
    records = [dict(job) for job in jobs]
    owner_user_ids: List[str] = []

    for record in records:
        owner_user_id = _job_owner_user_id(record)
        record["owner_user_id"] = owner_user_id
        if owner_user_id and owner_user_id not in owner_user_ids:
            owner_user_ids.append(owner_user_id)

    # Clerk lookups are optional display enrichment. Cap and parallelize cold
    # lookups so a large job list cannot hold the admin request open.
    profile_user_ids = owner_user_ids[:16]
    if profile_user_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(profile_user_ids))) as executor:
            resolved_profiles = executor.map(clerk_user_profile, profile_user_ids)
            profiles = dict(zip(profile_user_ids, resolved_profiles))
    else:
        profiles = {}

    for record in records:
        owner_user_id = record.get("owner_user_id")
        profile = profiles.get(owner_user_id) if isinstance(owner_user_id, str) else None
        display_name = profile.get("display_name") if profile else None
        email = profile.get("email") if profile else None
        github_username = profile.get("github_username") if profile else None
        record["owner_display_name"] = display_name
        record["owner_email"] = email
        record["owner_github_username"] = github_username
        record["owner_username"] = github_username or email or display_name or owner_user_id

    return records


def _require_job_reader(job: Dict[str, Any], user: UserContext) -> None:
    # API-key service principals are deliberately scoped like user principals;
    # only a Clerk/local administrator gets the global job view.
    if user.is_admin and not user.provider.endswith("-api-key"):
        return
    owner_user_id = _job_owner_user_id(job)
    if owner_user_id and owner_user_id == user.owner_user_id:
        return
    if _job_a2a_principal(job) == a2a_principal_for_user(user):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own jobs.")


def _delete_cancelled_generation_projects(job_id: str, job: Optional[Dict[str, Any]] = None) -> None:
    """Remove project artifacts persisted by a worker after its job was cancelled."""
    current_job = job or JOB_STORE.get_job(job_id) or {}
    owner_user_id = _job_owner_user_id(current_job)
    if not owner_user_id:
        return
    deleted_chats: List[tuple[str, str]] = []
    for revision in list_latest_project_revisions(owner_user_id):
        project_id = str(revision.project_id)
        hardware_ir = revision.state.model_dump(mode="json")
        if not isinstance(hardware_ir, dict) or not isinstance(project_id, str):
            continue
        metadata = hardware_ir.get("assembly_metadata")
        if not isinstance(metadata, dict) or metadata.get("frontend_job_id") != job_id:
            continue
        deleted = update_project_deletion_state(
            project_id,
            owner_user_id=owner_user_id,
            allowed_statuses=["active"],
            updates={
                "status": "deletion_pending",
                "deleted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "deletion_requested_by": owner_user_id,
            },
        )
        if deleted:
            chat_id = getattr(deleted, "chat_id", None)
            title = getattr(deleted, "title", None)
            if isinstance(chat_id, str) and isinstance(title, str):
                deleted_chats.append((chat_id, title))
            logger.info("Removed project %s created after cancelled job %s.", project_id, job_id)

    payload = current_job.get("payload") if isinstance(current_job.get("payload"), dict) else {}
    prompt_title = str(payload.get("prompt") or "").splitlines()[0].strip()[:80] or "Cancelled project"
    for chat_id, deleted_title in deleted_chats:
        chat = get_project_chat(chat_id, owner_user_id)
        if not chat or getattr(chat, "title", None) != deleted_title:
            continue
        upsert_project_chat(
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            title=prompt_title,
            messages=getattr(chat, "messages", None) or [],
            created_at=getattr(chat, "created_at", None) or datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )


# Initialize and seed database on startup
@app.on_event("startup")
async def startup_event():
    global _project_purge_stop_event, _project_purge_task
    logger.info("Starting up Forma server...")
    validate_runtime_configuration()
    require_user_secrets_key()
    require_project_list_cache_config()
    logger.info("Authentication mode: %s", "clerk" if deployed_auth_required() else "local")
    try:
        init_db()
        count = count_component_templates()
        if count == 0:
            logger.info("Database empty. Seeding templates automatically...")
            seed_database()
        else:
            logger.info("Database ready with %s component templates.", count)
    except Exception as e:
        logger.exception("Error during database startup: %s", e)
        raise
    await start_a2a_tcp_server()
    _project_purge_stop_event = asyncio.Event()
    _project_purge_task = asyncio.create_task(
        purge_worker(_project_purge_stop_event),
        name="project-retention-purge-worker",
    )


@app.on_event("shutdown")
async def shutdown_event():
    global _project_purge_stop_event, _project_purge_task
    if _project_purge_stop_event is not None:
        _project_purge_stop_event.set()
    if _project_purge_task is not None:
        await _project_purge_task
    _project_purge_stop_event = None
    _project_purge_task = None
    await stop_a2a_tcp_server()
    flush_langfuse()

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "Forma Open-Source Hardware Compiler",
        "version": __version__,
        "docs_url": "/api/docs"
    }


@app.get("/admin/session")
def admin_session_endpoint(user: UserContext = Depends(optional_user_context)):
    """Reports whether the current signed-in user has Forma admin access."""
    return {
        "is_admin": user.is_admin,
        "user_id": user.owner_user_id,
        "provider": user.provider,
    }


@app.get("/debug/config")
def debug_config_endpoint(
    provider: Optional[str] = Query(None, description="Optional runtime LLM provider override to validate."),
    model: Optional[str] = Query(None, description="Optional runtime LLM model override to validate."),
    user: UserContext = Depends(optional_user_context),
):
    """
    Reports LLM provider and model resolution state without exposing credentials.
    """
    settings = _resolve_user_integrations(user)
    try:
        orchestrator = HardwarePipelineOrchestrator(provider_name=provider, model_name=model, settings=settings)
        llm_config = orchestrator.get_debug_config()
        return {
            **llm_config,
            "forma_dev_mode": forma_dev_mode_enabled(),
            "deployment": _deployment_runtime_config(llm_config),
            "database": get_database_config(),
            "job_metadata": JOB_STORE.get_config(),
            "image_output": get_image_output_debug_config(settings=settings),
            "image_storage": get_image_storage_config(),
            "observability": get_langfuse_debug_config(),
            "debug": get_debug_mode_config(),
            "video_generation": GMICloudProvider(settings=settings).get_debug_config(),
            "video_self_correction": FireworksVideoReviewClient().get_debug_config(),
            "video_storage": get_video_storage_config(),
            "security": security_config(),
            "workflows": list_workflows(),
            "data_sources": list_generation_data_sources(),
            "project_namespaces": [namespace.model_dump(mode="json") for namespace in list_project_namespaces()],
        }
    except LLMProviderConfigError as e:
        correlation_id = new_error_correlation_id()
        log_exception(logger, "Debug config rejected", e, correlation_id=correlation_id, level=logging.WARNING)
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(
                code="llm_config_invalid",
                message=str(e),
                exc=e,
                provider=provider,
                model=model,
                correlation_id=correlation_id,
            ),
        ) from e
    except Exception as e:
        correlation_id = new_error_correlation_id()
        log_exception(logger, "Debug config failed", e, correlation_id=correlation_id)
        raise HTTPException(
            status_code=500,
            detail=api_error_detail(
                code="debug_config_failed",
                message="Debug config failed.",
                exc=e,
                correlation_id=correlation_id,
            ),
        ) from e


@app.get("/runtime/config")
def runtime_config_endpoint(user: UserContext = Depends(optional_user_context)):
    """Return the canonical, credential-safe runtime contract for this user."""
    try:
        settings = _runtime_config_settings(user)
        _, contract = _resolved_client_runtime_config(settings, user)
        return contract
    except LLMProviderConfigError as e:
        correlation_id = new_error_correlation_id()
        log_exception(logger, "Runtime config rejected", e, correlation_id=correlation_id, level=logging.WARNING)
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(code="llm_config_invalid", message=str(e), exc=e, correlation_id=correlation_id),
        ) from e
    except Exception as e:
        correlation_id = new_error_correlation_id()
        log_exception(logger, "Runtime config resolution failed", e, correlation_id=correlation_id)
        raise HTTPException(
            status_code=500,
            detail=api_error_detail(
                code="runtime_config_failed",
                message="Runtime config failed.",
                exc=e,
                correlation_id=correlation_id,
            ),
        ) from e

@app.post("/generate", response_model=Dict[str, Any])
async def generate_project_endpoint(request: GenerateProjectRequest, user: UserContext = Depends(require_user_context)):
    """
    Submits a natural language hardware idea and optional multimodal reference image.
    Runs the 7-agent compilation workflow, circuit safety auditor, and returns a verified Hardware Intermediate Representation, SVG schematic, and Mermaid diagram.
    """
    require_hosted_chat_enabled()
    owner_user_id = _require_authenticated_user(user)
    if request.project_id:
        try:
            ensure_project_action_allowed(
                request.project_id,
                owner_user_id,
                "forma.generate_project",
                require_workflow=True,
            )
        except WorkflowStateError as exc:
            status_code = status.HTTP_404_NOT_FOUND if exc.code == "workflow_not_found" else status.HTTP_409_CONFLICT
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
    settings = _resolve_user_integrations(user)
    try:
        llm_config = get_workflow_debug_config(
            request.workflow,
            provider_name=request.provider,
            model_name=request.model,
            external_source_provider=request.external_source_provider,
            settings=settings,
        )
    except LLMProviderConfigError as e:
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(
                code="llm_config_invalid",
                message=str(e),
                exc=e,
                provider=request.provider,
                model=request.model,
                context={
                    "workflow": request.workflow,
                    "generate_image": request.generate_image,
                    "external_source_provider": request.external_source_provider,
                },
            ),
        ) from e
    deployment_config = _deployment_runtime_config(llm_config)
    if deployment_config["alpha_generation_gate_active"]:
        detail = generation_unavailable_detail(llm_config)
        logger.warning(
            "Generation unavailable for provider=%s model=%s: %s",
            detail.get("provider"),
            detail.get("model"),
            detail.get("reason") or detail.get("message"),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )

    if not (request.prompt or "").strip() and not request.image_data:
        message = "Provide a prompt or reference image."
        detail = (
            api_error_detail(
                code="generation_input_invalid",
                message=message,
                provider=request.provider,
                model=request.model,
                context={
                    "workflow": request.workflow,
                    "has_image": bool(request.image_data),
                    "external_source_provider": request.external_source_provider,
                },
            )
            if debug_mode_enabled()
            else message
        )
        raise HTTPException(status_code=400, detail=detail)

    project_id = request.project_id or str(uuid4())
    try:
        ensure_chat_project(
            project_id,
            owner_user_id,
            prompt=request.prompt,
            chat_id=request.chat_id,
        )
    except (ValueError, DesignBriefAccessError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    job_id = request.client_job_id or f"job_frontend_{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    correlation_id = new_error_correlation_id()
    if request.source_project_id:
        try:
            source_project = resolve_project_for_read(request.source_project_id, owner_user_id).project
        except ProjectReadError as exc:
            raise HTTPException(status_code=404, detail="Source project not found.") from exc
        _require_project_chat_owner(source_project, user)
    payload = {
        "prompt": request.prompt,
        "project_id": project_id,
        "retry_stage": request.retry_stage,
        "workflow": request.workflow,
        "image_data": request.image_data,
        "generate_image": request.generate_image or request.generation_mode == "progressive",
        "generation_mode": request.generation_mode,
        "provider": request.provider,
        "model": request.model,
        "chat_id": request.chat_id,
        "source_project_id": request.source_project_id,
        "client_job_id": request.client_job_id,
        "owner_user_id": owner_user_id,
        "external_source_provider": request.external_source_provider,
        "data_sources": request.data_sources,
        "past_jobs_limit": request.past_jobs_limit,
    }
    JOB_STORE.create_job(
        job_id=job_id,
        message_id=message_id,
        correlation_id=correlation_id,
        action="forma.generate_project",
        sender="frontend",
        recipient="forma",
        payload=payload,
        server_owned=True,
        status="queued",
    )
    JOB_STORE.mark_running(job_id)

    try:
        past_job_context = None
        if PAST_JOBS_DATA_SOURCE in request.data_sources:
            def load_project_for_context(project_id: str) -> Any:
                try:
                    return resolve_project_for_read(project_id, owner_user_id).project
                except ProjectReadError:
                    return None

            past_job_context = await PastJobContextSource(JOB_STORE, load_project_for_context).retrieve(
                request.prompt,
                owner_user_id=owner_user_id,
                limit=request.past_jobs_limit,
                exclude_job_id=job_id,
            )
        with observe_agent_pipeline(
            lambda event: JOB_STORE.append_progress_event(job_id, event.as_dict()),
            cancellation_check=lambda: JOB_STORE.is_cancelled(job_id),
        ):
            response = await asyncio.to_thread(
                build_generation_response,
                request.prompt,
                request.image_data,
                generate_image=request.generate_image or request.generation_mode == "progressive",
                generation_mode=request.generation_mode,
                workflow=request.workflow,
                provider=request.provider,
                model=request.model,
                external_source_provider=request.external_source_provider,
                chat_id=request.chat_id,
                source_project_id=request.source_project_id,
                frontend_job_id=job_id,
                owner_user_id=owner_user_id,
                data_sources=request.data_sources,
                past_job_context=past_job_context,
                project_id=project_id,
                retry_stage=request.retry_stage,
            )
        if JOB_STORE.is_cancelled(job_id):
            raise JobCancelledError(f"Job {job_id} was cancelled.")
        generation_status = str(response.get("generation_status") or "succeeded").lower()
        if generation_status == "partial":
            JOB_STORE.mark_partial(job_id, response)
        elif generation_status == "failed":
            JOB_STORE.mark_failed(
                job_id,
                "A required root generation stage failed.",
                error_code="generation_root_failed",
                correlation_id=correlation_id,
            )
        else:
            JOB_STORE.mark_succeeded(job_id, response)
        job = JOB_STORE.get_job(job_id)
        if str((job or {}).get("status") or "").lower() in {"cancelled", "canceled"}:
            raise JobCancelledError(f"Job {job_id} was cancelled.")
        response = _attach_generation_timing_metadata(response, job)
        metadata = (response.get("project_ir", {}).get("assembly_metadata") or {})
        project_id = metadata.get("project_id")
        return {
            **response,
            "project_id": project_id,
            "chat_id": metadata.get("chat_id"),
            "can_chat": True,
            "job_id": job_id,
            "job": job,
        }
    except PipelineCancelledError as e:
        JOB_STORE.mark_cancelled(job_id)
        _delete_cancelled_generation_projects(job_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=api_error_detail(
                code="generation_cancelled",
                message="Generation was stopped by the user.",
                exc=e,
                job_id=job_id,
                provider=request.provider,
                model=request.model,
            ),
        ) from e
    except ValueError as e:
        error_debug = exception_debug_payload(e, context=payload) if debug_mode_enabled() else None
        JOB_STORE.mark_failed(
            job_id,
            "The generation request is invalid.",
            error_debug,
            error_code="generation_request_invalid",
            correlation_id=correlation_id,
        )
        log_exception(
            logger,
            "Generation request rejected",
            e,
            correlation_id=correlation_id,
            context={"job_id": job_id, "workflow": request.workflow},
            level=logging.WARNING,
        )
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(
                code="generation_request_invalid",
                message=str(e),
                exc=e,
                job_id=job_id,
                provider=request.provider,
                model=request.model,
                context=payload,
            ),
        ) from e
    except LLMProviderConfigError as e:
        error_debug = exception_debug_payload(e, context=payload) if debug_mode_enabled() else None
        JOB_STORE.mark_failed(
            job_id,
            "The requested model configuration is invalid.",
            error_debug,
            error_code="llm_config_invalid",
            correlation_id=correlation_id,
        )
        log_exception(
            logger,
            "Generation LLM config failed",
            e,
            correlation_id=correlation_id,
            context={"job_id": job_id, "workflow": request.workflow},
            level=logging.WARNING,
        )
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(
                code="llm_config_invalid",
                message=str(e),
                exc=e,
                job_id=job_id,
                provider=request.provider,
                model=request.model,
                context=payload,
            ),
        ) from e
    except LLMProviderOutputError as e:
        error_debug = exception_debug_payload(e, context=payload) if debug_mode_enabled() else None
        JOB_STORE.mark_failed(
            job_id,
            "The model provider returned an invalid response.",
            error_debug,
            error_code="llm_output_invalid",
            correlation_id=correlation_id,
        )
        log_exception(
            logger,
            "LLM output rejected",
            e,
            correlation_id=correlation_id,
            context={"job_id": job_id, "workflow": request.workflow},
            level=logging.WARNING,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=api_error_detail(
                code="llm_output_invalid",
                message=str(e),
                exc=e,
                job_id=job_id,
                provider=request.provider,
                model=request.model,
                context=payload,
            ),
        ) from e
    except AlphaGenerationUnavailableError as e:
        error_debug = exception_debug_payload(e, context=payload) if debug_mode_enabled() else None
        code = "alpha_generation_unavailable" if str(e) == ALPHA_GENERATION_UNAVAILABLE_MESSAGE else "llm_generation_unavailable"
        JOB_STORE.mark_failed(
            job_id,
            "Generation is currently unavailable.",
            error_debug,
            error_code=code,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=api_error_detail(
                code=code,
                message=str(e),
                exc=e,
                job_id=job_id,
                provider=request.provider,
                model=request.model,
                context=payload,
            ),
        ) from e
    except Exception as e:
        error_debug = exception_debug_payload(e, context=payload) if debug_mode_enabled() else None
        JOB_STORE.mark_failed(
            job_id,
            "Generation could not be completed.",
            error_debug,
            error_code="generation_failed",
            correlation_id=correlation_id,
        )
        log_exception(
            logger,
            "Generation failed",
            e,
            correlation_id=correlation_id,
            context={"job_id": job_id, "workflow": request.workflow},
        )
        raise HTTPException(
            status_code=500,
            detail=api_error_detail(
                code="generation_failed",
                message=f"Generation failed: {str(e)}",
                exc=e,
                job_id=job_id,
                provider=request.provider,
                model=request.model,
                context=payload,
            ),
        ) from e


@app.get("/workflows")
def list_generation_workflows_endpoint():
    """List generation workflows available to frontend and CLI clients."""
    return list_workflows()


@app.get("/data-sources")
def list_generation_data_sources_endpoint():
    """List optional lightweight context sources available during generation."""
    return list_generation_data_sources()


@app.get("/pipeline/steps")
def list_agent_pipeline_steps_endpoint(
    workflow: Optional[str] = Query(None, description="Generation workflow id, for example default or web_research."),
    include_image: bool = Query(False, description="Include optional product image generation stage."),
):
    """List public, user-safe agent pipeline stages for the selected workflow."""
    return {
        "workflow": pipeline_workflow_id(workflow),
        "include_image": include_image,
        "steps": list_agent_pipeline_steps(workflow, include_image=include_image),
    }


@app.post("/clarifying-questions", response_model=ClarifyingQuestionsResponse)
def clarifying_questions_endpoint(request: ClarifyingQuestionsRequest):
    """Run the core Context Clarifier Agent before starting a generation job."""
    require_hosted_chat_enabled()
    return ask_clarifying_questions(request)


class VideoImageToVideoRequest(BaseModel):
    projectId: str | None = None
    image: str | None = None
    prompt: str | None = None
    model: str | None = None
    duration: str | None = "5"
    aspectRatio: str | None = None
    aspect_ratio: str | None = None
    sound: str | None = "off"


class VideoToVideoRequest(BaseModel):
    projectId: str | None = None
    video: str | None = None
    prompt: str | None = None
    model: str | None = None
    duration: str | None = "5"
    aspectRatio: str | None = None
    aspect_ratio: str | None = None
    sound: str | None = "off"


VIDEO_FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled"}
VIDEO_SUCCESS_STATUSES = {"success", "succeeded", "completed", "complete", "done"}


def _normalize_video_model(model: str | None, mode: str = VIDEO_MODE_IMAGE_TO_VIDEO, settings: Optional[ResolvedIntegrationSettings] = None) -> str:
    normalized_mode = normalize_video_mode(mode)
    normalized = (model or get_default_video_model(normalized_mode, settings)).strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Video model is required.")
    allowed_models = get_available_video_models(normalized_mode, settings)
    if normalized not in allowed_models:
        raise HTTPException(status_code=400, detail=f"Unsupported {normalized_mode} model '{normalized}'.")
    return normalized


def _normalize_video_request_aspect_ratio(aspect_ratio: str | None, settings: Optional[ResolvedIntegrationSettings] = None) -> str:
    try:
        return normalize_video_aspect_ratio(aspect_ratio, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_non_empty(value: str | None, message: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=message)
    return normalized


def _require_authenticated_user(user: UserContext) -> str:
    if user.is_authenticated and user.owner_user_id:
        return user.owner_user_id
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to manage projects and chats.")


def _project_owner_user_id(project: Any) -> Optional[str]:
    value = project.get("owner_user_id") if isinstance(project, dict) else getattr(project, "owner_user_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _project_visibility(project: Any) -> str:
    value = project.get("visibility", "public") if isinstance(project, dict) else getattr(project, "visibility", "public")
    normalized = str(value or "public").strip().lower()
    return normalized if normalized in {"public", "private"} else "public"


def _user_owns_project(project: Any, user: UserContext) -> bool:
    return bool(user.owner_user_id and _project_owner_user_id(project) == user.owner_user_id)


def _require_project_reader(project: Any, user: UserContext) -> None:
    if _project_visibility(project) == "public" or _user_owns_project(project, user):
        return
    # Do not reveal that another user's private project exists.
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")


def creator_display_name(owner_user_id: Optional[str]) -> str:
    if not owner_user_id:
        return "unknown"
    normalized = owner_user_id.strip()
    if normalized == "local-dev-user":
        return "local_dev"
    if len(normalized) <= 12:
        return normalized
    return f"{normalized[:6]}_{normalized[-4:]}"


def _require_project_owner(project: Any, user: UserContext) -> str:
    user_id = _require_authenticated_user(user)
    owner_user_id = _project_owner_user_id(project)
    if owner_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only modify your own projects.")
    return user_id


def _resolve_project_owner(project_id: str, user: UserContext, *, include_deleted: bool = False) -> Any:
    """Resolve project state through the canonical read boundary before access checks."""
    try:
        resolved = resolve_project_for_read(project_id, user.owner_user_id, include_deleted=include_deleted)
    except ProjectReadError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.") from exc
    _require_project_owner(resolved.project, user)
    return resolved.project


def _resolve_project_reader(project_id: str, user: UserContext, *, include_deleted: bool = False) -> Any:
    """Resolve one project through the canonical boundary for read access."""
    try:
        resolved = resolve_project_for_read(project_id, user.owner_user_id, include_deleted=include_deleted)
    except ProjectReadError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.") from exc
    _require_project_reader(resolved.project, user)
    return resolved.project


def _require_project_chat_owner(project: Any, user: UserContext) -> str:
    user_id = _require_authenticated_user(user)
    owner_user_id = _project_owner_user_id(project)
    if owner_user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only chat with your own projects.")
    return user_id


def _store_video_results(
    result: Any,
    *,
    project_id: str,
    model: str,
    prompt: str | None = None,
    mode: str | None = None,
    aspect_ratio: str | None = None,
    source_url: str | None = None,
) -> List[Dict[str, Any]]:
    if not result.video_urls:
        return []
    try:
        stored_videos = upload_generated_videos_to_s3(
            result.video_urls,
            project_id=project_id,
            request_id=result.request_id,
            model=model,
            prompt=prompt,
            mode=mode,
            aspect_ratio=aspect_ratio,
            source_url=source_url,
        )
        return [stored_video.response_metadata() for stored_video in stored_videos]
    except Exception as exc:
        logger.exception(
            "Generated video S3 upload failed for project_id=%s request_id=%s source_urls=%s",
            project_id,
            result.request_id,
            result.video_urls,
        )
        raise HTTPException(
            status_code=502,
            detail=f"S3 upload failed for video request {result.request_id}: {str(exc)}",
        ) from exc


def _raise_if_completed_without_video(result: Any) -> None:
    if result.status in VIDEO_SUCCESS_STATUSES and not result.video_urls:
        raise HTTPException(
            status_code=502,
            detail=f"GMI Cloud video request {result.request_id} completed without a video URL.",
        )


def _video_route_response(
    result: Any,
    *,
    project_id: str,
    model: str,
    saved_videos: List[Dict[str, Any]],
    aspect_ratio: str | None = None,
    prompt: str | None = None,
    mode: str | None = None,
) -> Dict[str, Any]:
    return {
        "projectId": project_id,
        "requestId": result.request_id,
        "status": result.status,
        "model": model,
        "mode": mode,
        "prompt": prompt,
        "aspectRatio": aspect_ratio,
        "aspect_ratio": aspect_ratio,
        "source": "gmi-cloud",
        "videoUrls": result.video_urls,
        "savedVideos": saved_videos,
        "storedVideo": saved_videos[0] if saved_videos else None,
    }


@app.get("/video/models")
def list_video_models_endpoint(user: UserContext = Depends(optional_user_context)):
    """Returns the backend-approved video generation models."""
    settings = _resolve_user_integrations(user)
    models = get_available_video_model_options(settings=settings)
    default_model = get_default_video_model(VIDEO_MODE_IMAGE_TO_VIDEO, settings)
    default_video_to_video_model = get_default_video_model(VIDEO_MODE_VIDEO_TO_VIDEO, settings)
    provider_config = GMICloudProvider(settings=settings).get_debug_config()
    return {
        "models": [model.response_metadata() for model in models],
        "defaultModel": default_model,
        "default_model": default_model,
        "defaultVideoToVideoModel": default_video_to_video_model,
        "default_video_to_video_model": default_video_to_video_model,
        "aspectRatioOptions": get_available_video_aspect_ratios(settings),
        "aspect_ratio_options": get_available_video_aspect_ratios(settings),
        "generationConfigured": provider_config["configured"],
        "generation_configured": provider_config["configured"],
        "reason": provider_config.get("reason"),
    }


@app.get("/video/projects/{project_id}")
def list_project_videos_endpoint(project_id: str, user: UserContext = Depends(require_user_context)):
    """Lists videos saved for one project from configured backend storage."""
    project_id = _require_non_empty(project_id, "projectId is required.")
    project = _resolve_project_owner(project_id, user)
    try:
        videos = list_project_videos(project_id)
        return {
            "projectId": project_id,
            "videos": [video.response_metadata() for video in videos],
        }
    except Exception as exc:
        logger.exception("Video gallery list failed for project_id=%s", project_id)
        raise HTTPException(status_code=500, detail=f"Video gallery failed: {str(exc)}") from exc


@app.post("/video/image-to-video")
def create_image_to_video_endpoint(request: VideoImageToVideoRequest, user: UserContext = Depends(require_user_context)):
    """Queues a backend-only GMI Cloud image-to-video generation request."""
    require_hosted_chat_enabled()
    settings = _resolve_user_integrations(user)
    project_id = _require_non_empty(request.projectId, "projectId is required.")
    project = _resolve_project_owner(project_id, user)
    image = _require_non_empty(request.image, "image is required.")
    prompt = _require_non_empty(request.prompt, "prompt is required.")
    model = _normalize_video_model(request.model, VIDEO_MODE_IMAGE_TO_VIDEO, settings)
    duration = _require_non_empty(request.duration, "duration is required.")
    aspect_ratio = _normalize_video_request_aspect_ratio(request.aspectRatio or request.aspect_ratio, settings)
    sound = "on" if (request.sound or "").strip().lower() == "on" else "off"

    try:
        ensure_video_storage_configured()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    provider = GMICloudProvider(settings=settings)
    try:
        result = provider.create_image_to_video(
            image=image,
            prompt=prompt,
            model=model,
            duration=duration,
            aspect_ratio=aspect_ratio,
            sound=sound,
        )
    except Exception as exc:
        logger.exception("GMI Cloud image-to-video create failed for project_id=%s model=%s", project_id, model)
        status_code = 500 if "API key is missing" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    if result.status in VIDEO_FAILED_STATUSES:
        raise HTTPException(status_code=502, detail=f"GMI Cloud video request failed with status '{result.status}'.")
    _raise_if_completed_without_video(result)

    saved_videos = _store_video_results(
        result,
        project_id=project_id,
        model=model,
        prompt=prompt,
        mode=VIDEO_MODE_IMAGE_TO_VIDEO,
        aspect_ratio=aspect_ratio,
        source_url=image,
    )
    return _video_route_response(
        result,
        project_id=project_id,
        model=model,
        saved_videos=saved_videos,
        aspect_ratio=aspect_ratio,
        prompt=prompt,
        mode=VIDEO_MODE_IMAGE_TO_VIDEO,
    )


@app.post("/video/video-to-video")
def create_video_to_video_endpoint(request: VideoToVideoRequest, user: UserContext = Depends(require_user_context)):
    """Queues a backend-only GMI Cloud video-to-video generation request."""
    require_hosted_chat_enabled()
    settings = _resolve_user_integrations(user)
    project_id = _require_non_empty(request.projectId, "projectId is required.")
    project = _resolve_project_owner(project_id, user)
    video = _require_non_empty(request.video, "video is required.")
    prompt = _require_non_empty(request.prompt, "prompt is required.")
    model = _normalize_video_model(request.model, VIDEO_MODE_VIDEO_TO_VIDEO, settings)
    duration = _require_non_empty(request.duration, "duration is required.")
    aspect_ratio = _normalize_video_request_aspect_ratio(request.aspectRatio or request.aspect_ratio, settings)
    sound = "on" if (request.sound or "").strip().lower() == "on" else "off"

    try:
        ensure_video_storage_configured()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    provider = GMICloudProvider(settings=settings)
    try:
        result = provider.create_video_to_video(
            video=video,
            prompt=prompt,
            model=model,
            duration=duration,
            aspect_ratio=aspect_ratio,
            sound=sound,
        )
    except Exception as exc:
        logger.exception("GMI Cloud video-to-video create failed for project_id=%s model=%s", project_id, model)
        status_code = 500 if "API key is missing" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    if result.status in VIDEO_FAILED_STATUSES:
        raise HTTPException(status_code=502, detail=f"GMI Cloud video request failed with status '{result.status}'.")
    _raise_if_completed_without_video(result)

    saved_videos = _store_video_results(
        result,
        project_id=project_id,
        model=model,
        prompt=prompt,
        mode=VIDEO_MODE_VIDEO_TO_VIDEO,
        aspect_ratio=aspect_ratio,
        source_url=video,
    )
    return _video_route_response(
        result,
        project_id=project_id,
        model=model,
        saved_videos=saved_videos,
        aspect_ratio=aspect_ratio,
        prompt=prompt,
        mode=VIDEO_MODE_VIDEO_TO_VIDEO,
    )


@app.get("/video/image-to-video/status/{request_id}")
def get_image_to_video_status_endpoint(
    request_id: str,
    projectId: str | None = Query(None, description="Project id that owns this video generation request."),
    model: str | None = None,
    mode: str | None = Query(VIDEO_MODE_IMAGE_TO_VIDEO, description="Video generation mode."),
    prompt: str | None = Query(None, description="Prompt used for the original video request."),
    aspectRatio: str | None = Query(None, description="Aspect ratio used for the original video request."),
    sourceUrl: str | None = Query(None, description="Source image or video URL used for the original video request."),
    user: UserContext = Depends(require_user_context),
):
    """Polls GMI Cloud for a project-scoped video request and stores completed videos in S3."""
    request_id = _require_non_empty(request_id, "requestId is required.")
    project_id = _require_non_empty(projectId, "projectId is required.")
    project = _resolve_project_owner(project_id, user)
    settings = _resolve_user_integrations(user)
    normalized_mode = normalize_video_mode(mode)
    model = _normalize_video_model(model, normalized_mode, settings)
    aspect_ratio = _normalize_video_request_aspect_ratio(aspectRatio, settings) if aspectRatio else None

    provider = GMICloudProvider(settings=settings)
    try:
        result = provider.get_request_status(request_id)
    except Exception as exc:
        logger.exception("GMI Cloud image-to-video status failed for project_id=%s request_id=%s model=%s", project_id, request_id, model)
        status_code = 500 if "API key is missing" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    if result.status in VIDEO_FAILED_STATUSES:
        raise HTTPException(status_code=502, detail=f"GMI Cloud video request failed with status '{result.status}'.")
    _raise_if_completed_without_video(result)

    saved_videos: List[Dict[str, Any]] = []
    if result.video_urls or result.status in VIDEO_SUCCESS_STATUSES:
        try:
            ensure_video_storage_configured()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        saved_videos = _store_video_results(
            result,
            project_id=project_id,
            model=model,
            prompt=prompt,
            mode=normalized_mode,
            aspect_ratio=aspect_ratio,
            source_url=sourceUrl,
        )

    return _video_route_response(
        result,
        project_id=project_id,
        model=model,
        saved_videos=saved_videos,
        aspect_ratio=aspect_ratio,
        prompt=prompt,
        mode=normalized_mode,
    )


@app.post("/alpha-signups", response_model=AlphaSignupResponse)
def alpha_signup_endpoint(request: AlphaSignupRequest):
    """
    Captures alpha access interest while deployed generation is unavailable.
    """
    try:
        llm_config = HardwarePipelineOrchestrator(settings=_resolve_user_integrations(None)).get_debug_config()
        deployment_config = _deployment_runtime_config(llm_config)
        save_alpha_signup(
            name=request.name,
            email=request.email,
            organization=request.organization,
            additional_info=request.additional_info,
            source="web-alpha-gate",
            metadata={
                "deployment": deployment_config,
                "provider": llm_config.get("provider"),
                "requested_model": llm_config.get("requested_model"),
            },
            created_at=datetime.utcnow().isoformat() + "Z",
        )
        return AlphaSignupResponse(ok=True, message="Thanks. We will follow up when generation opens.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Signup failed: {str(e)}")


@app.get("/a2a/capabilities")
def a2a_capabilities_endpoint(_user: UserContext = Depends(require_a2a_user_context)):
    """Advertises Forma's A2A transports, actions, and MCP tools."""
    return get_a2a_capabilities()


@app.put("/a2a/agents/{agent_id}")
async def register_a2a_agent(
    agent_id: str,
    registration: A2AAgentRegistration,
    user: UserContext = Depends(require_a2a_user_context),
):
    """Registers an agent so it can receive queued A2A events."""
    normalized_agent_id = agent_id.strip()
    if not normalized_agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required.")
    requested_agent_id = (registration.agent_id or "").strip()
    if requested_agent_id and requested_agent_id != normalized_agent_id:
        raise HTTPException(status_code=400, detail="The registration agent_id must match the URL.")
    record = registration.model_dump()
    record["agent_id"] = normalized_agent_id
    try:
        return await A2A_HUB.register(
            normalized_agent_id,
            record,
            principal=a2a_principal_for_user(user),
            owner_user_id=user.owner_user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="The agent is owned by another authenticated principal.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="The agent registration is invalid.") from exc


@app.post("/a2a/messages")
async def send_a2a_message(message: A2AMessage, user: UserContext = Depends(require_a2a_user_context)):
    """Submits an A2A message and queues an async result for the sender."""
    request_correlation_id = message.correlation_id or new_error_correlation_id()
    try:
        ack = await submit_a2a_message(message.model_copy(update={"correlation_id": request_correlation_id}), user)
        return ack.model_dump()
    except HostedChatUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "hosted_chat_unavailable", "message": str(exc)},
        ) from exc
    except PermissionError as exc:
        correlation_id = new_error_correlation_id()
        detail = api_error_detail(
            code="authorization_required",
            message="You are not authorized to perform this action.",
            correlation_id=correlation_id,
            public=True,
        )
        log_exception(logger, "A2A request unauthorized", exc, correlation_id=correlation_id, level=logging.WARNING)
        raise HTTPException(status_code=403, detail=detail) from exc
    except ValueError as exc:
        correlation_id = new_error_correlation_id()
        detail = api_error_detail(
            code="a2a_invalid_request",
            message="The A2A request is invalid.",
            correlation_id=correlation_id,
            public=True,
        )
        log_exception(logger, "A2A request rejected", exc, correlation_id=correlation_id)
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        correlation_id = new_error_correlation_id()
        detail = api_error_detail(
            code="a2a_submit_failed",
            message="The A2A request could not be submitted.",
            correlation_id=correlation_id,
            public=True,
        )
        log_exception(logger, "A2A request failed", exc, correlation_id=correlation_id)
        raise HTTPException(status_code=500, detail=detail) from exc


@app.get("/a2a/agents/{agent_id}/events")
async def poll_a2a_events(
    agent_id: str,
    timeout: float = Query(25.0, ge=0.0, le=60.0),
    limit: int = Query(10, ge=1, le=100),
    user: UserContext = Depends(require_a2a_user_context),
):
    """Long-polls queued A2A events for an agent."""
    try:
        events = await A2A_HUB.poll(
            agent_id,
            timeout=timeout,
            limit=limit,
            principal=a2a_principal_for_user(user),
            create_if_missing=False,
        )
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail="You are not authorized to poll this agent queue.") from exc
    return [event.model_dump() for event in events]


@app.get("/a2a/jobs")
def list_a2a_jobs(
    sender: str | None = None,
    job_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    _user: UserContext = Depends(require_a2a_admin_user_context),
):
    """Lists persisted A2A job metadata."""
    jobs = JOB_STORE.list_jobs(sender=sender, status=job_status, limit=limit)
    if sender in {None, "conversation"}:
        jobs.extend(list_project_generation_jobs(status=job_status, limit=limit))
    jobs.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
    return _admin_job_records(jobs[:limit])


@app.get("/a2a/jobs/metrics")
def get_a2a_job_metrics(
    days: int = Query(7, ge=1, le=31),
    hours: int = Query(24, ge=1, le=168),
    interval_hours: int | None = Query(None, ge=1, le=744),
    _user: UserContext = Depends(require_a2a_admin_user_context),
):
    """Returns aggregate job volume and failure metrics for administrators."""
    return JOB_STORE.get_metrics(
        days=days,
        hours=hours,
        interval_hours=interval_hours,
        additional_rows=list_project_generation_jobs(limit=1000),
    )


@app.get("/a2a/jobs/{job_id}")
def get_a2a_job(job_id: str, user: UserContext = Depends(require_a2a_user_context)):
    """Fetches persisted metadata for one A2A job."""
    job = JOB_STORE.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="A2A job not found.")
    _require_job_reader(job, user)
    return job


@app.post("/a2a/jobs/{job_id}/cancel")
def cancel_a2a_job(job_id: str, user: UserContext = Depends(require_a2a_user_context)):
    """Stops a queued or running job owned by the current user."""
    job = JOB_STORE.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="A2A job not found.")
    _require_job_reader(job, user)
    cancelled_job = JOB_STORE.mark_cancelled(job_id) or job
    if str(cancelled_job.get("status") or "").lower() in {"cancelled", "canceled"}:
        _delete_cancelled_generation_projects(job_id, cancelled_job)
    return cancelled_job


def _parse_example_job_time(value: Any) -> datetime:
    if isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _format_example_job_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _example_job_status(status_value: Any) -> str:
    normalized = str(status_value or "").strip().lower()
    if normalized in {"pass", "passed", "success", "succeeded", "completed"}:
        return "succeeded"
    if normalized in {"fail", "failed", "error"}:
        return "failed"
    if normalized in {"running", "queued"}:
        return normalized
    return "failed"


def _example_job_id(summary_path: Path, index: int, result: Dict[str, Any]) -> str:
    provider = str(result.get("provider") or "provider")
    model = str(result.get("model") or "model")
    raw = f"example_{summary_path.stem}_{index}_{provider}_{model}"
    return "".join(char if char.isalnum() else "_" for char in raw).strip("_")


def _example_operation_summary(operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"succeeded": 0, "failed": 0, "pending": 0, "not_requested": 0}
    for operation in operations:
        operation_status = str(operation.get("status") or "unknown")
        counts[operation_status] = counts.get(operation_status, 0) + 1
    return {
        "total": len(operations),
        "failed": counts.get("failed", 0),
        "succeeded": counts.get("succeeded", 0),
        "pending": counts.get("pending", 0),
        "not_requested": counts.get("not_requested", 0),
        "ok": counts.get("failed", 0) == 0,
    }


def _example_project_object_jobs(limit: int, status: Optional[str]) -> List[Dict[str, Any]]:
    if not EXAMPLE_RESULTS_DIR.exists():
        return []

    jobs: List[Dict[str, Any]] = []
    normalized_filter = None if not status or status == "all" else status
    summary_paths = sorted(
        (path for path in EXAMPLE_RESULTS_DIR.glob("*-summary.json") if not path.name.startswith("latest-")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for summary_path in summary_paths:
        try:
            summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable example project summary: %s", summary_path)
            continue

        run_id = str(summary_payload.get("run_id") or summary_path.stem.removesuffix("-summary"))
        completed_at = _parse_example_job_time(summary_payload.get("created_at"))
        results = summary_payload.get("results") if isinstance(summary_payload.get("results"), list) else []
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            job_status = _example_job_status(result.get("status"))
            if normalized_filter and normalized_filter != job_status:
                continue

            duration_seconds = float(result.get("duration_seconds") or 0.0)
            started_at = completed_at - timedelta(seconds=max(0.0, duration_seconds))
            provider = result.get("runtime_provider") or result.get("provider")
            model = result.get("runtime_model") or result.get("model")
            project_operation = {
                "id": "example_project_object_generation",
                "label": "Project object generation",
                "status": "succeeded" if job_status == "succeeded" else "failed",
                "provider": provider,
                "model": model,
                "error": result.get("error"),
                "details": {
                    "version": result.get("version"),
                    "namespace_count": len(result.get("namespaces") or []),
                    "pipeline": result.get("pipeline"),
                },
            }
            operation_statuses = [project_operation]
            seen_operation_ids = {project_operation["id"]}
            saved_operations = result.get("operation_statuses")
            if isinstance(saved_operations, list):
                for saved_operation in saved_operations:
                    if not isinstance(saved_operation, dict):
                        continue
                    operation_id = str(saved_operation.get("id") or "")
                    if operation_id and operation_id in seen_operation_ids:
                        continue
                    if operation_id:
                        seen_operation_ids.add(operation_id)
                    operation_statuses.append(saved_operation)
            operation_summary = _example_operation_summary(operation_statuses)
            jobs.append(
                {
                    "job_id": _example_job_id(summary_path, index, result),
                    "message_id": f"example_{run_id}",
                    "correlation_id": run_id,
                    "action": "examples.project_object_generation",
                    "sender": "examples",
                    "recipient": "forma",
                    "status": job_status,
                    "server_owned": False,
                    "created_at": _format_example_job_time(started_at),
                    "updated_at": _format_example_job_time(completed_at),
                    "started_at": _format_example_job_time(started_at),
                    "completed_at": _format_example_job_time(completed_at),
                    "payload": {
                        "provider": result.get("provider"),
                        "model": result.get("model"),
                        "runtime_provider": result.get("runtime_provider"),
                        "runtime_model": result.get("runtime_model"),
                        "summary_path": str(summary_path.relative_to(ROOT_DIR)),
                    },
                    "result_summary": {
                        "project_id": result.get("object_id"),
                        "title": result.get("title") or f"{result.get('provider')}/{result.get('model')}",
                        "is_valid": result.get("is_valid"),
                        "llm_provider": provider,
                        "model_name": model,
                        "pipeline": result.get("pipeline"),
                        "workflow": "examples",
                        "has_product_image": result.get("has_product_image"),
                        "image_output_requested": result.get("image_output_requested"),
                        "image_output_enabled": result.get("image_output_enabled"),
                        "image_output_configured": result.get("image_output_configured"),
                        "image_output_status": result.get("image_output_status"),
                        "image_output_failed": result.get("image_output_status") == "failed",
                        "image_output_error": result.get("image_output_error"),
                        "image_output_error_type": result.get("image_output_error_type"),
                        "image_output_generated_count": result.get("image_output_generated_count"),
                        "product_image_provider": result.get("image_output_provider"),
                        "product_image_model": result.get("image_output_model"),
                        "product_image_error": result.get("image_output_error"),
                        "source_usage": {
                            "workflow": "examples",
                            "source_labels": ["Examples"],
                        },
                        "operation_statuses": operation_statuses,
                        "operation_summary": operation_summary,
                        "namespace_count": len(result.get("namespaces") or []),
                        "duration_seconds": duration_seconds,
                    },
                    "source_usage": {
                        "workflow": "examples",
                        "source_labels": ["Examples"],
                    },
                    "error": result.get("error"),
                    "error_debug": (
                        {
                            "error_type": result.get("error_type"),
                            "error": result.get("error"),
                            "traceback": result.get("traceback"),
                        }
                        if result.get("error") or result.get("traceback")
                        else None
                    ),
                }
            )

            if len(jobs) >= limit:
                return jobs

    return jobs


@app.get("/example-project-object-jobs")
def list_example_project_object_jobs(
    job_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    _user: UserContext = Depends(require_admin_user_context),
):
    """Lists project-object jobs created by scripts under examples/results."""
    try:
        return _example_project_object_jobs(limit=limit, status=job_status)
    except Exception as exc:
        logger.exception("Example project object job listing failed.")
        raise HTTPException(status_code=500, detail=f"Example project jobs unavailable: {str(exc)}") from exc


@app.websocket("/a2a/socket/{agent_id}")
async def a2a_websocket_endpoint(websocket: WebSocket, agent_id: str):
    """WebSocket A2A transport. Send A2AMessage JSON; receive A2AEvent JSON."""
    try:
        user = await require_a2a_websocket_context(websocket)
    except HTTPException as exc:
        close_code = 4403 if exc.status_code == status.HTTP_403_FORBIDDEN else 4401
        await websocket.close(code=close_code, reason="Authentication is required for A2A transports.")
        return
    await handle_a2a_websocket(websocket, agent_id, user)


@app.post("/mcp")
async def mcp_endpoint(payload: Any = Body(...), _user: UserContext = Depends(require_mcp_user_context)):
    """MCP Streamable HTTP endpoint exposing Forma tools."""
    response = await handle_mcp_json_rpc(payload, _user)
    if response is None:
        return Response(status_code=202)
    return response


@app.post("/a2a/mcp")
async def a2a_mcp_endpoint(payload: Any = Body(...), _user: UserContext = Depends(require_mcp_user_context)):
    """Alias for agents that discover MCP under the A2A route prefix."""
    response = await handle_mcp_json_rpc(payload, _user)
    if response is None:
        return Response(status_code=202)
    return response


def _project_summary_response(
    project: Any,
    current_user_id: Optional[str] = None,
    *,
    hydrate_storage: bool = True,
    include_inline_images: bool = True,
    image_metadata: Optional[Dict[str, Any]] = None,
) -> ProjectSummary:
    owner_user_id = _project_owner_user_id(project)
    can_chat = bool(current_user_id and owner_user_id == current_user_id)
    hardware_ir = getattr(project, "hardware_ir", None) if isinstance(getattr(project, "hardware_ir", None), dict) else {}
    components = hardware_ir.get("components") if isinstance(hardware_ir, dict) else []
    project_metadata = (
        hardware_ir.get("assembly_metadata")
        if isinstance(hardware_ir, dict) and isinstance(hardware_ir.get("assembly_metadata"), dict)
        else {}
    )
    # Resolved metadata is authoritative for cross-store project reads. The
    # legacy project payload remains the fallback for callers without a resolver.
    metadata = image_metadata if isinstance(image_metadata, dict) else project_metadata
    hydrated_metadata = (
        hydrate_image_storage_metadata(metadata, project.project_id)
        if metadata and hydrate_storage
        else metadata
    )
    sequence = hydrated_metadata.get("product_visual_sequence")
    first_sequence_image = None
    if isinstance(sequence, list):
        first_sequence_image = next(
            (
                item
                for item in sequence
                if isinstance(item, dict) and (item.get("url") or item.get("data"))
            ),
            None,
        )
    product_image_url = (
        (first_sequence_image.get("url") if isinstance(first_sequence_image, dict) else None)
        or hydrated_metadata.get("product_case_image_url")
        or hydrated_metadata.get("product_image_url")
    )
    product_image_content_type = (
        (first_sequence_image.get("content_type") if isinstance(first_sequence_image, dict) else None)
        or hydrated_metadata.get("product_case_image_content_type")
        or hydrated_metadata.get("product_image_content_type")
    )
    product_image_data = hydrated_metadata.get("product_image_data")
    if not product_image_data and isinstance(first_sequence_image, dict):
        product_image_data = first_sequence_image.get("data")
    has_product_image = bool(product_image_url or product_image_data)
    if not include_inline_images:
        product_image_data = None
        sequence = [
            {key: value for key, value in item.items() if key not in {"data", "image_data"}}
            for item in sequence[:12]
            if isinstance(item, dict)
        ] if isinstance(sequence, list) else []
    stored_creator_display = metadata.get("creator_display") or metadata.get("creator_username")
    creator_display = (
        stored_creator_display.strip()
        if isinstance(stored_creator_display, str) and stored_creator_display.strip()
        else creator_display_name(owner_user_id)
    )
    stored_creator_image_url = metadata.get("creator_image_url")
    creator_image_url = (
        stored_creator_image_url.strip()
        if isinstance(stored_creator_image_url, str)
        and stored_creator_image_url.strip().startswith(("http://", "https://"))
        else None
    )
    return ProjectSummary(
        project_id=project.project_id,
        creation_channel=getattr(project, "creation_channel", "hosted"),
        chat_id=getattr(project, "chat_id", None) if can_chat else None,
        title=project.title,
        prompt=project.prompt,
        created_at=project.created_at,
        visibility=_project_visibility(project),
        can_chat=can_chat,
        creator_display=creator_display,
        creator_username=creator_display,
        creator_image_url=creator_image_url,
        parts_count=len(components) if isinstance(components, list) else 0,
        save_count=0,
        remix_count=0,
        saved=False,
        has_product_image=has_product_image,
        product_image_url=product_image_url,
        product_image_data=product_image_data,
        product_image_content_type=product_image_content_type,
        product_image_model=hydrated_metadata.get("product_image_model") or hydrated_metadata.get("image_output_model"),
        product_visual_sequence=sequence if isinstance(sequence, list) else [],
        image_output_status=hydrated_metadata.get("image_output_status"),
        generation_status=metadata.get("generation_status", "succeeded"),
        project_readiness=metadata.get("project_readiness", "complete"),
    )


def _canonical_project_summary_response(
    revision: Any,
    brief: Any,
    *,
    owner_user_id: str,
    hydrate_storage: bool = True,
) -> ProjectSummary:
    """Adapt canonical project state to the established gallery response."""

    state = revision.state
    overview = getattr(state, "overview", None)
    title = str(getattr(overview, "title", "") or getattr(brief, "summary", "") or "Untitled project")
    project = types.SimpleNamespace(
        project_id=str(revision.project_id),
        chat_id=brief.conversation_id,
        owner_user_id=owner_user_id,
        visibility="public",
        title=title,
        prompt=brief.summary,
        created_at=revision.created_at,
        hardware_ir=state.model_dump(mode="json"),
    )
    return _project_summary_response(
        project,
        current_user_id=owner_user_id,
        hydrate_storage=hydrate_storage,
    )


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _gallery_inventory_summary(record: Any, *, current_user_id: Optional[str]) -> Optional[ProjectSummary]:
    """Adapt one inventory row without loading chat or DesignBrief history."""
    source_value = str(getattr(record, "source", "") or "").strip().lower()
    source = source_value or ("legacy" if hasattr(record, "hardware_ir") else "canonical")
    project_id = str(getattr(record, "project_id", "") or "").strip()
    if not project_id:
        return None
    if source == "legacy":
        legacy_hardware_ir = _json_object(getattr(record, "legacy_hardware_ir", None))
        legacy_project = None
        if legacy_hardware_ir:
            legacy_project = types.SimpleNamespace(
                project_id=project_id,
                owner_user_id=getattr(record, "owner_user_id", None),
                creation_channel=getattr(record, "creation_channel", "hosted"),
                chat_id=getattr(record, "chat_id", None),
                visibility=getattr(record, "visibility", "public"),
                title=getattr(record, "title", ""),
                prompt=getattr(record, "prompt", ""),
                created_at=getattr(record, "created_at", None),
                updated_at=getattr(record, "updated_at", None),
                hardware_ir=legacy_hardware_ir,
            )
        elif hasattr(record, "hardware_ir"):
            legacy_project = record
        if legacy_project is None:
            return None
        project = types.SimpleNamespace(
            project_id=project_id,
            owner_user_id=getattr(legacy_project, "owner_user_id", getattr(record, "owner_user_id", None)),
            creation_channel=getattr(legacy_project, "creation_channel", getattr(record, "creation_channel", "hosted")),
            chat_id=getattr(legacy_project, "chat_id", getattr(record, "chat_id", None)),
            visibility=getattr(legacy_project, "visibility", getattr(record, "visibility", "public")),
            title=getattr(legacy_project, "title", getattr(record, "title", "")),
            prompt=getattr(legacy_project, "prompt", getattr(record, "prompt", "")),
            created_at=getattr(legacy_project, "created_at", getattr(record, "created_at", None)),
            updated_at=getattr(legacy_project, "updated_at", getattr(record, "updated_at", None)),
            hardware_ir=getattr(legacy_project, "hardware_ir", {}),
        )
        return _project_summary_response(
            project,
            current_user_id=current_user_id,
            hydrate_storage=False,
            include_inline_images=True,
        )

    identity = {
        "project_id": project_id,
        "owner_user_id": getattr(record, "owner_user_id", None),
        "creation_channel": getattr(record, "creation_channel", "hosted"),
        "chat_id": getattr(record, "chat_id", None),
         "visibility": getattr(record, "visibility", "public"),
        "title": getattr(record, "title", ""),
        "prompt": getattr(record, "prompt", ""),
        "created_at": getattr(record, "created_at", None),
        "updated_at": getattr(record, "updated_at", None),
    }
    if str(identity["creation_channel"]).strip().lower() == "cli":
        try:
            manifest = ProjectManifest.from_document(_json_object(getattr(record, "revision_payload_json", None)))
        except (TypeError, ValueError):
            logger.warning("Skipping invalid CLI project in gallery inventory: project_id=%s", project_id)
            return None
        project = types.SimpleNamespace(**identity, hardware_ir=manifest.project_ir)
        return _project_summary_response(
            project,
            current_user_id=current_user_id,
            hydrate_storage=False,
            include_inline_images=False,
        )

    try:
        revision = ProjectRevision.model_validate(_json_object(getattr(record, "revision_payload_json", None)))
    except (TypeError, ValueError):
        logger.warning("Skipping invalid canonical project in gallery inventory: project_id=%s", project_id)
        return None
    project_ir = revision.state.model_dump(mode="json")
    metadata = project_ir.get("assembly_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.update({
        "project_id": project_id,
        "project_revision": revision.revision,
        "canonical_revision_id": str(revision.revision_id),
    })
    project_ir["assembly_metadata"] = metadata
    overview = getattr(revision.state, "overview", None)
    identity["title"] = str(getattr(overview, "title", "") or identity["title"] or "Untitled project")
    project = types.SimpleNamespace(
        **identity,
        hardware_ir=project_ir,
    )
    return _project_summary_response(
        project,
        current_user_id=current_user_id,
        hydrate_storage=False,
        include_inline_images=False,
    )


def _gallery_inventory_cache_record(record: Any) -> Optional[Dict[str, Any]]:
    summary = _gallery_inventory_summary(record, current_user_id=None)
    if summary is None:
        return None
    cached = summary.model_dump(mode="json")
    cached[_CACHE_OWNER_DIGEST_FIELD] = _project_owner_digest(getattr(record, "owner_user_id", None))
    cached[_CACHE_OWNER_CHAT_FIELD] = getattr(record, "chat_id", None)
    return cached


def _paginated_gallery_summaries(
    *,
    owner_user_id: Optional[str],
    visibility: Optional[str],
    limit: int,
    offset: int,
    search: Optional[str],
    summary_builder: Callable[[Any], Optional[Dict[str, Any]]],
    on_page_records: Optional[Callable[[List[Any]], None]] = None,
    include_search_in_page_call: bool = False,
) -> tuple[List[Dict[str, Any]], int]:
    """Return a page of valid summaries without letting bad rows consume slots."""
    page_kwargs: Dict[str, Any] = {
        "owner_user_id": owner_user_id,
        "visibility": visibility,
        "limit": limit,
        "offset": offset,
    }
    if include_search_in_page_call or search is not None:
        page_kwargs["search"] = search
    records, total = list_project_gallery_inventory_page(
        **page_kwargs,
    )
    if on_page_records:
        on_page_records(records)
    page_items = [
        summary
        for record in records
        if (summary := summary_builder(record)) is not None
    ]
    if len(page_items) == len(records) and (records or total == 0):
        return page_items, total

    all_records = list_project_gallery_inventory(
        owner_user_id=owner_user_id,
        visibility=visibility,
        search=search,
    )
    valid_items = [
        summary
        for record in all_records
        if (summary := summary_builder(record)) is not None
    ]
    return valid_items[offset:offset + limit], len(valid_items)


def _log_gallery_legacy_fallback(endpoint: str, records: List[Any]) -> None:
    count = sum(1 for record in records if str(getattr(record, "source", "") or "").lower() == "legacy")
    logger.info("project_gallery_legacy_fallback endpoint=%s count=%d", endpoint, count)


def _project_owner_digest(owner_user_id: Optional[str]) -> Optional[str]:
    if not isinstance(owner_user_id, str) or not owner_user_id.strip():
        return None
    return hashlib.sha256(owner_user_id.strip().encode("utf-8")).hexdigest()


def _public_project_cache_record(project: Any) -> Dict[str, Any]:
    """Build one shared gallery record with non-response ownership hints."""
    summary = _project_summary_response(
        project,
        current_user_id=None,
        hydrate_storage=False,
        include_inline_images=False,
    ).model_dump(mode="json")
    summary[_CACHE_OWNER_DIGEST_FIELD] = _project_owner_digest(_project_owner_user_id(project))
    summary[_CACHE_OWNER_CHAT_FIELD] = getattr(project, "chat_id", None)
    return summary


def _personalize_public_project_records(
    records: List[Dict[str, Any]],
    current_user_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Strip cache-only fields and restore owner-only gallery capabilities."""
    current_owner_digest = _project_owner_digest(current_user_id)
    response: List[Dict[str, Any]] = []
    for record in records:
        item = {
            key: value
            for key, value in record.items()
            if key not in {_CACHE_OWNER_DIGEST_FIELD, _CACHE_OWNER_CHAT_FIELD}
        }
        can_chat = bool(
            current_owner_digest
            and record.get(_CACHE_OWNER_DIGEST_FIELD) == current_owner_digest
        )
        item["can_chat"] = can_chat
        item["chat_id"] = record.get(_CACHE_OWNER_CHAT_FIELD) if can_chat else None
        response.append(item)
    return response


def _with_project_engagement(
    records: List[Dict[str, Any]],
    current_user_id: Optional[str],
) -> List[Dict[str, Any]]:
    """Attach live save/remix counts and the current user's save state."""
    records = [
        record.model_dump(mode="json") if isinstance(record, ProjectSummary) else record
        for record in records
    ]
    if not records:
        return records
    project_ids = [str(record.get("project_id") or "") for record in records]
    engagement = project_engagement_for_ids(project_ids, current_user_id)
    enriched: List[Dict[str, Any]] = []
    for record in records:
        project_id = str(record.get("project_id") or "")
        stats = engagement.get(project_id, {})
        enriched.append(
            {
                **record,
                "save_count": max(0, int(stats.get("save_count") or 0)),
                "remix_count": max(0, int(stats.get("remix_count") or 0)),
                "saved": bool(stats.get("saved")),
            }
        )
    return enriched


def _without_downloadable_project_assets(hardware_ir: Dict[str, Any]) -> Dict[str, Any]:
    """Keep public project reads inspectable while withholding owner-only files."""
    sanitized = json.loads(json.dumps(hardware_ir))
    if "cad_model" in sanitized:
        sanitized["cad_model"] = None
    mechanical = sanitized.get("mechanical")
    if isinstance(mechanical, dict):
        if "cad_model" in mechanical:
            mechanical["cad_model"] = None
    if isinstance(mechanical, dict) and isinstance(mechanical.get("cad_sources"), list):
        sanitized_sources = []
        for source in mechanical["cad_sources"]:
            if not isinstance(source, dict):
                sanitized_sources.append(source)
                continue
            sanitized_source = dict(source)
            # MechanicalSource.url is required by HardwareIntermediateRepresentation. Keep the public
            # shape valid while removing the downloadable target itself.
            sanitized_source["url"] = ""
            for key in ("href", "download_url", "downloadUrl", "file_url", "fileUrl", "source_url", "sourceUrl"):
                sanitized_source.pop(key, None)
            sanitized_sources.append(sanitized_source)
        mechanical["cad_sources"] = sanitized_sources

    components = sanitized.get("components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            category = str(component.get("category") or "").strip().lower()
            if category not in {"mechanical", "3d print"}:
                continue
            for key in ("url", "href", "download_url", "downloadUrl", "file_url", "fileUrl", "source_url", "sourceUrl", "sourcing_url"):
                component.pop(key, None)

    metadata = sanitized.get("assembly_metadata")
    if isinstance(metadata, dict):
        metadata.pop("chat_id", None)
        if "cad_model" in metadata:
            metadata["cad_model"] = None
        metadata["can_chat"] = False
        metadata["downloadable_assets_owner_only"] = True
    return sanitized


def _cli_project_response(project_id: str, owner_user_id: str) -> Optional[ProjectDetail]:
    """Adapt an authenticated CLI resolution to the web project's response shape."""
    try:
        resolved = resolve_project_for_read(project_id, owner_user_id)
    except ProjectReadError:
        return None
    if resolved.source != "cli":
        return None

    project_ir = json.loads(json.dumps(resolved.project_ir))
    metadata = project_ir.get("assembly_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.update(
        {
            "project_id": str(resolved.project_id),
            "chat_id": None,
            "can_chat": False,
            "project_revision": resolved.current_revision,
            "cloud_revision_id": resolved.revision_id,
            "source_prompt": resolved.prompt,
            "project_source": "cli",
        }
    )
    project_ir["assembly_metadata"] = metadata
    overview = project_ir.get("overview") if isinstance(project_ir.get("overview"), dict) else {}
    components = project_ir.get("components") if isinstance(project_ir.get("components"), list) else []
    title = str(resolved.title or overview.get("title") or "Untitled project")
    product_image_url = metadata.get("product_image_url") or metadata.get("product_case_image_url")
    product_image_data = metadata.get("product_image_data")

    project_object = None
    mermaid_code = None
    svg_schematic = None
    try:
        typed_ir = HardwareIntermediateRepresentation.model_validate(project_ir)
        project_ir = typed_ir.model_dump(mode="json")
        project_object = build_project_object(typed_ir).model_dump(mode="json")
        mermaid_code = generate_mermaid_chart(typed_ir)
        svg_schematic = generate_svg_schematic(typed_ir)
    except ValidationError:
        # Keep older CLI manifests inspectable even if they predate HardwareIntermediateRepresentation.
        pass

    return ProjectDetail(
        project_id=str(resolved.project_id),
        creation_channel="cli",
        title=title,
        prompt=resolved.prompt,
        created_at=resolved.created_at,
         visibility="public",
        creator_display=creator_display_name(owner_user_id),
        creator_username=creator_display_name(owner_user_id),
        parts_count=len(components),
        has_product_image=bool(product_image_url or product_image_data),
        product_image_url=product_image_url,
        product_image_data=product_image_data,
        product_visual_sequence=metadata.get("product_visual_sequence") or [],
        project_ir=project_ir,
        project_object=project_object,
        mermaid_code=mermaid_code,
        svg_schematic=svg_schematic,
        generation_status=metadata.get("generation_status", "succeeded"),
        project_readiness=metadata.get("project_readiness", "complete"),
        generation_stages=(metadata.get("generation_run") or {}).get("records", {}),
    )


def _list_project_identities(owner_user_id: str) -> List[ProjectIdentityResponse]:
    """Read the shared identity table, tolerating pre-migration local stores."""
    try:
        return [ProjectIdentityResponse.model_validate(record) for record in list_project_identities(owner_user_id)]
    except Exception as exc:
        if "no such table" in str(exc).lower() and "projects" in str(exc).lower():
            return []
        raise


@app.get("/projects")
def list_projects_endpoint(
    user: UserContext = Depends(optional_user_context),
    limit: Optional[int] = None,
    offset: int = 0,
    q: Optional[str] = None,
):
    """Lists public compiled hardware projects."""
    try:
        if limit is not None:
            limit = max(1, min(int(limit), 50))
            offset = max(0, int(offset))
            q = (q or "").strip() or None
            cached_page, generation = get_cached_project_page(
                "public", None, limit=limit, offset=offset, search=q,
            )
            if cached_page is not None:
                items, total = cached_page["items"], cached_page["total"]
            else:
                items, total = _paginated_gallery_summaries(
                    owner_user_id=None,
                    visibility="public",
                    limit=limit,
                    offset=offset,
                    search=q,
                    summary_builder=_gallery_inventory_cache_record,
                    on_page_records=lambda records: _log_gallery_legacy_fallback("public", records),
                    include_search_in_page_call=True,
                )
                cache_project_page(
                    "public", None, jsonable_encoder(items), total, generation,
                    limit=limit, offset=offset, search=q,
                )
            # Personalization and engagement stay OUTSIDE the shared cache.
            return {
                "items": _with_project_engagement(
                    _personalize_public_project_records(items, user.owner_user_id),
                    user.owner_user_id,
                ),
                "total": total,
                "limit": max(1, min(int(limit), 50)),
                "offset": max(0, int(offset)),
                "has_more": max(0, int(offset)) + len(items) < total,
            }
        cached, generation = get_cached_project_list("public", None)
        if cached is not None:
            return _with_project_engagement(
                _personalize_public_project_records(cached, user.owner_user_id),
                user.owner_user_id,
            )
        projects = list_project_gallery_inventory(visibility="public")
        cache_records = jsonable_encoder(
            [
                cached
                for project in projects
                if (cached := _gallery_inventory_cache_record(project)) is not None
            ]
        )
        _log_gallery_legacy_fallback("public", projects)
        cache_project_list("public", None, cache_records, generation)
        return _with_project_engagement(
            _personalize_public_project_records(cache_records, user.owner_user_id),
            user.owner_user_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/my/projects")
def list_my_projects_endpoint(
    user: UserContext = Depends(require_user_context),
    limit: Optional[int] = None,
    offset: int = 0,
    q: Optional[str] = None,
):
    """Lists projects owned by the signed-in user."""
    owner_user_id = _require_authenticated_user(user)
    try:
        if limit is not None:
            items, total = _paginated_gallery_summaries(
                owner_user_id=owner_user_id,
                visibility=None,
                limit=limit,
                offset=offset,
                search=q,
                summary_builder=lambda project: (
                    summary.model_dump(mode="json")
                    if (summary := _gallery_inventory_summary(project, current_user_id=owner_user_id)) is not None
                    else None
                ),
                on_page_records=lambda records: _log_gallery_legacy_fallback("mine", records),
            )
            return {
                "items": _with_project_engagement(items, owner_user_id),
                "total": total,
                "limit": max(1, min(int(limit), 50)),
                "offset": max(0, int(offset)),
                "has_more": max(0, int(offset)) + len(items) < total,
            }
        identities = _list_project_identities(owner_user_id)
        if identities:
            response: List[Dict[str, Any]] = []
            fallback_projects: Optional[dict[str, Any]] = None
            for identity in identities:
                project_id = str(identity.project_id)
                if identity.creation_channel == "cli":
                    project = _cli_project_response(project_id, owner_user_id)
                    if project is not None:
                        response.append(project.model_dump(mode="json"))
                    continue
                try:
                    revision = get_latest_project_revision(project_id, owner_user_id)
                    brief = get_latest_design_brief(project_id, owner_user_id)
                except (ProjectStateError, DesignBriefNotFoundError):
                    if fallback_projects is None:
                        fallback_rows, _ = list_project_gallery_inventory_page(
                            owner_user_id=owner_user_id,
                            visibility=None,
                            limit=50,
                            offset=0,
                        )
                        fallback_projects = {str(row.project_id): row for row in fallback_rows}
                    fallback = fallback_projects.get(project_id)
                    if fallback is not None:
                        summary = _gallery_inventory_summary(fallback, current_user_id=owner_user_id)
                        if summary is not None:
                            response.append(summary.model_dump(mode="json"))
                    continue
                response.append(_canonical_project_summary_response(
                    revision,
                    brief,
                    owner_user_id=owner_user_id,
                    hydrate_storage=False,
                ).model_dump(mode="json"))
            return _with_project_engagement(response, owner_user_id)
        cached, generation = get_cached_project_list("mine", owner_user_id)
        if cached is not None:
            return _with_project_engagement(cached, owner_user_id)
        projects, total = list_project_gallery_inventory_page(
            owner_user_id=owner_user_id,
            visibility=None,
            limit=50,
            offset=0,
        )
        response = []
        for project in projects:
            summary = _gallery_inventory_summary(project, current_user_id=owner_user_id)
            if summary is not None:
                response.append(summary.model_dump(mode="json"))
        response = _with_project_engagement(response, owner_user_id)
        response.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        response = jsonable_encoder(response)
        cache_project_list("mine", owner_user_id, response, generation)
        return _with_project_engagement(response, owner_user_id)
    except Exception as exc:
        logger.exception("My project list failed for owner_user_id=%s", owner_user_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/projects/{project_id}/image-summary")
def get_project_image_summary_endpoint(project_id: str, user: UserContext = Depends(optional_user_context)):
    """Returns gallery-safe project metadata without validating or expanding the full hardware intermediate representation."""
    try:
        resolved = resolve_project_for_read(project_id, user.owner_user_id)
    except ProjectReadError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc

    try:
        summary = _project_summary_response(
            resolved.project,
            current_user_id=user.owner_user_id,
            image_metadata=getattr(resolved, "image_metadata", None),
        ).model_dump(mode="json")
        summary["can_chat"] = resolved.can_chat
        summary["chat_id"] = resolved.chat_id if resolved.can_chat else None
        summaries = _with_project_engagement(
            [summary],
            user.owner_user_id,
        )
        return summaries[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading project image summary: {str(e)}")


@app.get("/projects/{project_id}")
def get_project_endpoint(project_id: str, user: UserContext = Depends(optional_user_context)):
    """Retrieves a specific hardware design and its corresponding schematics."""
    try:
        resolved = resolve_project_for_read(project_id, user.owner_user_id)
    except ProjectReadError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc

    if resolved.source == "cli":
        owner_user_id = str(user.owner_user_id or "").strip()
        cli_response = _cli_project_response(project_id, owner_user_id)
        if cli_response is not None:
            return cli_response
        raise HTTPException(status_code=404, detail="Project not found.")

    if resolved.source == "canonical":
        revision = resolved.revision
        brief = resolved.design_brief
        if revision is None or brief is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        ir = revision.state.model_copy(deep=True)
        design_outcome = evaluate_design_outcome(ir)
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "project_id": str(revision.project_id),
            "chat_id": brief.conversation_id,
            "can_chat": True,
            "project_revision": revision.revision,
            "design_brief_version": revision.design_brief_version,
            "canonical_revision_id": str(revision.revision_id),
            "project_readiness": design_outcome.project_readiness,
        }
        return {
            "project_id": str(revision.project_id),
            "chat_id": brief.conversation_id,
            "prompt": brief.summary,
            "created_at": revision.created_at,
            "can_chat": True,
            "project_ir": ir.model_dump(mode="json"),
            "project_object": build_project_object(ir).model_dump(mode="json"),
            "mermaid_code": generate_mermaid_chart(ir),
            "svg_schematic": generate_svg_schematic(ir),
            "generation_status": (ir.assembly_metadata or {}).get("generation_status", "succeeded"),
            "project_readiness": design_outcome.project_readiness,
            "generation_stages": ((ir.assembly_metadata or {}).get("generation_run") or {}).get("records", {}),
        }
    project = resolved.project

    can_chat = resolved.can_chat
    stored_hardware_ir = json.loads(json.dumps(project.hardware_ir or {}))
    try:
        ir = HardwareIntermediateRepresentation(**stored_hardware_ir)
    except ValidationError as exc:
        # Saved projects can outlive the current HardwareIntermediateRepresentation schema. They should
        # remain inspectable, but public readers still receive a redacted copy.
        logger.warning(
            "Returning legacy project IR without derived artifacts: project_id=%s validation_errors=%s",
            project.project_id,
            len(exc.errors()),
        )
        metadata = stored_hardware_ir.get("assembly_metadata")
        if isinstance(metadata, dict):
            stored_hardware_ir["assembly_metadata"] = hydrate_image_storage_metadata(metadata, project.project_id)
        response_payload = stored_hardware_ir if can_chat else _without_downloadable_project_assets(stored_hardware_ir)
        response_metadata = response_payload.get("assembly_metadata")
        return {
            "project_id": project.project_id,
            "chat_id": (
                getattr(project, "chat_id", None)
                or (response_metadata.get("chat_id") if isinstance(response_metadata, dict) else None)
            ) if can_chat else None,
            "prompt": project.prompt,
            "created_at": project.created_at,
            "can_chat": can_chat,
            "project_ir": response_payload,
            "project_object": None,
            "mermaid_code": None,
            "svg_schematic": None,
            "generation_status": (response_metadata or {}).get("generation_status", "succeeded"),
            "project_readiness": "partial",
            "generation_stages": ((response_metadata or {}).get("generation_run") or {}).get("records", {}),
        }

    try:
        ir.assembly_metadata = hydrate_image_storage_metadata(ir.assembly_metadata, project.project_id)
        if can_chat:
            response_ir = ir
        else:
            sanitized_payload = _without_downloadable_project_assets(ir.model_dump())
            try:
                response_ir = HardwareIntermediateRepresentation(**sanitized_payload)
            except ValidationError:
                return {
                    "project_id": project.project_id,
                    "chat_id": None,
                    "prompt": project.prompt,
                    "created_at": project.created_at,
                    "can_chat": False,
                    "project_ir": sanitized_payload,
                    "project_object": None,
                    "mermaid_code": None,
                    "svg_schematic": None,
                }
        mermaid_code = generate_mermaid_chart(ir)
        svg_schematic = generate_svg_schematic(ir)
        
        return {
            "project_id": project.project_id,
            "chat_id": (
                getattr(project, "chat_id", None) or (ir.assembly_metadata or {}).get("chat_id")
            ) if can_chat else None,
            "prompt": project.prompt,
            "created_at": project.created_at,
            "can_chat": can_chat,
            "project_ir": response_ir.model_dump(),
            "project_object": build_project_object(response_ir).model_dump(mode="json"),
            "mermaid_code": mermaid_code,
            "svg_schematic": svg_schematic,
            "generation_status": (ir.assembly_metadata or {}).get("generation_status", "succeeded"),
            "project_readiness": evaluate_design_outcome(ir).project_readiness,
            "generation_stages": ((ir.assembly_metadata or {}).get("generation_run") or {}).get("records", {}),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading project IR: {str(e)}")


@app.patch("/projects/{project_id}")
def update_project_endpoint(
    project_id: str,
    request: ProjectUpdateRequest,
    user: UserContext = Depends(require_user_context),
):
    """Updates owner-managed project metadata, including public/private visibility."""
    require_hosted_chat_enabled()
    project = _resolve_project_owner(project_id, user)
    owner_user_id = user.owner_user_id
    saved = update_project_identity(
        project.project_id,
        owner_user_id=owner_user_id,
        title=request.title,
        prompt=request.prompt,
        visibility=request.visibility,
    )
    if not saved:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"ok": True, "project_id": project.project_id}


@app.post("/projects/{project_id}/save")
def save_project_endpoint(project_id: str, user: UserContext = Depends(require_user_context)):
    """Save a readable project for the signed-in user."""
    project = _resolve_project_reader(project_id, user)
    owner_user_id = _require_authenticated_user(user)
    try:
        return {"ok": True, "project_id": project.project_id, **save_project_for_user(project.project_id, owner_user_id)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/projects/{project_id}/save")
def unsave_project_endpoint(project_id: str, user: UserContext = Depends(require_user_context)):
    """Remove a previously saved project for the signed-in user."""
    project = _resolve_project_reader(project_id, user)
    owner_user_id = _require_authenticated_user(user)
    try:
        return {"ok": True, "project_id": project.project_id, **unsave_project_for_user(project.project_id, owner_user_id)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/projects/{project_id}/remix")
def remix_project_endpoint(project_id: str, user: UserContext = Depends(require_user_context)):
    """Copy a readable project into a new owned project the signed-in user can edit."""
    require_hosted_chat_enabled()
    project = _resolve_project_reader(project_id, user)
    owner_user_id = _require_authenticated_user(user)
    try:
        remixed = remix_generated_project(project.project_id, owner_user_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if remixed is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    engagement = project_engagement_for_ids([project.project_id], owner_user_id).get(project.project_id, {})
    return {
        "ok": True,
        "project_id": remixed.project_id,
        "chat_id": getattr(remixed, "chat_id", None),
        "title": remixed.title,
        "prompt": remixed.prompt,
        "created_at": remixed.created_at,
        "source_project_id": project.project_id,
        "remix_count": max(0, int(engagement.get("remix_count") or 0)),
        "can_chat": True,
    }


@app.delete("/projects/{project_id}", status_code=status.HTTP_202_ACCEPTED)
def delete_project_endpoint(
    project_id: str,
    user: UserContext = Depends(require_destructive_user_context),
):
    """Immediately hide a project and schedule its permanent purge."""
    require_hosted_chat_enabled()
    owner_user_id = _require_authenticated_user(user)
    try:
        try:
            resolved = resolve_project_for_read(project_id, owner_user_id, include_deleted=True)
            project = resolved.project
        except ProjectReadError:
            project = get_project_identity(project_id)
        if not project:
            audit = get_latest_project_deletion_audit(project_id)
            if (
                audit
                and getattr(audit, "acting_user_id", None) == owner_user_id
                and getattr(audit, "action", None) == "purge_completed"
                and getattr(audit, "status", None) == "succeeded"
            ):
                return {
                    "ok": True,
                    "project_id": project_id,
                    "status": "purged",
                    "policy_version": DELETION_POLICY_VERSION,
                }
            raise HTTPException(status_code=404, detail="Project not found.")
        _require_project_owner(project, user)
        deleted = request_project_deletion(project_id, owner_user_id)
        return {
            "ok": True,
            "project_id": project_id,
            "status": getattr(deleted, "status", "deletion_pending"),
            "deleted_at": getattr(deleted, "deleted_at", None),
            "purge_after": getattr(deleted, "purge_after", None),
            "policy_version": DELETION_POLICY_VERSION,
        }
    except HTTPException:
        raise
    except (LookupError, ValueError):
        raise HTTPException(status_code=404, detail="Project not found.")


@app.post("/projects/{project_id}/restore")
def restore_project_endpoint(
    project_id: str,
    user: UserContext = Depends(require_destructive_user_context),
):
    """Restore a project while it is still inside the retention window."""
    require_hosted_chat_enabled()
    owner_user_id = _require_authenticated_user(user)
    try:
        project = restore_project(project_id, owner_user_id)
    except (LookupError, ValueError):
        raise HTTPException(status_code=404, detail="Project not found.")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "project_id": project_id, "status": getattr(project, "status", "active")}


@app.get("/projects/{project_id}/data-contribution-consent")
def get_project_contribution_consent_endpoint(
    project_id: str,
    user: UserContext = Depends(require_user_context),
):
    owner_user_id = _require_authenticated_user(user)
    try:
        project = _resolve_project_owner(project_id, user, include_deleted=True)
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="Project not found.")
    consent = get_project_contribution_consent(project_id, owner_user_id)
    return {
        "project_id": project_id,
        "granted": bool(consent and not getattr(consent, "withdrawn_at", None)),
        "consent_version": getattr(consent, "consent_version", None),
        "permitted_purposes": getattr(consent, "permitted_purposes", []) if consent else [],
        "granted_at": getattr(consent, "granted_at", None),
        "withdrawn_at": getattr(consent, "withdrawn_at", None),
        "anonymized_at": getattr(consent, "anonymized_at", None),
        "available_purposes": sorted(PERMITTED_CONTRIBUTION_PURPOSES),
    }


@app.put("/projects/{project_id}/data-contribution-consent")
def grant_project_contribution_consent_endpoint(
    project_id: str,
    request: ProjectContributionConsentRequest,
    user: UserContext = Depends(require_destructive_user_context),
):
    owner_user_id = _require_authenticated_user(user)
    try:
        consent = grant_contribution_consent(
            project_id,
            owner_user_id,
            consent_version=request.consent_version,
            permitted_purposes=request.permitted_purposes,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Project not found.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "ok": True,
        "project_id": project_id,
        "granted": True,
        "consent_version": getattr(consent, "consent_version", request.consent_version),
        "permitted_purposes": getattr(consent, "permitted_purposes", request.permitted_purposes),
        "granted_at": getattr(consent, "granted_at", None),
    }


@app.delete("/projects/{project_id}/data-contribution-consent")
def withdraw_project_contribution_consent_endpoint(
    project_id: str,
    user: UserContext = Depends(require_destructive_user_context),
):
    owner_user_id = _require_authenticated_user(user)
    try:
        project = _resolve_project_owner(project_id, user, include_deleted=True)
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="Project not found.")
    withdrawn = withdraw_contribution(project_id, owner_user_id)
    return {"ok": True, "project_id": project_id, "granted": False, "withdrawn": bool(withdrawn)}


@app.get("/admin/project-deletions")
def list_project_deletions_endpoint(
    limit: int = Query(100, ge=1, le=500),
    _user: UserContext = Depends(require_admin_user_context),
):
    """Return content-free deletion lifecycle events for operational audits."""
    return [
        {
            "id": getattr(event, "id", None),
            "project_id": getattr(event, "project_id", None),
            "acting_user_id": getattr(event, "acting_user_id", None),
            "action": getattr(event, "action", None),
            "status": getattr(event, "status", None),
            "policy_version": getattr(event, "policy_version", None),
            "details": getattr(event, "details_json", {}) or {},
            "created_at": getattr(event, "created_at", None),
        }
        for event in list_project_deletion_audits(limit)
    ]


@app.get("/admin/project-deletion-metrics")
def project_deletion_metrics_endpoint(
    _user: UserContext = Depends(require_admin_user_context),
):
    return deletion_metrics()


def _chat_response(chat: Any) -> Dict[str, Any]:
    return Chat(
        chat_id=chat.chat_id,
        title=chat.title,
        messages=getattr(chat, "messages", []) or [],
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    ).model_dump(mode="json", exclude_unset=True)


@app.get("/chats")
def list_chats_endpoint(user: UserContext = Depends(require_user_context)):
    """Lists private chats for the signed-in user."""
    owner_user_id = _require_authenticated_user(user)
    return [_chat_response(chat) for chat in list_project_chats(owner_user_id)]


@app.get("/chats/{chat_id}")
def get_chat_endpoint(chat_id: str, user: UserContext = Depends(require_user_context)):
    """Retrieves one private chat owned by the signed-in user."""
    owner_user_id = _require_authenticated_user(user)
    chat = get_project_chat(chat_id, owner_user_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return _chat_response(chat)


@app.put("/chats/{chat_id}")
def upsert_chat_endpoint(
    chat_id: str,
    request: ChatUpsertRequest,
    user: UserContext = Depends(require_user_context),
):
    """Creates or updates a private chat owned by the signed-in user."""
    require_hosted_chat_enabled(user)
    owner_user_id = _require_authenticated_user(user)
    now = datetime.utcnow().isoformat() + "Z"
    chat = upsert_project_chat(
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        title=request.title or "Untitled chat",
        messages=[message.model_dump(mode="json", exclude_unset=True) for message in request.messages or []],
        created_at=now,
        updated_at=now,
    )
    return _chat_response(chat)


@app.delete("/chats/{chat_id}")
def delete_chat_endpoint(chat_id: str, user: UserContext = Depends(require_user_context)):
    """Deletes a private chat owned by the signed-in user."""
    require_hosted_chat_enabled()
    owner_user_id = _require_authenticated_user(user)
    deleted = delete_project_chat(chat_id, owner_user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return {"ok": True, "chat_id": chat_id}


@app.get("/projects/{project_id}/video-prompt")
def generate_project_video_prompt_endpoint(project_id: str, user: UserContext = Depends(optional_user_context)):
    """Builds an image-to-video prompt from Forma project namespaces."""
    require_hosted_chat_enabled()
    project = _resolve_project_reader(project_id, user)

    try:
        ir = HardwareIntermediateRepresentation(**project.hardware_ir)
        ir.assembly_metadata = hydrate_image_storage_metadata(ir.assembly_metadata, project.project_id)
        prompt_payload = generate_image_to_video_prompt_from_namespaces(ir)
        return {
            "project_id": project.project_id,
            **prompt_payload,
        }
    except Exception as e:
        logger.exception("Project video prompt generation failed for project_id=%s", project_id)
        raise HTTPException(status_code=500, detail=f"Video prompt generation failed: {str(e)}") from e


class ProgressiveVisualDecisionRequest(BaseModel):
    """Persist a human decision on the Progressive whole-system concept."""

    decision: str
    feedback: Optional[str] = None


@app.post("/projects/{project_id}/visual-decision")
def project_visual_decision_endpoint(
    project_id: str,
    request: ProgressiveVisualDecisionRequest,
    user: UserContext = Depends(require_user_context),
):
    """Approve/reject a Progressive visual and optionally continue into CAD."""

    require_hosted_chat_enabled()
    owner_user_id = _require_authenticated_user(user)
    try:
        resolved = resolve_project_for_read(project_id, owner_user_id)
    except ProjectReadError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc

    if resolved.source == "cli":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Progressive visual decisions are not supported for CLI-only projects.",
        )

    _require_project_owner(resolved.project, user)
    try:
        ensure_project_action_allowed(project_id, owner_user_id, "forma.iterate_project")
    except WorkflowStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.as_dict()) from exc

    if resolved.source == "canonical" and resolved.revision is not None:
        ir = resolved.revision.state.model_copy(deep=True)
        prompt = resolved.design_brief.summary if resolved.design_brief is not None else ""
        chat_id = resolved.design_brief.conversation_id if resolved.design_brief is not None else None
    else:
        ir = HardwareIntermediateRepresentation.model_validate(resolved.project.hardware_ir)
        prompt = str(getattr(resolved.project, "prompt", "") or "")
        chat_id = getattr(resolved.project, "chat_id", None)

    ir.assembly_metadata = {
        **(ir.assembly_metadata or {}),
        "project_id": project_id,
        "chat_id": chat_id or (ir.assembly_metadata or {}).get("chat_id"),
    }
    if not is_progressive_generation(ir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Visual decisions are only available for Progressive generation projects.",
        )

    lifecycle = load_design_lifecycle(ir)
    if not lifecycle.visual_gate.visual_artifact_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No whole-system visual is available for review.",
        )

    decision = str(request.decision or "").strip().lower()
    if decision not in {"approve", "revise", "continue_to_cad"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision must be approve, revise, or continue_to_cad.",
        )
    feedback = str(request.feedback or "").strip() or None
    if decision == "revise" and not feedback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Revision feedback is required when requesting changes.",
        )

    cad_generated = False
    try:
        if decision == "revise":
            lifecycle = record_visual_decision(ir, approved=False, feedback=feedback)
        else:
            if lifecycle.visual_gate.status != VisualApprovalStatus.APPROVED:
                lifecycle = record_visual_decision(ir, approved=True, feedback=feedback)
            if decision == "continue_to_cad":
                metadata = ir.assembly_metadata or {}
                cad_generated = ensure_native_cad_model(
                    ir,
                    project_id=project_id,
                    required=bool(metadata.get("cad_required", False)),
                    authoring_agent="forma-progressive-review",
                    workflow=str(metadata.get("workflow") or "default"),
                )
                lifecycle = load_design_lifecycle(ir)

        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "visual_approval_status": lifecycle.visual_gate.status.value,
            "visual_approval_feedback": lifecycle.visual_gate.feedback,
        }
        persisted = persist_chat_project_revision(
            project_id,
            owner_user_id,
            ir,
            source_job_id=f"visual-decision-{uuid4().hex}",
            prompt=prompt or getattr(getattr(ir, "overview", None), "title", "") or "Progressive design review",
            chat_id=chat_id,
        )
        persisted_ir = persisted.state.model_copy(deep=True)
        persisted_ir.assembly_metadata = {
            **(persisted_ir.assembly_metadata or {}),
            "project_id": project_id,
            "chat_id": chat_id or (persisted_ir.assembly_metadata or {}).get("chat_id"),
            "can_chat": True,
            "project_revision": persisted.revision,
        }
        persisted_lifecycle = load_design_lifecycle(persisted_ir)
        return {
            "ok": True,
            "project_id": project_id,
            "chat_id": chat_id,
            "can_chat": True,
            "prompt": prompt,
            "decision": decision,
            "cad_generated": cad_generated,
            "visual_approval_status": persisted_lifecycle.visual_gate.status.value,
            "project_ir": persisted_ir.model_dump(mode="json"),
        }
    except HTTPException:
        raise
    except (ValueError, ProjectStateError) as exc:
        status_code = status.HTTP_409_CONFLICT if isinstance(exc, ProjectStateError) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Progressive visual decision failed for project_id=%s", project_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not apply the Progressive visual decision.",
        ) from exc


@app.post("/projects/{project_id}/iterate")
def iterate_project_endpoint(
    project_id: str,
    request: IterateProjectRequest,
    user: UserContext = Depends(require_user_context),
):
    """Applies an iteration instruction to an existing project through forma_core."""
    require_hosted_chat_enabled()
    settings = _resolve_user_integrations(user)
    try:
        project = resolve_project_for_read(project_id, user.owner_user_id).project
    except ProjectReadError:
        project = None
    canonical_revision = None
    canonical_brief = None
    if project is not None:
        _require_project_reader(project, user)
        save_owner_user_id = _require_project_owner(project, user) if request.save else None
        current_ir = HardwareIntermediateRepresentation(**project.hardware_ir)
        project_prompt = project.prompt
        project_chat_id = getattr(project, "chat_id", None)
        project_created_at = project.created_at
        can_chat = _user_owns_project(project, user)
        if save_owner_user_id:
            try:
                canonical_revision = get_latest_project_revision(project_id, save_owner_user_id)
                canonical_brief = get_latest_design_brief(project_id, save_owner_user_id)
            except (ProjectStateError, DesignBriefNotFoundError):
                canonical_revision = None
                canonical_brief = None
            else:
                current_ir = canonical_revision.state
                project_prompt = canonical_brief.summary
                project_chat_id = canonical_brief.conversation_id
                project_created_at = canonical_revision.created_at
    else:
        save_owner_user_id = _require_authenticated_user(user)
        try:
            canonical_revision = get_latest_project_revision(project_id, save_owner_user_id)
            canonical_brief = get_latest_design_brief(project_id, save_owner_user_id)
        except (ProjectStateError, DesignBriefNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Project not found.") from exc
        current_ir = canonical_revision.state
        project_prompt = canonical_brief.summary
        project_chat_id = canonical_brief.conversation_id
        project_created_at = canonical_revision.created_at
        can_chat = True

    if save_owner_user_id:
        try:
            ensure_project_action_allowed(project_id, save_owner_user_id, "forma.iterate_project")
        except WorkflowStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.as_dict()) from exc

    try:
        iterator = ProjectIterator(provider_name=request.provider, model_name=request.model, settings=settings)
        revised_ir = iterator.iterate_project(
            current_ir,
            request.instruction,
            original_prompt=project_prompt,
            project_id=project_id,
            target_namespace=request.namespace,
        )
        revised_ir.assembly_metadata = hydrate_image_storage_metadata(revised_ir.assembly_metadata, project_id)
        if request.save:
            persisted_revision = append_project_revision(
                project_id,
                save_owner_user_id,
                revised_ir,
                source_job_id=f"iteration-{request.idempotency_key or uuid4().hex}",
            )
            revised_ir = persisted_revision.state
            if project is not None:
                if canonical_brief is not None:
                    publish_project_revision(persisted_revision, canonical_brief, save_owner_user_id)
                else:
                    refresh_legacy_project_projection(
                        project_id,
                        revised_ir.model_dump(mode="json"),
                        owner_user_id=save_owner_user_id,
                    )

        return {
            "project_id": project_id,
            "chat_id": project_chat_id or (revised_ir.assembly_metadata or {}).get("chat_id"),
            "prompt": project_prompt,
            "created_at": project_created_at,
            "can_chat": can_chat,
            "saved": request.save,
            "iteration": (revised_ir.assembly_metadata or {}).get("last_iteration"),
            "project_ir": revised_ir.model_dump(mode="json"),
            "project_object": build_project_object(revised_ir, target_namespace=request.namespace).model_dump(mode="json"),
            "mermaid_code": generate_mermaid_chart(revised_ir),
            "svg_schematic": generate_svg_schematic(revised_ir),
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=runtime_safe_error_message(str(e), provider=request.provider, model=request.model),
        ) from e
    except ProjectStateError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if e.retryable or e.code.endswith("conflict") else 404,
            detail=e.as_dict(),
        ) from e
    except LLMProviderConfigError as e:
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(
                code="llm_config_invalid",
                message=str(e),
                exc=e,
                provider=request.provider,
                model=request.model,
                context={"project_id": project_id, "instruction": request.instruction},
            ),
        ) from e
    except LLMProviderOutputError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=api_error_detail(
                code="llm_output_invalid",
                message=str(e),
                exc=e,
                provider=request.provider,
                model=request.model,
                context={"project_id": project_id, "instruction": request.instruction},
            ),
        ) from e
    except Exception as e:
        logger.exception("Project iteration failed for project_id=%s provider=%s model=%s", project_id, request.provider, request.model)
        raise HTTPException(
            status_code=500,
            detail=runtime_safe_error_message(
                f"Project iteration failed: {str(e)}",
                provider=request.provider,
                model=request.model,
            ),
        ) from e


def _stored_video_metadata_value(video: Any, keys: List[str]) -> str:
    metadata = getattr(video, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    lowered = {str(key).lower(): value for key, value in metadata.items()}
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        lowered_value = lowered.get(key.lower())
        if isinstance(lowered_value, str) and lowered_value.strip():
            return lowered_value.strip()
    return ""


def _normalized_url_path(value: str) -> str:
    if not value:
        return ""
    try:
        return urllib.parse.unquote(urllib.parse.urlparse(value).path).strip("/")
    except Exception:
        return ""


def _stored_video_matches_review_request(video: Any, *, video_url: str, video_key: Optional[str]) -> bool:
    requested_key = str(video_key or "").strip()
    stored_key = str(getattr(video, "key", "") or "").strip()
    stored_s3_uri = str(getattr(video, "s3_uri", "") or "").strip()
    if requested_key and requested_key in {stored_key, stored_s3_uri}:
        return True

    requested_url = str(video_url or "").strip()
    candidates = {
        str(getattr(video, "public_url", "") or "").strip(),
        str(getattr(video, "signed_url", "") or "").strip(),
        stored_s3_uri,
    }
    if requested_url and requested_url in candidates:
        return True

    requested_path = _normalized_url_path(requested_url)
    return bool(stored_key and requested_path and requested_path.endswith(stored_key))


def _resolve_stored_video_review_target(project_id: str, request: VideoSelfCorrectRequest) -> str:
    try:
        videos = list_project_videos(project_id)
    except Exception as exc:
        logger.exception("Video review target lookup failed for project_id=%s", project_id)
        raise HTTPException(
            status_code=400,
            detail="Video review requires a saved project video.",
        ) from exc

    matched_video = next(
        (
            video
            for video in videos
            if _stored_video_matches_review_request(video, video_url=request.video_url, video_key=request.video_key)
        ),
        None,
    )
    if matched_video is None:
        raise HTTPException(
            status_code=400,
            detail="Video review only supports saved videos for this project.",
        )

    review_url = (
        str(getattr(matched_video, "public_url", "") or "").strip()
        or str(getattr(matched_video, "signed_url", "") or "").strip()
        or request.video_url
    )
    if not review_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The saved video needs an HTTP(S) URL before it can be reviewed.")
    return review_url


@app.post("/projects/{project_id}/video-self-correct")
def video_self_correct_project_endpoint(
    project_id: str,
    request: VideoSelfCorrectRequest,
    user: UserContext = Depends(require_user_context),
):
    """Reviews a generated project video with a Fireworks native video model and applies a corrective iteration."""
    require_hosted_chat_enabled()
    settings = _resolve_user_integrations(user)
    project = _resolve_project_owner(project_id, user)
    owner_user_id = user.owner_user_id
    try:
        canonical_brief = get_latest_design_brief(project_id, owner_user_id)
    except DesignBriefNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project brief not found.") from exc

    try:
        current_ir = HardwareIntermediateRepresentation(**project.hardware_ir)
        review_video_url = _resolve_stored_video_review_target(project.project_id, request)
        agent = FireworksVideoSelfCorrectionAgent(
            review_client=FireworksVideoReviewClient(model=request.review_model),
            iterator=ProjectIterator(provider_name=request.provider, model_name=request.model, settings=settings),
        )
        revised_ir, review = agent.correct_project_from_video(
            current_ir,
            video_url=review_video_url,
            original_prompt=project.prompt,
            project_id=project.project_id,
            target_namespace=request.namespace,
        )
        revised_ir.assembly_metadata = hydrate_image_storage_metadata(revised_ir.assembly_metadata, project.project_id)
        if request.save:
            persisted_revision = append_project_revision(
                project.project_id,
                owner_user_id,
                revised_ir,
                source_job_id=f"video-correction-{request.idempotency_key or uuid4().hex}",
            )
            revised_ir = persisted_revision.state
            if canonical_brief is None:
                refresh_legacy_project_projection(
                    project.project_id,
                    revised_ir.model_dump(mode="json"),
                    owner_user_id=owner_user_id,
                )
            else:
                publish_project_revision(persisted_revision, canonical_brief, owner_user_id)

        target_namespace = (revised_ir.assembly_metadata or {}).get("iteration_target_namespace") or request.namespace
        return {
            "project_id": project.project_id,
            "prompt": project.prompt,
            "created_at": project.created_at,
            "saved": request.save,
            "video_review": review.model_dump(mode="json"),
            "iteration": (revised_ir.assembly_metadata or {}).get("last_iteration"),
            "project_ir": revised_ir.model_dump(mode="json"),
            "project_object": build_project_object(revised_ir, target_namespace=target_namespace).model_dump(mode="json"),
            "mermaid_code": generate_mermaid_chart(revised_ir),
            "svg_schematic": generate_svg_schematic(revised_ir),
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LLMProviderConfigError as e:
        raise HTTPException(
            status_code=400,
            detail=api_error_detail(
                code="video_review_config_invalid",
                message=str(e),
                exc=e,
                provider="fireworks",
                model=request.review_model,
                context={"project_id": project_id, "video_url": request.video_url, "video_key": request.video_key},
            ),
        ) from e
    except LLMProviderOutputError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=api_error_detail(
                code="video_review_output_invalid",
                message=str(e),
                exc=e,
                provider="fireworks",
                model=request.review_model,
                context={"project_id": project_id, "video_url": request.video_url, "video_key": request.video_key},
            ),
        ) from e
    except Exception as e:
        logger.exception("Video self-correction failed for project_id=%s", project_id)
        raise HTTPException(status_code=500, detail=f"Video self-correction failed: {str(e)}") from e

@app.get("/components")
def get_components_endpoint():
    """Returns the template library of seed electrical parts."""
    try:
        components = list_component_templates()
        return [
            {
                "id": c.id,
                "part_number": c.part_number,
                "name": c.name,
                "category": c.category,
                "description": c.description,
                "price": c.price,
                "sourcing_url": c.sourcing_url,
                "pins": c.pins,
                "use_cases": c.use_cases
            }
            for c in components
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/seed", status_code=status.HTTP_201_CREATED)
def trigger_db_seeding():
    """Manual trigger to re-seed the parts library database."""
    try:
        seed_database()
        return {"message": "Database templates successfully seeded."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ValidateCircuitRequest(BaseModel):
    components: List[ComponentInstance]
    nets: List[ConnectionNet]

@app.post("/validate", response_model=ValidationReport)
def validate_circuit_endpoint(request: ValidateCircuitRequest):
    """
    Accepts arbitrary list of parts and electrical connection nets.
    Runs rule checks and returns validation errors or warnings.
    """
    try:
        issues = validate_circuit(request.components, request.nets)
        is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in issues)
        return ValidationReport(is_valid=is_valid, issues=issues)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
