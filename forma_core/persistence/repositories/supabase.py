from __future__ import annotations

from collections import Counter
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from forma_core.workspaces.projects.manifest import build_canonical_revision_record

logger = logging.getLogger(__name__)


def _record(row: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**row)


def _postgrest_ilike_pattern(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace("*", "\\*")
    )
    return f'"*{escaped}*"'


class SupabaseRepository:
    """Application repository implemented through Supabase PostgREST."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def count_component_templates(self) -> int:
        rows = self._client.table("component_templates").select("id").execute().data or []
        return len(rows)

    def list_component_templates(self) -> List[Any]:
        rows = self._client.table("component_templates").select("*").order("id").execute().data or []
        return [_record(row) for row in rows]

    def get_component_template_by_part_number(self, part_number: str) -> Optional[Any]:
        rows = (
            self._client.table("component_templates")
            .select("*")
            .eq("part_number", part_number)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def insert_component_template(self, record: Dict[str, Any]) -> None:
        self._client.table("component_templates").insert(record).execute()

    def upsert_project_identity(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("projects").upsert(record, on_conflict="project_id").execute().data or []
        return _record(rows[0]) if rows else None

    def get_project_identity(self, project_id: str) -> Optional[Any]:
        rows = self._client.table("projects").select("*").eq("project_id", project_id).limit(1).execute().data or []
        # Project identity callers use mapping access, matching the SQLite repository.
        return rows[0] if rows else None

    def list_project_identities(self, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("projects")
            .select("*")
            .eq("owner_user_id", owner_user_id)
            .eq("status", "active")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def list_project_gallery_inventory_page(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> tuple[List[Any], int]:
        """Read one bounded page from the canonical gallery inventory view."""
        query = self._client.table("project_gallery_inventory").select(
            "project_id,owner_user_id,creation_channel,title,prompt,chat_id,workspace_id,visibility,status,"
            "created_at,updated_at,source,revision_id,revision,revision_payload_json,revision_created_at,"
            "legacy_hardware_ir,legacy_id",
            count="exact",
        ).eq("status", "active")
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        if visibility:
            query = query.eq("visibility", visibility)
        if search:
            pattern = _postgrest_ilike_pattern(search)
            query = query.or_(f"title.ilike.{pattern},prompt.ilike.{pattern}")
        response = (
            query.order("updated_at", desc=True)
            .order("project_id", desc=True)
            .range(max(0, int(offset)), max(0, int(offset)) + max(1, int(limit)) - 1)
            .execute()
        )
        rows = response.data or []
        response_count = getattr(response, "count", None)
        total = response_count if isinstance(response_count, int) else len(rows)
        return [_record(row) for row in rows], total

    def list_project_gallery_inventory(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        search: Optional[str] = None,
    ) -> List[Any]:
        query = self._client.table("project_gallery_inventory").select(
            "project_id,owner_user_id,creation_channel,title,prompt,chat_id,workspace_id,visibility,status,"
            "created_at,updated_at,source,revision_id,revision,revision_payload_json,revision_created_at,"
            "legacy_hardware_ir,legacy_id"
        ).eq("status", "active")
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        if visibility:
            query = query.eq("visibility", visibility)
        if search:
            pattern = _postgrest_ilike_pattern(search)
            query = query.or_(f"title.ilike.{pattern},prompt.ilike.{pattern}")
        rows = query.order("updated_at", desc=True).order("project_id", desc=True).execute().data or []
        return [_record(row) for row in rows]

    def save_generated_project(
        self,
        record: Dict[str, Any],
        chat_record: Optional[Dict[str, Any]],
    ) -> None:
        self._client.table("generated_projects").insert(record).execute()
        self._client.table("projects").upsert({
            "project_id": record["project_id"],
            "owner_user_id": record.get("owner_user_id"),
            "creation_channel": record.get("creation_channel", "hosted"),
            "title": record.get("title", ""),
            "prompt": record.get("prompt", ""),
            "chat_id": record.get("chat_id"),
            "visibility": record.get("visibility", "public"),
            "status": record.get("status", "active"),
            "created_at": record["created_at"],
            "updated_at": record["created_at"],
        }, on_conflict="project_id").execute()
        if chat_record:
            self.upsert_project_chat(chat_record)

    def list_generated_projects(self, owner_user_id: Optional[str]) -> List[Any]:
        query = self._client.table("generated_projects").select(
            "id,project_id,chat_id,title,prompt,created_at,owner_user_id,creation_channel,visibility,hardware_ir,status,"
            "deleted_at,deletion_requested_by,purge_after,purge_started_at,purge_completed_at,deletion_error"
        ).eq("status", "active")
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        rows = query.order("id", desc=True).execute().data or []
        return [_record(row) for row in rows]

    def list_generated_projects_page(
        self,
        owner_user_id: Optional[str],
        *,
        visibility: Optional[str],
        limit: int,
        offset: int,
        search: Optional[str] = None,
    ) -> tuple[List[Any], int]:
        query = self._client.table("generated_projects").select(
            "id,project_id,chat_id,title,prompt,created_at,owner_user_id,creation_channel,visibility,hardware_ir,status,"
            "deleted_at,deletion_requested_by,purge_after,purge_started_at,purge_completed_at,deletion_error",
            count="exact",
        ).eq("status", "active")
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        if visibility:
            query = query.eq("visibility", visibility)
        if search:
            pattern = _postgrest_ilike_pattern(search)
            query = query.or_(f"title.ilike.{pattern},prompt.ilike.{pattern}")
        response = (
            query.order("created_at", desc=True)
            .order("id", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = response.data or []
        response_count = getattr(response, "count", None)
        total = response_count if isinstance(response_count, int) else len(rows)
        return [_record(row) for row in rows], total

    def get_generated_project(self, project_id: str, include_deleted: bool = False) -> Optional[Any]:
        query = self._client.table("generated_projects").select("*").eq("project_id", project_id)
        if not include_deleted:
            query = query.eq("status", "active")
        rows = query.limit(1).execute().data or []
        return _record(rows[0]) if rows else None

    def insert_design_brief_version(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("design_briefs").insert(record).execute().data or []
        return _record(rows[0]) if rows else _record(record)

    def list_design_brief_versions(self, project_id: str, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("design_briefs")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .order("brief_version")
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def get_design_brief_version(
        self,
        project_id: str,
        owner_user_id: str,
        brief_version: int,
    ) -> Optional[Any]:
        rows = (
            self._client.table("design_briefs")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("brief_version", brief_version)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def get_latest_design_brief(self, project_id: str, owner_user_id: Optional[str]) -> Optional[Any]:
        query = self._client.table("design_briefs").select("*").eq("project_id", project_id)
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        rows = query.order("brief_version", desc=True).limit(1).execute().data or []
        return _record(rows[0]) if rows else None

    def get_project_workflow(self, project_id: str, owner_user_id: Optional[str]) -> Optional[Any]:
        query = self._client.table("project_workflows").select("*").eq("project_id", project_id)
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        rows = query.limit(1).execute().data or []
        return _record(rows[0]) if rows else None

    def list_project_workflow_transitions(self, project_id: str, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("project_workflow_transitions")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .order("revision")
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def get_project_workflow_transition_by_idempotency(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]:
        rows = (
            self._client.table("project_workflow_transitions")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def apply_project_workflow_transition(
        self,
        state_record: Dict[str, Any],
        transition_record: Dict[str, Any],
        expected_state: Optional[str],
        expected_revision: Optional[int],
    ) -> Optional[tuple[Any, Any]]:
        data = (
            self._client.rpc(
                "apply_project_workflow_transition",
                {
                    "p_state": state_record,
                    "p_transition": transition_record,
                    "p_expected_state": expected_state,
                    "p_expected_revision": expected_revision,
                },
            ).execute().data
        )
        payload = data[0] if isinstance(data, list) and data else data
        if not isinstance(payload, dict) or not payload.get("workflow") or not payload.get("transition"):
            return None
        return _record(payload["workflow"]), _record(payload["transition"])

    def get_project_build_by_idempotency(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]:
        rows = (
            self._client.table("project_builds")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def get_latest_project_build(self, project_id: str, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("project_builds")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def apply_project_build_initiation(
        self,
        state_record: Dict[str, Any],
        transition_record: Dict[str, Any],
        build_record: Dict[str, Any],
        expected_state: str,
        expected_revision: int,
    ) -> Optional[tuple[Any, Any, Any]]:
        data = (
            self._client.rpc(
                "apply_project_build_initiation",
                {
                    "p_state": state_record,
                    "p_transition": transition_record,
                    "p_build": build_record,
                    "p_expected_state": expected_state,
                    "p_expected_revision": expected_revision,
                },
            ).execute().data
        )
        payload = data[0] if isinstance(data, list) and data else data
        if not isinstance(payload, dict) or not all(payload.get(key) for key in ("workflow", "transition", "build")):
            return None
        return _record(payload["workflow"]), _record(payload["transition"]), _record(payload["build"])

    def insert_worker_execution_plan(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("worker_execution_plans").insert(record).execute().data or []
        return _record(rows[0]) if rows else _record(record)

    def get_worker_execution_plan(self, plan_id: str, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("worker_execution_plans")
            .select("*")
            .eq("id", plan_id)
            .eq("owner_user_id", owner_user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def list_worker_execution_plans(self, limit: int = 200) -> List[Any]:
        rows = (
            self._client.table("worker_execution_plans")
            .select("*")
            .order("created_at", desc=True)
            .limit(max(1, min(int(limit), 1000)))
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def update_worker_execution_plan(
        self,
        plan_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Any]:
        rows = (
            self._client.table("worker_execution_plans")
            .update(updates)
            .eq("id", plan_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def get_latest_project_revision(self, project_id: str, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("project_revisions")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .order("revision", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def list_project_revisions(self, project_id: str, owner_user_id: str, *, limit: int, before: int | None = None) -> List[Any]:
        query = (self._client.table("project_revisions").select("*")
                 .eq("project_id", project_id).eq("owner_user_id", owner_user_id))
        if before is not None:
            query = query.lt("revision", before)
        rows = query.order("revision", desc=True).limit(limit).execute().data or []
        return [_record(row) for row in rows]

    def get_project_revision_by_id(self, project_id: str, owner_user_id: str, revision_id: str) -> Optional[Any]:
        rows = (self._client.table("project_revisions").select("*")
                .eq("project_id", project_id).eq("owner_user_id", owner_user_id)
                .eq("id", revision_id).limit(1).execute().data or [])
        return _record(rows[0]) if rows else None

    def list_latest_project_revisions(self, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("project_revisions")
            .select("*")
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        latest_by_project: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            project_id = str(row.get("project_id") or "").strip()
            if not project_id:
                continue
            current = latest_by_project.get(project_id)
            if current is None or int(row.get("revision") or 0) > int(current.get("revision") or 0):
                latest_by_project[project_id] = row
        latest = sorted(
            latest_by_project.values(),
            key=lambda row: str(row.get("created_at") or ""),
            reverse=True,
        )
        return [_record(row) for row in latest]

    def get_project_revision(
        self,
        project_id: str,
        owner_user_id: str,
        revision: int,
    ) -> Optional[Any]:
        rows = (
            self._client.table("project_revisions")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("revision", revision)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def get_project_revision_by_source_job(
        self,
        project_id: str,
        owner_user_id: str,
        source_job_id: str,
    ) -> Optional[Any]:
        rows = (
            self._client.table("project_revisions")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("source_job_id", source_job_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def insert_initial_project_revision(self, record: Dict[str, Any]) -> Optional[Any]:
        data = self._client.rpc("insert_initial_project_revision", {"p_revision": record}).execute().data
        payload = data[0] if isinstance(data, list) and data else data
        return _record(payload) if isinstance(payload, dict) else None

    def get_cli_project(self, project_id: str, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("cli_projects")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def list_cli_projects(self, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("cli_projects")
            .select("*")
            .eq("owner_user_id", owner_user_id)
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def get_cli_project_revision(
        self,
        project_id: str,
        owner_user_id: str,
        revision_id: Optional[str] = None,
    ) -> Optional[Any]:
        query = (
            self._client.table("cli_project_revisions")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
        )
        if revision_id:
            query = query.eq("revision_id", revision_id)
        rows = query.order("revision", desc=True).limit(1).execute().data or []
        return _record(rows[0]) if rows else None

    def insert_cli_project_revision(
        self,
        project_record: Dict[str, Any],
        revision_record: Dict[str, Any],
        expected_revision_id: Optional[str],
    ) -> Optional[Any]:
        identity = self.get_project_identity(project_record["project_id"])
        if identity is not None and getattr(identity, "status", "active") != "active":
            logger.warning(
                "cli_project_push_rejected_lifecycle project_id=%s status=%s",
                project_record["project_id"],
                getattr(identity, "status", None),
            )
            return None
        project = self.get_cli_project(project_record["project_id"], project_record["owner_user_id"])
        latest = self.get_cli_project_revision(project_record["project_id"], project_record["owner_user_id"])
        if (
            latest is not None
            and getattr(latest, "revision", None) == revision_record["revision"]
            and getattr(latest, "parent_revision_id", None) == expected_revision_id
            and getattr(latest, "manifest_json", None) == revision_record.get("manifest_json")
        ):
            try:
                logger.info("cli_project_push_recovered project_id=%s revision_id=%s", project_record["project_id"], latest.revision_id)
                self._ensure_canonical_cli_revision(
                    project_record,
                    {
                        "revision_id": latest.revision_id,
                        "revision": latest.revision,
                        "manifest_json": latest.manifest_json,
                        "created_at": latest.created_at,
                    },
                )
                self._client.table("projects").upsert({
                    "project_id": project_record["project_id"],
                    "owner_user_id": project_record["owner_user_id"],
                    "creation_channel": "cli",
                    "title": project_record["title"],
                    "prompt": str((revision_record.get("manifest_json") or {}).get("prompt") or ""),
                    "workspace_id": project_record.get("workspace_id"),
                    "visibility": project_record.get("visibility", "public"),
                    "status": "active",
                    "current_revision": latest.revision,
                    "current_revision_id": latest.revision_id,
                    "created_at": project_record["created_at"],
                    "updated_at": latest.created_at,
                }, on_conflict="project_id").execute()
                project_updates = {
                    "workspace_id": project_record["workspace_id"],
                    "title": project_record["title"],
                    "current_revision": latest.revision,
                    "current_revision_id": latest.revision_id,
                    "updated_at": latest.created_at,
                }
                if project is None:
                    self._client.table("cli_projects").insert({
                        **project_record,
                        **project_updates,
                    }).execute()
                else:
                    self._client.table("cli_projects").update(project_updates).eq(
                        "project_id", project_record["project_id"]
                    ).eq("owner_user_id", project_record["owner_user_id"]).execute()
            except Exception:
                logger.exception("cli_project_push_recovery_failed project_id=%s revision_id=%s", project_record["project_id"], latest.revision_id)
                return None
            return latest
        if project is None:
            if expected_revision_id is not None:
                return None
            try:
                self._client.table("cli_projects").insert(project_record).execute()
            except Exception:
                logger.exception("cli_project_push_compatibility_create_failed project_id=%s", project_record["project_id"])
                return None
        elif (
            getattr(project, "current_revision_id", None) != expected_revision_id
            or int(getattr(project, "current_revision", 0)) + 1 != revision_record["revision"]
        ):
            return None
        try:
            rows = self._client.table("cli_project_revisions").insert(revision_record).execute().data or []
            self._ensure_canonical_cli_revision(project_record, revision_record)
            self._client.table("projects").upsert({
                "project_id": project_record["project_id"],
                "owner_user_id": project_record["owner_user_id"],
                "creation_channel": "cli",
                "title": project_record["title"],
                "prompt": str((revision_record.get("manifest_json") or {}).get("prompt") or ""),
                "workspace_id": project_record.get("workspace_id"),
                "visibility": project_record.get("visibility", "public"),
                "status": "active",
                "current_revision": revision_record["revision"],
                "current_revision_id": revision_record["revision_id"],
                "created_at": project_record["created_at"],
                "updated_at": revision_record["created_at"],
            }, on_conflict="project_id").execute()
            self._client.table("cli_projects").update({
                "workspace_id": project_record["workspace_id"],
                "title": project_record["title"],
                "current_revision": revision_record["revision"],
                "current_revision_id": revision_record["revision_id"],
                "updated_at": revision_record["created_at"],
            }).eq("project_id", project_record["project_id"]).eq(
                "owner_user_id", project_record["owner_user_id"]
            ).execute()
        except Exception:
            logger.exception("cli_project_push_partial_write project_id=%s revision_id=%s", project_record["project_id"], revision_record["revision_id"])
            return None
        return _record(rows[0]) if rows else None

    def get_cli_project_delivery(
        self,
        project_id: str,
        owner_user_id: str,
        idempotency_key: str,
    ) -> Optional[Any]:
        rows = (
            self._client.table("cli_project_deliveries")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def get_cli_project_delivery_by_id(self, delivery_id: str) -> Optional[Any]:
        rows = (
            self._client.table("cli_project_deliveries")
            .select("*")
            .eq("delivery_id", delivery_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def list_cli_project_deliveries(self, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("cli_project_deliveries")
            .select("*")
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def insert_cli_project_delivery(self, record: Dict[str, Any]) -> Any:
        try:
            rows = self._client.table("cli_project_deliveries").insert(record).execute().data or []
        except Exception:
            existing = self.get_cli_project_delivery(
                record["project_id"],
                record["owner_user_id"],
                record["idempotency_key"],
            )
            if existing is None:
                raise
            return existing
        return _record(rows[0]) if rows else _record(record)

    def update_cli_project_delivery(
        self,
        delivery_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Any]:
        rows = (
            self._client.table("cli_project_deliveries")
            .update(updates)
            .eq("delivery_id", delivery_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def update_cli_project_visibility(
        self,
        project_id: str,
        owner_user_id: str,
        visibility: str,
    ) -> Optional[Any]:
        rows = (
            self._client.table("cli_projects")
            .update({"visibility": visibility})
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def record_project_publish_audit(self, record: Dict[str, Any]) -> None:
        self._client.table("project_publish_audit").insert(record).execute()

    def list_project_publish_audits(self, project_id: str, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("project_publish_audit")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def _ensure_canonical_cli_revision(
        self,
        project_record: Dict[str, Any],
        revision_record: Dict[str, Any],
    ) -> None:
        canonical = build_canonical_revision_record(project_record, revision_record)
        if canonical is None:
            logger.info(
                "cli_project_canonical_revision_skipped project_id=%s reason=legacy_identifier_or_invalid_ir",
                project_record["project_id"],
            )
            return
        existing = (
            self._client.table("project_revisions")
            .select("id")
            .eq("project_id", canonical["project_id"])
            .eq("revision", canonical["revision"])
            .limit(1)
            .execute()
            .data
            or []
        )
        if not existing:
            self._client.table("project_revisions").insert(canonical).execute()

    def get_cli_device_authorization(self, device_code_hash: Optional[str] = None, user_code_hash: Optional[str] = None) -> Optional[Any]:
        query = self._client.table("cli_device_authorizations").select("*")
        if device_code_hash:
            query = query.eq("device_code_hash", device_code_hash)
        if user_code_hash:
            query = query.eq("user_code_hash", user_code_hash)
        rows = query.limit(1).execute().data or []
        return _record(rows[0]) if rows else None

    def insert_cli_device_authorization(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("cli_device_authorizations").insert(record).execute().data or []
        return _record(rows[0]) if rows else _record(record)

    def update_cli_device_authorization(
        self,
        device_code_hash: str,
        updates: Dict[str, Any],
        expected_status: Optional[str] = None,
        expected_consumed: Optional[bool] = None,
    ) -> Optional[Any]:
        query = self._client.table("cli_device_authorizations").update(updates).eq(
            "device_code_hash", device_code_hash
        )
        if expected_status is not None:
            query = query.eq("status", expected_status)
        if expected_consumed is not None:
            query = query.eq("consumed", expected_consumed)
        rows = query.select("*").execute().data or []
        return _record(rows[0]) if rows else None

    def get_cli_token_session(self, token_hash: str) -> Optional[Any]:
        rows = self._client.table("cli_token_sessions").select("*").eq("token_hash", token_hash).limit(1).execute().data or []
        return _record(rows[0]) if rows else None

    def insert_cli_token_session(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("cli_token_sessions").insert(record).execute().data or []
        return _record(rows[0]) if rows else _record(record)

    def revoke_cli_token_sessions(
        self,
        *,
        token_hash: Optional[str] = None,
        refresh_token_hash: Optional[str] = None,
        revoked_at: float,
    ) -> int:
        count = 0
        seen: set[str] = set()
        for column, value in (("token_hash", token_hash), ("refresh_token_hash", refresh_token_hash)):
            if not value:
                continue
            rows = (
                self._client.table("cli_token_sessions")
                .update({"revoked_at": revoked_at})
                .eq(column, value)
                .is_("revoked_at", "null")
                .execute()
                .data
                or []
            )
            for row in rows:
                token_hash_value = str(row.get("token_hash") or "")
                if token_hash_value and token_hash_value not in seen:
                    seen.add(token_hash_value)
                    count += 1
        return count

    def insert_project_revision(
        self,
        record: Dict[str, Any],
        expected_parent_revision: int,
    ) -> Optional[Any]:
        latest = self.get_latest_project_revision(record["project_id"], record["owner_user_id"])
        if (
            latest is None
            or int(latest.revision) != expected_parent_revision
            or record["parent_revision"] != expected_parent_revision
            or record["revision"] != expected_parent_revision + 1
        ):
            return None
        try:
            rows = self._client.table("project_revisions").insert(record).execute().data or []
        except Exception:
            return None
        return _record(rows[0]) if rows else None

    def get_validation_report(self, report_id: str, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("project_validation_reports")
            .select("*")
            .eq("id", report_id)
            .eq("owner_user_id", owner_user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def get_validation_report_by_source_job(
        self,
        project_id: str,
        owner_user_id: str,
        source_job_id: str,
    ) -> Optional[Any]:
        rows = (
            self._client.table("project_validation_reports")
            .select("*")
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("source_job_id", source_job_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def insert_project_validation_report(self, record: Dict[str, Any]) -> Optional[Any]:
        data = self._client.rpc("insert_project_validation_report", {"p_report": record}).execute().data
        payload = data[0] if isinstance(data, list) and data else data
        return _record(payload) if isinstance(payload, dict) else None

    def list_due_project_purges(self, before: str, limit: int) -> List[Any]:
        canonical_rows = (
            self._client.table("projects")
            .select("*")
            .in_("status", ["deletion_pending", "deletion_failed", "purging"])
            .lte("purge_after", before)
            .order("purge_after")
            .limit(limit)
            .execute()
            .data
            or []
        )
        canonical = [_record(row) for row in canonical_rows]
        remaining = max(0, limit - len(canonical))
        if not remaining:
            return canonical
        canonical_ids = {str(project.project_id) for project in canonical}
        legacy_rows = (
            self._client.table("generated_projects")
            .select("*")
            .in_("status", ["deletion_pending", "deletion_failed", "purging"])
            .lte("purge_after", before)
            .order("purge_after")
            .limit(limit)
            .execute()
            .data
            or []
        )
        return [*canonical, *[_record(row) for row in legacy_rows if str(row.get("project_id")) not in canonical_ids][:remaining]]

    def update_project_deletion_state(
        self,
        project_id: str,
        owner_user_id: Optional[str],
        allowed_statuses: List[str],
        updates: Dict[str, Any],
        expected_purge_started_at: Optional[str] = None,
    ) -> Optional[Any]:
        identity = self.get_project_identity(project_id)
        if identity is not None:
            identity_owner = getattr(identity, "owner_user_id", None) if not isinstance(identity, dict) else identity.get("owner_user_id")
            identity_status = getattr(identity, "status", "active") if not isinstance(identity, dict) else identity.get("status", "active")
            identity_purge_started_at = getattr(identity, "purge_started_at", None) if not isinstance(identity, dict) else identity.get("purge_started_at")
            if owner_user_id and identity_owner != owner_user_id:
                return None
            if identity_status not in allowed_statuses:
                return None
            if expected_purge_started_at is not None and identity_purge_started_at != expected_purge_started_at:
                return None
            updated_identity = (
                self._client.table("projects")
                .update(updates)
                .eq("project_id", project_id)
                .in_("status", allowed_statuses)
            )
            if owner_user_id:
                updated_identity = updated_identity.eq("owner_user_id", owner_user_id)
            if expected_purge_started_at is not None:
                updated_identity = updated_identity.eq("purge_started_at", expected_purge_started_at)
            rows = updated_identity.execute().data or []
            if not rows:
                return None
            projection_query = self._client.table("generated_projects").update(updates).eq("project_id", project_id)
            if owner_user_id:
                projection_query = projection_query.eq("owner_user_id", owner_user_id)
            projection_query.execute()
            return _record(rows[0])

        query = (
            self._client.table("generated_projects")
            .update(updates)
            .eq("project_id", project_id)
            .in_("status", allowed_statuses)
        )
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        if expected_purge_started_at is not None:
            query = query.eq("purge_started_at", expected_purge_started_at)
        rows = query.execute().data or []
        return _record(rows[0]) if rows else None

    def hard_purge_project(self, project_id: str, owner_user_id: Optional[str]) -> bool:
        identity = self.get_project_identity(project_id)
        project = self.get_generated_project(project_id, include_deleted=True)
        cli_project = self.get_cli_project(project_id, owner_user_id) if owner_user_id else None
        identity_owner = getattr(identity, "owner_user_id", None) if identity is not None else None
        if owner_user_id and identity_owner and identity_owner != owner_user_id:
            return False
        if owner_user_id and project and project.owner_user_id not in (None, owner_user_id):
            return False
        if not identity and not project and not cli_project:
            return False
        query = self._client.table("generated_projects").delete().eq("project_id", project_id)
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        deleted = bool(query.execute().data) if project else False
        if cli_project:
            self._client.table("cli_project_revisions").delete().eq("project_id", project_id).execute()
            self._client.table("cli_projects").delete().eq("project_id", project_id).execute()
        if identity or deleted:
            for table in (
                "project_validation_reports",
                "project_revisions",
                "worker_execution_plans",
                "project_builds",
                "design_briefs",
                "project_workflow_transitions",
                "project_workflows",
                "project_saves",
            ):
                self._client.table(table).delete().eq("project_id", project_id).execute()
            self._client.table("project_remixes").delete().eq("remix_project_id", project_id).execute()
            self._client.table("project_remixes").delete().eq("source_project_id", project_id).execute()
            identity_query = self._client.table("projects").delete().eq("project_id", project_id)
            if owner_user_id:
                identity_query = identity_query.eq("owner_user_id", owner_user_id)
            identity_query.execute()
        chat_id = getattr(project, "chat_id", None) or (getattr(identity, "chat_id", None) if identity else None)
        project_owner = getattr(project, "owner_user_id", None) or identity_owner
        if not chat_id or not project_owner:
            return bool(identity or deleted or cli_project)
        remaining = (
            self._client.table("generated_projects")
            .select("project_id")
            .eq("chat_id", chat_id)
            .eq("owner_user_id", project_owner)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not remaining:
            (
                self._client.table("project_chats")
                .delete()
                .eq("chat_id", chat_id)
                .eq("owner_user_id", project_owner)
                .execute()
            )
        else:
            chat = self.get_project_chat(chat_id, project_owner)
            if chat and isinstance(getattr(chat, "messages", None), list):
                messages = [
                    message
                    for message in chat.messages
                    if not isinstance(message, dict) or str(message.get("projectId") or message.get("project_id") or "") != project_id
                ]
                (
                    self._client.table("project_chats")
                    .update({"messages": messages})
                    .eq("chat_id", chat_id)
                    .eq("owner_user_id", project_owner)
                    .execute()
                )
        return bool(identity or deleted or cli_project)

    def update_generated_project_hardware_ir(
        self,
        project_id: str,
        hardware_ir: Dict[str, Any],
        chat_id: Optional[str],
        owner_user_id: Optional[str],
    ) -> bool:
        query = self._client.table("generated_projects").update(
            {"hardware_ir": hardware_ir, "chat_id": chat_id}
        ).eq("project_id", project_id).eq("status", "active")
        if owner_user_id:
            query = query.eq("owner_user_id", owner_user_id)
        return bool(query.execute().data)

    def claim_unowned_generated_project(
        self,
        project_id: str,
        hardware_ir: Dict[str, Any],
        chat_id: Optional[str],
        owner_user_id: str,
    ) -> bool:
        response = (
            self._client.table("generated_projects")
            .update({
                "hardware_ir": hardware_ir,
                "chat_id": chat_id,
                "owner_user_id": owner_user_id,
            })
            .eq("project_id", project_id)
            .eq("status", "active")
            .is_("owner_user_id", "null")
            .execute()
        )
        return bool(response.data)

    def update_generated_project_metadata(
        self,
        project_id: str,
        owner_user_id: str,
        updates: Dict[str, Any],
    ) -> bool:
        response = (
            self._client.table("generated_projects")
            .update(updates)
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .eq("status", "active")
            .execute()
        )
        return bool(response.data)

    def delete_generated_project(self, project_id: str, owner_user_id: str) -> bool:
        response = (
            self._client.table("generated_projects")
            .delete()
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
        )
        deleted = bool(response.data)
        if deleted:
            self._client.table("project_validation_reports").delete().eq("project_id", project_id).execute()
            self._client.table("project_revisions").delete().eq("project_id", project_id).execute()
            self._client.table("worker_execution_plans").delete().eq("project_id", project_id).execute()
            self._client.table("project_builds").delete().eq("project_id", project_id).execute()
            self._client.table("design_briefs").delete().eq("project_id", project_id).execute()
            self._client.table("project_workflow_transitions").delete().eq("project_id", project_id).execute()
            self._client.table("project_workflows").delete().eq("project_id", project_id).execute()
            self._client.table("project_saves").delete().eq("project_id", project_id).execute()
            self._client.table("project_remixes").delete().eq("remix_project_id", project_id).execute()
            self._client.table("project_remixes").delete().eq("source_project_id", project_id).execute()
        return deleted

    def get_project_contribution_consent(self, project_id: str, user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("project_contribution_consents")
            .select("*")
            .eq("project_id", project_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def upsert_project_contribution_consent(self, record: Dict[str, Any]) -> Any:
        existing = self.get_project_contribution_consent(record["project_id"], record["user_id"])
        if existing:
            values = {key: value for key, value in record.items() if key != "id"}
            response = (
                self._client.table("project_contribution_consents")
                .update(values)
                .eq("id", existing.id)
                .execute()
            )
        else:
            response = self._client.table("project_contribution_consents").insert(record).execute()
        rows = response.data or []
        return _record(rows[0]) if rows else _record({**record, "id": getattr(existing, "id", record["id"])})

    def withdraw_project_contribution_consent(self, project_id: str, user_id: str, withdrawn_at: str) -> Optional[Any]:
        rows = (
            self._client.table("project_contribution_consents")
            .update({"withdrawn_at": withdrawn_at})
            .eq("project_id", project_id)
            .eq("user_id", user_id)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def anonymize_project_contribution_consent(
        self,
        project_id: str,
        user_id: str,
        anonymized_project_id: str,
        anonymized_user_id: str,
        anonymized_at: str,
    ) -> bool:
        rows = (
            self._client.table("project_contribution_consents")
            .update(
                {
                    "project_id": anonymized_project_id,
                    "user_id": anonymized_user_id,
                    "workspace_id": None,
                    "anonymized_at": anonymized_at,
                }
            )
            .eq("project_id", project_id)
            .eq("user_id", user_id)
            .execute()
            .data
            or []
        )
        return bool(rows)

    def upsert_project_contribution_snapshot(self, record: Dict[str, Any]) -> Any:
        rows = (
            self._client.table("project_contribution_snapshots")
            .select("*")
            .eq("consent_record_id", record["consent_record_id"])
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("anonymized_at"):
            return _record(rows[0])
        if rows:
            values = {key: value for key, value in record.items() if key != "id"}
            response = (
                self._client.table("project_contribution_snapshots")
                .update(values)
                .eq("id", rows[0]["id"])
                .execute()
            )
        else:
            response = self._client.table("project_contribution_snapshots").insert(record).execute()
        response_rows = response.data or []
        return _record(response_rows[0]) if response_rows else _record(record)

    def anonymize_project_contribution_snapshot(
        self,
        consent_record_id: str,
        anonymized_source_id: str,
        anonymized_consent_id: str,
        anonymized_at: str,
    ) -> bool:
        rows = (
            self._client.table("project_contribution_snapshots")
            .update(
                {
                    "source_project_id": anonymized_source_id,
                    "consent_record_id": anonymized_consent_id,
                    "contribution_status": "anonymized",
                    "anonymized_at": anonymized_at,
                }
            )
            .eq("consent_record_id", consent_record_id)
            .is_("anonymized_at", "null")
            .execute()
            .data
            or []
        )
        if rows:
            (
                self._client.table("project_contribution_consents")
                .update({"anonymized_at": anonymized_at})
                .eq("id", consent_record_id)
                .execute()
            )
        return bool(rows)

    def purge_project_contribution_snapshots(self, consent_record_id: str, purged_at: str) -> int:
        rows = (
            self._client.table("project_contribution_snapshots")
            .select("id,anonymized_at")
            .eq("consent_record_id", consent_record_id)
            .execute()
            .data
            or []
        )
        snapshot_ids = [row["id"] for row in rows if not row.get("anonymized_at")]
        for snapshot_id in snapshot_ids:
            self._client.table("project_contribution_snapshots").delete().eq("id", snapshot_id).execute()
        self._client.table("project_contribution_consents").update({"purged_at": purged_at}).eq(
            "id", consent_record_id
        ).execute()
        return len(snapshot_ids)

    def add_project_deletion_audit(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("project_deletion_audit").insert(record).execute().data or []
        return _record(rows[0]) if rows else _record(record)

    def get_latest_project_deletion_audit(self, project_id: str) -> Optional[Any]:
        rows = (
            self._client.table("project_deletion_audit")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def list_project_deletion_audits(self, limit: int) -> List[Any]:
        rows = (
            self._client.table("project_deletion_audit")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
        return [_record(row) for row in rows]

    def upsert_project_chat(self, record: Dict[str, Any]) -> Any:
        rows = (
            self._client.table("project_chats")
            .select("*")
            .eq("chat_id", record["chat_id"])
            .eq("owner_user_id", record["owner_user_id"])
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            response = (
                self._client.table("project_chats")
                .update(
                    {
                        "title": record["title"],
                        "messages": record["messages"],
                        "updated_at": record["updated_at"],
                    }
                )
                .eq("chat_id", record["chat_id"])
                .eq("owner_user_id", record["owner_user_id"])
                .execute()
            )
        else:
            response = self._client.table("project_chats").insert(record).execute()
        response_rows = response.data or []
        return _record(response_rows[0]) if response_rows else _record(record)

    def list_project_chats(self, owner_user_id: str) -> List[Any]:
        rows = (
            self._client.table("project_chats")
            .select("*")
            .eq("owner_user_id", owner_user_id)
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        projects = (
            self._client.table("generated_projects")
            .select("project_id,chat_id,status")
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        canonical_projects = (
            self._client.table("projects")
            .select("project_id,chat_id,status")
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        by_chat: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for project in projects:
            if project.get("chat_id"):
                by_chat.setdefault(str(project["chat_id"]), {})[str(project["project_id"])] = project
        for project in canonical_projects:
            if project.get("chat_id"):
                by_chat.setdefault(str(project["chat_id"]), {})[str(project["project_id"])] = project
        visible = []
        for row in rows:
            linked = list(by_chat.get(str(row.get("chat_id") or ""), {}).values())
            if linked and not any(project.get("status") == "active" for project in linked):
                continue
            hidden_ids = {str(project["project_id"]) for project in linked if project.get("status") != "active"}
            if hidden_ids and isinstance(row.get("messages"), list):
                row["messages"] = [
                    message
                    for message in row["messages"]
                    if not isinstance(message, dict)
                    or str(message.get("projectId") or message.get("project_id") or "") not in hidden_ids
                ]
            visible.append(_record(row))
        return visible

    def get_project_chat(self, chat_id: str, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("project_chats")
            .select("*")
            .eq("chat_id", chat_id)
            .eq("owner_user_id", owner_user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return None
        projects = (
            self._client.table("generated_projects")
            .select("project_id,status")
            .eq("chat_id", chat_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        canonical_projects = (
            self._client.table("projects")
            .select("project_id,status")
            .eq("chat_id", chat_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
            .data
            or []
        )
        linked_by_id = {str(project["project_id"]): project for project in projects}
        linked_by_id.update({str(project["project_id"]): project for project in canonical_projects})
        projects = list(linked_by_id.values())
        if projects and not any(project.get("status") == "active" for project in projects):
            return None
        hidden_ids = {str(project["project_id"]) for project in projects if project.get("status") != "active"}
        if hidden_ids and isinstance(rows[0].get("messages"), list):
            rows[0]["messages"] = [
                message
                for message in rows[0]["messages"]
                if not isinstance(message, dict)
                or str(message.get("projectId") or message.get("project_id") or "") not in hidden_ids
            ]
        return _record(rows[0])

    def delete_project_chat(self, chat_id: str, owner_user_id: str) -> bool:
        response = (
            self._client.table("project_chats")
            .delete()
            .eq("chat_id", chat_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
        )
        if response.data:
            (
                self._client.table("generated_projects")
                .update({"chat_id": None})
                .eq("chat_id", chat_id)
                .eq("owner_user_id", owner_user_id)
                .execute()
            )
            (
                self._client.table("projects")
                .update({"chat_id": None})
                .eq("chat_id", chat_id)
                .eq("owner_user_id", owner_user_id)
                .execute()
            )
        return bool(response.data)

    def save_alpha_signup(self, record: Dict[str, Any]) -> Any:
        response = self._client.table("alpha_signups").insert(record).execute()
        rows = response.data or []
        return _record(rows[0]) if rows else _record(record)

    def insert_project_save(self, record: Dict[str, Any]) -> bool:
        existing = (
            self._client.table("project_saves")
            .select("id")
            .eq("project_id", record["project_id"])
            .eq("owner_user_id", record["owner_user_id"])
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            return False
        try:
            self._client.table("project_saves").insert(record).execute()
            return True
        except Exception as exc:
            message = str(exc).lower()
            if "23505" in message or "duplicate" in message or "unique" in message:
                return False
            raise

    def delete_project_save(self, project_id: str, owner_user_id: str) -> bool:
        response = (
            self._client.table("project_saves")
            .delete()
            .eq("project_id", project_id)
            .eq("owner_user_id", owner_user_id)
            .execute()
        )
        return bool(response.data)

    def count_project_saves(self, project_ids: List[str]) -> Dict[str, int]:
        if not project_ids:
            return {}
        rows = (
            self._client.table("project_saves")
            .select("project_id")
            .in_("project_id", project_ids)
            .execute()
            .data
            or []
        )
        return dict(Counter(str(row["project_id"]) for row in rows if row.get("project_id")))

    def list_saved_project_ids(self, owner_user_id: str, project_ids: List[str]) -> List[str]:
        if not project_ids:
            return []
        rows = (
            self._client.table("project_saves")
            .select("project_id")
            .eq("owner_user_id", owner_user_id)
            .in_("project_id", project_ids)
            .execute()
            .data
            or []
        )
        return [str(row["project_id"]) for row in rows if row.get("project_id")]

    def insert_project_remix(self, record: Dict[str, Any]) -> Any:
        rows = self._client.table("project_remixes").insert(record).execute().data or []
        return _record(rows[0]) if rows else _record(record)

    def count_project_remixes(self, project_ids: List[str]) -> Dict[str, int]:
        if not project_ids:
            return {}
        rows = (
            self._client.table("project_remixes")
            .select("source_project_id")
            .in_("source_project_id", project_ids)
            .execute()
            .data
            or []
        )
        return dict(Counter(str(row["source_project_id"]) for row in rows if row.get("source_project_id")))

    def get_user_settings(self, owner_user_id: str) -> Optional[Any]:
        rows = (
            self._client.table("user_settings")
            .select("*")
            .eq("owner_user_id", owner_user_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _record(rows[0]) if rows else None

    def upsert_user_settings(self, record: Dict[str, Any]) -> Any:
        response = (
            self._client.table("user_settings")
            .upsert(record, on_conflict="owner_user_id")
            .execute()
        )
        rows = response.data or []
        return _record(rows[0]) if rows else _record(record)

    def list_model_training_opt_out_user_ids(self) -> List[str]:
        rows = (
            self._client.table("user_settings")
            .select("owner_user_id")
            .eq("model_training_opt_out", True)
            .execute()
            .data
            or []
        )
        return [str(row["owner_user_id"]) for row in rows]
