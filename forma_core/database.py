import logging
from forma_core.config import config
import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from forma_core.runtime import deployment_mode_enabled, forma_dev_mode_enabled
from forma_core.project_list_cache import invalidate_project_lists
from forma_core.workspaces.projects.objects import attach_project_object_metadata_to_dict
from forma_core.workspaces.design_briefs import DesignBrief, DesignBriefCreate
from forma_core.workspaces.projects import ProjectRevision, ProjectStateError, ProjectStateService
from forma_core.workspaces.projects.resolver import ProjectReadResolution, ProjectReadResolver
from forma_core.workspaces.readiness import (
    BuildInitiationOutcome,
    BuildMode,
    ProjectBuild,
    ProjectBuildService,
    ReadinessError,
    ReadinessResult,
    ReadinessStatus,
    evaluate_readiness,
)
from forma_core.workspaces.workflow import (
    ProjectWorkflow,
    ProjectWorkflowService,
    ProjectWorkflowState,
    ProjectWorkflowTransition,
    WorkflowActorType,
    WorkflowTransitionOutcome,
    ensure_action_allowed,
)
from forma_core.persistence.base import DatabaseProvider
from forma_core.persistence.models import (
    Base,
    DBAlphaSignup,
    DBComponentTemplate,
    DBProjectChat,
    DBUserIntegrationConfig,
    DBUserSettings,
    DBWorkspaceIntegrationConfig,
)
from forma_core.persistence.providers import SQLiteProvider, SupabaseProvider, create_sqlite_provider
from forma_core.persistence.repositories import (
    ApplicationRepository,
    SqlAlchemyRepository,
    SupabaseRepository,
)

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_DATABASE_URL = "sqlite:///./forma.db"
SUPABASE_KEY_ENV_VARS = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SECRET_KEY",
)
DATABASE_BACKEND_ENV_VARS = ("DATABASE_BACKEND", "DATABASE_PROVIDER", "DB_BACKEND", "DB_PROVIDER")


@dataclass(frozen=True)
class DatabaseConfig:
    backend: str
    source: str
    url: str


class DesignBriefNotFoundError(LookupError):
    """The requested project brief or version is not visible to the caller."""


class DesignBriefAccessError(PermissionError):
    """The project id is already owned by a different user."""


class CliProjectConflictError(RuntimeError):
    """A CLI sync write was based on a stale remote revision."""


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = config.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _backend_override() -> Optional[str]:
    aliases = {
        "sqlite": "sqlite",
        "sqlite3": "sqlite",
        "supabase": "supabase",
    }
    for name in DATABASE_BACKEND_ENV_VARS:
        value = _env(name)
        if not value:
            continue
        normalized = aliases.get(value.lower())
        if normalized:
            return normalized
        logger.warning("Ignoring unsupported %s=%r. Expected sqlite or supabase.", name, value)
    return None


def _sqlite_database_url() -> str:
    value = _env("SQLITE_DATABASE_URL") or _env("SQLITE_DB_URL") or DEFAULT_SQLITE_DATABASE_URL
    if "://" not in value:
        return f"sqlite:///{value}"
    return value


def _supabase_url() -> Optional[str]:
    return _env("SUPABASE_URL") or _env("NEXT_PUBLIC_SUPABASE_URL")


