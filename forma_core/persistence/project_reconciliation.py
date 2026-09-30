"""Backfill and reconcile legacy project projections without destructive cleanup."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import sessionmaker

from forma_core.persistence.models import (
    DBCliProject,
    DBCliProjectRevision,
    DBDesignBrief,
    DBGeneratedProject,
    DBProject,
    DBProjectRevision,
)
from forma_core.workspaces.design_briefs import DesignBrief
from forma_core.workspaces.projects.manifest import build_canonical_revision_record
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.state import ProjectRevision


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def _owner(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _json_equal(left: Any, right: Any) -> bool:
    return left == right


@dataclass
class ReconciliationRecord:
    project_id: str
    source_channel: str
    ownership_decision: str
    status: str
    actions: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReconciliationReport:
    run_id: str
    started_at: str
    completed_at: str | None
    dry_run: bool
    retry_failed: bool
    scanned: int = 0
    migrated: int = 0
    repaired: int = 0
    mismatches: int = 0
    skipped: int = 0
    failed: int = 0
    records: list[ReconciliationRecord] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["records"] = [asdict(record) for record in self.records]
        return payload


def _synthetic_brief(project_id: str, owner_user_id: str, title: str, prompt: str, created_at: str) -> tuple[DesignBrief, DBDesignBrief]:
    brief_id = uuid5(NAMESPACE_URL, f"forma-legacy-design-brief:{project_id}")
    row_id = uuid5(NAMESPACE_URL, f"forma-legacy-design-brief-row:{project_id}")
    summary = prompt.strip() or title.strip() or "Migrated legacy hardware project."
    brief = DesignBrief(
        schema_version="1.0",
        conversation_id=f"legacy-migration-{project_id}",
        intent=summary,
        summary=summary,
        design_brief_id=brief_id,
        project_id=UUID(project_id),
        brief_version=1,
        created_at=created_at,
    )
    row = DBDesignBrief(
        id=str(row_id),
        design_brief_id=str(brief_id),
        project_id=project_id,
        conversation_id=brief.conversation_id,
        owner_user_id=owner_user_id,
        brief_version=1,
        schema_version="1.0",
        previous_version=None,
        payload_json=brief.model_dump(mode="json"),
        created_at=created_at,
    )
    return brief, row


def _legacy_revision(
    project: DBGeneratedProject,
    owner_user_id: str,
    brief: DesignBrief,
) -> tuple[ProjectRevision, DBProjectRevision]:
    project_id = str(UUID(str(project.project_id)))
    revision_id = uuid5(NAMESPACE_URL, f"forma-legacy-generated-revision:{project_id}")
    state = HardwareIntermediateRepresentation.model_validate(project.hardware_ir or {})
    state.assembly_metadata = {
        **(state.assembly_metadata or {}),
        "project_id": project_id,
        "revision": 1,
        "design_brief_id": str(brief.design_brief_id),
        "design_brief_version": 1,
        "source_job_id": f"legacy-generated-{project_id}",
    }
    revision = ProjectRevision(
        state=state,
        components=list(state.components),
        systems=[],
        artifacts=[],
        assumptions=[],
        revision_id=revision_id,
        project_id=UUID(project_id),
        owner_user_id=owner_user_id,
        revision=1,
        parent_revision=None,
        design_brief_id=brief.design_brief_id,
        design_brief_version=1,
        source_job_id=f"legacy-generated-{project_id}",
        created_at=project.created_at,
    )
    row = DBProjectRevision(
        id=str(revision.revision_id),
        project_id=project_id,
        owner_user_id=owner_user_id,
        revision=1,
        parent_revision=None,
        design_brief_id=str(brief.design_brief_id),
        design_brief_version=1,
        source_job_id=revision.source_job_id,
        payload_json=revision.model_dump(mode="json"),
        created_at=revision.created_at.isoformat(),
    )
    return revision, row


def _identity_values(
    project_id: str,
    owner_user_id: str | None,
    source_channel: str,
    title: str,
    prompt: str,
    chat_id: str | None,
    workspace_id: str | None,
    visibility: str,
    status: str,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "owner_user_id": owner_user_id,
        "creation_channel": source_channel,
        "title": title or "Untitled Forma Project",
        "prompt": prompt or "",
        "chat_id": chat_id,
        "workspace_id": workspace_id,
        "visibility": visibility or "public",
        "status": status or "active",
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _record_status(record: ReconciliationRecord, report: ReconciliationReport) -> None:
    report.records.append(record)
    report.scanned += 1
    report.mismatches += len(record.mismatches)
    if record.status == "failed":
        report.failed += 1
    elif record.status == "skipped":
        report.skipped += 1
    else:
        report.migrated += sum(action.startswith("create_") for action in record.actions)
        report.repaired += sum(action.startswith("repair_") for action in record.actions)


def _apply_identity(identity: DBProject | None, values: dict[str, Any], session: Any) -> DBProject:
    if identity is None:
        identity = DBProject(**values)
        session.add(identity)
    else:
        for key, value in values.items():
            if key != "project_id":
                setattr(identity, key, value)
    return identity


def _reconcile_generated(
    session: Any,
    project: DBGeneratedProject,
    *,
    dry_run: bool,
) -> ReconciliationRecord:
    project_id = str(project.project_id)
    source_channel = str(getattr(project, "creation_channel", None) or "hosted")
    owner = _owner(project.owner_user_id)
    record = ReconciliationRecord(
        project_id=project_id,
        source_channel=source_channel,
        ownership_decision="owned_by_record" if owner else "unowned_legacy",
        status="planned" if dry_run else "clean",
    )
    canonical_id = _uuid(project_id)
    if canonical_id is None:
        record.status = "skipped"
        record.error = "project_id is not a UUID"
        return record
    if owner is None:
        record.status = "skipped"
        record.error = "owner_user_id is missing; no ownership was invented"
        return record

    identity = session.query(DBProject).filter(DBProject.project_id == canonical_id).first()
    if identity is not None and _owner(identity.owner_user_id) not in (None, owner):
        record.status = "failed"
        record.ownership_decision = "ownership_conflict"
        record.error = "canonical identity has a different owner"
        return record
    if identity is not None and identity.status != "active":
        record.status = "skipped"
        record.error = f"canonical identity is {identity.status}; stale projection was not allowed to resurrect it"
        return record
    values = _identity_values(
        canonical_id,
        owner,
        source_channel,
        str(project.title or ""),
        str(project.prompt or ""),
        project.chat_id,
        None,
        str(project.visibility or "public"),
        str(project.status or "active"),
        str(project.created_at),
        str(project.created_at),
    )
    if identity is None:
        record.actions.append("create_identity")
    else:
        for key, value in values.items():
            if key != "project_id" and getattr(identity, key, None) != value:
                record.mismatches.append(f"identity.{key}")

    revision = session.query(DBProjectRevision).filter(
        DBProjectRevision.project_id == canonical_id,
        DBProjectRevision.owner_user_id == owner,
    ).order_by(DBProjectRevision.revision.desc()).first()
    virtual_revision: ProjectRevision | None = None
    revision_row: DBProjectRevision | None = None
    synthetic_brief: DBDesignBrief | None = None
    if revision is None:
        try:
            brief_record = session.query(DBDesignBrief).filter(
                DBDesignBrief.project_id == canonical_id,
                DBDesignBrief.owner_user_id == owner,
            ).order_by(DBDesignBrief.brief_version.desc()).first()
            if brief_record is None:
                brief, synthetic_brief = _synthetic_brief(
                    canonical_id, owner, str(project.title or ""), str(project.prompt or ""), str(project.created_at)
                )
                record.actions.append("create_synthetic_design_brief")
            else:
                brief = DesignBrief.model_validate(brief_record.payload_json)
            virtual_revision, revision_row = _legacy_revision(project, owner, brief)
            record.actions.append("create_synthetic_revision")
            if not dry_run and synthetic_brief is not None:
                session.add(synthetic_brief)
        except Exception as exc:
            record.status = "failed"
            record.error = f"legacy state could not be validated: {exc}"
            return record
    else:
        try:
            virtual_revision = ProjectRevision.model_validate(revision.payload_json)
        except Exception as exc:
            record.status = "failed"
            record.error = f"canonical revision is invalid: {exc}"
            return record

    assert virtual_revision is not None
    canonical_ir = virtual_revision.state.model_dump(mode="json")
    canonical_identity = identity or SimpleNamespace(
        owner_user_id=owner,
        creation_channel=source_channel,
        title=project.title,
        prompt=project.prompt,
        chat_id=project.chat_id,
        visibility=project.visibility,
        status=project.status,
    )
    projection_values = {
        "hardware_ir": canonical_ir,
        "title": str((virtual_revision.state.overview.title if virtual_revision.state.overview else canonical_identity.title) or "Untitled Forma Project"),
        "prompt": str(canonical_identity.prompt or ""),
        "owner_user_id": _owner(canonical_identity.owner_user_id),
        "creation_channel": str(canonical_identity.creation_channel or source_channel),
        "visibility": str(canonical_identity.visibility or "public"),
        "status": str(canonical_identity.status or "active"),
        "chat_id": getattr(canonical_identity, "chat_id", None),
        "deleted_at": getattr(canonical_identity, "deleted_at", None),
        "deletion_requested_by": getattr(canonical_identity, "deletion_requested_by", None),
        "purge_after": getattr(canonical_identity, "purge_after", None),
        "purge_started_at": getattr(canonical_identity, "purge_started_at", None),
        "purge_completed_at": getattr(canonical_identity, "purge_completed_at", None),
        "deletion_error": getattr(canonical_identity, "deletion_error", None),
    }
    for key, value in projection_values.items():
        if getattr(project, key, None) != value:
            record.mismatches.append(f"generated_projects.{key}")
    if record.mismatches and "repair_generated_projection" not in record.actions:
        record.actions.append("repair_generated_projection")
    if identity is None or getattr(identity, "current_revision", 0) != virtual_revision.revision or getattr(identity, "current_revision_id", None) != str(virtual_revision.revision_id):
        if "repair_identity" not in record.actions:
            record.actions.append("repair_identity")

    if not dry_run:
        if identity is None:
            identity = _apply_identity(identity, values, session)
        identity.current_revision = virtual_revision.revision
        identity.current_revision_id = str(virtual_revision.revision_id)
        if revision is None and revision_row is not None:
            session.add(revision_row)
        for key, value in projection_values.items():
            setattr(project, key, value)
        record.status = "repaired" if record.actions else "clean"
    elif record.actions:
        record.status = "planned"
    return record


def _manifest_from_canonical(project: DBProject, revision: ProjectRevision, existing: dict[str, Any] | None) -> dict[str, Any]:
    manifest = dict(existing or {})
    manifest.update({
        "project_id": str(revision.project_id),
        "title": project.title,
        "prompt": getattr(project, "prompt", "") or manifest.get("prompt", ""),
        "workspace_id": project.workspace_id,
        "project_ir": revision.state.model_dump(mode="json"),
        "artifacts": [
            {
                "path": artifact.uri,
                "media_type": artifact.media_type,
                "sha256": artifact.checksum,
                **({"size_bytes": artifact.metadata["size_bytes"]} if "size_bytes" in artifact.metadata else {}),
            }
            for artifact in revision.artifacts
            if artifact.kind == "file"
        ],
    })
    return manifest


def _reconcile_cli(
    session: Any,
    project: DBProject,
    cli_project: DBCliProject,
    cli_revisions: list[DBCliProjectRevision],
    *,
    dry_run: bool,
) -> ReconciliationRecord:
    project_id = str(cli_project.project_id)
    owner = _owner(cli_project.owner_user_id)
    record = ReconciliationRecord(
        project_id=project_id,
        source_channel="cli",
        ownership_decision="owned_by_record" if owner else "unowned_legacy",
        status="planned" if dry_run else "clean",
    )
    if owner is None:
        record.status = "skipped"
        record.error = "owner_user_id is missing; no ownership was invented"
        return record
    canonical_id = _uuid(project_id)
    if canonical_id is None:
        record.status = "skipped"
        record.error = "project_id is not a UUID; compatibility data was retained"
        return record
    if project is not None and project.status != "active":
        record.status = "skipped"
        record.error = f"canonical identity is {project.status}; stale CLI data was not allowed to resurrect it"
        return record

    canonical_revisions: list[DBProjectRevision] = []
    for cli_revision in cli_revisions:
        existing = session.query(DBProjectRevision).filter(
            DBProjectRevision.project_id == canonical_id,
            DBProjectRevision.owner_user_id == owner,
            DBProjectRevision.revision == cli_revision.revision,
        ).first()
        if existing is not None:
            canonical_revisions.append(existing)
            continue
        canonical = build_canonical_revision_record(
            {"project_id": canonical_id, "owner_user_id": owner},
            {
                "revision_id": cli_revision.revision_id,
                "revision": cli_revision.revision,
                "manifest_json": cli_revision.manifest_json,
                "created_at": cli_revision.created_at,
            },
        )
        if canonical is None:
            record.status = "failed"
            record.error = f"CLI revision {cli_revision.revision_id} could not be validated"
            return record
        record.actions.append("create_canonical_cli_revision")
        if not dry_run:
            row = DBProjectRevision(**canonical)
            session.add(row)
            canonical_revisions.append(row)
        else:
            canonical_revisions.append(DBProjectRevision(**canonical))

    if not canonical_revisions:
        record.status = "skipped"
        record.error = "CLI project has no revisions"
        return record
    latest = max(canonical_revisions, key=lambda row: int(row.revision))
    try:
        latest_revision = ProjectRevision.model_validate(latest.payload_json)
    except Exception as exc:
        record.status = "failed"
        record.error = f"canonical CLI revision is invalid: {exc}"
        return record
    identity_values = _identity_values(
        canonical_id,
        owner,
        "cli",
        str(cli_project.title or ""),
        str((cli_revisions[-1].manifest_json or {}).get("prompt") or ""),
        None,
        cli_project.workspace_id,
        "private",
        "active",
        str(cli_project.created_at),
        str(cli_project.updated_at),
    )
    if project is None:
        record.actions.append("create_identity")
    else:
        for key, value in identity_values.items():
            if key != "project_id" and getattr(project, key, None) != value:
                record.mismatches.append(f"identity.{key}")
    if project is None or getattr(project, "current_revision", 0) != latest.revision or getattr(project, "current_revision_id", None) != str(latest.id):
        record.actions.append("repair_identity")
    if (
        cli_project.current_revision != latest.revision
        or cli_project.current_revision_id != cli_revisions[-1].revision_id
        or cli_project.title != (project.title if project is not None else identity_values["title"])
        or cli_project.workspace_id != (project.workspace_id if project is not None else identity_values["workspace_id"])
    ) and "repair_cli_projection" not in record.actions:
        record.actions.append("repair_cli_projection")
    identity_for_manifest = project or DBProject(**identity_values)
    for index, cli_revision in enumerate(cli_revisions):
        canonical_revision = next(
            item for item in canonical_revisions if int(item.revision) == int(cli_revision.revision)
        )
        canonical_payload = ProjectRevision.model_validate(canonical_revision.payload_json)
        desired_manifest = _manifest_from_canonical(identity_for_manifest, canonical_payload, cli_revision.manifest_json)
        if not _json_equal(cli_revision.manifest_json, desired_manifest):
            record.mismatches.append(f"cli_project_revisions.{cli_revision.revision}.manifest_json")
            if "repair_cli_projection" not in record.actions:
                record.actions.append("repair_cli_projection")
        expected_parent = cli_revisions[index - 1].revision_id if index else None
        if cli_revision.parent_revision_id != expected_parent:
            record.mismatches.append(f"cli_project_revisions.{cli_revision.revision}.parent_revision_id")
            if "repair_cli_projection" not in record.actions:
                record.actions.append("repair_cli_projection")
    if not dry_run:
        if project is None:
            project = _apply_identity(project, identity_values, session)
        project.current_revision = latest.revision
        project.current_revision_id = str(latest.id)
        for cli_revision in cli_revisions:
            canonical_revision = next(
                item for item in canonical_revisions if int(item.revision) == int(cli_revision.revision)
            )
            cli_revision.manifest_json = _manifest_from_canonical(
                project,
                ProjectRevision.model_validate(canonical_revision.payload_json),
                cli_revision.manifest_json,
            )
        for index, cli_revision in enumerate(cli_revisions):
            cli_revision.parent_revision_id = cli_revisions[index - 1].revision_id if index else None
        latest_cli = max(cli_revisions, key=lambda row: int(row.revision))
        cli_project.title = project.title if project is not None else identity_values["title"]
        cli_project.workspace_id = project.workspace_id if project is not None else identity_values["workspace_id"]
        cli_project.current_revision = latest.revision
        cli_project.current_revision_id = latest_cli.revision_id
        cli_project.updated_at = latest_cli.created_at
        record.status = "repaired" if record.actions else "clean"
    elif record.actions:
        record.status = "planned"
    return record


def _reconcile_canonical_hosted_projection(
    session: Any,
    identity: DBProject,
    *,
    dry_run: bool,
) -> ReconciliationRecord:
    project_id = str(identity.project_id)
    owner = _owner(identity.owner_user_id)
    record = ReconciliationRecord(
        project_id=project_id,
        source_channel="hosted",
        ownership_decision="owned_by_identity" if owner else "unowned_legacy",
        status="planned" if dry_run else "clean",
    )
    if owner is None:
        record.status = "skipped"
        record.error = "owner_user_id is missing; no ownership was invented"
        return record
    if identity.status != "active":
        record.status = "skipped"
        record.error = "deleted canonical identity has no compatibility projection; it was not resurrected"
        return record
    revision = session.query(DBProjectRevision).filter(
        DBProjectRevision.project_id == project_id,
        DBProjectRevision.owner_user_id == owner,
    ).order_by(DBProjectRevision.revision.desc()).first()
    if revision is None:
        record.status = "skipped"
        record.error = "canonical project has no revision"
        return record
    try:
        canonical = ProjectRevision.model_validate(revision.payload_json)
    except Exception as exc:
        record.status = "failed"
        record.error = f"canonical revision is invalid: {exc}"
        return record
    desired = {
        "project_id": project_id,
        "chat_id": identity.chat_id,
        "owner_user_id": owner,
        "creation_channel": "hosted",
        "visibility": identity.visibility,
        "title": str((canonical.state.overview.title if canonical.state.overview else identity.title) or "Untitled Forma Project"),
        "prompt": identity.prompt or "",
        "hardware_ir": canonical.state.model_dump(mode="json"),
        "created_at": identity.created_at,
        "status": identity.status,
    }
    projection = session.query(DBGeneratedProject).filter(DBGeneratedProject.project_id == project_id).first()
    if projection is None:
        record.actions.append("create_generated_projection")
    else:
        record.mismatches.extend(
            f"generated_projects.{key}" for key, value in desired.items()
            if key != "project_id" and getattr(projection, key, None) != value
        )
        if record.mismatches:
            record.actions.append("repair_generated_projection")
    if identity.current_revision != canonical.revision or identity.current_revision_id != str(revision.id):
        record.mismatches.append("projects.current_revision_pointer")
        if "repair_identity" not in record.actions:
            record.actions.append("repair_identity")
    if not dry_run:
        identity.current_revision = canonical.revision
        identity.current_revision_id = str(revision.id)
        if projection is None:
            session.add(DBGeneratedProject(**desired))
        else:
            for key, value in desired.items():
                if key != "project_id":
                    setattr(projection, key, value)
        record.status = "repaired" if record.actions else "clean"
    elif record.actions:
        record.status = "planned"
    return record


def _reconcile_canonical_cli_projection(
    session: Any,
    identity: DBProject,
    *,
    dry_run: bool,
) -> ReconciliationRecord:
    project_id = str(identity.project_id)
    owner = _owner(identity.owner_user_id)
    record = ReconciliationRecord(
        project_id=project_id,
        source_channel="cli",
        ownership_decision="owned_by_identity" if owner else "unowned_legacy",
        status="planned" if dry_run else "clean",
    )
    if owner is None:
        record.status = "skipped"
        record.error = "owner_user_id is missing; no ownership was invented"
        return record
    if identity.status != "active":
        record.status = "skipped"
        record.error = "deleted canonical identity has no CLI projection; it was not resurrected"
        return record
    revisions = session.query(DBProjectRevision).filter(
        DBProjectRevision.project_id == project_id,
        DBProjectRevision.owner_user_id == owner,
    ).order_by(DBProjectRevision.revision.asc()).all()
    if not revisions:
        record.status = "skipped"
        record.error = "canonical CLI project has no revisions"
        return record
    cli_project = session.query(DBCliProject).filter(DBCliProject.project_id == project_id).first()
    if cli_project is None:
        record.actions.append("create_cli_projection")
    missing_revisions = [
        revision for revision in revisions
        if session.query(DBCliProjectRevision).filter(
            DBCliProjectRevision.revision_id == str(revision.id)
        ).first() is None
    ]
    if missing_revisions:
        record.actions.append("create_cli_projection_revisions")
    latest = revisions[-1]
    if identity.current_revision != latest.revision or identity.current_revision_id != str(latest.id):
        record.actions.append("repair_identity")
    if not dry_run:
        if cli_project is None:
            cli_project = DBCliProject(
                project_id=project_id,
                workspace_id=identity.workspace_id,
                owner_user_id=owner,
                title=identity.title,
                current_revision=latest.revision,
                current_revision_id=str(latest.id),
                created_at=identity.created_at,
                updated_at=identity.updated_at,
            )
            session.add(cli_project)
        else:
            cli_project.workspace_id = identity.workspace_id
            cli_project.title = identity.title
            cli_project.current_revision = latest.revision
            cli_project.current_revision_id = str(latest.id)
            cli_project.updated_at = identity.updated_at
        for index, revision in enumerate(revisions):
            if revision not in missing_revisions:
                continue
            payload = ProjectRevision.model_validate(revision.payload_json)
            session.add(DBCliProjectRevision(
                revision_id=str(revision.id),
                project_id=project_id,
                owner_user_id=owner,
                revision=revision.revision,
                parent_revision_id=str(revisions[index - 1].id) if index else None,
                manifest_json=_manifest_from_canonical(identity, payload, {"project_id": project_id}),
                created_at=revision.created_at,
            ))
        identity.current_revision = latest.revision
        identity.current_revision_id = str(latest.id)
        record.status = "repaired" if record.actions else "clean"
    elif record.actions:
        record.status = "planned"
    return record


def reconcile_sqlite(
    session_factory: sessionmaker,
    *,
    dry_run: bool = True,
    retry_project_ids: Iterable[str] | None = None,
    limit: int | None = None,
) -> ReconciliationReport:
    """Backfill and reconcile one SQLite database in deterministic project order."""
    started = _now()
    report = ReconciliationReport(
        run_id=str(uuid5(NAMESPACE_URL, f"forma-reconciliation:{started}")),
        started_at=started,
        completed_at=None,
        dry_run=dry_run,
        retry_failed=retry_project_ids is not None,
    )
    retry_ids = {str(value) for value in retry_project_ids or ()}
    with session_factory() as session:
        generated = session.query(DBGeneratedProject).order_by(DBGeneratedProject.project_id.asc()).all()
        cli_projects = session.query(DBCliProject).order_by(DBCliProject.project_id.asc()).all()
        candidates: list[tuple[str, Any]] = [(str(project.project_id), project) for project in generated]
        candidates.extend((str(project.project_id), project) for project in cli_projects)
        known_ids = {project_id for project_id, _ in candidates}
        candidates.extend(
            (str(identity.project_id), identity)
            for identity in session.query(DBProject).order_by(DBProject.project_id.asc()).all()
            if identity.project_id not in known_ids
        )
        candidates.sort(key=lambda item: item[0])
        if limit is not None:
            candidates = candidates[: max(0, int(limit))]
        for project_id, source in candidates:
            if retry_project_ids is not None and project_id not in retry_ids:
                continue
            try:
                if isinstance(source, DBGeneratedProject):
                    result = _reconcile_generated(session, source, dry_run=dry_run)
                elif isinstance(source, DBCliProject):
                    revisions = session.query(DBCliProjectRevision).filter(
                        DBCliProjectRevision.project_id == project_id,
                        DBCliProjectRevision.owner_user_id == source.owner_user_id,
                    ).order_by(DBCliProjectRevision.revision.asc()).all()
                    identity = session.query(DBProject).filter(DBProject.project_id == project_id).first()
                    result = _reconcile_cli(session, identity, source, revisions, dry_run=dry_run)
                elif source.creation_channel == "cli":
                    result = _reconcile_canonical_cli_projection(session, source, dry_run=dry_run)
                else:
                    result = _reconcile_canonical_hosted_projection(session, source, dry_run=dry_run)
            except Exception as exc:
                logger.exception("Project reconciliation failed project_id=%s", project_id)
                result = ReconciliationRecord(
                    project_id=project_id,
                    source_channel="cli" if isinstance(source, DBCliProject) else "hosted",
                    ownership_decision="undetermined",
                    status="failed",
                    error=str(exc),
                )
            _record_status(result, report)
        if not dry_run:
            session.commit()
    report.completed_at = _now()
    return report


def _supabase_rows(client: Any, table: str) -> list[dict[str, Any]]:
    return client.table(table).select("*").execute().data or []


def reconcile_supabase(
    client: Any,
    *,
    dry_run: bool = True,
    retry_project_ids: Iterable[str] | None = None,
    limit: int | None = None,
) -> ReconciliationReport:
    """Backfill and reconcile the hosted Supabase tables without deleting rows."""
    started = _now()
    report = ReconciliationReport(
        run_id=str(uuid5(NAMESPACE_URL, f"forma-reconciliation:{started}")),
        started_at=started,
        completed_at=None,
        dry_run=dry_run,
        retry_failed=retry_project_ids is not None,
    )
    retry_ids = {str(value) for value in retry_project_ids or ()}
    generated = sorted(_supabase_rows(client, "generated_projects"), key=lambda row: str(row.get("project_id") or ""))
    cli_projects = sorted(_supabase_rows(client, "cli_projects"), key=lambda row: str(row.get("project_id") or ""))
    cli_revisions = _supabase_rows(client, "cli_project_revisions")
    canonical_revisions = _supabase_rows(client, "project_revisions")
    identities = {str(row.get("project_id")): row for row in _supabase_rows(client, "projects")}
    candidates: list[tuple[str, str, dict[str, Any]]] = [
        (str(row.get("project_id") or ""), str(row.get("creation_channel") or "hosted"), row) for row in generated
    ]
    candidates.extend((str(row.get("project_id") or ""), "cli", row) for row in cli_projects)
    known_ids = {project_id for project_id, _, _ in candidates}
    candidates.extend(
        (project_id, str(identity.get("creation_channel") or "hosted"), identity)
        for project_id, identity in identities.items()
        if project_id not in known_ids
    )
    candidates.sort(key=lambda item: (item[0], item[1]))
    if limit is not None:
        candidates = candidates[: max(0, int(limit))]

    for project_id, source_channel, source in candidates:
        if retry_project_ids is not None and project_id not in retry_ids:
            continue
        owner = _owner(source.get("owner_user_id"))
        result = ReconciliationRecord(
            project_id=project_id,
            source_channel=source_channel,
            ownership_decision="owned_by_record" if owner else "unowned_legacy",
            status="planned" if dry_run else "clean",
        )
        try:
            canonical_id = _uuid(project_id)
            if canonical_id is None:
                result.status = "skipped"
                result.error = "project_id is not a UUID; compatibility data was retained"
            elif owner is None:
                result.status = "skipped"
                result.error = "owner_user_id is missing; no ownership was invented"
            else:
                identity = identities.get(canonical_id)
                if identity is not None and _owner(identity.get("owner_user_id")) not in (None, owner):
                    result.status = "failed"
                    result.ownership_decision = "ownership_conflict"
                    result.error = "canonical identity has a different owner"
                elif identity is not None and identity.get("status", "active") != "active":
                    result.status = "skipped"
                    result.error = f"canonical identity is {identity.get('status')}; stale projection was not allowed to resurrect it"
                elif source_channel == "hosted" or "hardware_ir" in source:
                    values = _identity_values(
                        canonical_id, owner, source_channel, str(source.get("title") or ""),
                        str(source.get("prompt") or ""), source.get("chat_id"), None,
                        str(source.get("visibility") or "public"), str(source.get("status") or "active"),
                        str(source.get("created_at") or _now()), str(source.get("created_at") or _now()),
                    )
                    if identity is None:
                        result.actions.append("create_identity")
                    else:
                        result.mismatches.extend(
                            f"identity.{key}" for key, value in values.items()
                            if key != "project_id" and identity.get(key) != value
                        )
                        values = {key: identity.get(key, value) for key, value in values.items()}
                    rows = [
                        row for row in canonical_revisions
                        if row.get("project_id") == canonical_id and row.get("owner_user_id") == owner
                    ]
                    canonical = max(rows, key=lambda row: int(row.get("revision") or 0), default=None)
                    synthetic_brief_row: DBDesignBrief | None = None
                    if canonical is None:
                        brief_rows = (
                            client.table("design_briefs")
                            .select("*")
                            .eq("project_id", canonical_id)
                            .eq("owner_user_id", owner)
                            .order("brief_version", desc=True)
                            .limit(1)
                            .execute()
                            .data
                            or []
                        )
                        if brief_rows:
                            brief = DesignBrief.model_validate(brief_rows[0].get("payload_json") or {})
                        else:
                            brief, synthetic_brief_row = _synthetic_brief(
                                canonical_id,
                                owner,
                                str(source.get("title") or ""),
                                str(source.get("prompt") or ""),
                                str(source.get("created_at") or _now()),
                            )
                        legacy = _legacy_revision(
                            SimpleNamespace(
                                project_id=canonical_id,
                                hardware_ir=source.get("hardware_ir") or {},
                                created_at=str(source.get("created_at") or _now()),
                            ), owner, brief,
                        )[0]
                        canonical = {
                            "id": str(legacy.revision_id), "project_id": canonical_id, "owner_user_id": owner,
                            "revision": 1, "parent_revision": None,
                            "design_brief_id": str(legacy.design_brief_id), "design_brief_version": 1,
                            "source_job_id": legacy.source_job_id, "payload_json": legacy.model_dump(mode="json"),
                            "created_at": legacy.created_at.isoformat(),
                        }
                        result.actions.extend(["create_synthetic_design_brief", "create_synthetic_revision"])
                    revision = ProjectRevision.model_validate(canonical["payload_json"])
                    if identity is None or identity.get("current_revision") != revision.revision or identity.get("current_revision_id") != str(canonical["id"]):
                        if "repair_identity" not in result.actions:
                            result.actions.append("repair_identity")
                    canonical_identity = identity or source
                    projection = {
                        "hardware_ir": revision.state.model_dump(mode="json"),
                        "title": str((revision.state.overview.title if revision.state.overview else canonical_identity.get("title")) or "Untitled Forma Project"),
                        "prompt": str(canonical_identity.get("prompt") or ""),
                        "owner_user_id": canonical_identity.get("owner_user_id") or owner,
                        "creation_channel": canonical_identity.get("creation_channel") or source_channel,
                        "visibility": canonical_identity.get("visibility") or "public",
                        "status": canonical_identity.get("status") or "active",
                        "chat_id": canonical_identity.get("chat_id"),
                        "deleted_at": canonical_identity.get("deleted_at"),
                        "deletion_requested_by": canonical_identity.get("deletion_requested_by"),
                        "purge_after": canonical_identity.get("purge_after"),
                        "purge_started_at": canonical_identity.get("purge_started_at"),
                        "purge_completed_at": canonical_identity.get("purge_completed_at"),
                        "deletion_error": canonical_identity.get("deletion_error"),
                    }
                    if any(source.get(key) != value for key, value in projection.items()):
                        result.mismatches.append("generated_projects.projection")
                        result.actions.append("repair_generated_projection")
                    generated_exists = any(row.get("project_id") == project_id for row in generated)
                    if not generated_exists and "create_generated_projection" not in result.actions:
                        result.actions.append("create_generated_projection")
                    if not dry_run:
                        values.update({"current_revision": revision.revision, "current_revision_id": str(canonical["id"])})
                        client.table("projects").upsert(values, on_conflict="project_id").execute()
                        if not rows:
                            if not dry_run and synthetic_brief_row is not None:
                                client.table("design_briefs").insert({
                                    "id": synthetic_brief_row.id,
                                    "design_brief_id": synthetic_brief_row.design_brief_id,
                                    "project_id": synthetic_brief_row.project_id,
                                    "conversation_id": synthetic_brief_row.conversation_id,
                                    "owner_user_id": synthetic_brief_row.owner_user_id,
                                    "brief_version": synthetic_brief_row.brief_version,
                                    "schema_version": synthetic_brief_row.schema_version,
                                    "previous_version": synthetic_brief_row.previous_version,
                                    "payload_json": synthetic_brief_row.payload_json,
                                    "created_at": synthetic_brief_row.created_at,
                                }).execute()
                            client.table("project_revisions").insert(canonical).execute()
                        projection_record = {
                            "project_id": project_id,
                            "chat_id": identity.get("chat_id") if identity else source.get("chat_id"),
                            "owner_user_id": owner,
                            "creation_channel": source_channel,
                            "visibility": values.get("visibility", "public"),
                            "title": projection["title"],
                            "prompt": projection["prompt"],
                            "created_at": values["created_at"],
                            "status": values.get("status", "active"),
                            **projection,
                        }
                        if generated_exists:
                            client.table("generated_projects").update(projection).eq("project_id", project_id).execute()
                        else:
                            client.table("generated_projects").insert(projection_record).execute()
                else:
                    project_revisions = [row for row in cli_revisions if row.get("project_id") == project_id and row.get("owner_user_id") == owner]
                    project_revisions.sort(key=lambda row: int(row.get("revision") or 0))
                    if not project_revisions:
                        project_revisions = [
                            {
                                "revision_id": row.get("id"),
                                "project_id": project_id,
                                "owner_user_id": owner,
                                "revision": row.get("revision"),
                                "manifest_json": _manifest_from_canonical(
                                    SimpleNamespace(title=source.get("title") or "", workspace_id=source.get("workspace_id")),
                                    ProjectRevision.model_validate(row.get("payload_json") or {}),
                                    {"project_id": project_id},
                                ),
                                "created_at": row.get("created_at"),
                            }
                            for row in canonical_revisions
                            if row.get("project_id") == canonical_id and row.get("owner_user_id") == owner
                        ]
                        if project_revisions:
                            result.actions.append("create_cli_projection")
                            result.actions.append("create_cli_projection_revisions")
                    for index, cli_revision in enumerate(project_revisions):
                        created_canonical = False
                        matching = next(
                            (row for row in canonical_revisions
                             if row.get("project_id") == canonical_id
                             and row.get("owner_user_id") == owner
                             and int(row.get("revision") or 0) == int(cli_revision.get("revision") or 0)),
                            None,
                        )
                        if matching is None:
                            matching = build_canonical_revision_record(
                                {"project_id": canonical_id, "owner_user_id": owner},
                                {"revision_id": cli_revision.get("revision_id"), "revision": cli_revision.get("revision"),
                                 "manifest_json": cli_revision.get("manifest_json"), "created_at": cli_revision.get("created_at")},
                            )
                            if matching is None:
                                raise ValueError(f"CLI revision {cli_revision.get('revision_id')} could not be validated")
                            canonical_revisions.append(matching)
                            result.actions.append("create_canonical_cli_revision")
                            created_canonical = True
                        canonical_payload = ProjectRevision.model_validate(matching["payload_json"])
                        desired = _manifest_from_canonical(
                            SimpleNamespace(title=source.get("title") or "", workspace_id=source.get("workspace_id")),
                            canonical_payload,
                            cli_revision.get("manifest_json"),
                        )
                        if cli_revision.get("manifest_json") != desired:
                            result.mismatches.append(f"cli_project_revisions.{cli_revision.get('revision')}.manifest_json")
                            if "repair_cli_projection" not in result.actions:
                                result.actions.append("repair_cli_projection")
                        expected_parent = project_revisions[index - 1].get("revision_id") if index else None
                        if cli_revision.get("parent_revision_id") != expected_parent:
                            result.mismatches.append(f"cli_project_revisions.{cli_revision.get('revision')}.parent_revision_id")
                            if "repair_cli_projection" not in result.actions:
                                result.actions.append("repair_cli_projection")
                        if not dry_run:
                            if created_canonical:
                                client.table("project_revisions").insert(matching).execute()
                            if any(row.get("revision_id") == cli_revision.get("revision_id") for row in cli_revisions):
                                client.table("cli_project_revisions").update({
                                    "manifest_json": desired,
                                    "parent_revision_id": expected_parent,
                                }).eq("revision_id", cli_revision["revision_id"]).execute()
                            else:
                                client.table("cli_project_revisions").insert({
                                    **cli_revision,
                                    "manifest_json": desired,
                                    "parent_revision_id": expected_parent,
                                }).execute()
                    if project_revisions:
                        latest_cli = project_revisions[-1]
                        latest_canonical = max(
                            (row for row in canonical_revisions if row.get("project_id") == canonical_id and row.get("owner_user_id") == owner),
                            key=lambda row: int(row.get("revision") or 0),
                        )
                        identity = identities.get(canonical_id)
                        identity_values = _identity_values(
                            canonical_id, owner, "cli", str(source.get("title") or ""),
                            str((latest_cli.get("manifest_json") or {}).get("prompt") or ""), None,
                            source.get("workspace_id"), "private", "active", str(source.get("created_at") or _now()),
                            str(source.get("updated_at") or _now()),
                        )
                        if identity is None:
                            result.actions.append("create_identity")
                        else:
                            identity_values = {
                                key: identity.get(key, value)
                                for key, value in identity_values.items()
                            }
                        if identity is None or identity.get("current_revision") != int(latest_canonical.get("revision") or 0) or identity.get("current_revision_id") != str(latest_canonical.get("id")):
                            if "repair_identity" not in result.actions:
                                result.actions.append("repair_identity")
                        if (
                            source.get("current_revision") != int(latest_cli.get("revision") or 0)
                            or source.get("current_revision_id") != latest_cli.get("revision_id")
                            or source.get("title") != identity_values.get("title")
                            or source.get("workspace_id") != identity_values.get("workspace_id")
                        ) and "repair_cli_projection" not in result.actions:
                            result.actions.append("repair_cli_projection")
                        if not dry_run:
                            identity_values.update({"current_revision": int(latest_canonical.get("revision") or 0), "current_revision_id": str(latest_canonical.get("id"))})
                            client.table("projects").upsert(identity_values, on_conflict="project_id").execute()
                            cli_project_exists = any(row.get("project_id") == project_id for row in cli_projects)
                            if cli_project_exists:
                                client.table("cli_projects").update({
                                    "current_revision": int(latest_cli.get("revision") or 0),
                                    "current_revision_id": latest_cli.get("revision_id"),
                                }).eq("project_id", project_id).eq("owner_user_id", owner).execute()
                            else:
                                client.table("cli_projects").insert({
                                    "project_id": project_id,
                                    "workspace_id": source.get("workspace_id"),
                                    "owner_user_id": owner,
                                    "title": source.get("title") or "Untitled Forma Project",
                                    "current_revision": int(latest_cli.get("revision") or 0),
                                    "current_revision_id": latest_cli.get("revision_id"),
                                    "created_at": source.get("created_at") or _now(),
                                    "updated_at": source.get("updated_at") or _now(),
                                }).execute()
                    else:
                        result.status = "skipped"
                        result.error = "CLI project has no revisions"
                if not dry_run and result.status != "failed":
                    result.status = "repaired" if result.actions else "clean"
                elif result.actions:
                    result.status = "planned"
        except Exception as exc:
            logger.exception("Supabase project reconciliation failed project_id=%s", project_id)
            result.status = "failed"
            result.error = str(exc)
        _record_status(result, report)
    report.completed_at = _now()
    return report


def load_retry_project_ids(path: str | Path) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        str(record["project_id"])
        for record in payload.get("records", [])
        if record.get("status") == "failed" and record.get("project_id")
    ]


def write_reconciliation_report(report: ReconciliationReport, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)


__all__ = [
    "ReconciliationRecord",
    "ReconciliationReport",
    "load_retry_project_ids",
    "reconcile_supabase",
    "reconcile_sqlite",
    "write_reconciliation_report",
]
