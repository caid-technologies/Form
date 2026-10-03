"""Deterministic read-side resolution across Forma project storage surfaces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import logging
from types import SimpleNamespace
from typing import Any, Optional

from forma_core.persistence.repositories.base import ApplicationRepository
from forma_core.workspaces.design_briefs import DesignBrief
from forma_core.workspaces.projects.identity import ProjectCreationChannel
from forma_core.workspaces.projects.manifest import ProjectManifest
from forma_core.workspaces.projects.state import ProjectRevision, ProjectStateError, ProjectStateService


logger = logging.getLogger(__name__)


class ProjectReadError(LookupError):
    """Base error for a project that cannot be returned to the reader."""


class ProjectReadNotFoundError(ProjectReadError):
    """The project is missing or intentionally hidden from the reader."""


class ProjectReadAccessError(ProjectReadError, PermissionError):
    """The project exists but is not readable by the supplied user."""


@dataclass(frozen=True)
class ProjectReadResolution:
    """One normalized read result regardless of the backing project source."""

    project_id: str
    source: str
    creation_channel: ProjectCreationChannel
    owner_user_id: Optional[str]
    title: str
    prompt: str
    chat_id: Optional[str]
    created_at: Any
    updated_at: Any
    visibility: str
    status: str
    current_revision: Optional[int]
    revision_id: Optional[str]
    project_ir: dict[str, Any]
    image_metadata: dict[str, Any]
    can_chat: bool
    legacy_fallback: bool
    project: Any
    revision: Optional[ProjectRevision] = None
    design_brief: Optional[DesignBrief] = None


def _owner(value: Any) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


def _creation_channel(value: Any) -> ProjectCreationChannel:
    try:
        return ProjectCreationChannel(str(value or ProjectCreationChannel.HOSTED).strip().lower())
    except ValueError:
        return ProjectCreationChannel.HOSTED


def _project_ir_metadata(project_ir: Any) -> dict[str, Any]:
    if not isinstance(project_ir, dict):
        return {}
    metadata = project_ir.get("assembly_metadata")
    return deepcopy(metadata) if isinstance(metadata, dict) else {}


def _identity_record(value: Any) -> Any:
    """Ignore unconfigured mock repositories while accepting SQL records."""
    if isinstance(value, dict) or type(value).__name__ == "SimpleNamespace":
        return value
    return None


def _record_value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _brief(repository: ApplicationRepository, project_id: str, owner_user_id: str) -> DesignBrief:
    record = repository.get_latest_design_brief(project_id, owner_user_id)
    payload = getattr(record, "payload_json", None) if record is not None else None
    if not isinstance(payload, dict):
        raise ProjectReadNotFoundError("Project DesignBrief not found.")
    return DesignBrief.model_validate(payload)


class ProjectReadResolver:
    """Resolve the authoritative readable representation of one project."""

    def __init__(self, repository: ApplicationRepository) -> None:
        self._repository = repository
        self._state = ProjectStateService(repository)

    def resolve(
        self,
        project_id: str,
        owner_user_id: Optional[str] = None,
        *,
        include_deleted: bool = False,
    ) -> ProjectReadResolution:
        normalized_project_id = str(project_id or "").strip()
        normalized_owner = _owner(owner_user_id)
        if not normalized_project_id:
            raise ProjectReadNotFoundError("Project not found.")

        identity = _identity_record(self._repository.get_project_identity(normalized_project_id))
        identity_owner = _owner(_record_value(identity, "owner_user_id"))
        identity_status = str(_record_value(identity, "status", "active") or "active").strip().lower()
        identity_visibility = str(_record_value(identity, "visibility", "public") or "public").strip().lower()
        identity_channel = str(_record_value(identity, "creation_channel", "hosted") or "hosted").strip().lower()
        if identity is not None:
            is_owner = bool(normalized_owner and normalized_owner == identity_owner)
            if identity_status != "active" and (not include_deleted or not is_owner):
                raise ProjectReadNotFoundError("Project not found.")
            if identity_status == "active" and not is_owner and identity_visibility != "public":
                raise ProjectReadNotFoundError("Project not found.")

        generated = self._repository.get_generated_project(normalized_project_id, include_deleted=True)
        if generated is not None:
            return self._resolve_generated(
                normalized_project_id,
                generated,
                normalized_owner,
                include_deleted=include_deleted,
                identity=identity,
            )

        if normalized_owner or (identity is not None and identity_visibility == "public"):
            revision_owner = normalized_owner or identity_owner
            if identity_channel != "cli":
                canonical = self._resolve_canonical_only(
                    normalized_project_id,
                    revision_owner,
                    identity=identity,
                    reader_owner_user_id=normalized_owner,
                )
                if canonical is not None:
                    return canonical
            cli = self._resolve_cli(normalized_project_id, normalized_owner or identity_owner, identity=identity)
            if cli is not None:
                return cli
        raise ProjectReadNotFoundError("Project not found.")

    def _resolve_generated(
        self,
        project_id: str,
        generated: Any,
        owner_user_id: Optional[str],
        *,
        include_deleted: bool,
        identity: Any = None,
    ) -> ProjectReadResolution:
        status = str(getattr(generated, "status", "active") or "active").strip().lower()
        project_owner = _owner(getattr(generated, "owner_user_id", None))
        if identity is not None:
            project_owner = _owner(_record_value(identity, "owner_user_id")) or project_owner
            status = str(_record_value(identity, "status", status) or status).strip().lower()
        is_owner = bool(owner_user_id and project_owner == owner_user_id)
        visibility = str(getattr(generated, "visibility", "public") or "public").strip().lower()
        if identity is not None:
            visibility = str(_record_value(identity, "visibility", visibility) or visibility).strip().lower()

        if status != "active":
            if not include_deleted or not is_owner:
                raise ProjectReadNotFoundError("Project not found.")
        elif not is_owner and visibility != "public":
            raise ProjectReadNotFoundError("Project not found.")

        if is_owner and status == "active":
            try:
                revision = self._state.get_latest(project_id, owner_user_id)
                brief = _brief(self._repository, project_id, owner_user_id)
            except (ProjectStateError, ProjectReadNotFoundError, ValueError):
                revision = None
                brief = None
            if revision is not None and brief is not None:
                project_ir = revision.state.model_dump(mode="json")
                metadata = _project_ir_metadata(project_ir)
                project = SimpleNamespace(
                    project_id=project_id,
                    owner_user_id=project_owner,
                    creation_channel=getattr(generated, "creation_channel", "hosted"),
                    chat_id=brief.conversation_id,
                    title=str(getattr(getattr(revision.state, "overview", None), "title", "") or getattr(generated, "title", "")),
                    prompt=brief.summary,
                    created_at=getattr(generated, "created_at", revision.created_at),
                    updated_at=getattr(generated, "updated_at", revision.created_at),
                    visibility=visibility,
                    hardware_ir=project_ir,
                )
                return ProjectReadResolution(
                    project_id=project_id,
                    source="generated",
                    creation_channel=_creation_channel(getattr(generated, "creation_channel", "hosted")),
                    owner_user_id=project_owner,
                    title=project.title,
                    prompt=project.prompt,
                    chat_id=project.chat_id if is_owner else None,
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                    visibility=visibility,
                    status=status,
                    current_revision=revision.revision,
                    revision_id=str(revision.revision_id),
                    project_ir=project_ir,
                    image_metadata=metadata,
                    can_chat=is_owner,
                    legacy_fallback=False,
                    project=project,
                    revision=revision,
                    design_brief=brief,
                )

        project_ir = getattr(generated, "hardware_ir", {})
        project_ir = deepcopy(project_ir) if isinstance(project_ir, dict) else {}
        metadata = _project_ir_metadata(project_ir)
        logger.info(
            "project_read_resolver_legacy_fallback project_id=%s source=generated status=%s",
            project_id,
            status,
        )
        raw_revision = metadata.get("project_revision", metadata.get("revision"))
        try:
            current_revision = int(raw_revision) if raw_revision is not None else None
        except (TypeError, ValueError):
            current_revision = None
        return ProjectReadResolution(
            project_id=project_id,
            source="generated",
            creation_channel=_creation_channel(getattr(generated, "creation_channel", "hosted")),
            owner_user_id=project_owner,
            title=str(getattr(generated, "title", "") or "Untitled project"),
            prompt=str(getattr(generated, "prompt", "") or ""),
            chat_id=getattr(generated, "chat_id", None) if is_owner and status == "active" else None,
            created_at=getattr(generated, "created_at", None),
            updated_at=getattr(generated, "updated_at", None),
            visibility=visibility,
            status=status,
            current_revision=current_revision,
            revision_id=None,
            project_ir=project_ir,
            image_metadata=metadata,
            can_chat=is_owner and status == "active",
            legacy_fallback=True,
            project=generated,
        )

    def _resolve_canonical_only(
        self,
        project_id: str,
        owner_user_id: str,
        *,
        identity: Any = None,
        reader_owner_user_id: Optional[str] = None,
    ) -> Optional[ProjectReadResolution]:
        try:
            revision = self._state.get_latest(project_id, owner_user_id)
        except (ProjectStateError, ProjectReadNotFoundError, ValueError):
            return None
        try:
            brief = _brief(self._repository, project_id, owner_user_id)
        except (ProjectReadNotFoundError, ValueError):
            # A revision is sufficient for image-summary reads; design briefs are
            # optional context and should not make a valid project look missing.
            brief = None
        project_ir = revision.state.model_dump(mode="json")
        metadata = _project_ir_metadata(project_ir)
        overview = getattr(revision.state, "overview", None)
        title = str(
            _record_value(identity, "title", "")
            or getattr(overview, "title", "")
            or getattr(brief, "summary", "")
            or "Untitled project"
        )
        prompt = str(getattr(brief, "summary", "") or getattr(overview, "description", "") or "")
        visibility = str(_record_value(identity, "visibility", "public") or "public")
        status = str(_record_value(identity, "status", "active") or "active")
        project_owner = _owner(_record_value(identity, "owner_user_id")) or owner_user_id
        chat_id = getattr(brief, "conversation_id", None) if reader_owner_user_id == project_owner else None
        project = SimpleNamespace(
            project_id=project_id,
            owner_user_id=project_owner,
            creation_channel="hosted",
            chat_id=chat_id,
            title=title,
            prompt=prompt,
            created_at=revision.created_at,
            updated_at=revision.created_at,
            visibility=visibility,
            status=status,
            deleted_at=_record_value(identity, "deleted_at"),
            deletion_requested_by=_record_value(identity, "deletion_requested_by"),
            purge_after=_record_value(identity, "purge_after"),
            purge_started_at=_record_value(identity, "purge_started_at"),
            purge_completed_at=_record_value(identity, "purge_completed_at"),
            deletion_error=_record_value(identity, "deletion_error"),
            hardware_ir=project_ir,
        )
        return ProjectReadResolution(
            project_id=project_id,
            source="canonical",
            creation_channel=ProjectCreationChannel.HOSTED,
            owner_user_id=project_owner,
            title=title,
            prompt=prompt,
            chat_id=chat_id,
            created_at=revision.created_at,
            updated_at=revision.created_at,
            visibility=visibility,
            status=status,
            current_revision=revision.revision,
            revision_id=str(revision.revision_id),
            project_ir=project_ir,
            image_metadata=metadata,
            can_chat=brief is not None and reader_owner_user_id == project_owner,
            legacy_fallback=False,
            project=project,
            revision=revision,
            design_brief=brief,
        )

    def _resolve_cli(self, project_id: str, owner_user_id: str, *, identity: Any = None) -> Optional[ProjectReadResolution]:
        record = self._repository.get_cli_project_revision(project_id, owner_user_id, None)
        if record is None:
            return None
        manifest_payload = getattr(record, "manifest_json", None)
        if not isinstance(manifest_payload, dict):
            return None
        try:
            manifest = ProjectManifest.from_document(manifest_payload)
        except (TypeError, ValueError):
            logger.warning("project_read_resolver_invalid_cli_manifest project_id=%s", project_id)
            return None
        project_ir = deepcopy(manifest.project_ir)
        metadata = _project_ir_metadata(project_ir)
        metadata.update({"project_id": project_id, "chat_id": None, "project_source": "cli"})
        project_ir["assembly_metadata"] = metadata
        project = SimpleNamespace(
            project_id=project_id,
            owner_user_id=owner_user_id,
            creation_channel="cli",
            chat_id=None,
            title=manifest.title or "Untitled project",
            prompt=manifest.prompt,
            created_at=getattr(record, "created_at", None),
            updated_at=getattr(record, "created_at", None),
            visibility=str(_record_value(identity, "visibility", "public") or "public"),
            status=str(_record_value(identity, "status", "active") or "active") if identity else "active",
            deleted_at=_record_value(identity, "deleted_at"),
            deletion_requested_by=_record_value(identity, "deletion_requested_by"),
            purge_after=_record_value(identity, "purge_after"),
            purge_started_at=_record_value(identity, "purge_started_at"),
            purge_completed_at=_record_value(identity, "purge_completed_at"),
            deletion_error=_record_value(identity, "deletion_error"),
            hardware_ir=project_ir,
        )
        return ProjectReadResolution(
            project_id=project_id,
            source="cli",
            creation_channel=ProjectCreationChannel.CLI,
            owner_user_id=owner_user_id,
            title=project.title,
            prompt=project.prompt,
            chat_id=None,
            created_at=project.created_at,
            updated_at=project.updated_at,
            visibility=str(_record_value(identity, "visibility", "public") or "public"),
            status="active",
            current_revision=int(getattr(record, "revision", 0) or 0) or None,
            revision_id=str(getattr(record, "revision_id", "") or "") or None,
            project_ir=project_ir,
            image_metadata=metadata,
            can_chat=False,
            legacy_fallback=False,
            project=project,
        )


__all__ = [
    "ProjectReadAccessError",
    "ProjectReadError",
    "ProjectReadNotFoundError",
    "ProjectReadResolution",
    "ProjectReadResolver",
]