def _is_local_supabase_url(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def _supabase_key() -> tuple[Optional[str], Optional[str]]:
    for name in SUPABASE_KEY_ENV_VARS:
        value = _env(name)
        if value:
            return value, name
    return None, None


def _public_supabase_key_sources() -> List[str]:
    return [
        name
        for name in ("SUPABASE_KEY", "SUPABASE_ANON_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
        if _env(name)
    ]


def _warn_ignored_database_urls() -> None:
    ignored = [
        name
        for name in (
            "SUPABASE_DATABASE_URL",
            "SUPABASE_DB_URL",
            "SUPABASE_POSTGRES_URL",
            "SUPABASE_POOLER_URL",
            "DATABASE_URL",
            "POSTGRES_URL",
            "POSTGRES_URL_NON_POOLING",
        )
        if _env(name)
    ]
    if ignored:
        logger.warning(
            "Ignoring raw database URL env vars (%s). Supabase mode uses SUPABASE_URL plus "
            "SUPABASE_SERVICE_ROLE_KEY/SUPABASE_SECRET_KEY through the Supabase client.",
            ", ".join(ignored),
        )


def _build_supabase_client(url: str, key: str):
    try:
        import httpx
        from supabase import create_client
        from supabase.lib.client_options import SyncClientOptions
    except ImportError as exc:
        raise RuntimeError("Supabase client is not installed. Run pip install -r apps/api/requirements.txt.") from exc
    # The sync Supabase client enables HTTP/2 by default, but its httpcore stream
    # bookkeeping is not safe when shared by FastAPI's concurrent worker threads.
    http_client = httpx.Client(http2=False, follow_redirects=True, timeout=120)
    return create_client(url, key, options=SyncClientOptions(httpx_client=http_client))


def _select_database_config() -> tuple[DatabaseConfig, Any, Any]:
    override = _backend_override()
    sqlite_url = _sqlite_database_url()
    url = _supabase_url()

    if forma_dev_mode_enabled():
        if override == "supabase" and _is_local_supabase_url(url):
            logger.info("FORMA_DEV_MODE=true with local DATABASE_BACKEND=supabase; using local Supabase.")
        else:
            if override == "supabase":
                logger.warning("FORMA_DEV_MODE=true overrides remote DATABASE_BACKEND=supabase; using SQLite.")
            provider = create_sqlite_provider(source="FORMA_DEV_MODE", url=sqlite_url)
            return DatabaseConfig(backend="sqlite", source="FORMA_DEV_MODE", url=sqlite_url), provider.engine, None

    if override == "sqlite":
        provider = create_sqlite_provider(source="SQLITE_DATABASE_URL", url=sqlite_url)
        return DatabaseConfig(backend="sqlite", source="SQLITE_DATABASE_URL", url=sqlite_url), provider.engine, None

    key, key_source = _supabase_key()
    public_key_sources = _public_supabase_key_sources()
    if override == "supabase" and (not url or not key):
        public_key_hint = (
            f" Found public/anon key env vars instead: {', '.join(public_key_sources)}."
            if public_key_sources
            else ""
        )
        raise RuntimeError(
            "DATABASE_BACKEND=supabase requires SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY "
            "or SUPABASE_SECRET_KEY. The backend writes seed/project data and cannot use anon/publishable keys."
            f"{public_key_hint}"
        )

    if url and key:
        _warn_ignored_database_urls()
        client = _build_supabase_client(url, key)
        return DatabaseConfig(backend="supabase", source=f"SUPABASE_URL+{key_source}", url=url), None, client

    if url or key:
        if deployment_mode_enabled():
            raise RuntimeError(
                "FORMA_DEPLOYMENT_MODE=hosted requires SUPABASE_URL plus "
                "SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY. Refusing to fall back to local SQLite."
            )
        logger.warning(
            "Supabase client is partially configured. Provide both SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY/SUPABASE_SECRET_KEY. Falling back to SQLite."
        )
    else:
        if deployment_mode_enabled():
            raise RuntimeError(
                "FORMA_DEPLOYMENT_MODE=hosted requires Supabase persistence. Set DATABASE_BACKEND=sqlite "
                "only for an explicitly documented local/self-hosted deployment."
            )
        _warn_ignored_database_urls()

    provider = create_sqlite_provider(source="SQLITE_DATABASE_URL", url=sqlite_url)
    return DatabaseConfig(backend="sqlite", source="SQLITE_DATABASE_URL", url=sqlite_url), provider.engine, None


_ACTIVE_DATABASE_CONFIG, engine, _SUPABASE_CLIENT = _select_database_config()
DATABASE_BACKEND = _ACTIVE_DATABASE_CONFIG.backend
DATABASE_SOURCE = _ACTIVE_DATABASE_CONFIG.source
DATABASE_URL = _ACTIVE_DATABASE_CONFIG.url
_DATABASE_PROVIDER: DatabaseProvider
if DATABASE_BACKEND == "supabase":
    _DATABASE_PROVIDER = SupabaseProvider(
        source=DATABASE_SOURCE,
        url=DATABASE_URL,
        client=_SUPABASE_CLIENT,
    )
else:
    assert engine is not None
    _DATABASE_PROVIDER = SQLiteProvider(
        source=DATABASE_SOURCE,
        url=DATABASE_URL,
        engine=engine,
    )
_DATABASE_REPOSITORY: ApplicationRepository
if DATABASE_BACKEND == "supabase":
    SessionLocal = None
    _DATABASE_REPOSITORY = SupabaseRepository(_SUPABASE_CLIENT)
else:
    assert isinstance(_DATABASE_PROVIDER, SQLiteProvider)
    SessionLocal = _DATABASE_PROVIDER.session_factory
    assert SessionLocal is not None
    _DATABASE_REPOSITORY = SqlAlchemyRepository(SessionLocal)


def _canonical_project_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"generated_projects.project_id must be a UUID, got {value!r}.") from exc


def _normalize_chat_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_user_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_visibility(value: Optional[str]) -> str:
    normalized = (value or "public").strip().lower()
    return normalized if normalized in {"public", "private"} else "public"


def _hardware_ir_with_project_id(
    project_id: str,
    hardware_ir: Dict[str, Any],
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    hardware_ir = dict(hardware_ir or {})
    metadata = dict(hardware_ir.get("assembly_metadata") or {})
    metadata_project_id = metadata.get("project_id")
    if metadata_project_id and _canonical_project_id(metadata_project_id) != project_id:
        raise ValueError(
            "hardware_ir.assembly_metadata.project_id must match generated_projects.project_id."
        )
    metadata["project_id"] = project_id
    normalized_chat_id = _normalize_chat_id(chat_id) or _normalize_chat_id(metadata.get("chat_id"))
    if normalized_chat_id:
        metadata["chat_id"] = normalized_chat_id
    hardware_ir["assembly_metadata"] = metadata
    object_metadata = metadata.get("project_object") if isinstance(metadata.get("project_object"), dict) else {}
    target_namespace = metadata.get("iteration_target_namespace") or object_metadata.get("target_namespace")
    return attach_project_object_metadata_to_dict(hardware_ir, target_namespace=target_namespace)


def _sqlite_session():
    if SessionLocal is None:
        raise RuntimeError("SQLite session requested while Supabase backend is active.")
    return SessionLocal()


def get_supabase_client():
    if DATABASE_BACKEND != "supabase" or _SUPABASE_CLIENT is None:
        raise RuntimeError("Supabase client requested while SQLite backend is active.")
    return _SUPABASE_CLIENT


def get_database_provider() -> DatabaseProvider:
    """Return the provider selected once at application composition time."""

    return _DATABASE_PROVIDER


def init_db() -> None:
    _DATABASE_PROVIDER.initialize()


def get_db():
    db = _sqlite_session()
    try:
        yield db
    finally:
        db.close()


def count_component_templates() -> int:
    return _DATABASE_REPOSITORY.count_component_templates()


def list_component_templates() -> List[Any]:
    return _DATABASE_REPOSITORY.list_component_templates()


def get_component_template_by_part_number(part_number: str) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_component_template_by_part_number(part_number)


def insert_component_template_if_missing(component: Dict[str, Any]) -> bool:
    if get_component_template_by_part_number(component["part_number"]):
        return False

    record = {
        "part_number": component["part_number"],
        "name": component["name"],
        "category": component["category"],
        "description": component["description"],
        "price": component["price"],
        "sourcing_url": component["sourcing_url"],
        "pins": component["pins"],
        "use_cases": component["use_cases"],
    }
    _DATABASE_REPOSITORY.insert_component_template(record)
    return True


def save_generated_project(
    project_id: str,
    title: str,
    prompt: str,
    hardware_ir: Dict[str, Any],
    created_at: str,
    chat_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    visibility: Optional[str] = "public",
    create_chat_record: bool = True,
) -> None:
    project_id = _canonical_project_id(project_id)
    source_project_id = (hardware_ir.get("assembly_metadata") or {}).get("source_project_id") if isinstance(hardware_ir, dict) else None
    if source_project_id:
        source_project = get_generated_project(_canonical_project_id(source_project_id), include_deleted=True)
        if not source_project or getattr(source_project, "status", "active") != "active":
            raise RuntimeError("Cannot persist output for a deleted or missing source project.")
    hardware_ir = _hardware_ir_with_project_id(project_id, hardware_ir, chat_id=chat_id)
    metadata = hardware_ir.get("assembly_metadata") if isinstance(hardware_ir.get("assembly_metadata"), dict) else {}
    normalized_chat_id = _normalize_chat_id(chat_id) or _normalize_chat_id(metadata.get("chat_id"))
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    linked_brief = _DATABASE_REPOSITORY.get_latest_design_brief(project_id, None)
    if linked_brief is not None and getattr(linked_brief, "owner_user_id", None) != normalized_owner_user_id:
        raise DesignBriefAccessError("DesignBrief is not owned by the project owner.")
    linked_workflow = _DATABASE_REPOSITORY.get_project_workflow(project_id, None)
    if linked_workflow is not None and getattr(linked_workflow, "owner_user_id", None) != normalized_owner_user_id:
        raise DesignBriefAccessError("Workflow is not owned by the project owner.")
    normalized_visibility = _normalize_visibility(visibility)
    record = {
        "project_id": project_id,
        "chat_id": normalized_chat_id,
        "owner_user_id": normalized_owner_user_id,
        "creation_channel": "hosted",
        "visibility": normalized_visibility,
        "title": title,
        "prompt": prompt,
        "hardware_ir": hardware_ir,
        "created_at": created_at,
        "status": "active",
    }
    chat_record = None
    if create_chat_record and normalized_chat_id and normalized_owner_user_id:
        chat_record = {
            "chat_id": normalized_chat_id,
            "owner_user_id": normalized_owner_user_id,
            "title": title or prompt[:80] or "Untitled chat",
            "messages": [],
            "created_at": created_at,
            "updated_at": created_at,
        }
    _DATABASE_REPOSITORY.save_generated_project(record, chat_record)
    invalidate_project_lists()


def persist_legacy_project_projection(
    *,
    project_id: str,
    title: str,
    prompt: str,
    hardware_ir: Dict[str, Any],
    created_at: str,
    chat_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    visibility: Optional[str] = "public",
    create_chat_record: bool = True,
) -> None:
    """Write the retained generated-project compatibility projection."""

    save_generated_project(
        project_id=project_id,
        title=title,
        prompt=prompt,
        hardware_ir=hardware_ir,
        created_at=created_at,
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        visibility=visibility,
        create_chat_record=create_chat_record,
    )


def ensure_project_identity(
    project_id: str,
    owner_user_id: str,
    *,
    title: str = "Untitled Forma Project",
    prompt: str = "",
    chat_id: Optional[str] = None,
    created_at: Optional[str] = None,
    visibility: str = "public",
) -> Dict[str, Any]:
    """Create the canonical identity needed before a chat project can build."""

    canonical_project_id = _canonical_project_id(project_id)
    owner = _normalize_user_id(owner_user_id)
    if not owner:
        raise ValueError("owner_user_id is required.")
    existing_identity = _DATABASE_REPOSITORY.get_project_identity(canonical_project_id)
    if existing_identity is not None:
        existing_owner = _normalize_user_id(existing_identity.get("owner_user_id"))
        if existing_owner != owner:
            raise DesignBriefAccessError("Project is not owned by the current user.")
        if existing_identity.get("status", "active") != "active":
            raise DesignBriefAccessError("Cannot initialize a deleted project.")
        title = existing_identity.get("title") or title
        prompt = existing_identity.get("prompt") or prompt
        chat_id = existing_identity.get("chat_id") or chat_id
        created_at = existing_identity.get("created_at") or created_at
        visibility = existing_identity.get("visibility") or visibility
    timestamp = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record = {
        "project_id": canonical_project_id,
        "owner_user_id": owner,
        "creation_channel": "hosted",
        "title": str(title or "Untitled Forma Project").strip() or "Untitled Forma Project",
        "prompt": str(prompt or "").strip(),
        "chat_id": _normalize_chat_id(chat_id),
        "workspace_id": None,
        "visibility": _normalize_visibility(visibility),
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _DATABASE_REPOSITORY.upsert_project_identity(record)
    return record


def update_project_identity(
    project_id: str,
    owner_user_id: str,
    *,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
    visibility: Optional[str] = None,
) -> bool:
    """Update canonical project metadata; compatibility projections are rebuilt separately."""
    canonical_project_id = _canonical_project_id(project_id)
    owner = _normalize_user_id(owner_user_id)
    identity = _DATABASE_REPOSITORY.get_project_identity(canonical_project_id)
    if not owner or identity is None or _normalize_user_id(identity.get("owner_user_id")) != owner:
        return False
    if identity.get("status", "active") != "active":
        return False
    record = dict(identity)
    if title is not None:
        record["title"] = title.strip() or "Untitled Forma Project"
    if prompt is not None:
        record["prompt"] = prompt.strip()
    if visibility is not None:
        record["visibility"] = _normalize_visibility(visibility)
    record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _DATABASE_REPOSITORY.upsert_project_identity(record)
    invalidate_project_lists()
    return True


def ensure_chat_project(
    project_id: str,
    owner_user_id: str,
    *,
    prompt: str,
    chat_id: Optional[str] = None,
    title: Optional[str] = None,
) -> DesignBrief:
    """Bootstrap a chat project's canonical identity and first frozen brief."""

    canonical_project_id = _canonical_project_id(project_id)
    owner = _normalize_user_id(owner_user_id)
    if not owner:
        raise ValueError("owner_user_id is required.")
    existing_identity = get_project_identity(canonical_project_id)
    existing_legacy = None
    if existing_identity is None:
        # Pre-migration local databases still need to bootstrap into the
        # canonical identity/revision model without making the legacy row the
        # ongoing source of truth.
        existing_legacy = get_generated_project(canonical_project_id, include_deleted=True)
    existing = existing_identity or existing_legacy
    if existing is not None:
        existing_owner = _normalize_user_id(
            existing.get("owner_user_id") if isinstance(existing, dict) else getattr(existing, "owner_user_id", None)
        )
        if existing_owner != owner:
            raise DesignBriefAccessError("Project is not owned by the current user.")
        existing_status = existing.get("status", "active") if isinstance(existing, dict) else getattr(existing, "status", "active")
        if existing_status != "active":
            raise DesignBriefAccessError("Cannot initialize a deleted project.")

    summary = str(prompt or "").strip() or str(title or "Untitled Forma Project").strip()
    existing_title = existing.get("title") if isinstance(existing, dict) else getattr(existing, "title", None)
    existing_prompt = existing.get("prompt") if isinstance(existing, dict) else getattr(existing, "prompt", None)
    existing_created_at = existing.get("created_at") if isinstance(existing, dict) else getattr(existing, "created_at", None)
    existing_visibility = existing.get("visibility") if isinstance(existing, dict) else getattr(existing, "visibility", "public")
    identity = ensure_project_identity(
        canonical_project_id,
        owner,
        title=(title or existing_title or summary),
        prompt=existing_prompt or summary,
        chat_id=chat_id,
        created_at=existing_created_at,
        visibility=existing_visibility or "public",
    )
    canonical_chat_id = identity.get("chat_id") or _normalize_chat_id(chat_id)

    try:
        brief = get_latest_design_brief(canonical_project_id, owner)
    except DesignBriefNotFoundError:
        brief = create_design_brief_version(
            canonical_project_id,
            owner,
            DesignBriefCreate(
                schema_version="1.0",
                conversation_id=canonical_chat_id or f"project-{canonical_project_id}",
                intent=summary,
                summary=summary,
            ),
        )
    return brief


def persist_chat_project_revision(
    project_id: str,
    owner_user_id: str,
    state: Any,
    *,
    source_job_id: str,
    prompt: str,
    chat_id: Optional[str] = None,
    visibility: Optional[str] = None,
) -> ProjectRevision:
    """Commit generated chat output canonically, then refresh its gallery projection."""

    from forma_core.workers.generation import build_generation_draft
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

    canonical_project_id = _canonical_project_id(project_id)
    brief = ensure_chat_project(
        canonical_project_id,
        owner_user_id,
        prompt=prompt,
        chat_id=chat_id,
    )
    if visibility is not None:
        if not update_project_identity(
            canonical_project_id,
            owner_user_id,
            visibility=visibility,
        ):
            raise ValueError("Could not update the canonical project visibility.")
    service = ProjectStateService(_DATABASE_REPOSITORY)
    candidate = HardwareIntermediateRepresentation.model_validate(state)
    try:
        service.get_latest(canonical_project_id, owner_user_id)
    except ProjectStateError as exc:
        if exc.code != "project_revision_not_found":
            raise
        metadata = dict(candidate.assembly_metadata or {})
        metadata["project_id"] = canonical_project_id
        metadata.pop("revision", None)
        candidate.assembly_metadata = metadata
        revision = service.create_initial_revision(
            build_generation_draft(brief, candidate),
            project_id=canonical_project_id,
            owner_user_id=owner_user_id,
            design_brief_id=brief.design_brief_id,
            design_brief_version=brief.brief_version,
            source_job_id=source_job_id,
        ).revision
    else:
        revision = service.create_revision(
            build_generation_draft(brief, candidate),
            project_id=canonical_project_id,
            owner_user_id=owner_user_id,
            source_job_id=source_job_id,
        ).revision
    publish_project_revision(revision, brief, owner_user_id)
    return revision


def publish_project_revision(
    revision: ProjectRevision,
    brief: DesignBrief,
    owner_user_id: str,
    *,
    visibility: Optional[str] = None,
) -> str:
    """Project canonical state into the public gallery's generated-project store."""

    project_id = _canonical_project_id(str(revision.project_id))
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id or normalized_owner_user_id != revision.owner_user_id:
        raise ValueError("Project revision owner must match owner_user_id.")
    if revision.project_id != brief.project_id or revision.design_brief_id != brief.design_brief_id:
        raise ValueError("Project revision and DesignBrief identities must match.")

    ir = revision.state.model_copy(deep=True)
    ir.assembly_metadata = {
        **(ir.assembly_metadata or {}),
        "project_id": project_id,
        "chat_id": brief.conversation_id,
        "project_revision": revision.revision,
        "design_brief_version": revision.design_brief_version,
    }
    hardware_ir = ir.model_dump(mode="json")
    existing = get_generated_project(project_id, include_deleted=True)
    if existing is not None:
        if getattr(existing, "owner_user_id", None) != normalized_owner_user_id:
            raise DesignBriefAccessError("Project is not owned by the revision owner.")
        if getattr(existing, "status", "active") != "active":
            raise RuntimeError("Cannot publish a deleted project revision.")
        if not update_generated_project_hardware_ir(
            project_id,
            hardware_ir,
            owner_user_id=normalized_owner_user_id,
        ):
            raise RuntimeError("Could not refresh the generated-project gallery projection.")
        if visibility is not None and not update_generated_project_metadata(
            project_id,
            owner_user_id=normalized_owner_user_id,
            visibility=visibility,
        ):
            raise RuntimeError("Could not refresh the generated-project gallery visibility.")
        return project_id

    save_generated_project(
        project_id=project_id,
        title=(ir.overview.title if ir.overview else brief.summary) or "Untitled Forma Project",
        prompt=brief.summary,
        hardware_ir=hardware_ir,
        created_at=revision.created_at.isoformat().replace("+00:00", "Z"),
        chat_id=brief.conversation_id,
        owner_user_id=normalized_owner_user_id,
        visibility=visibility or "public",
        # Context gathering already owns the chat and its messages. Publishing
        # the gallery projection must not replace that thread with an empty one.
        create_chat_record=False,
    )
    return project_id


def list_generated_projects(owner_user_id: Optional[str] = None) -> List[Any]:
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    return _DATABASE_REPOSITORY.list_generated_projects(normalized_owner_user_id)


def list_generated_projects_page(
    owner_user_id: Optional[str] = None,
    *,
    visibility: Optional[str] = None,
    limit: int = 6,
    offset: int = 0,
    search: Optional[str] = None,
) -> tuple[List[Any], int]:
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    normalized_visibility = str(visibility or "").strip().lower() or None
    if normalized_visibility not in {None, "public", "private"}:
        raise ValueError("visibility must be public or private.")
    normalized_limit = max(1, min(int(limit), 50))
    normalized_offset = max(0, int(offset))
    normalized_search = " ".join(str(search or "").split())[:100] or None
    return _DATABASE_REPOSITORY.list_generated_projects_page(
        normalized_owner_user_id,
        visibility=normalized_visibility,
        limit=normalized_limit,
        offset=normalized_offset,
        search=normalized_search,
    )


def get_generated_project(project_id: str, *, include_deleted: bool = False) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_generated_project(project_id, include_deleted=include_deleted)


def get_project_identity(project_id: str) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_project_identity(_canonical_project_id(project_id))


def resolve_project_for_read(
    project_id: str,
    owner_user_id: Optional[str] = None,
    *,
    include_deleted: bool = False,
) -> ProjectReadResolution:
    """Resolve one readable project across generated, canonical, and CLI stores."""

    return ProjectReadResolver(_DATABASE_REPOSITORY).resolve(
        project_id,
        owner_user_id,
        include_deleted=include_deleted,
    )


def list_cli_projects(owner_user_id: str) -> List[Dict[str, Any]]:
    """Return private CLI project metadata without project payloads."""
    owner = _normalize_user_id(owner_user_id)
    if not owner:
        return []
    return [
        {
            "project_id": str(record.project_id),
            "workspace_id": getattr(record, "workspace_id", None),
            "creation_channel": getattr(record, "creation_channel", "cli"),
            "title": getattr(record, "title", ""),
            "revision_id": getattr(record, "current_revision_id", None),
            "revision": getattr(record, "current_revision", 0),
            "updated_at": getattr(record, "updated_at", None),
            "created_at": getattr(record, "created_at", None),
        }
        for record in _DATABASE_REPOSITORY.list_cli_projects(owner)
    ]


def list_project_identities(owner_user_id: str) -> List[Dict[str, Any]]:
    """Return the canonical project identities owned by a user."""
    owner = _normalize_user_id(owner_user_id)
    if not owner:
        return []
    return [
        {
            "project_id": str(record.project_id),
            "owner_user_id": getattr(record, "owner_user_id", owner),
            "creation_channel": getattr(record, "creation_channel", "hosted"),
            "title": getattr(record, "title", ""),
            "prompt": getattr(record, "prompt", ""),
            "chat_id": getattr(record, "chat_id", None),
            "workspace_id": getattr(record, "workspace_id", None),
            "visibility": getattr(record, "visibility", "public"),
            "status": getattr(record, "status", "active"),
            "current_revision": getattr(record, "current_revision", 0),
            "current_revision_id": getattr(record, "current_revision_id", None),
            "created_at": getattr(record, "created_at", None),
            "updated_at": getattr(record, "updated_at", None),
        }
        for record in _DATABASE_REPOSITORY.list_project_identities(owner)
    ]


def list_project_gallery_inventory_page(
    owner_user_id: Optional[str] = None,
    *,
    visibility: Optional[str] = None,
    limit: int = 6,
    offset: int = 0,
    search: Optional[str] = None,
) -> tuple[List[Any], int]:
    """Return a bounded page of canonical identities and current revisions."""
    owner = _normalize_user_id(owner_user_id)
    normalized_visibility = str(visibility or "").strip().lower() or None
    if normalized_visibility not in {None, "public", "private"}:
        raise ValueError("visibility must be public or private.")
    normalized_limit = max(1, min(int(limit), 50))
    normalized_offset = max(0, int(offset))
    normalized_search = " ".join(str(search or "").split())[:100] or None
    return _DATABASE_REPOSITORY.list_project_gallery_inventory_page(
        owner,
        visibility=normalized_visibility,
        limit=normalized_limit,
        offset=normalized_offset,
        search=normalized_search,
    )


def list_project_gallery_inventory(
    owner_user_id: Optional[str] = None,
    *,
    visibility: Optional[str] = None,
    search: Optional[str] = None,
) -> List[Any]:
    """Return the compatibility no-limit gallery inventory from the same source."""
    owner = _normalize_user_id(owner_user_id)
    normalized_visibility = str(visibility or "").strip().lower() or None
    if normalized_visibility not in {None, "public", "private"}:
        raise ValueError("visibility must be public or private.")
    normalized_search = " ".join(str(search or "").split())[:100] or None
    return _DATABASE_REPOSITORY.list_project_gallery_inventory(
        owner,
        visibility=normalized_visibility,
        search=normalized_search,
    )


def get_cli_project_revision(
    project_id: str,
    owner_user_id: str,
    revision_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    owner = _normalize_user_id(owner_user_id)
    project = str(project_id or "").strip()
    if not owner or not project:
        return None
    record = _DATABASE_REPOSITORY.get_cli_project_revision(project, owner, revision_id)
    if record is None:
        return None
    return {
        "revision_id": str(record.revision_id),
        "project_id": str(record.project_id),
        "revision": int(record.revision),
        "parent_revision_id": getattr(record, "parent_revision_id", None),
        "manifest": getattr(record, "manifest_json", {}),
        "created_at": getattr(record, "created_at", None),
    }


def insert_cli_project_revision(
    manifest: Dict[str, Any],
    owner_user_id: str,
    *,
    expected_revision_id: Optional[str] = None,
    revision_id: Optional[str] = None,
    revision: Optional[int] = None,
) -> Dict[str, Any]:
    """Create one public-by-default project revision with compare-and-swap ancestry."""
    owner = _normalize_user_id(owner_user_id)
    project_id = str(manifest.get("project_id") or "").strip()
    if not owner or not project_id:
        raise ValueError("A project_id and authenticated owner are required.")
    existing = _DATABASE_REPOSITORY.get_cli_project(project_id, owner)
    existing_identity = _DATABASE_REPOSITORY.get_project_identity(project_id)
    requested_visibility = manifest.get("visibility") or "public"
    if existing_identity is not None:
        requested_visibility = (
            getattr(existing_identity, "visibility", None)
            or (existing_identity.get("visibility") if isinstance(existing_identity, dict) else None)
            or requested_visibility
        )
    next_revision = int(revision) if revision is not None else (
        int(getattr(existing, "current_revision", 0)) + 1 if existing else 1
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    requested_revision_id = str(revision_id or uuid.uuid4())
    latest = _DATABASE_REPOSITORY.get_cli_project_revision(project_id, owner)
    if (
        latest is not None
        and int(getattr(latest, "revision", 0)) == next_revision
        and str(getattr(latest, "parent_revision_id", "") or "") == str(expected_revision_id or "")
        and getattr(latest, "manifest_json", None) == manifest
    ):
        return {
            "revision_id": str(latest.revision_id),
            "project_id": project_id,
            "revision": int(latest.revision),
            "parent_revision_id": getattr(latest, "parent_revision_id", None),
            "manifest": getattr(latest, "manifest_json", manifest),
            "created_at": getattr(latest, "created_at", None),
        }
    revision_record = {
        "revision_id": requested_revision_id,
        "project_id": project_id,
        "owner_user_id": owner,
        "revision": next_revision,
        "parent_revision_id": expected_revision_id,
        "manifest_json": manifest,
        "created_at": now,
    }
    project_record = {
        "project_id": project_id,
        "workspace_id": manifest.get("workspace_id"),
        "owner_user_id": owner,
        "creation_channel": "cli",
        "visibility": _normalize_visibility(str(requested_visibility)),
        "title": str(manifest.get("title") or "Untitled Forma Project"),
        "current_revision": 0,
        "current_revision_id": None,
        "created_at": now,
        "updated_at": now,
    }
    saved = _DATABASE_REPOSITORY.insert_cli_project_revision(
        project_record,
        revision_record,
        expected_revision_id,
    )
    if saved is None:
        raise CliProjectConflictError("The cloud project changed since the local project was last pulled.")
    return {
        "revision_id": str(getattr(saved, "revision_id", requested_revision_id)),
        "project_id": project_id,
        "revision": int(getattr(saved, "revision", next_revision)),
        "parent_revision_id": getattr(saved, "parent_revision_id", expected_revision_id),
        "manifest": getattr(saved, "manifest_json", manifest),
        "created_at": getattr(saved, "created_at", now),
    }


def _cli_delivery_from_record(record: Any) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    return {
        "delivery_id": str(record.delivery_id),
        "project_id": str(record.project_id),
        "owner_user_id": str(record.owner_user_id),
        "idempotency_key": str(record.idempotency_key),
        "revision_id": str(record.revision_id),
        "revision": int(record.revision),
        "parent_revision_id": getattr(record, "parent_revision_id", None),
        "manifest": getattr(record, "manifest_json", {}),
        "manifest_digest": getattr(record, "manifest_digest", "") or "",
        "status": str(getattr(record, "status", "pending")),
        "receipt": getattr(record, "receipt_json", None),
        "created_at": getattr(record, "created_at", None),
        "completed_at": getattr(record, "completed_at", None),
    }


def get_cli_project_delivery(
    project_id: str,
    owner_user_id: str,
    idempotency_key: str,
) -> Optional[Dict[str, Any]]:
    return _cli_delivery_from_record(
        _DATABASE_REPOSITORY.get_cli_project_delivery(
            str(project_id or "").strip(),
            _normalize_user_id(owner_user_id) or "",
            str(idempotency_key or "").strip(),
        )
    )


def get_cli_project_delivery_by_id(delivery_id: str) -> Optional[Dict[str, Any]]:
    return _cli_delivery_from_record(
        _DATABASE_REPOSITORY.get_cli_project_delivery_by_id(str(delivery_id or "").strip())
    )


def list_cli_project_deliveries(owner_user_id: str) -> List[Dict[str, Any]]:
    records = _DATABASE_REPOSITORY.list_cli_project_deliveries(_normalize_user_id(owner_user_id) or "")
    return [_cli_delivery_from_record(record) for record in records]


def insert_cli_project_delivery(record: Dict[str, Any]) -> Dict[str, Any]:
    saved = _DATABASE_REPOSITORY.insert_cli_project_delivery(record)
    if saved is None:
        raise ValueError("Delivery session could not be created.")
    delivery = _cli_delivery_from_record(saved)
    if delivery is None:
        raise ValueError("Delivery session could not be created.")
    return delivery


def update_cli_project_delivery(
    delivery_id: str,
    owner_user_id: str,
    updates: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    return _cli_delivery_from_record(
        _DATABASE_REPOSITORY.update_cli_project_delivery(
            str(delivery_id or "").strip(),
            _normalize_user_id(owner_user_id) or "",
            updates,
        )
    )


def publish_cli_project(
    project_id: str,
    owner_user_id: str,
    acting_user_id: str,
) -> Dict[str, Any]:
    """Flip an owned private CLI project to public and record the explicit action."""
    project_id = str(project_id or "").strip()
    owner = _normalize_user_id(owner_user_id)
    acting = _normalize_user_id(acting_user_id) or owner
    if not owner or not project_id:
        raise ValueError("A project_id and authenticated owner are required.")
    identity = _DATABASE_REPOSITORY.get_project_identity(project_id)
    if identity is None:
        raise ValueError("Project identity not found.")
    identity_owner = (
        identity.get("owner_user_id")
        if isinstance(identity, dict)
        else getattr(identity, "owner_user_id", None)
    )
    if identity_owner and str(identity_owner) != owner:
        raise ValueError("Project is owned by another user.")
    current = _normalize_visibility(
        identity.get("visibility")
        if isinstance(identity, dict)
        else getattr(identity, "visibility", None)
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if current == "public":
        _DATABASE_REPOSITORY.update_cli_project_visibility(project_id, owner, "public")
        return {"project_id": project_id, "visibility": "public", "published": False, "already_public": True}
    _DATABASE_REPOSITORY.upsert_project_identity(
        {
            "project_id": project_id,
            "owner_user_id": owner,
            "visibility": "public",
            "updated_at": now,
        }
    )
    _DATABASE_REPOSITORY.update_cli_project_visibility(project_id, owner, "public")
    _DATABASE_REPOSITORY.record_project_publish_audit(
        {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "owner_user_id": owner,
            "acting_user_id": acting,
            "visibility_before": current,
            "created_at": now,
        }
    )
    invalidate_project_lists()
    return {
        "project_id": project_id,
        "visibility": "public",
        "published": True,
        "visibility_before": current,
        "published_at": now,
    }


def list_project_publish_audits(project_id: str, owner_user_id: str) -> List[Dict[str, Any]]:
    records = _DATABASE_REPOSITORY.list_project_publish_audits(
        str(project_id or "").strip(),
        _normalize_user_id(owner_user_id) or "",
    )
    return [
        {
            "id": str(record.id),
            "project_id": str(record.project_id),
            "owner_user_id": str(record.owner_user_id),
            "acting_user_id": str(record.acting_user_id),
            "visibility_before": str(record.visibility_before),
            "created_at": getattr(record, "created_at", None),
        }
        for record in records
    ]


def get_cli_device_authorization(device_code_hash: Optional[str] = None, user_code_hash: Optional[str] = None) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_cli_device_authorization(device_code_hash, user_code_hash)


def insert_cli_device_authorization(record: Dict[str, Any]) -> Any:
    return _DATABASE_REPOSITORY.insert_cli_device_authorization(record)


def update_cli_device_authorization(
    device_code_hash: str,
    updates: Dict[str, Any],
    *,
    expected_status: Optional[str] = None,
    expected_consumed: Optional[bool] = None,
) -> Optional[Any]:
    return _DATABASE_REPOSITORY.update_cli_device_authorization(
        device_code_hash,
        updates,
        expected_status=expected_status,
        expected_consumed=expected_consumed,
    )


def get_cli_token_session(token_hash: str) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_cli_token_session(token_hash)


def insert_cli_token_session(record: Dict[str, Any]) -> Any:
    return _DATABASE_REPOSITORY.insert_cli_token_session(record)


def revoke_cli_token_sessions(
    *,
    token_hash: Optional[str] = None,
    refresh_token_hash: Optional[str] = None,
    revoked_at: float,
) -> int:
    return _DATABASE_REPOSITORY.revoke_cli_token_sessions(
        token_hash=token_hash,
        refresh_token_hash=refresh_token_hash,
        revoked_at=revoked_at,
    )


def _design_brief_from_record(record: Any) -> DesignBrief:
    payload = getattr(record, "payload_json", None)
    if not isinstance(payload, dict):
        raise ValueError("Persisted DesignBrief payload_json must be an object.")
    return DesignBrief.model_validate(payload)


def create_design_brief_version(
    project_id: str,
    owner_user_id: str,
    brief: DesignBriefCreate,
) -> DesignBrief:
    canonical_project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        raise ValueError("owner_user_id is required.")

    project = _DATABASE_REPOSITORY.get_generated_project(canonical_project_id, include_deleted=True)
    if project is not None:
        if getattr(project, "owner_user_id", None) != normalized_owner_user_id:
            raise DesignBriefAccessError("Project is not owned by the current user.")
        if getattr(project, "status", "active") != "active":
            raise DesignBriefAccessError("Cannot append a DesignBrief to a deleted project.")

    workflow = _DATABASE_REPOSITORY.get_project_workflow(canonical_project_id, None)
    if workflow is not None and getattr(workflow, "owner_user_id", None) != normalized_owner_user_id:
        raise DesignBriefAccessError("Project workflow is not owned by the current user.")

    previous = _DATABASE_REPOSITORY.get_latest_design_brief(canonical_project_id, None)
    if previous is not None and getattr(previous, "owner_user_id", None) != normalized_owner_user_id:
        raise DesignBriefAccessError("Project is not owned by the current user.")

    previous_brief = _design_brief_from_record(previous) if previous is not None else None
    next_version = previous_brief.brief_version + 1 if previous_brief else 1
    now = datetime.now(timezone.utc)
    design_brief = DesignBrief(
        **brief.model_dump(),
        design_brief_id=previous_brief.design_brief_id if previous_brief else uuid.uuid4(),
        project_id=canonical_project_id,
        brief_version=next_version,
        previous_version=previous_brief.brief_version if previous_brief else None,
        created_at=now,
    )
    record = {
        "id": str(uuid.uuid4()),
        "design_brief_id": str(design_brief.design_brief_id),
        "project_id": canonical_project_id,
        "conversation_id": design_brief.conversation_id,
        "owner_user_id": normalized_owner_user_id,
        "brief_version": design_brief.brief_version,
        "schema_version": design_brief.schema_version,
        "previous_version": design_brief.previous_version,
        "payload_json": design_brief.model_dump(mode="json"),
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }
    return _design_brief_from_record(_DATABASE_REPOSITORY.insert_design_brief_version(record))


def list_design_brief_versions(project_id: str, owner_user_id: str) -> List[DesignBrief]:
    canonical_project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        return []
    records = _DATABASE_REPOSITORY.list_design_brief_versions(
        canonical_project_id,
        normalized_owner_user_id,
    )
    return [_design_brief_from_record(record) for record in records]


def get_design_brief_version(
    project_id: str,
    owner_user_id: str,
    brief_version: int,
) -> DesignBrief:
    canonical_project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    record = None
    if normalized_owner_user_id and brief_version >= 1:
        record = _DATABASE_REPOSITORY.get_design_brief_version(
            canonical_project_id,
            normalized_owner_user_id,
            brief_version,
        )
    if record is None:
        raise DesignBriefNotFoundError("DesignBrief version not found.")
    return _design_brief_from_record(record)


def get_latest_design_brief(project_id: str, owner_user_id: str) -> DesignBrief:
    canonical_project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    record = None
    if normalized_owner_user_id:
        record = _DATABASE_REPOSITORY.get_latest_design_brief(
            canonical_project_id,
            normalized_owner_user_id,
        )
    if record is None:
        raise DesignBriefNotFoundError("DesignBrief not found.")
    return _design_brief_from_record(record)


def evaluate_project_readiness(project_id: str, owner_user_id: str) -> ReadinessResult:
    try:
        brief = get_latest_design_brief(project_id, owner_user_id)
    except DesignBriefNotFoundError as exc:
        raise ReadinessError("design_brief_not_found", "DesignBrief not found.") from exc
    return evaluate_readiness(brief)


def _project_build_from_record(record: Any) -> ProjectBuild:
    return ProjectBuild.model_validate({
        "build_id": record.id,
        "project_id": record.project_id,
        "owner_user_id": record.owner_user_id,
        "design_brief_id": record.design_brief_id,
        "brief_version": record.brief_version,
        "brief_snapshot": record.brief_snapshot_json,
        "mode": record.mode,
        "readiness": record.readiness_result_json,
        "introduced_assumptions": record.introduced_assumptions_json,
        "warnings": record.warnings_json,
        "transition_id": record.transition_id,
        "idempotency_key": record.idempotency_key,
        "initiated_by": record.initiated_by,
        "created_at": record.created_at,
    })


def get_latest_project_build(project_id: str, owner_user_id: str) -> ProjectBuild:
    canonical_project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    record = None
    if normalized_owner_user_id:
        record = _DATABASE_REPOSITORY.get_latest_project_build(canonical_project_id, normalized_owner_user_id)
    if record is None:
        raise ReadinessError("project_build_not_found", "Project build not found.")
    return _project_build_from_record(record)


def initiate_project_build(
    project_id: str,
    owner_user_id: str,
    *,
    mode: BuildMode,
    actor_id: str,
    assumptions: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
    resolve_unanswered_questions: bool = False,
) -> BuildInitiationOutcome:
    canonical_project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        raise ValueError("owner_user_id is required.")
    try:
        latest = get_latest_design_brief(canonical_project_id, normalized_owner_user_id)
    except DesignBriefNotFoundError as exc:
        raise ReadinessError("design_brief_not_found", "DesignBrief not found.") from exc
    readiness = evaluate_readiness(latest)
    service = ProjectBuildService(_DATABASE_REPOSITORY)
    normalized_key = str(idempotency_key or "").strip() or None
    normalized_assumptions = list(dict.fromkeys(
        normalized
        for item in assumptions or []
        if (normalized := str(item or "").strip())
    ))

    if normalized_key and _DATABASE_REPOSITORY.get_project_build_by_idempotency(
        canonical_project_id, normalized_owner_user_id, normalized_key
    ) is not None:
        return service.initiate(
            latest,
            readiness,
            normalized_owner_user_id,
            mode=mode,
            actor_id=actor_id,
            introduced_assumptions=normalized_assumptions,
            warnings=[],
            idempotency_key=normalized_key,
        )

    # Validate workflow ownership/state before appending an assumption-bearing brief.
    workflow = get_project_workflow(canonical_project_id, normalized_owner_user_id)
    if workflow.state not in {ProjectWorkflowState.GATHERING_CONTEXT, ProjectWorkflowState.READY_TO_BUILD}:
        raise ReadinessError(
            "build_transition_not_allowed",
            f"Build cannot start while the project workflow is {workflow.state.value}.",
            context={"project_id": canonical_project_id, "workflow_state": workflow.state.value},
        )

    frozen_brief = latest
    initial_readiness = readiness
    warnings: List[str] = []
    if mode == BuildMode.BUILD_ANYWAY:
        if readiness.status == ReadinessStatus.BLOCKED and not resolve_unanswered_questions:
            raise ReadinessError(
                "critical_readiness_blockers",
                "Build Anyway cannot bypass critical project unknowns.",
                context=readiness.model_dump(mode="json"),
            )
        if resolve_unanswered_questions:
            normalized_assumptions = list(dict.fromkeys([
                *normalized_assumptions,
                *(
                    f"Build agents will choose a safe prototype default for: {question}"
                    for question in latest.unresolved_questions
                ),
            ]))
        if readiness.status != ReadinessStatus.READY and not normalized_assumptions:
            raise ReadinessError(
                "build_anyway_assumptions_required",
                "Build Anyway requires explicit assumptions for incomplete context.",
            )
        if normalized_assumptions:
            payload = latest.model_dump(exclude={
                "design_brief_id", "project_id", "brief_version", "previous_version", "created_at"
            })
            payload["assumptions"] = list(dict.fromkeys([*latest.assumptions, *normalized_assumptions]))
            if resolve_unanswered_questions:
                payload["unresolved_questions"] = []
            frozen_brief = create_design_brief_version(
                canonical_project_id,
                normalized_owner_user_id,
                DesignBriefCreate.model_validate(payload),
            )
            readiness = evaluate_readiness(frozen_brief)
        if readiness.status == ReadinessStatus.BLOCKED:
            raise ReadinessError(
                "critical_readiness_blockers",
                "Build Anyway cannot bypass critical project unknowns.",
                context=readiness.model_dump(mode="json"),
            )
        warnings = [
            f"Build Anyway bypassed non-critical blocker {blocker.code}: {blocker.message}"
            for blocker in initial_readiness.unresolved_blockers
            if not blocker.critical
        ]
        if resolve_unanswered_questions:
            warnings.extend(
                f"Conversational build delegated blocker {blocker.code} to the build agents: {blocker.message}"
                for blocker in initial_readiness.unresolved_blockers
                if blocker.critical
            )
        warnings.extend(f"Execution relies on user assumption: {assumption}" for assumption in normalized_assumptions)

    return service.initiate(
        frozen_brief,
        readiness,
        normalized_owner_user_id,
        mode=mode,
        actor_id=actor_id,
        introduced_assumptions=normalized_assumptions,
        warnings=warnings,
        idempotency_key=normalized_key,
    )


def initialize_project_workflow(
    project_id: str,
    owner_user_id: str,
    *,
    actor_type: WorkflowActorType = WorkflowActorType.SYSTEM,
    actor_id: Optional[str] = None,
    reason: str = "Project workflow initialized.",
    chat_id: Optional[str] = None,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
) -> WorkflowTransitionOutcome:
    outcome = ProjectWorkflowService(_DATABASE_REPOSITORY).initialize(
        project_id,
        owner_user_id,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
    )
    if chat_id or title or prompt:
        ensure_project_identity(
            project_id,
            owner_user_id,
            title=title or "Untitled Forma Project",
            prompt=prompt or "",
            chat_id=chat_id,
        )
    return outcome


def get_project_workflow(project_id: str, owner_user_id: str) -> ProjectWorkflow:
    return ProjectWorkflowService(_DATABASE_REPOSITORY).get(project_id, owner_user_id)


def get_latest_project_revision(project_id: str, owner_user_id: str) -> ProjectRevision:
    """Read the latest immutable state through the canonical project boundary."""

    return ProjectStateService(_DATABASE_REPOSITORY).get_latest(project_id, owner_user_id)


def get_project_revision_by_source_job(
    project_id: str,
    owner_user_id: str,
    source_job_id: str,
) -> Optional[ProjectRevision]:
    """Return an existing revision for an idempotent worker/source delivery."""
    return ProjectStateService(_DATABASE_REPOSITORY).get_by_source_job(project_id, owner_user_id, source_job_id)


def list_project_revisions(project_id: str, owner_user_id: str, *, limit: int = 21, before: int | None = None) -> List[ProjectRevision]:
    """Return a bounded page of the owner's saved snapshots."""
    return ProjectStateService(_DATABASE_REPOSITORY).list_revisions(project_id, owner_user_id, limit=limit, before=before)


def get_project_revision_by_id(project_id: str, owner_user_id: str, revision_id: str) -> ProjectRevision:
    """Resolve the immutable revision referenced by a chat result or history entry."""
    return ProjectStateService(_DATABASE_REPOSITORY).get_revision_by_id(project_id, owner_user_id, revision_id)


def list_latest_project_revisions(owner_user_id: str) -> List[ProjectRevision]:
    """List each owned project's latest immutable canonical revision."""

    owner = _normalize_user_id(owner_user_id)
    if not owner:
        return []
    revisions: List[ProjectRevision] = []
    for record in _DATABASE_REPOSITORY.list_latest_project_revisions(owner):
        payload = getattr(record, "payload_json", None)
        if not isinstance(payload, dict):
            logger.warning(
                "Skipping project revision with invalid payload while listing owner projects: project_id=%s",
                getattr(record, "project_id", "unknown"),
            )
            continue
        try:
            revisions.append(ProjectRevision.model_validate(payload))
        except Exception:
            logger.exception(
                "Skipping invalid canonical project revision while listing owner projects: project_id=%s",
                getattr(record, "project_id", "unknown"),
            )
    return revisions


def append_project_revision(
    project_id: str,
    owner_user_id: str,
    state: Any,
    *,
    source_job_id: str,
) -> ProjectRevision:
    """Persist an iteration as the next immutable canonical project revision."""

    from forma_core.workers.generation import build_generation_draft
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

    service = ProjectStateService(_DATABASE_REPOSITORY)
    try:
        parent = service.get_latest(project_id, owner_user_id)
    except ProjectStateError as exc:
        if exc.code != "project_revision_not_found":
            raise

        # Legacy generated_projects rows have no immutable history. Migrate the
        # current projection once before appending the requested iteration.
        legacy = get_generated_project(project_id, include_deleted=True)
        if legacy is None or getattr(legacy, "status", "active") != "active":
            raise
        if str(getattr(legacy, "owner_user_id", "") or "").strip() != str(owner_user_id or "").strip():
            raise ProjectStateError("project_revision_not_found", "Project revision not found.")

        project_uuid = _canonical_project_id(project_id)
        try:
            brief = get_latest_design_brief(project_id, owner_user_id)
        except DesignBriefNotFoundError:
            prompt = str(getattr(legacy, "prompt", "") or "").strip()
            title = str(getattr(legacy, "title", "") or "").strip()
            brief = create_design_brief_version(
                project_uuid,
                owner_user_id,
                DesignBriefCreate(
                    schema_version="1.0",
                    conversation_id=str(getattr(legacy, "chat_id", "") or "").strip()
                    or f"project-{project_uuid}",
                    intent=prompt or title or "Update an existing hardware project.",
                    summary=prompt or title or "Update an existing hardware project.",
                ),
            )

        baseline = HardwareIntermediateRepresentation.model_validate(getattr(legacy, "hardware_ir", {}))
        baseline.assembly_metadata = {
            **(baseline.assembly_metadata or {}),
            "project_id": str(project_uuid),
            "revision": 1,
        }
        baseline_draft = build_generation_draft(brief, baseline)
        try:
            service.create_initial_revision(
                baseline_draft,
                project_id=project_uuid,
                owner_user_id=owner_user_id,
                design_brief_id=brief.design_brief_id,
                design_brief_version=brief.brief_version,
                source_job_id=f"legacy-migration-{project_uuid}",
            )
        except ProjectStateError as migration_exc:
            if migration_exc.code != "initial_project_revision_exists":
                raise
        parent = service.get_latest(project_id, owner_user_id)

    brief = service.get_frozen_design_brief(
        project_id,
        owner_user_id,
        parent.design_brief_id,
        parent.design_brief_version,
    )
    draft = build_generation_draft(brief, HardwareIntermediateRepresentation.model_validate(state))
    revision = service.create_revision(
        draft,
        project_id=project_id,
        owner_user_id=owner_user_id,
        source_job_id=source_job_id,
    ).revision
    invalidate_project_lists()
    return revision


def create_project_generation_plan(
    build: ProjectBuild,
    owner_user_id: str,
    *,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    generation_mode: str = "regular",
):
    """Create or replay the durable initial-generation plan for one frozen build."""

    from forma_core.workers import (
        GENERATION_CAPABILITY_ID,
        GENERATION_INPUT_VERSION,
        GENERATION_WORKER_ID,
        WORKER_CONTRACT_VERSION,
        GenerationWorker,
        HardwareIntermediateRepresentationGenerationEngine,
        WorkerOrchestrator,
        WorkerRequest,
    )
    from forma_core.workspaces.projects.generation_mode import GenerationMode

    try:
        normalized_generation_mode = GenerationMode(str(generation_mode or "regular").strip().lower())
    except ValueError as exc:
        raise ValueError("generation_mode must be regular or progressive.") from exc

    owner = _normalize_user_id(owner_user_id)
    if not owner or owner != build.owner_user_id:
        raise ValueError("The build owner must match owner_user_id.")
    plan_id = f"build-plan-{build.build_id}"
    existing = _DATABASE_REPOSITORY.get_worker_execution_plan(plan_id, owner)
    engine = HardwareIntermediateRepresentationGenerationEngine(provider_name=provider_name, model_name=model_name)
    worker = GenerationWorker(ProjectStateService(_DATABASE_REPOSITORY), engine)
    orchestrator = WorkerOrchestrator(
        _DATABASE_REPOSITORY,
        [worker],
        workflow_service=ProjectWorkflowService(_DATABASE_REPOSITORY),
    )
    if existing is not None:
        return orchestrator.get_plan(plan_id, owner)

    job_id = f"generation-{build.build_id}"
    request = WorkerRequest(
        contract_version=WORKER_CONTRACT_VERSION,
        project_id=build.project_id,
        project_revision=1,
        design_brief_id=build.design_brief_id,
        design_brief_version=build.brief_version,
        job_id=job_id,
        correlation_id=f"build-{build.build_id}",
        worker_id=GENERATION_WORKER_ID,
        capability_id=GENERATION_CAPABILITY_ID,
        input_contract_version=GENERATION_INPUT_VERSION,
        payload={"design_brief": build.brief_snapshot.model_dump(mode="json")},
        metadata={
            "build_id": str(build.build_id),
            "cad_required": False,
            "generation_mode": normalized_generation_mode.value,
            **(
                {"visual_approval_policy": "require_approval"}
                if normalized_generation_mode == GenerationMode.PROGRESSIVE
                else {}
            ),
        },
    )
    return orchestrator.create_plan([request], owner, max_concurrency=1, plan_id=plan_id)


def get_project_generation_plan(plan_id: str, owner_user_id: str):
    """Return a persisted worker plan without exposing the backing repository."""

    from forma_core.workers import GenerationWorker, WorkerOrchestrator

    owner = _normalize_user_id(owner_user_id)
    orchestrator = WorkerOrchestrator(
        _DATABASE_REPOSITORY,
        [GenerationWorker(ProjectStateService(_DATABASE_REPOSITORY))],
        workflow_service=ProjectWorkflowService(_DATABASE_REPOSITORY),
    )
    return orchestrator.get_plan(plan_id, owner)


def list_project_generation_jobs(
    *,
    status: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Project durable generation-plan tasks in the admin job record shape."""

    from forma_core.workers import OrchestrationTaskStatus, WorkerExecutionPlan

    normalized_status = str(status or "").strip().lower()
    records = _DATABASE_REPOSITORY.list_worker_execution_plans(limit=limit)
    jobs: List[Dict[str, Any]] = []
    for record in records:
        state = getattr(record, "state_json", None)
        if not isinstance(state, dict):
            logger.warning("Skipping worker execution plan with invalid state_json.")
            continue
        try:
            plan = WorkerExecutionPlan.model_validate(state)
        except Exception:
            logger.exception("Skipping invalid worker execution plan while listing admin jobs.")
            continue

        for job_id, task in plan.jobs.items():
            job_status = task.status.value
            if task.status == OrchestrationTaskStatus.BLOCKED:
                job_status = "failed"
            if normalized_status and normalized_status != "all" and normalized_status != job_status:
                continue

            brief = task.request.payload.get("design_brief")
            brief = brief if isinstance(brief, dict) else {}
            progress_events = [
                event
                for progress in task.progress
                for event in [progress.metadata.get("pipeline_event")]
                if isinstance(event, dict)
            ]
            error = task.error.message if task.error is not None else None
            completed_at = task.completed_at or (task.result.completed_at if task.result else None)
            result_output = task.result.output if task.result is not None else {}
            project_revision = result_output.get("project_revision")
            project_revision = project_revision if isinstance(project_revision, dict) else {}
            project_state = project_revision.get("state")
            project_state = project_state if isinstance(project_state, dict) else {}
            assembly_metadata = project_state.get("assembly_metadata")
            assembly_metadata = assembly_metadata if isinstance(assembly_metadata, dict) else {}
            image_status = assembly_metadata.get("image_output_status")
            image_error = assembly_metadata.get("image_output_error") or assembly_metadata.get("product_image_error")
            jobs.append(
                {
                    "job_id": job_id,
                    "message_id": plan.plan_id,
                    "correlation_id": plan.correlation_id,
                    "action": "forma.generate_project",
                    "sender": "conversation",
                    "recipient": task.request.worker_id,
                    "status": job_status,
                    "server_owned": True,
                    "created_at": plan.created_at.isoformat(),
                    "updated_at": plan.updated_at.isoformat(),
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "completed_at": completed_at.isoformat() if completed_at else None,
                    "payload": {
                        "owner_user_id": plan.owner_user_id,
                        "project_id": plan.project_id,
                        "prompt": brief.get("intent") or brief.get("summary"),
                        "design_brief_id": str(task.request.design_brief_id),
                        "design_brief_version": task.request.design_brief_version,
                    },
                    "result_summary": {
                        "project_id": plan.project_id,
                        "title": brief.get("summary") or brief.get("intent"),
                        "workflow": "default",
                        "image_output_status": image_status,
                        "image_output_failed": bool(
                            assembly_metadata.get("image_output_failed") or image_status == "failed"
                        ),
                        "image_output_error": image_error,
                        "image_output_error_type": assembly_metadata.get("image_output_error_type"),
                        "image_output_reason": assembly_metadata.get("image_output_reason"),
                        "image_output_debug": assembly_metadata.get("image_output_debug"),
                        "image_output_provider": assembly_metadata.get("image_output_provider"),
                        "image_output_model": assembly_metadata.get("image_output_model"),
                        "operation_statuses": assembly_metadata.get("operation_statuses") or [],
                        "operation_summary": assembly_metadata.get("operation_summary"),
                    },
                    "source_usage": {"workflow": "default", "source_labels": ["Conversation"]},
                    "progress_events": progress_events,
                    "error": error,
                    "error_debug": task.error.model_dump(mode="json") if task.error is not None else None,
                }
            )
            if len(jobs) >= limit:
                return jobs
    return jobs


async def execute_project_generation_plan(
    plan_id: str,
    owner_user_id: str,
    *,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    cancellation_check: Optional[Callable[[], bool]] = None,
):
    """Execute or resume a persisted project-generation plan."""

    from forma_core.workers import GenerationWorker, HardwareIntermediateRepresentationGenerationEngine, WorkerOrchestrator

    owner = _normalize_user_id(owner_user_id)
    worker = GenerationWorker(
        ProjectStateService(_DATABASE_REPOSITORY),
        HardwareIntermediateRepresentationGenerationEngine(provider_name=provider_name, model_name=model_name),
        project_publisher=publish_project_revision,
    )
    orchestrator = WorkerOrchestrator(
        _DATABASE_REPOSITORY,
        [worker],
        workflow_service=ProjectWorkflowService(_DATABASE_REPOSITORY),
        cancellation_check=cancellation_check,
    )
    plan = await orchestrator.execute(plan_id, owner)
    invalidate_project_lists()
    return plan


async def cancel_project_generation_plan(plan_id: str, owner_user_id: str):
    """Persist cancellation for a project-generation plan and its active worker."""

    from forma_core.workers import GenerationWorker, WorkerOrchestrator

    owner = _normalize_user_id(owner_user_id)
    orchestrator = WorkerOrchestrator(
        _DATABASE_REPOSITORY,
        [GenerationWorker(ProjectStateService(_DATABASE_REPOSITORY))],
        workflow_service=ProjectWorkflowService(_DATABASE_REPOSITORY),
    )
    return await orchestrator.cancel(plan_id, owner)


async def reset_project_generation_plan(plan_id: str, owner_user_id: str):
    """Reset failed generation work while preserving the frozen project brief."""

    from forma_core.workers import GenerationWorker, WorkerOrchestrator

    owner = _normalize_user_id(owner_user_id)
    orchestrator = WorkerOrchestrator(
        _DATABASE_REPOSITORY,
        [GenerationWorker(ProjectStateService(_DATABASE_REPOSITORY))],
        workflow_service=ProjectWorkflowService(_DATABASE_REPOSITORY),
    )
    return await orchestrator.reset(plan_id, owner)


def list_project_workflow_transitions(
    project_id: str,
    owner_user_id: str,
) -> List[ProjectWorkflowTransition]:
    return ProjectWorkflowService(_DATABASE_REPOSITORY).history(project_id, owner_user_id)


def transition_project_workflow(
    project_id: str,
    owner_user_id: str,
    to_state: ProjectWorkflowState,
    *,
    actor_type: WorkflowActorType,
    actor_id: Optional[str],
    reason: str,
    idempotency_key: Optional[str] = None,
) -> WorkflowTransitionOutcome:
    return ProjectWorkflowService(_DATABASE_REPOSITORY).transition(
        project_id,
        owner_user_id,
        to_state,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def ensure_project_action_allowed(
    project_id: str,
    owner_user_id: str,
    action: str,
    *,
    require_workflow: bool = False,
) -> None:
    """Apply project workflow execution policy without coupling callers to persistence."""

    try:
        workflow = ProjectWorkflowService(_DATABASE_REPOSITORY).get(project_id, owner_user_id)
    except WorkflowStateError:
        if not require_workflow:
            return
        raise
    ensure_action_allowed(workflow, action)


def list_due_project_purges(before: str, limit: int = 25) -> List[Any]:
    return _DATABASE_REPOSITORY.list_due_project_purges(before, max(1, min(limit, 100)))


def update_project_deletion_state(
    project_id: str,
    *,
    owner_user_id: Optional[str],
    allowed_statuses: List[str],
    updates: Dict[str, Any],
    expected_purge_started_at: Optional[str] = None,
) -> Optional[Any]:
    project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    updated = _DATABASE_REPOSITORY.update_project_deletion_state(
        project_id,
        normalized_owner_user_id,
        allowed_statuses,
        updates,
        expected_purge_started_at,
    )
    if updated:
        invalidate_project_lists()
    return updated


def hard_purge_generated_project(project_id: str, owner_user_id: Optional[str] = None) -> bool:
    project_id = _canonical_project_id(project_id)
    purged = _DATABASE_REPOSITORY.hard_purge_project(project_id, _normalize_user_id(owner_user_id))
    if purged:
        invalidate_project_lists()
    return purged


def update_generated_project_hardware_ir(
    project_id: str,
    hardware_ir: Dict[str, Any],
    owner_user_id: Optional[str] = None,
) -> bool:
    project_id = _canonical_project_id(project_id)
    hardware_ir = _hardware_ir_with_project_id(project_id, hardware_ir)
    metadata = hardware_ir.get("assembly_metadata") if isinstance(hardware_ir.get("assembly_metadata"), dict) else {}
    chat_id = _normalize_chat_id(metadata.get("chat_id"))
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    updated = _DATABASE_REPOSITORY.update_generated_project_hardware_ir(
        project_id,
        hardware_ir,
        chat_id,
        normalized_owner_user_id,
    )
    if updated:
        invalidate_project_lists()
    return updated


def refresh_legacy_project_projection(
    project_id: str,
    hardware_ir: Dict[str, Any],
    *,
    owner_user_id: Optional[str] = None,
) -> bool:
    """Refresh the retained legacy projection during compatibility reads."""

    return update_generated_project_hardware_ir(
        project_id,
        hardware_ir,
        owner_user_id=owner_user_id,
    )


def claim_unowned_generated_project(
    project_id: str,
    hardware_ir: Dict[str, Any],
    owner_user_id: str,
) -> bool:
    project_id = _canonical_project_id(project_id)
    hardware_ir = _hardware_ir_with_project_id(project_id, hardware_ir)
    metadata = hardware_ir.get("assembly_metadata") if isinstance(hardware_ir.get("assembly_metadata"), dict) else {}
    chat_id = _normalize_chat_id(metadata.get("chat_id"))
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        return False
    identity_claimed = False
    identity = _DATABASE_REPOSITORY.get_project_identity(project_id)
    if identity is not None and not _normalize_user_id(identity.get("owner_user_id")):
        if identity.get("status", "active") != "active":
            return False
        identity_record = dict(identity)
        identity_record["owner_user_id"] = normalized_owner_user_id
        if chat_id:
            identity_record["chat_id"] = chat_id
        identity_record["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _DATABASE_REPOSITORY.upsert_project_identity(identity_record)
        invalidate_project_lists()
        identity_claimed = True
    claimed = _DATABASE_REPOSITORY.claim_unowned_generated_project(
        project_id,
        hardware_ir,
        chat_id,
        normalized_owner_user_id,
    )
    if claimed or identity_claimed:
        invalidate_project_lists()
    return claimed or identity_claimed


def _hardware_ir_with_overview_title(hardware_ir: Any, title: str) -> Optional[Dict[str, Any]]:
    if hardware_ir is None:
        return None
    if hasattr(hardware_ir, "model_dump"):
        payload = hardware_ir.model_dump(mode="json")
    elif isinstance(hardware_ir, dict):
        payload = dict(hardware_ir)
    else:
        return None
    overview = payload.get("overview")
    next_overview = dict(overview) if isinstance(overview, dict) else {}
    next_overview["title"] = title
    payload["overview"] = next_overview
    return payload


def update_generated_project_metadata(
    project_id: str,
    *,
    owner_user_id: str,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
    visibility: Optional[str] = None,
) -> bool:
    project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        return False
    updates: Dict[str, Any] = {}
    if title is not None:
        next_title = title.strip() or "Untitled Forma Project"
        updates["title"] = next_title
        project = get_generated_project(project_id)
        hardware_ir = _hardware_ir_with_overview_title(
            None if project is None else getattr(project, "hardware_ir", None),
            next_title,
        )
        if hardware_ir is not None:
            updates["hardware_ir"] = hardware_ir
    if prompt is not None:
        updates["prompt"] = prompt.strip()
    if visibility is not None:
        updates["visibility"] = _normalize_visibility(visibility)
    if not updates:
        return True
    updated = _DATABASE_REPOSITORY.update_generated_project_metadata(
        project_id,
        normalized_owner_user_id,
        updates,
    )
    if updated:
        invalidate_project_lists()
    return updated


def delete_generated_project(project_id: str, owner_user_id: str) -> bool:
    project_id = _canonical_project_id(project_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        return False
    deleted = _DATABASE_REPOSITORY.delete_generated_project(project_id, normalized_owner_user_id)
    if deleted:
        invalidate_project_lists()
    return deleted


def get_project_contribution_consent(project_id: str, user_id: str) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_project_contribution_consent(
        _canonical_project_id(project_id),
        _normalize_user_id(user_id) or "",
    )


def upsert_project_contribution_consent(record: Dict[str, Any]) -> Any:
    normalized = dict(record)
    normalized["project_id"] = _canonical_project_id(normalized["project_id"])
    normalized["user_id"] = _normalize_user_id(normalized.get("user_id")) or ""
    if not normalized["user_id"]:
        raise ValueError("Contribution consent requires a user id.")
    return _DATABASE_REPOSITORY.upsert_project_contribution_consent(normalized)


def withdraw_project_contribution_consent(project_id: str, user_id: str, withdrawn_at: str) -> Optional[Any]:
    return _DATABASE_REPOSITORY.withdraw_project_contribution_consent(
        _canonical_project_id(project_id),
        _normalize_user_id(user_id) or "",
        withdrawn_at,
    )


def anonymize_project_contribution_consent(project_id: str, user_id: str, anonymized_at: str) -> bool:
    return _DATABASE_REPOSITORY.anonymize_project_contribution_consent(
        _canonical_project_id(project_id),
        _normalize_user_id(user_id) or "",
        str(uuid.uuid4()),
        f"anonymous-{uuid.uuid4()}",
        anonymized_at,
    )


def upsert_project_contribution_snapshot(record: Dict[str, Any]) -> Any:
    normalized = dict(record)
    normalized["source_project_id"] = _canonical_project_id(normalized["source_project_id"])
    return _DATABASE_REPOSITORY.upsert_project_contribution_snapshot(normalized)


def anonymize_project_contribution_snapshot(consent_record_id: str, anonymized_at: str) -> bool:
    return _DATABASE_REPOSITORY.anonymize_project_contribution_snapshot(
        consent_record_id,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        anonymized_at,
    )


def purge_project_contribution_snapshots(consent_record_id: str, purged_at: str) -> int:
    return _DATABASE_REPOSITORY.purge_project_contribution_snapshots(consent_record_id, purged_at)


def add_project_deletion_audit(record: Dict[str, Any]) -> Any:
    normalized = dict(record)
    normalized["project_id"] = _canonical_project_id(normalized["project_id"])
    normalized["acting_user_id"] = _normalize_user_id(normalized.get("acting_user_id"))
    return _DATABASE_REPOSITORY.add_project_deletion_audit(normalized)


def get_latest_project_deletion_audit(project_id: str) -> Optional[Any]:
    return _DATABASE_REPOSITORY.get_latest_project_deletion_audit(_canonical_project_id(project_id))


def list_project_deletion_audits(limit: int = 100) -> List[Any]:
    return _DATABASE_REPOSITORY.list_project_deletion_audits(max(1, min(limit, 500)))


def upsert_project_chat(
    *,
    chat_id: str,
    owner_user_id: str,
    title: str,
    messages: Optional[List[Dict[str, Any]]] = None,
    created_at: str,
    updated_at: str,
) -> Any:
    normalized_chat_id = _normalize_chat_id(chat_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_chat_id or not normalized_owner_user_id:
        raise ValueError("chat_id and owner_user_id are required.")
    for message in messages or []:
        linked_project_id = None
        if isinstance(message, dict):
            linked_project_id = message.get("projectId") or message.get("project_id")
        if not linked_project_id:
            continue
        try:
            linked_project = get_generated_project(str(linked_project_id), include_deleted=True)
        except ValueError:
            linked_project = None
        if linked_project is not None:
            linked_project_is_active = getattr(linked_project, "status", "active") == "active"
        else:
            try:
                get_latest_project_revision(str(linked_project_id), normalized_owner_user_id)
                linked_project_is_active = True
            except (ProjectStateError, ValueError):
                linked_project_is_active = False
        if not linked_project_is_active:
            raise ValueError("Cannot write chat data for a deleted or missing project.")
    record = {
        "chat_id": normalized_chat_id,
        "owner_user_id": normalized_owner_user_id,
        "title": title.strip() or "Untitled chat",
        "messages": messages or [],
        "created_at": created_at,
        "updated_at": updated_at,
    }
    return _DATABASE_REPOSITORY.upsert_project_chat(record)


def list_project_chats(owner_user_id: str) -> List[Any]:
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        return []
    return _DATABASE_REPOSITORY.list_project_chats(normalized_owner_user_id)


def get_project_chat(chat_id: str, owner_user_id: str) -> Optional[Any]:
    normalized_chat_id = _normalize_chat_id(chat_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_chat_id or not normalized_owner_user_id:
        return None
    return _DATABASE_REPOSITORY.get_project_chat(normalized_chat_id, normalized_owner_user_id)


def delete_project_chat(chat_id: str, owner_user_id: str) -> bool:
    normalized_chat_id = _normalize_chat_id(chat_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_chat_id or not normalized_owner_user_id:
        return False
    return _DATABASE_REPOSITORY.delete_project_chat(normalized_chat_id, normalized_owner_user_id)


def save_alpha_signup(
    *,
    name: str,
    email: str,
    organization: Optional[str],
    additional_info: Optional[str],
    source: str,
    metadata: Optional[Dict[str, Any]],
    created_at: str,
) -> Any:
    record = {
        "name": name,
        "email": email.lower(),
        "organization": organization,
        "additional_info": additional_info,
        "source": source,
        "metadata_json": metadata or {},
        "created_at": created_at,
    }
    return _DATABASE_REPOSITORY.save_alpha_signup(record)


def project_engagement_for_ids(
    project_ids: List[str],
    owner_user_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    normalized_ids: List[str] = []
    seen: set[str] = set()
    for value in project_ids:
        project_id = str(value or "").strip()
        if not project_id or project_id in seen:
            continue
        seen.add(project_id)
        normalized_ids.append(project_id)
    save_counts = _DATABASE_REPOSITORY.count_project_saves(normalized_ids)
    remix_counts = _DATABASE_REPOSITORY.count_project_remixes(normalized_ids)
    saved_ids = set()
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if normalized_owner_user_id:
        saved_ids = set(_DATABASE_REPOSITORY.list_saved_project_ids(normalized_owner_user_id, normalized_ids))
    return {
        project_id: {
            "save_count": int(save_counts.get(project_id, 0)),
            "remix_count": int(remix_counts.get(project_id, 0)),
            "saved": project_id in saved_ids,
        }
        for project_id in normalized_ids
    }


def _project_engagement_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save_project_for_user(project_id: str, owner_user_id: str) -> Dict[str, Any]:
    normalized_project_id = str(project_id or "").strip()
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_project_id or not normalized_owner_user_id:
        raise ValueError("project_id and owner_user_id are required.")
    created = _DATABASE_REPOSITORY.insert_project_save(
        {
            "project_id": normalized_project_id,
            "owner_user_id": normalized_owner_user_id,
            "created_at": _project_engagement_timestamp(),
        }
    )
    if created:
        invalidate_project_lists()
    engagement = project_engagement_for_ids([normalized_project_id], normalized_owner_user_id).get(
        normalized_project_id,
        {"save_count": 0, "remix_count": 0, "saved": True},
    )
    return {
        "saved": True,
        "save_count": int(engagement.get("save_count") or 0),
        "remix_count": int(engagement.get("remix_count") or 0),
    }


def unsave_project_for_user(project_id: str, owner_user_id: str) -> Dict[str, Any]:
    normalized_project_id = str(project_id or "").strip()
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_project_id or not normalized_owner_user_id:
        raise ValueError("project_id and owner_user_id are required.")
    deleted = _DATABASE_REPOSITORY.delete_project_save(normalized_project_id, normalized_owner_user_id)
    if deleted:
        invalidate_project_lists()
    engagement = project_engagement_for_ids([normalized_project_id], normalized_owner_user_id).get(
        normalized_project_id,
        {"save_count": 0, "remix_count": 0, "saved": False},
    )
    return {
        "saved": False,
        "save_count": int(engagement.get("save_count") or 0),
        "remix_count": int(engagement.get("remix_count") or 0),
    }


def remix_generated_project(source_project_id: str, owner_user_id: str) -> Optional[Any]:
    source = get_generated_project(str(source_project_id or "").strip())
    if source is None:
        return None
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        raise ValueError("owner_user_id is required.")
    new_project_id = str(uuid.uuid4())
    new_chat_id = str(uuid.uuid4())
    created_at = _project_engagement_timestamp()
    source_ir = getattr(source, "hardware_ir", None)
    hardware_ir = copy.deepcopy(source_ir) if isinstance(source_ir, dict) else {}
    metadata = hardware_ir.get("assembly_metadata") if isinstance(hardware_ir.get("assembly_metadata"), dict) else {}
    metadata = dict(metadata)
    metadata["project_id"] = new_project_id
    metadata["chat_id"] = new_chat_id
    metadata["source_project_id"] = source.project_id
    metadata["remixed_from_project_id"] = source.project_id
    hardware_ir["assembly_metadata"] = metadata
    title = getattr(source, "title", None) or "Untitled project"
    record = {
        "project_id": new_project_id,
        "chat_id": new_chat_id,
        "owner_user_id": normalized_owner_user_id,
        "visibility": _normalize_visibility(getattr(source, "visibility", None)),
        "title": title,
        "prompt": getattr(source, "prompt", None) or "",
        "hardware_ir": hardware_ir,
        "created_at": created_at,
        "status": "active",
    }
    chat_record = {
        "chat_id": new_chat_id,
        "owner_user_id": normalized_owner_user_id,
        "title": (title.strip()[:80] if isinstance(title, str) else "") or "Untitled chat",
        "messages": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    _DATABASE_REPOSITORY.save_generated_project(record, chat_record)
    _DATABASE_REPOSITORY.insert_project_remix(
        {
            "remix_project_id": new_project_id,
            "source_project_id": source.project_id,
            "owner_user_id": normalized_owner_user_id,
            "created_at": created_at,
        }
    )
    invalidate_project_lists()
    return get_generated_project(new_project_id)


def get_user_settings(owner_user_id: str) -> Optional[Any]:
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        return None
    return _DATABASE_REPOSITORY.get_user_settings(normalized_owner_user_id)


def set_user_model_training_preference(
    owner_user_id: str,
    *,
    allow_model_training: bool,
    updated_at: str,
) -> Any:
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if not normalized_owner_user_id:
        raise ValueError("owner_user_id is required.")
    existing = _DATABASE_REPOSITORY.get_user_settings(normalized_owner_user_id)
    record = {
        "owner_user_id": normalized_owner_user_id,
        "model_training_opt_out": not allow_model_training,
        "created_at": getattr(existing, "created_at", None) or updated_at,
        "updated_at": updated_at,
    }
    return _DATABASE_REPOSITORY.upsert_user_settings(record)


def list_model_training_opt_out_user_ids() -> List[str]:
    """Return owner ids that dataset exports must exclude from model training."""

    return _DATABASE_REPOSITORY.list_model_training_opt_out_user_ids()


def get_database_config() -> Dict[str, Any]:
    config = _DATABASE_PROVIDER.describe()
    config["dev_mode"] = forma_dev_mode_enabled()
    return config
