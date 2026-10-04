from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from forma_core.persistence.models import (
    DBAlphaSignup,
    DBComponentTemplate,
    DBDesignBrief,
    DBGeneratedProject,
    DBProject,
    DBProjectContributionConsent,
    DBProjectContributionSnapshot,
    DBProjectChat,
    DBProjectBuild,
    DBProjectDeletionAudit,
    DBProjectPublishAudit,
    DBProjectRemix,
    DBProjectSave,
    DBProjectWorkflow,
    DBProjectWorkflowTransition,
    DBProjectRevision,
    DBProjectShare,
    DBCliProject,
    DBCliProjectDelivery,
    DBCliProjectRevision,
    DBCliDeviceAuthorization,
    DBCliTokenSession,
    DBProjectValidationReport,
    DBWorkerExecutionPlan,
    DBUserSettings,
)
from forma_core.workspaces.projects.manifest import build_canonical_revision_record


logger = logging.getLogger(__name__)


class SqlAlchemyRepository:
    """Application repository shared by SQLAlchemy-backed database providers."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def _session(self) -> Any:
        return self._session_factory()

    def count_component_templates(self) -> int:
        with self._session() as session:
            return session.query(DBComponentTemplate).count()

    def list_component_templates(self) -> List[Any]:
        with self._session() as session:
            return session.query(DBComponentTemplate).all()

    def get_component_template_by_part_number(self, part_number: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBComponentTemplate).filter(
                DBComponentTemplate.part_number == part_number
            ).first()

    def insert_component_template(self, record: Dict[str, Any]) -> None:
        with self._session() as session, session.begin():
            session.add(DBComponentTemplate(**record))

    def upsert_project_identity(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            project = session.query(DBProject).filter(DBProject.project_id == record["project_id"]).first()
            if project is None:
                project = DBProject(**record)
                session.add(project)
            else:
                for key, value in record.items():
                    if key != "project_id":
                        setattr(project, key, value)
            return project

    def get_project_identity(self, project_id: str) -> Optional[Any]:
        with self._session() as session:
            project = session.query(DBProject).filter(DBProject.project_id == project_id).first()
            return {
                column.name: getattr(project, column.name)
                for column in DBProject.__table__.columns
            } if project is not None else None

    def list_project_identities(self, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            return session.query(DBProject).filter(
                DBProject.owner_user_id == owner_user_id,
                DBProject.status == "active",
            ).order_by(DBProject.updated_at.desc()).all()

    def list_project_gallery_inventory_page(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> tuple[List[Any], int]:
        """Read one bounded, privacy-filtered canonical gallery page."""
        filters = ["status = :status"]
        parameters: Dict[str, Any] = {
            "status": "active",
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }
        if owner_user_id:
            filters.append("owner_user_id = :owner_user_id")
            parameters["owner_user_id"] = owner_user_id
        if visibility:
            filters.append("visibility = :visibility")
            parameters["visibility"] = visibility
        if search:
            filters.append("(lower(title) like lower(:search) or lower(prompt) like lower(:search))")
            parameters["search"] = f"%{search}%"
        where = " AND ".join(filters)
        with self._session() as session:
            total = session.execute(
                text(f"SELECT COUNT(*) FROM project_gallery_inventory WHERE {where}"),
                parameters,
            ).scalar_one()
            rows = session.execute(
                text(
                    "SELECT project_id, owner_user_id, creation_channel, title, prompt, chat_id, "
                    "workspace_id, visibility, status, created_at, updated_at, source, revision_id, "
                    "revision, revision_payload_json, revision_created_at, legacy_hardware_ir, legacy_id "
                    f"FROM project_gallery_inventory WHERE {where} "
                    "ORDER BY updated_at DESC, project_id DESC LIMIT :limit OFFSET :offset"
                ),
                parameters,
            ).mappings().all()
            return [SimpleNamespace(**dict(row)) for row in rows], int(total or 0)

    def list_project_gallery_inventory(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        search: Optional[str] = None,
    ) -> List[Any]:
        filters = ["status = :status"]
        parameters: Dict[str, Any] = {"status": "active"}
        if owner_user_id:
            filters.append("owner_user_id = :owner_user_id")
            parameters["owner_user_id"] = owner_user_id
        if visibility:
            filters.append("visibility = :visibility")
            parameters["visibility"] = visibility
        if search:
            filters.append("(lower(title) like lower(:search) or lower(prompt) like lower(:search))")
            parameters["search"] = f"%{search}%"
        where = " AND ".join(filters)
        with self._session() as session:
            rows = session.execute(
                text(
                    "SELECT project_id, owner_user_id, creation_channel, title, prompt, chat_id, "
                    "workspace_id, visibility, status, created_at, updated_at, source, revision_id, "
                    "revision, revision_payload_json, revision_created_at, legacy_hardware_ir, legacy_id "
                    f"FROM project_gallery_inventory WHERE {where} "
                    "ORDER BY updated_at DESC, project_id DESC"
                ),
                parameters,
            ).mappings().all()
            return [SimpleNamespace(**dict(row)) for row in rows]

    def save_generated_project(
        self,
        record: Dict[str, Any],
        chat_record: Optional[Dict[str, Any]],
    ) -> None:
        with self._session() as session, session.begin():
            identity = session.query(DBProject).filter(DBProject.project_id == record["project_id"]).first()
            identity_record = {
                "project_id": record["project_id"],
                "owner_user_id": record.get("owner_user_id"),
                "creation_channel": record.get("creation_channel", "hosted"),
                "title": record.get("title", ""),
                "prompt": record.get("prompt", ""),
                "chat_id": record.get("chat_id"),
                "workspace_id": None,
                "visibility": record.get("visibility", "public"),
                "status": record.get("status", "active"),
                "created_at": record["created_at"],
                "updated_at": record["created_at"],
            }
            if identity is None:
                session.add(DBProject(**identity_record))
            else:
                for key, value in identity_record.items():
                    if key != "project_id":
                        setattr(identity, key, value)
            session.add(DBGeneratedProject(**record))
            if not chat_record:
                return
            chat = session.query(DBProjectChat).filter(
                DBProjectChat.chat_id == chat_record["chat_id"]
            ).first()
            if chat:
                if chat.owner_user_id == chat_record["owner_user_id"]:
                    chat.title = chat.title or chat_record["title"]
                    chat.updated_at = chat_record["updated_at"]
                return
            session.add(DBProjectChat(**chat_record))

    def list_generated_projects(self, owner_user_id: Optional[str]) -> List[Any]:
        with self._session() as session:
            query = session.query(DBGeneratedProject).filter(DBGeneratedProject.status == "active")
            if owner_user_id:
                query = query.filter(DBGeneratedProject.owner_user_id == owner_user_id)
            return query.order_by(DBGeneratedProject.id.desc()).all()

    def list_generated_projects_page(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> tuple[List[Any], int]:
        with self._session() as session:
            query = session.query(DBGeneratedProject).filter(DBGeneratedProject.status == "active")
            if owner_user_id:
                query = query.filter(DBGeneratedProject.owner_user_id == owner_user_id)
            if visibility:
                query = query.filter(DBGeneratedProject.visibility == visibility)
            if search:
                pattern = f"%{search}%"
                query = query.filter(or_(
                    DBGeneratedProject.title.ilike(pattern),
                    DBGeneratedProject.prompt.ilike(pattern),
                ))
            total = query.count()
            rows = (
                query.order_by(DBGeneratedProject.created_at.desc(), DBGeneratedProject.id.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return rows, total

    def get_generated_project(self, project_id: str, include_deleted: bool = False) -> Optional[Any]:
        with self._session() as session:
            query = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.project_id == project_id
            )
            if not include_deleted:
                query = query.filter(DBGeneratedProject.status == "active")
            return query.first()

    def insert_design_brief_version(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            design_brief = DBDesignBrief(**record)
            session.add(design_brief)
            session.flush()
            session.refresh(design_brief)
            session.expunge(design_brief)
            return design_brief

    def list_design_brief_versions(self, project_id: str, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            return (
                session.query(DBDesignBrief)
                .filter(
                    DBDesignBrief.project_id == project_id,
                    DBDesignBrief.owner_user_id == owner_user_id,
                )
                .order_by(DBDesignBrief.brief_version.asc())
                .all()
            )

    def get_design_brief_version(
        self,
        project_id: str,
        owner_user_id: str,
        brief_version: int,
    ) -> Optional[Any]:
        with self._session() as session:
            return (
                session.query(DBDesignBrief)
                .filter(
                    DBDesignBrief.project_id == project_id,
                    DBDesignBrief.owner_user_id == owner_user_id,
                    DBDesignBrief.brief_version == brief_version,
                )
                .first()
            )

    def get_latest_design_brief(self, project_id: str, owner_user_id: Optional[str]) -> Optional[Any]:
        with self._session() as session:
            query = session.query(DBDesignBrief).filter(DBDesignBrief.project_id == project_id)
            if owner_user_id:
                query = query.filter(DBDesignBrief.owner_user_id == owner_user_id)
            return query.order_by(DBDesignBrief.brief_version.desc()).first()

    def get_project_workflow(self, project_id: str, owner_user_id: Optional[str]) -> Optional[Any]:
        with self._session() as session:
            query = session.query(DBProjectWorkflow).filter(DBProjectWorkflow.project_id == project_id)
            if owner_user_id:
                query = query.filter(DBProjectWorkflow.owner_user_id == owner_user_id)
            return query.first()

    def list_project_workflow_transitions(self, project_id: str, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            return (
                session.query(DBProjectWorkflowTransition)
                .filter(
                    DBProjectWorkflowTransition.project_id == project_id,
                    DBProjectWorkflowTransition.owner_user_id == owner_user_id,
                )
                .order_by(DBProjectWorkflowTransition.revision.asc())
                .all()
            )

    def get_project_workflow_transition_by_idempotency(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]:
        with self._session() as session:
            return (
                session.query(DBProjectWorkflowTransition)
                .filter(
                    DBProjectWorkflowTransition.project_id == project_id,
                    DBProjectWorkflowTransition.owner_user_id == owner_user_id,
                    DBProjectWorkflowTransition.idempotency_key == idempotency_key,
                )
                .first()
            )

    def apply_project_workflow_transition(
        self,
        state_record: Dict[str, Any],
        transition_record: Dict[str, Any],
        expected_state: Optional[str],
        expected_revision: Optional[int],
    ) -> Optional[tuple[Any, Any]]:
        try:
            with self._session() as session, session.begin():
                workflow = session.query(DBProjectWorkflow).filter(
                    DBProjectWorkflow.project_id == state_record["project_id"]
                ).first()
                if expected_state is None:
                    if workflow is not None:
                        return None
                    workflow = DBProjectWorkflow(**state_record)
                    session.add(workflow)
                else:
                    if (
                        workflow is None
                        or workflow.owner_user_id != state_record["owner_user_id"]
                        or workflow.state != expected_state
                        or workflow.revision != expected_revision
                    ):
                        return None
                    workflow.state = state_record["state"]
                    workflow.revision = state_record["revision"]
                    workflow.updated_at = state_record["updated_at"]
                transition = DBProjectWorkflowTransition(**transition_record)
                session.add(transition)
                session.flush()
                session.refresh(workflow)
                session.refresh(transition)
                session.expunge(workflow)
                session.expunge(transition)
                return workflow, transition
        except IntegrityError:
            return None

    def get_project_build_by_idempotency(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectBuild).filter(
                DBProjectBuild.project_id == project_id,
                DBProjectBuild.owner_user_id == owner_user_id,
                DBProjectBuild.idempotency_key == idempotency_key,
            ).first()

    def get_latest_project_build(self, project_id: str, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            return (
                session.query(DBProjectBuild)
                .filter(
                    DBProjectBuild.project_id == project_id,
                    DBProjectBuild.owner_user_id == owner_user_id,
                )
                .order_by(DBProjectBuild.created_at.desc())
                .first()
            )

    def apply_project_build_initiation(
        self,
        state_record: Dict[str, Any],
        transition_record: Dict[str, Any],
        build_record: Dict[str, Any],
        expected_state: str,
        expected_revision: int,
    ) -> Optional[tuple[Any, Any, Any]]:
        try:
            with self._session() as session, session.begin():
                workflow = session.query(DBProjectWorkflow).filter(
                    DBProjectWorkflow.project_id == state_record["project_id"]
                ).first()
                if (
                    workflow is None
                    or workflow.owner_user_id != state_record["owner_user_id"]
                    or workflow.state != expected_state
                    or workflow.revision != expected_revision
                ):
                    return None
                workflow.state = state_record["state"]
                workflow.revision = state_record["revision"]
                workflow.updated_at = state_record["updated_at"]
                transition = DBProjectWorkflowTransition(**transition_record)
                build = DBProjectBuild(**build_record)
                session.add(transition)
                session.add(build)
                session.flush()
                session.refresh(workflow)
                session.refresh(transition)
                session.refresh(build)
                session.expunge(workflow)
                session.expunge(transition)
                session.expunge(build)
                return workflow, transition, build
        except IntegrityError:
            return None

    def insert_worker_execution_plan(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            plan = DBWorkerExecutionPlan(**record)
            session.add(plan)
            session.flush()
            session.refresh(plan)
            session.expunge(plan)
            return plan

    def get_worker_execution_plan(self, plan_id: str, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBWorkerExecutionPlan).filter(
                DBWorkerExecutionPlan.id == plan_id,
                DBWorkerExecutionPlan.owner_user_id == owner_user_id,
            ).first()

    def list_worker_execution_plans(self, limit: int = 200) -> List[Any]:
        with self._session() as session:
            return (
                session.query(DBWorkerExecutionPlan)
                .order_by(DBWorkerExecutionPlan.created_at.desc())
                .limit(max(1, min(int(limit), 1000)))
                .all()
            )

    def update_worker_execution_plan(
        self,
        plan_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Any]:
        with self._session() as session, session.begin():
            plan = session.query(DBWorkerExecutionPlan).filter(
                DBWorkerExecutionPlan.id == plan_id,
                DBWorkerExecutionPlan.owner_user_id == owner_user_id,
            ).first()
            if plan is None:
                return None
            for key, value in updates.items():
                setattr(plan, key, value)
            session.flush()
            session.refresh(plan)
            session.expunge(plan)
            return plan

    def get_latest_project_revision(self, project_id: str, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            return (
                session.query(DBProjectRevision)
                .filter(
                    DBProjectRevision.project_id == project_id,
                    DBProjectRevision.owner_user_id == owner_user_id,
                )
                .order_by(DBProjectRevision.revision.desc())
                .first()
            )

    def list_project_revisions(self, project_id: str, owner_user_id: str, *, limit: int, before: int | None = None) -> List[Any]:
        with self._session() as session:
            query = session.query(DBProjectRevision).filter(
                DBProjectRevision.project_id == project_id,
                DBProjectRevision.owner_user_id == owner_user_id,
            )
            if before is not None:
                query = query.filter(DBProjectRevision.revision < before)
            return query.order_by(DBProjectRevision.revision.desc()).limit(limit).all()

    def get_project_revision_by_id(self, project_id: str, owner_user_id: str, revision_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectRevision).filter(
                DBProjectRevision.project_id == project_id,
                DBProjectRevision.owner_user_id == owner_user_id,
                DBProjectRevision.id == revision_id,
            ).first()

    def insert_project_share(self, record: Dict[str, Any]) -> None:
        with self._session() as session, session.begin():
            session.add(DBProjectShare(**record))

    def get_active_project_share(self, project_id: str, owner_user_id: str, revision_id: str, token_hash: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectShare).filter(
                DBProjectShare.project_id == project_id,
                DBProjectShare.owner_user_id == owner_user_id,
                DBProjectShare.revision_id == revision_id,
                DBProjectShare.token_hash == token_hash,
                DBProjectShare.revoked_at.is_(None),
            ).first()

    def list_project_shares(self, project_id: str, owner_user_id: str, revision_id: str, *, limit: int, offset: int) -> List[Any]:
        with self._session() as session:
            return session.query(DBProjectShare).filter(
                DBProjectShare.project_id == project_id,
                DBProjectShare.owner_user_id == owner_user_id,
                DBProjectShare.revision_id == revision_id,
            ).order_by(DBProjectShare.created_at.desc(), DBProjectShare.id.desc()).offset(offset).limit(limit).all()

    def revoke_project_share(self, project_id: str, owner_user_id: str, revision_id: str, share_id: str, revoked_at: str) -> None:
        with self._session() as session, session.begin():
            session.query(DBProjectShare).filter(
                DBProjectShare.id == share_id,
                DBProjectShare.project_id == project_id,
                DBProjectShare.owner_user_id == owner_user_id,
                DBProjectShare.revision_id == revision_id,
                DBProjectShare.revoked_at.is_(None),
            ).update({"revoked_at": revoked_at}, synchronize_session=False)

    def list_latest_project_revisions(self, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            rows = (
                session.query(DBProjectRevision)
                .filter(DBProjectRevision.owner_user_id == owner_user_id)
                .order_by(DBProjectRevision.created_at.desc())
                .all()
            )
            latest_by_project: Dict[str, Any] = {}
            for row in rows:
                current = latest_by_project.get(row.project_id)
                if current is None or row.revision > current.revision:
                    latest_by_project[row.project_id] = row
            return sorted(
                latest_by_project.values(),
                key=lambda row: str(row.created_at or ""),
                reverse=True,
            )

    def get_project_revision(
        self,
        project_id: str,
        owner_user_id: str,
        revision: int,
    ) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectRevision).filter(
                DBProjectRevision.project_id == project_id,
                DBProjectRevision.owner_user_id == owner_user_id,
                DBProjectRevision.revision == revision,
            ).first()

    def get_project_revision_by_source_job(
        self,
        project_id: str,
        owner_user_id: str,
        source_job_id: str,
    ) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectRevision).filter(
                DBProjectRevision.project_id == project_id,
                DBProjectRevision.owner_user_id == owner_user_id,
                DBProjectRevision.source_job_id == source_job_id,
            ).first()

    def insert_initial_project_revision(self, record: Dict[str, Any]) -> Optional[Any]:
        try:
            with self._session() as session, session.begin():
                existing = session.query(DBProjectRevision).filter(
                    DBProjectRevision.project_id == record["project_id"]
                ).first()
                if existing is not None or record["revision"] != 1 or record["parent_revision"] is not None:
                    return None
                revision = DBProjectRevision(**record)
                session.add(revision)
                session.flush()
                session.refresh(revision)
                session.expunge(revision)
                return revision
        except IntegrityError:
            return None

    def get_cli_project(self, project_id: str, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBCliProject).filter(
                DBCliProject.project_id == project_id,
                DBCliProject.owner_user_id == owner_user_id,
            ).first()

    def list_cli_projects(self, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            return session.query(DBCliProject).filter(
                DBCliProject.owner_user_id == owner_user_id,
            ).order_by(DBCliProject.updated_at.desc()).all()

    def get_cli_project_revision(
        self,
        project_id: str,
        owner_user_id: str,
        revision_id: Optional[str] = None,
    ) -> Optional[Any]:
        with self._session() as session:
            query = session.query(DBCliProjectRevision).filter(
                DBCliProjectRevision.project_id == project_id,
                DBCliProjectRevision.owner_user_id == owner_user_id,
            )
            if revision_id:
                query = query.filter(DBCliProjectRevision.revision_id == revision_id)
            return query.order_by(DBCliProjectRevision.revision.desc()).first()

    def insert_cli_project_revision(
        self,
        project_record: Dict[str, Any],
        revision_record: Dict[str, Any],
        expected_revision_id: Optional[str],
    ) -> Optional[Any]:
        try:
            with self._session() as session, session.begin():
                identity = session.query(DBProject).filter(
                    DBProject.project_id == project_record["project_id"],
                ).first()
                if identity is not None and identity.status != "active":
                    return None
                project = session.query(DBCliProject).filter(
                    DBCliProject.project_id == project_record["project_id"],
                ).first()
                if project is None:
                    if expected_revision_id is not None:
                        return None
                    project = DBCliProject(**project_record)
                    session.add(project)
                elif (
                    project.owner_user_id != project_record["owner_user_id"]
                    or project.current_revision_id != expected_revision_id
                    or project.current_revision + 1 != revision_record["revision"]
                ):
                    return None
                identity_record = {
                    "project_id": project_record["project_id"],
                    "owner_user_id": project_record["owner_user_id"],
                    "creation_channel": "cli",
                    "title": project_record["title"],
                    "prompt": str((revision_record.get("manifest_json") or {}).get("prompt") or ""),
                    "chat_id": None,
                    "workspace_id": project_record.get("workspace_id"),
                    "visibility": project_record.get("visibility", "public"),
                    "status": "active",
                    "current_revision": revision_record["revision"],
                    "current_revision_id": revision_record["revision_id"],
                    "created_at": project_record["created_at"],
                    "updated_at": revision_record["created_at"],
                }
                if identity is None:
                    session.add(DBProject(**identity_record))
                else:
                    for key, value in identity_record.items():
                        if key != "project_id":
                            setattr(identity, key, value)
                revision = DBCliProjectRevision(**revision_record)
                session.add(revision)
                canonical_revision = build_canonical_revision_record(project_record, revision_record)
                if canonical_revision is not None:
                    session.add(DBProjectRevision(**canonical_revision))
                else:
                    logger.info(
                        "cli_project_canonical_revision_skipped project_id=%s reason=legacy_identifier_or_invalid_ir",
                        project_record["project_id"],
                    )
                project.workspace_id = project_record["workspace_id"]
                project.title = project_record["title"]
                project.current_revision = revision_record["revision"]
                project.current_revision_id = revision_record["revision_id"]
                project.updated_at = revision_record["created_at"]
                session.flush()
                session.refresh(revision)
                session.expunge(revision)
                return revision
        except IntegrityError:
            return None

    def get_cli_project_delivery(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBCliProjectDelivery).filter(
                DBCliProjectDelivery.project_id == project_id,
                DBCliProjectDelivery.owner_user_id == owner_user_id,
                DBCliProjectDelivery.idempotency_key == idempotency_key,
            ).first()

    def get_cli_project_delivery_by_id(self, delivery_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBCliProjectDelivery).filter(
                DBCliProjectDelivery.delivery_id == delivery_id
            ).first()

    def list_cli_project_deliveries(self, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            return (
                session.query(DBCliProjectDelivery)
                .filter(DBCliProjectDelivery.owner_user_id == owner_user_id)
                .order_by(DBCliProjectDelivery.created_at.desc())
                .all()
            )

    def insert_cli_project_delivery(self, record: Dict[str, Any]) -> Any:
        try:
            with self._session() as session, session.begin():
                delivery = DBCliProjectDelivery(**record)
                session.add(delivery)
                session.flush()
                session.refresh(delivery)
                session.expunge(delivery)
                return delivery
        except IntegrityError:
            return self.get_cli_project_delivery(
                record["project_id"],
                record["owner_user_id"],
                record["idempotency_key"],
            )

    def update_cli_project_visibility(
        self,
        project_id: str,
        owner_user_id: str,
        visibility: str,
    ) -> Optional[Any]:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            with self._session() as session, session.begin():
                project = session.query(DBCliProject).filter(
                    DBCliProject.project_id == project_id,
                    DBCliProject.owner_user_id == owner_user_id,
                ).first()
                if project is None:
                    return None
                project.visibility = visibility
                project.updated_at = now
                session.flush()
                session.refresh(project)
                session.expunge(project)
                return project
        except IntegrityError:
            return None

    def update_cli_project_delivery(
        self,
        delivery_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Any]:
        try:
            with self._session() as session, session.begin():
                delivery = session.query(DBCliProjectDelivery).filter(
                    DBCliProjectDelivery.delivery_id == delivery_id,
                    DBCliProjectDelivery.owner_user_id == owner_user_id,
                ).first()
                if delivery is None:
                    return None
                for key, value in updates.items():
                    setattr(delivery, key, value)
                session.flush()
                session.refresh(delivery)
                session.expunge(delivery)
                return delivery
        except IntegrityError:
            return None

    def record_project_publish_audit(self, record: Dict[str, Any]) -> None:
        with self._session() as session, session.begin():
            session.add(DBProjectPublishAudit(**record))

    def list_project_publish_audits(self, project_id: str, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            return (
                session.query(DBProjectPublishAudit)
                .filter(
                    DBProjectPublishAudit.project_id == project_id,
                    DBProjectPublishAudit.owner_user_id == owner_user_id,
                )
                .order_by(DBProjectPublishAudit.created_at.desc())
                .all()
            )

    def get_cli_device_authorization(self, device_code_hash: Optional[str] = None, user_code_hash: Optional[str] = None) -> Optional[Any]:
        with self._session() as session:
            query = session.query(DBCliDeviceAuthorization)
            if device_code_hash:
                query = query.filter(DBCliDeviceAuthorization.device_code_hash == device_code_hash)
            if user_code_hash:
                query = query.filter(DBCliDeviceAuthorization.user_code_hash == user_code_hash)
            return query.first()

    def insert_cli_device_authorization(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            authorization = DBCliDeviceAuthorization(**record)
            session.add(authorization)
            session.flush()
            session.refresh(authorization)
            session.expunge(authorization)
            return authorization

    def update_cli_device_authorization(
        self,
        device_code_hash: str,
        updates: Dict[str, Any],
        expected_status: Optional[str] = None,
        expected_consumed: Optional[bool] = None,
    ) -> Optional[Any]:
        with self._session() as session, session.begin():
            query = session.query(DBCliDeviceAuthorization).filter(
                DBCliDeviceAuthorization.device_code_hash == device_code_hash,
            )
            if expected_status is not None:
                query = query.filter(DBCliDeviceAuthorization.status == expected_status)
            if expected_consumed is not None:
                query = query.filter(DBCliDeviceAuthorization.consumed == expected_consumed)
            authorization = query.first()
            if authorization is None:
                return None
            for key, value in updates.items():
                setattr(authorization, key, value)
            session.flush()
            session.refresh(authorization)
            session.expunge(authorization)
            return authorization

    def get_cli_token_session(self, token_hash: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBCliTokenSession).filter(
                DBCliTokenSession.token_hash == token_hash,
            ).first()

    def insert_cli_token_session(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            token = DBCliTokenSession(**record)
            session.add(token)
            session.flush()
            session.refresh(token)
            session.expunge(token)
            return token

    def revoke_cli_token_sessions(
        self,
        *,
        token_hash: Optional[str] = None,
        refresh_token_hash: Optional[str] = None,
        revoked_at: float,
    ) -> int:
        with self._session() as session, session.begin():
            count = 0
            queries = []
            if token_hash:
                queries.append(session.query(DBCliTokenSession).filter(
                    DBCliTokenSession.token_hash == token_hash,
                    DBCliTokenSession.revoked_at.is_(None),
                ))
            if refresh_token_hash:
                queries.append(session.query(DBCliTokenSession).filter(
                    DBCliTokenSession.refresh_token_hash == refresh_token_hash,
                    DBCliTokenSession.revoked_at.is_(None),
                ))
            seen: set[str] = set()
            for query in queries:
                for token in query.all():
                    if token.token_hash not in seen:
                        token.revoked_at = revoked_at
                        seen.add(token.token_hash)
                        count += 1
            return count

    def insert_project_revision(
        self,
        record: Dict[str, Any],
        expected_parent_revision: int,
    ) -> Optional[Any]:
        try:
            with self._session() as session, session.begin():
                latest = (
                    session.query(DBProjectRevision)
                    .filter(
                        DBProjectRevision.project_id == record["project_id"],
                        DBProjectRevision.owner_user_id == record["owner_user_id"],
                    )
                    .order_by(DBProjectRevision.revision.desc())
                    .first()
                )
                if (
                    latest is None
                    or latest.revision != expected_parent_revision
                    or record["parent_revision"] != expected_parent_revision
                    or record["revision"] != expected_parent_revision + 1
                ):
                    return None
                revision = DBProjectRevision(**record)
                session.add(revision)
                session.flush()
                session.refresh(revision)
                session.expunge(revision)
                return revision
        except IntegrityError:
            return None

    def get_validation_report(self, report_id: str, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectValidationReport).filter(
                DBProjectValidationReport.id == report_id,
                DBProjectValidationReport.owner_user_id == owner_user_id,
            ).first()

    def get_validation_report_by_source_job(
        self,
        project_id: str,
        owner_user_id: str,
        source_job_id: str,
    ) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectValidationReport).filter(
                DBProjectValidationReport.project_id == project_id,
                DBProjectValidationReport.owner_user_id == owner_user_id,
                DBProjectValidationReport.source_job_id == source_job_id,
            ).first()

    def insert_project_validation_report(self, record: Dict[str, Any]) -> Optional[Any]:
        try:
            with self._session() as session, session.begin():
                revision = session.query(DBProjectRevision).filter(
                    DBProjectRevision.project_id == record["project_id"],
                    DBProjectRevision.owner_user_id == record["owner_user_id"],
                    DBProjectRevision.revision == record["project_revision"],
                    DBProjectRevision.design_brief_id == record["design_brief_id"],
                    DBProjectRevision.design_brief_version == record["design_brief_version"],
                ).first()
                if revision is None:
                    return None
                parent_id = record.get("revalidation_of_report_id")
                if parent_id:
                    parent = session.query(DBProjectValidationReport).filter(
                        DBProjectValidationReport.id == parent_id,
                        DBProjectValidationReport.project_id == record["project_id"],
                        DBProjectValidationReport.owner_user_id == record["owner_user_id"],
                        DBProjectValidationReport.project_revision < record["project_revision"],
                    ).first()
                    if parent is None:
                        return None
                report = DBProjectValidationReport(**record)
                session.add(report)
                session.flush()
                session.refresh(report)
                session.expunge(report)
                return report
        except IntegrityError:
            return None

    def list_due_project_purges(self, before: str, limit: int) -> List[Any]:
        with self._session() as session:
            canonical = (
                session.query(DBProject)
                .filter(
                    DBProject.status.in_(("deletion_pending", "deletion_failed", "purging")),
                    DBProject.purge_after.isnot(None),
                    DBProject.purge_after <= before,
                )
                .order_by(DBProject.purge_after.asc())
                .limit(limit)
                .all()
            )
            canonical_ids = {str(project.project_id) for project in canonical}
            remaining = max(0, limit - len(canonical))
            legacy = (
                session.query(DBGeneratedProject)
                .filter(
                    DBGeneratedProject.project_id.notin_(canonical_ids or ["__none__"]),
                    DBGeneratedProject.status.in_(("deletion_pending", "deletion_failed", "purging")),
                    DBGeneratedProject.purge_after.isnot(None),
                    DBGeneratedProject.purge_after <= before,
                )
                .order_by(DBGeneratedProject.purge_after.asc())
                .limit(remaining)
                .all()
                if remaining
                else []
            )
            return [*canonical, *legacy]

    def update_project_deletion_state(
        self,
        project_id: str,
        owner_user_id: Optional[str],
        allowed_statuses: List[str],
        updates: Dict[str, Any],
        expected_purge_started_at: Optional[str] = None,
    ) -> Optional[Any]:
        with self._session() as session, session.begin():
            identity_query = session.query(DBProject).filter(
                DBProject.project_id == project_id,
                DBProject.status.in_(allowed_statuses),
            )
            if owner_user_id:
                identity_query = identity_query.filter(DBProject.owner_user_id == owner_user_id)
            if expected_purge_started_at is not None:
                identity_query = identity_query.filter(DBProject.purge_started_at == expected_purge_started_at)
            identity = identity_query.first()
            if identity is not None:
                for key, value in updates.items():
                    setattr(identity, key, value)
                projection = session.query(DBGeneratedProject).filter(
                    DBGeneratedProject.project_id == project_id,
                ).first()
                if projection is not None:
                    for key, value in updates.items():
                        if hasattr(projection, key):
                            setattr(projection, key, value)
                session.flush()
                session.refresh(identity)
                session.expunge(identity)
                return identity

            # An existing canonical identity that failed the ownership/status/
            # lease check cannot be bypassed through its legacy projection.
            if session.query(DBProject.project_id).filter(DBProject.project_id == project_id).first() is not None:
                return None

            query = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.project_id == project_id,
                DBGeneratedProject.status.in_(allowed_statuses),
            )
            if owner_user_id:
                query = query.filter(DBGeneratedProject.owner_user_id == owner_user_id)
            if expected_purge_started_at is not None:
                query = query.filter(DBGeneratedProject.purge_started_at == expected_purge_started_at)
            project = query.first()
            if not project:
                return None
            for key, value in updates.items():
                setattr(project, key, value)
            session.flush()
            session.refresh(project)
            session.expunge(project)
            return project

    def hard_purge_project(self, project_id: str, owner_user_id: Optional[str]) -> bool:
        with self._session() as session, session.begin():
            identity = session.query(DBProject).filter(DBProject.project_id == project_id).first()
            if identity is not None and owner_user_id and identity.owner_user_id != owner_user_id:
                return False
            project = session.query(DBGeneratedProject).filter(DBGeneratedProject.project_id == project_id).first()
            cli_project = session.query(DBCliProject).filter(DBCliProject.project_id == project_id).first()
            if identity is None and project is None and cli_project is None:
                return False
            if owner_user_id:
                if project is not None and project.owner_user_id not in (None, owner_user_id):
                    return False
                if cli_project is not None and cli_project.owner_user_id != owner_user_id:
                    return False
            chat_id = getattr(project, "chat_id", None) or getattr(identity, "chat_id", None)
            project_owner_user_id = (
                getattr(identity, "owner_user_id", None)
                or getattr(project, "owner_user_id", None)
                or getattr(cli_project, "owner_user_id", None)
            )
            session.query(DBProjectValidationReport).filter(
                DBProjectValidationReport.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBProjectRevision).filter(
                DBProjectRevision.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBWorkerExecutionPlan).filter(
                DBWorkerExecutionPlan.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBProjectBuild).filter(DBProjectBuild.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBDesignBrief).filter(DBDesignBrief.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProjectWorkflowTransition).filter(
                DBProjectWorkflowTransition.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBProjectWorkflow).filter(DBProjectWorkflow.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProjectSave).filter(DBProjectSave.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProjectRemix).filter(
                or_(
                    DBProjectRemix.remix_project_id == project_id,
                    DBProjectRemix.source_project_id == project_id,
                )
            ).delete(synchronize_session=False)
            session.query(DBGeneratedProject).filter(DBGeneratedProject.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBCliProjectRevision).filter(DBCliProjectRevision.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBCliProject).filter(DBCliProject.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProject).filter(DBProject.project_id == project_id).delete(
                synchronize_session=False
            )
            session.flush()
            if chat_id and project_owner_user_id:
                remaining = session.query(DBGeneratedProject).filter(
                    DBGeneratedProject.chat_id == chat_id,
                    DBGeneratedProject.owner_user_id == project_owner_user_id,
                ).count()
                chat = session.query(DBProjectChat).filter(
                    DBProjectChat.chat_id == chat_id,
                    DBProjectChat.owner_user_id == project_owner_user_id,
                ).first()
                if chat and remaining == 0:
                    session.delete(chat)
                elif chat and isinstance(chat.messages, list):
                    chat.messages = [
                        message
                        for message in chat.messages
                        if not isinstance(message, dict) or str(message.get("projectId") or message.get("project_id") or "") != project_id
                    ]
            return True

    def update_generated_project_hardware_ir(
        self,
        project_id: str,
        hardware_ir: Dict[str, Any],
        chat_id: Optional[str],
        owner_user_id: Optional[str],
    ) -> bool:
        with self._session() as session, session.begin():
            query = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.project_id == project_id
            )
            if owner_user_id:
                query = query.filter(DBGeneratedProject.owner_user_id == owner_user_id)
            project = query.first()
            if not project:
                return False
            if project.status != "active":
                return False
            project.hardware_ir = hardware_ir
            project.chat_id = chat_id
            return True

    def claim_unowned_generated_project(
        self,
        project_id: str,
        hardware_ir: Dict[str, Any],
        chat_id: Optional[str],
        owner_user_id: str,
    ) -> bool:
        with self._session() as session, session.begin():
            project = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.project_id == project_id,
                DBGeneratedProject.status == "active",
                DBGeneratedProject.owner_user_id.is_(None),
            ).first()
            if not project:
                return False
            project.hardware_ir = hardware_ir
            project.chat_id = chat_id
            project.owner_user_id = owner_user_id
            return True

    def update_generated_project_metadata(
        self,
        project_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> bool:
        with self._session() as session, session.begin():
            project = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.project_id == project_id,
                DBGeneratedProject.owner_user_id == owner_user_id,
                DBGeneratedProject.status == "active",
            ).first()
            if not project:
                return False
            for key, value in updates.items():
                setattr(project, key, value)
            return True

    def delete_generated_project(self, project_id: str, owner_user_id: str) -> bool:
        with self._session() as session, session.begin():
            project = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.project_id == project_id,
                DBGeneratedProject.owner_user_id == owner_user_id,
            ).first()
            if not project:
                return False
            session.query(DBProjectValidationReport).filter(
                DBProjectValidationReport.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBProjectRevision).filter(
                DBProjectRevision.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBWorkerExecutionPlan).filter(
                DBWorkerExecutionPlan.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBProjectBuild).filter(DBProjectBuild.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBDesignBrief).filter(DBDesignBrief.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProjectWorkflowTransition).filter(
                DBProjectWorkflowTransition.project_id == project_id
            ).delete(synchronize_session=False)
            session.query(DBProjectWorkflow).filter(DBProjectWorkflow.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProjectSave).filter(DBProjectSave.project_id == project_id).delete(
                synchronize_session=False
            )
            session.query(DBProjectRemix).filter(
                or_(
                    DBProjectRemix.remix_project_id == project_id,
                    DBProjectRemix.source_project_id == project_id,
                )
            ).delete(synchronize_session=False)
            session.delete(project)
            return True

    def get_project_contribution_consent(self, project_id: str, user_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectContributionConsent).filter(
                DBProjectContributionConsent.project_id == project_id,
                DBProjectContributionConsent.user_id == user_id,
            ).first()

    def upsert_project_contribution_consent(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            consent = session.query(DBProjectContributionConsent).filter(
                DBProjectContributionConsent.project_id == record["project_id"],
                DBProjectContributionConsent.user_id == record["user_id"],
            ).first()
            if consent:
                for key, value in record.items():
                    if key != "id":
                        setattr(consent, key, value)
            else:
                consent = DBProjectContributionConsent(**record)
                session.add(consent)
            session.flush()
            session.refresh(consent)
            session.expunge(consent)
            return consent

    def withdraw_project_contribution_consent(self, project_id: str, user_id: str, withdrawn_at: str) -> Optional[Any]:
        with self._session() as session, session.begin():
            consent = session.query(DBProjectContributionConsent).filter(
                DBProjectContributionConsent.project_id == project_id,
                DBProjectContributionConsent.user_id == user_id,
            ).first()
            if not consent:
                return None
            consent.withdrawn_at = withdrawn_at
            session.flush()
            session.refresh(consent)
            session.expunge(consent)
            return consent

    def anonymize_project_contribution_consent(
        self,
        project_id: str,
        user_id: str,
        anonymized_project_id: str,
        anonymized_user_id: str,
        anonymized_at: str,
    ) -> bool:
        with self._session() as session, session.begin():
            consent = session.query(DBProjectContributionConsent).filter(
                DBProjectContributionConsent.project_id == project_id,
                DBProjectContributionConsent.user_id == user_id,
            ).first()
            if not consent:
                return False
            consent.project_id = anonymized_project_id
            consent.user_id = anonymized_user_id
            consent.workspace_id = None
            consent.anonymized_at = anonymized_at
            return True

    def upsert_project_contribution_snapshot(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            snapshot = session.query(DBProjectContributionSnapshot).filter(
                DBProjectContributionSnapshot.consent_record_id == record["consent_record_id"]
            ).first()
            if snapshot:
                if snapshot.anonymized_at:
                    return snapshot
                for key, value in record.items():
                    if key != "id":
                        setattr(snapshot, key, value)
            else:
                snapshot = DBProjectContributionSnapshot(**record)
                session.add(snapshot)
            session.flush()
            session.refresh(snapshot)
            session.expunge(snapshot)
            return snapshot

    def anonymize_project_contribution_snapshot(
        self,
        consent_record_id: str,
        anonymized_source_id: str,
        anonymized_consent_id: str,
        anonymized_at: str,
    ) -> bool:
        with self._session() as session, session.begin():
            snapshot = session.query(DBProjectContributionSnapshot).filter(
                DBProjectContributionSnapshot.consent_record_id == consent_record_id,
                DBProjectContributionSnapshot.anonymized_at.is_(None),
            ).first()
            if not snapshot:
                return False
            snapshot.source_project_id = anonymized_source_id
            snapshot.consent_record_id = anonymized_consent_id
            snapshot.contribution_status = "anonymized"
            snapshot.anonymized_at = anonymized_at
            consent = session.query(DBProjectContributionConsent).filter(
                DBProjectContributionConsent.id == consent_record_id
            ).first()
            if consent:
                consent.anonymized_at = anonymized_at
            return True

    def purge_project_contribution_snapshots(self, consent_record_id: str, purged_at: str) -> int:
        with self._session() as session, session.begin():
            snapshots = session.query(DBProjectContributionSnapshot).filter(
                DBProjectContributionSnapshot.consent_record_id == consent_record_id,
                DBProjectContributionSnapshot.anonymized_at.is_(None),
            ).all()
            count = len(snapshots)
            for snapshot in snapshots:
                session.delete(snapshot)
            consent = session.query(DBProjectContributionConsent).filter(
                DBProjectContributionConsent.id == consent_record_id
            ).first()
            if consent:
                consent.purged_at = purged_at
            return count

    def add_project_deletion_audit(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            audit = DBProjectDeletionAudit(**record)
            session.add(audit)
            session.flush()
            session.refresh(audit)
            session.expunge(audit)
            return audit

    def get_latest_project_deletion_audit(self, project_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBProjectDeletionAudit).filter(
                DBProjectDeletionAudit.project_id == project_id
            ).order_by(DBProjectDeletionAudit.created_at.desc()).first()

    def list_project_deletion_audits(self, limit: int) -> List[Any]:
        with self._session() as session:
            return session.query(DBProjectDeletionAudit).order_by(
                DBProjectDeletionAudit.created_at.desc()
            ).limit(limit).all()

    def upsert_project_chat(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            chat = session.query(DBProjectChat).filter(
                DBProjectChat.chat_id == record["chat_id"],
                DBProjectChat.owner_user_id == record["owner_user_id"],
            ).first()
            if chat:
                chat.title = record["title"]
                chat.messages = record["messages"]
                chat.updated_at = record["updated_at"]
            else:
                chat = DBProjectChat(**record)
                session.add(chat)
            session.flush()
            session.refresh(chat)
            session.expunge(chat)
            return chat

    def list_project_chats(self, owner_user_id: str) -> List[Any]:
        with self._session() as session:
            chats = session.query(DBProjectChat).filter(
                DBProjectChat.owner_user_id == owner_user_id
            ).order_by(DBProjectChat.updated_at.desc()).all()
            projects = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.owner_user_id == owner_user_id,
                DBGeneratedProject.chat_id.isnot(None),
            ).all()
            canonical_projects = session.query(DBProject).filter(
                DBProject.owner_user_id == owner_user_id,
                DBProject.chat_id.isnot(None),
            ).all()
            by_chat: Dict[str, Dict[str, Any]] = {}
            for project in projects:
                by_chat.setdefault(str(project.chat_id), {})[str(project.project_id)] = project
            for project in canonical_projects:
                by_chat.setdefault(str(project.chat_id), {})[str(project.project_id)] = project
            visible = []
            for chat in chats:
                linked = list(by_chat.get(chat.chat_id, {}).values())
                if linked and not any(project.status == "active" for project in linked):
                    continue
                hidden_ids = {project.project_id for project in linked if project.status != "active"}
                if hidden_ids and isinstance(chat.messages, list):
                    chat.messages = [
                        message
                        for message in chat.messages
                        if not isinstance(message, dict)
                        or str(message.get("projectId") or message.get("project_id") or "") not in hidden_ids
                    ]
                visible.append(chat)
            return visible

    def get_project_chat(self, chat_id: str, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            chat = session.query(DBProjectChat).filter(
                DBProjectChat.chat_id == chat_id,
                DBProjectChat.owner_user_id == owner_user_id,
            ).first()
            if not chat:
                return None
            linked = session.query(DBGeneratedProject).filter(
                DBGeneratedProject.chat_id == chat_id,
                DBGeneratedProject.owner_user_id == owner_user_id,
            ).all()
            canonical = session.query(DBProject).filter(
                DBProject.chat_id == chat_id,
                DBProject.owner_user_id == owner_user_id,
            ).all()
            linked_by_id = {str(project.project_id): project for project in linked}
            linked_by_id.update({str(project.project_id): project for project in canonical})
            linked = list(linked_by_id.values())
            if linked and not any(project.status == "active" for project in linked):
                return None
            hidden_ids = {project.project_id for project in linked if project.status != "active"}
            if hidden_ids and isinstance(chat.messages, list):
                chat.messages = [
                    message
                    for message in chat.messages
                    if not isinstance(message, dict)
                    or str(message.get("projectId") or message.get("project_id") or "") not in hidden_ids
                ]
            return chat

    def delete_project_chat(self, chat_id: str, owner_user_id: str) -> bool:
        with self._session() as session, session.begin():
            chat = session.query(DBProjectChat).filter(
                DBProjectChat.chat_id == chat_id,
                DBProjectChat.owner_user_id == owner_user_id,
            ).first()
            if not chat:
                return False
            session.delete(chat)
            session.query(DBGeneratedProject).filter(
                DBGeneratedProject.chat_id == chat_id,
                DBGeneratedProject.owner_user_id == owner_user_id,
            ).update({"chat_id": None})
            session.query(DBProject).filter(
                DBProject.chat_id == chat_id,
                DBProject.owner_user_id == owner_user_id,
            ).update({"chat_id": None})
            return True

    def save_alpha_signup(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            signup = DBAlphaSignup(**record)
            session.add(signup)
            session.flush()
            session.refresh(signup)
            session.expunge(signup)
            return signup

    def insert_project_save(self, record: Dict[str, Any]) -> bool:
        try:
            with self._session() as session, session.begin():
                existing = session.query(DBProjectSave).filter(
                    DBProjectSave.project_id == record["project_id"],
                    DBProjectSave.owner_user_id == record["owner_user_id"],
                ).first()
                if existing:
                    return False
                session.add(DBProjectSave(**record))
                return True
        except IntegrityError:
            return False

    def delete_project_save(self, project_id: str, owner_user_id: str) -> bool:
        with self._session() as session, session.begin():
            deleted = session.query(DBProjectSave).filter(
                DBProjectSave.project_id == project_id,
                DBProjectSave.owner_user_id == owner_user_id,
            ).delete(synchronize_session=False)
            return bool(deleted)

    def count_project_saves(self, project_ids: List[str]) -> Dict[str, int]:
        if not project_ids:
            return {}
        with self._session() as session:
            rows = (
                session.query(DBProjectSave.project_id, func.count(DBProjectSave.id))
                .filter(DBProjectSave.project_id.in_(project_ids))
                .group_by(DBProjectSave.project_id)
                .all()
            )
            return {str(project_id): int(count) for project_id, count in rows}

    def list_saved_project_ids(self, owner_user_id: str, project_ids: List[str]) -> List[str]:
        if not project_ids:
            return []
        with self._session() as session:
            rows = (
                session.query(DBProjectSave.project_id)
                .filter(
                    DBProjectSave.owner_user_id == owner_user_id,
                    DBProjectSave.project_id.in_(project_ids),
                )
                .all()
            )
            return [str(row[0]) for row in rows]

    def insert_project_remix(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            remix = DBProjectRemix(**record)
            session.add(remix)
            session.flush()
            session.refresh(remix)
            session.expunge(remix)
            return remix

    def count_project_remixes(self, project_ids: List[str]) -> Dict[str, int]:
        if not project_ids:
            return {}
        with self._session() as session:
            rows = (
                session.query(DBProjectRemix.source_project_id, func.count(DBProjectRemix.id))
                .filter(DBProjectRemix.source_project_id.in_(project_ids))
                .group_by(DBProjectRemix.source_project_id)
                .all()
            )
            return {str(project_id): int(count) for project_id, count in rows}

    def get_user_settings(self, owner_user_id: str) -> Optional[Any]:
        with self._session() as session:
            return session.query(DBUserSettings).filter(
                DBUserSettings.owner_user_id == owner_user_id
            ).first()

    def upsert_user_settings(self, record: Dict[str, Any]) -> Any:
        with self._session() as session, session.begin():
            settings = session.query(DBUserSettings).filter(
                DBUserSettings.owner_user_id == record["owner_user_id"]
            ).first()
            if settings:
                settings.model_training_opt_out = record["model_training_opt_out"]
                settings.updated_at = record["updated_at"]
            else:
                settings = DBUserSettings(**record)
                session.add(settings)
            session.flush()
            session.refresh(settings)
            session.expunge(settings)
            return settings

    def list_model_training_opt_out_user_ids(self) -> List[str]:
        with self._session() as session:
            rows = session.query(DBUserSettings.owner_user_id).filter(
                DBUserSettings.model_training_opt_out.is_(True)
            ).all()
            return [str(row[0]) for row in rows]
