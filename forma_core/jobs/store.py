import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from forma_core.jobs.metrics import summarize_job_metrics
from forma_core.jobs.repositories import (
    JobCancelledError,
    JobRepository,
    SQLiteJobRepository,
    SupabaseJobRepository,
)
from forma_core.jobs.source_usage import infer_source_usage
from forma_core.persistence.providers import SQLiteProvider, SupabaseProvider, create_sqlite_provider
from forma_core.debug import (
    new_error_correlation_id,
    public_error_message,
    redact_debug_text,
    redact_debug_value,
    redact_error_value,
)

load_dotenv()

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _redact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    redacted = redact_debug_value(dict(payload or {}))
    if redacted.get("image_data"):
        redacted["image_data"] = "<redacted>"
        redacted["image_data_present"] = True
    return redacted


def _persisted_error_debug(
    error_debug: Optional[Dict[str, Any]],
    correlation_id: str,
    error_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not error_debug and not error_code:
        return None
    payload: Dict[str, Any] = {
        "correlation_id": correlation_id,
    }
    if error_code:
        payload["code"] = str(error_code)[:80]
    if error_debug:
        payload["error_type"] = str(error_debug.get("error_type") or "Error")
    return payload


def _operation_summary(operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    failed = sum(1 for operation in operations if operation.get("status") == "failed")
    partial = sum(1 for operation in operations if operation.get("status") == "partial")
    succeeded = sum(1 for operation in operations if operation.get("status") == "succeeded")
    pending = sum(1 for operation in operations if operation.get("status") == "pending")
    not_requested = sum(1 for operation in operations if operation.get("status") == "not_requested")
    return {
        "total": len(operations),
        "failed": failed,
        "partial": partial,
        "succeeded": succeeded,
        "pending": pending,
        "not_requested": not_requested,
        "ok": failed == 0 and partial == 0,
    }


def _result_operation_statuses(project_ir: Dict[str, Any], metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_operations = metadata.get("operation_statuses")
    operations = [item for item in raw_operations if isinstance(item, dict)] if isinstance(raw_operations, list) else []
    operation_ids = {str(item.get("id") or "") for item in operations}
    if "hardware_generation" not in operation_ids:
        validation = project_ir.get("validation") or {}
        generation_status = str(metadata.get("generation_status") or "succeeded").lower()
        operations.insert(
            0,
            {
                "id": "hardware_generation",
                "label": "Hardware generation",
                "status": generation_status,
                "provider": metadata.get("runtime_provider") or metadata.get("llm_provider"),
                "model": metadata.get("runtime_model") or metadata.get("model_name"),
                "details": {
                    "is_valid": project_ir.get("is_valid"),
                    "component_count": len(project_ir.get("components") or []),
                    "net_count": len(project_ir.get("nets") or []),
                    "critical_issue_count": len(validation.get("critical") or []),
                    "warning_issue_count": len(validation.get("warning") or []),
                },
            },
        )
    return operations


def summarize_result(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not result:
        return None

    project_ir = result.get("project_ir") if isinstance(result, dict) else None
    if not isinstance(project_ir, dict):
        return {"result_keys": sorted(result.keys()) if isinstance(result, dict) else []}

    overview = project_ir.get("overview") or {}
    metadata = project_ir.get("assembly_metadata") or {}
    validation = project_ir.get("validation") or {}
    source_usage = infer_source_usage(result=result)
    operation_statuses = _result_operation_statuses(project_ir, metadata)

    return {
        "project_id": metadata.get("project_id"),
        "chat_id": metadata.get("chat_id"),
        "source_project_id": metadata.get("source_project_id"),
        "title": overview.get("title"),
        "category": overview.get("category"),
        "estimated_cost": overview.get("estimated_cost"),
        "is_valid": project_ir.get("is_valid"),
        "component_count": len(project_ir.get("components") or []),
        "net_count": len(project_ir.get("nets") or []),
        "critical_issue_count": len(validation.get("critical") or []),
        "warning_issue_count": len(validation.get("warning") or []),
        "llm_provider": metadata.get("llm_provider"),
        "model_name": metadata.get("model_name"),
        "has_product_image": bool(metadata.get("product_image_data") or metadata.get("product_image_url")),
        "image_output_requested": metadata.get("image_output_requested"),
        "image_output_enabled": metadata.get("image_output_enabled"),
        "image_output_configured": metadata.get("image_output_configured"),
        "image_output_status": metadata.get("image_output_status"),
        "image_output_failed": metadata.get("image_output_failed"),
        "image_output_error": metadata.get("image_output_error") or metadata.get("product_image_error"),
        "image_output_error_type": metadata.get("image_output_error_type"),
        "image_output_reason": metadata.get("image_output_reason"),
        "image_output_debug": metadata.get("image_output_debug"),
        "image_output_generated_count": metadata.get("image_output_generated_count"),
        "product_image_provider": metadata.get("product_image_provider") or metadata.get("image_output_provider"),
        "product_image_model": metadata.get("product_image_model") or metadata.get("image_output_model"),
        "product_image_error": metadata.get("product_image_error"),
        "product_image_storage_error": metadata.get("product_image_storage_error") or metadata.get("product_case_image_storage_error"),
        "operation_statuses": operation_statuses,
        "operation_summary": _operation_summary(operation_statuses),
        "workflow": metadata.get("workflow"),
        "source_usage": source_usage,
        "pipeline": metadata.get("pipeline"),
        "generation_status": metadata.get("generation_status", "succeeded"),
        "project_readiness": metadata.get("project_readiness", "complete"),
        "generation_stages": ((metadata.get("generation_run") or {}).get("records") or {}),
        "generation_stage_failures": metadata.get("generation_stage_failures") or [],
    }


class JobMetadataStore:
    """Durable A2A jobs stored through the application's primary database."""

    def __init__(self, db_path: Optional[str] = None, backend: Optional[str] = None) -> None:
        self.db_path = db_path
        requested_backend = (backend or "auto").strip().lower()
        if db_path is not None:
            requested_backend = "sqlite"
        self.requested_backend = requested_backend if requested_backend in {"auto", "supabase", "sqlite"} else "auto"
        self.backend = "unconfigured"
        self._client = None
        self._provider = None
        self._repository: Optional[JobRepository] = None
        self._standalone = db_path is not None
        self._initialized = False

    def _ensure_backend_configured(self) -> None:
        if self._repository is not None:
            return

        if self.backend != "unconfigured":
            if self.backend == "supabase" and self._client is not None:
                self._repository = SupabaseJobRepository(self._client)
                return
            if self.backend == "sqlite" and self._provider is not None:
                if not isinstance(self._provider, SQLiteProvider):
                    raise TypeError("SQLite job repository requires SQLiteProvider.")
                self._repository = SQLiteJobRepository(self._provider)
                return

        if self._standalone:
            assert self.db_path is not None
            database_url = (
                "sqlite:///:memory:"
                if self.db_path == ":memory:"
                else f"sqlite:///{Path(self.db_path).expanduser().resolve()}"
            )
            self._provider = create_sqlite_provider(
                source="explicit job-store test/CLI path",
                url=database_url,
                import_legacy_jobs=False,
            )
            self.backend = "sqlite"
        else:
            from forma_core.database import get_database_provider

            self._provider = get_database_provider()
            self.backend = self._provider.backend
            if isinstance(self._provider, SupabaseProvider):
                self._client = self._provider.client

        if self.requested_backend != "auto" and self.requested_backend != self.backend:
            raise RuntimeError(
                f"Job metadata uses the primary {self.backend} database; "
                f"a separate {self.requested_backend} job backend is not supported."
            )
        if self.backend == "supabase":
            self._repository = SupabaseJobRepository(self._client)
        else:
            if not isinstance(self._provider, SQLiteProvider):
                raise TypeError("SQLite job repository requires SQLiteProvider.")
            self._repository = SQLiteJobRepository(self._provider)

    def get_config(self) -> Dict[str, Any]:
        self._ensure_backend_configured()
        if self._provider is None:
            return {"backend": self.backend, "client": "supabase-py", "table": "a2a_jobs", "scope": "primary"}
        config = self._provider.describe()
        config.update({"table": "a2a_jobs", "scope": "standalone" if self._standalone else "primary"})
        return config

    def close(self) -> None:
        """Release a standalone SQLite provider owned by this store."""

        if self._standalone and isinstance(self._provider, SQLiteProvider):
            self._provider.dispose()

    def init_db(self) -> None:
        if self._initialized:
            return
        self._ensure_backend_configured()
        assert self._repository is not None
        self._repository.initialize()
        self._initialized = True

    def create_job(
        self,
        *,
        job_id: str,
        message_id: str,
        correlation_id: Optional[str],
        action: str,
        sender: str,
        recipient: str,
        payload: Dict[str, Any],
        server_owned: bool,
        status: str = "queued",
        replace_existing: bool = True,
    ) -> Dict[str, Any]:
        self.init_db()
        now = _utc_now()
        correlation_id = correlation_id or new_error_correlation_id()
        source_usage = infer_source_usage(action=action, payload=payload)
        assert self._repository is not None
        self._repository.create(
            {
                "job_id": job_id,
                "message_id": message_id,
                "correlation_id": correlation_id,
                "action": action,
                "sender": sender,
                "recipient": recipient,
                "status": status,
                "server_owned": server_owned,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "payload_json": _redact_payload(payload),
                "result_summary_json": None,
                "source_usage_json": source_usage,
                "progress_events_json": [],
                "error_debug_json": None,
                "error": None,
            },
            replace_existing=replace_existing,
        )
        return self.get_job(job_id) or {}

    def append_progress_event(self, job_id: str, event: Dict[str, Any]) -> None:
        self.init_db()
        now = _utc_now()
        event_payload = dict(event or {})
        event_payload.setdefault("observed_at", now)
        event_payload = redact_error_value(event_payload)
        assert self._repository is not None
        self._repository.append_progress_event(job_id, event_payload, now)

    def mark_running(self, job_id: str) -> None:
        self.init_db()
        now = _utc_now()
        assert self._repository is not None
        self._repository.mark_running(job_id, now)

    def mark_routed(self, job_id: str) -> None:
        self._update_status(job_id, "routed")

    def mark_succeeded(self, job_id: str, result: Optional[Dict[str, Any]]) -> None:
        self._mark_completed_result(job_id, result, status="succeeded")

    def mark_partial(self, job_id: str, result: Optional[Dict[str, Any]]) -> None:
        self._mark_completed_result(job_id, result, status="partial")

    def _mark_completed_result(
        self,
        job_id: str,
        result: Optional[Dict[str, Any]],
        *,
        status: str,
    ) -> None:
        self.init_db()
        now = _utc_now()
        result_summary = redact_error_value(summarize_result(result))
        current = self.get_job(job_id) or {}
        if str(current.get("status") or "").lower() in {"cancelled", "canceled"}:
            return
        source_usage = infer_source_usage(
            action=current.get("action"),
            payload=current.get("payload"),
            result=result,
            result_summary=result_summary,
            current=current,
        )
        assert self._repository is not None
        self._repository.complete(
            job_id,
            str(current.get("status") or ""),
            {
                "status": status,
                "completed_at": now,
                "updated_at": now,
                "result_summary_json": result_summary,
                "source_usage_json": source_usage,
                "error_debug_json": None,
                "error": None,
            },
        )

    def mark_failed(
        self,
        job_id: str,
        error: str,
        error_debug: Optional[Dict[str, Any]] = None,
        *,
        error_code: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        self.init_db()
        now = _utc_now()
        current = self.get_job(job_id) or {}
        if str(current.get("status") or "").lower() in {"cancelled", "canceled"}:
            return
        resolved_correlation_id = str(
            correlation_id or current.get("correlation_id") or new_error_correlation_id()
        )
        safe_error = public_error_message(error_code or "internal_error", error)
        assert self._repository is not None
        self._repository.complete(
            job_id,
            str(current.get("status") or ""),
            {
                "status": "failed",
                "completed_at": now,
                "updated_at": now,
                "correlation_id": resolved_correlation_id,
                "error_debug_json": _persisted_error_debug(error_debug, resolved_correlation_id, error_code),
                "error": safe_error,
            },
        )

    def mark_cancelled(self, job_id: str, reason: str = "Cancelled by user.") -> Optional[Dict[str, Any]]:
        """Cancel a queued or running job without overwriting completed jobs."""
        self.init_db()
        now = _utc_now()
        assert self._repository is not None
        return self._repository.cancel(job_id, now, redact_debug_text(reason))

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return str((job or {}).get("status") or "").lower() in {"cancelled", "canceled"}

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        self.init_db()
        assert self._repository is not None
        return self._repository.get(job_id)

    def list_jobs(
        self,
        *,
        sender: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        self.init_db()
        limit = max(1, min(limit, 200))
        assert self._repository is not None
        return self._repository.list(sender=sender, status=status, limit=limit)

    def get_metrics(
        self,
        *,
        days: int = 7,
        hours: int = 24,
        interval_hours: Optional[int] = None,
        additional_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Return UTC job-volume and failure metrics for the admin dashboard."""
        self.init_db()
        now = datetime.now(timezone.utc)
        lookback_days = max(
            max(1, min(days, 31)),
            (max(1, min(hours, 168)) + 23) // 24,
            (max(1, min(interval_hours or 1, 31 * 24)) + 23) // 24,
        )
        created_since = (now - timedelta(days=lookback_days + 1)).isoformat().replace("+00:00", "Z")
        assert self._repository is not None
        rows = self._repository.list_metric_rows(created_since=created_since)
        if additional_rows:
            existing_job_ids = {str(row.get("job_id") or "") for row in rows}
            rows.extend(
                row
                for row in additional_rows
                if str(row.get("job_id") or "") not in existing_job_ids
            )
        return summarize_job_metrics(
            rows,
            now=now,
            days=days,
            hours=hours,
            interval_hours=interval_hours,
        )

    def list_project_jobs(self, project_id: str) -> List[Dict[str, Any]]:
        """Return every persisted job whose payload or result references a project."""
        self.init_db()
        assert self._repository is not None
        return self._repository.list_for_project(project_id)

    def cancel_project_jobs(
        self,
        project_id: str,
        reason: str = "Cancelled because the project was scheduled for deletion.",
    ) -> int:
        """Cancel all non-terminal work for a project and return the match count."""
        jobs = self.list_project_jobs(project_id)
        for job in jobs:
            if str(job.get("status") or "").lower() not in {
                "succeeded",
                "failed",
                "cancelled",
                "canceled",
            }:
                self.mark_cancelled(str(job["job_id"]), reason)
        return len(jobs)

    def delete_project_jobs(self, project_id: str) -> int:
        """Permanently remove durable job metadata associated with a project."""
        self.init_db()
        assert self._repository is not None
        return self._repository.delete_for_project(project_id)

    def _update_status(self, job_id: str, status: str) -> None:
        self.init_db()
        now = _utc_now()
        assert self._repository is not None
        self._repository.update_status(job_id, status, now)


JOB_STORE = JobMetadataStore()
