from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from typing import Any, Dict, List, Optional, Protocol

from forma_core.jobs.source_usage import infer_source_usage
from forma_core.persistence.providers.sqlite import SQLiteProvider
from forma_core.agents.pipeline import PipelineCancelledError


TERMINAL_JOB_STATUSES = {"succeeded", "partial", "failed", "cancelled", "canceled"}


class JobCancelledError(PipelineCancelledError):
    """Raised inside a worker when its persisted job was cancelled."""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _json_loads(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _row_to_dict(row: Any) -> Dict[str, Any]:
    result = dict(row)
    result["server_owned"] = bool(result["server_owned"])
    payload = _json_loads(result.pop("payload_json", None)) or {}
    result_summary = _json_loads(result.pop("result_summary_json", None))
    source_usage = _json_loads(result.pop("source_usage_json", None)) or {}
    result["progress_events"] = _json_loads(result.pop("progress_events_json", None)) or []
    result["error_debug"] = _json_loads(result.pop("error_debug_json", None))
    result["payload"] = payload
    result["result_summary"] = result_summary
    result["source_usage"] = infer_source_usage(
        action=result.get("action"),
        payload=payload,
        result_summary=result_summary,
        current={"source_usage": source_usage},
    )
    return result


def _job_matches_project(job: Dict[str, Any], project_id: str) -> bool:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    summary = job.get("result_summary") if isinstance(job.get("result_summary"), dict) else {}
    return any(
        str(value or "") == project_id
        for value in (
            payload.get("project_id"),
            payload.get("source_project_id"),
            summary.get("project_id"),
            summary.get("source_project_id"),
        )
    )


class JobRepository(Protocol):
    def initialize(self) -> None: ...

    def create(self, record: Dict[str, Any], *, replace_existing: bool = True) -> None: ...

    def append_progress_event(self, job_id: str, event: Dict[str, Any], now: str) -> None: ...

    def mark_running(self, job_id: str, now: str) -> None: ...

    def complete(self, job_id: str, current_status: str, values: Dict[str, Any]) -> None: ...

    def cancel(self, job_id: str, now: str, reason: str) -> Optional[Dict[str, Any]]: ...

    def get(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    def list(self, *, sender: Optional[str], status: Optional[str], limit: int) -> List[Dict[str, Any]]: ...

    def list_metric_rows(self, *, created_since: str) -> List[Dict[str, Any]]: ...

    def list_for_project(self, project_id: str) -> List[Dict[str, Any]]: ...

    def delete_for_project(self, project_id: str) -> int: ...

    def update_status(self, job_id: str, status: str, now: str) -> None: ...


class SupabaseJobRepository:
    _projection = (
        "job_id,message_id,correlation_id,action,sender,recipient,status,server_owned,"
        "created_at,updated_at,started_at,completed_at,payload_json,result_summary_json,"
        "source_usage_json,progress_events_json,error,error_debug_json"
    )

    def __init__(self, client: Any) -> None:
        self._client = client

    def initialize(self) -> None:
        self._client.table("a2a_jobs").select(self._projection).limit(1).execute()

    def create(self, record: Dict[str, Any], *, replace_existing: bool = True) -> None:
        table = self._client.table("a2a_jobs")
        if replace_existing:
            table.upsert(record, on_conflict="job_id").execute()
        else:
            table.insert(record).execute()

    def append_progress_event(self, job_id: str, event: Dict[str, Any], now: str) -> None:
        current = self.get(job_id) or {}
        if str(current.get("status") or "").lower() in {"cancelled", "canceled"}:
            raise JobCancelledError(f"Job {job_id} was cancelled.")
        events = list(current.get("progress_events") or [])
        events.append(event)
        self._client.table("a2a_jobs").update(
            {"updated_at": now, "progress_events_json": events}
        ).eq("job_id", job_id).execute()

    def mark_running(self, job_id: str, now: str) -> None:
        current = self.get(job_id) or {}
        current_status = str(current.get("status") or "").lower()
        if current_status in TERMINAL_JOB_STATUSES:
            return
        (
            self._client.table("a2a_jobs")
            .update(
                {
                    "status": "running",
                    "started_at": current.get("started_at") or now,
                    "updated_at": now,
                }
            )
            .eq("job_id", job_id)
            .eq("status", current_status)
            .execute()
        )

    def complete(self, job_id: str, current_status: str, values: Dict[str, Any]) -> None:
        query = self._client.table("a2a_jobs").update(values).eq("job_id", job_id)
        if current_status:
            query = query.eq("status", current_status)
        query.execute()

    def cancel(self, job_id: str, now: str, reason: str) -> Optional[Dict[str, Any]]:
        current = self.get(job_id)
        current_status = str((current or {}).get("status") or "").lower()
        if not current or current_status in TERMINAL_JOB_STATUSES:
            return current
        (
            self._client.table("a2a_jobs")
            .update(
                {
                    "status": "cancelled",
                    "completed_at": now,
                    "updated_at": now,
                    "error": reason,
                }
            )
            .eq("job_id", job_id)
            .eq("status", current_status)
            .execute()
        )
        return self.get(job_id)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        rows = (
            self._client.table("a2a_jobs")
            .select("*")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return _row_to_dict(rows[0]) if rows else None

    def list(
        self,
        *,
        sender: Optional[str],
        status: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        query = self._client.table("a2a_jobs").select("*")
        if sender:
            query = query.eq("sender", sender)
        if status:
            query = query.eq("status", status)
        rows = query.order("created_at", desc=True).limit(limit).execute().data or []
        return [_row_to_dict(row) for row in rows]

    def list_metric_rows(self, *, created_since: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        offset = 0
        batch_size = 1000
        while True:
            rows = (
                self._client.table("a2a_jobs")
                .select("job_id,status,created_at")
                .gte("created_at", created_since)
                .order("created_at")
                .range(offset, offset + batch_size - 1)
                .execute()
                .data
                or []
            )
            records.extend(dict(row) for row in rows)
            if len(rows) < batch_size:
                break
            offset += batch_size
        return records

    def update_status(self, job_id: str, status: str, now: str) -> None:
        self._client.table("a2a_jobs").update(
            {"status": status, "updated_at": now}
        ).eq("job_id", job_id).execute()

    def list_for_project(self, project_id: str) -> List[Dict[str, Any]]:
        matches: List[Dict[str, Any]] = []
        offset = 0
        batch_size = 1000
        while True:
            rows = (
                self._client.table("a2a_jobs")
                .select("*")
                .range(offset, offset + batch_size - 1)
                .execute()
                .data
                or []
            )
            matches.extend(job for job in (_row_to_dict(row) for row in rows) if _job_matches_project(job, project_id))
            if len(rows) < batch_size:
                break
            offset += batch_size
        return matches

    def delete_for_project(self, project_id: str) -> int:
        jobs = self.list_for_project(project_id)
        for job in jobs:
            self._client.table("a2a_jobs").delete().eq("job_id", job["job_id"]).execute()
        return len(jobs)


class SQLiteJobRepository:
    def __init__(self, provider: SQLiteProvider) -> None:
        self._provider = provider
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._provider.initialize()

    def create(self, record: Dict[str, Any], *, replace_existing: bool = True) -> None:
        values = dict(record)
        values["server_owned"] = 1 if values["server_owned"] else 0
        for column in (
            "payload_json",
            "result_summary_json",
            "source_usage_json",
            "progress_events_json",
            "error_debug_json",
        ):
            if values.get(column) is not None:
                values[column] = _json_dumps(values[column])
        columns = list(values)
        quoted_columns = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        insert_mode = "INSERT OR REPLACE" if replace_existing else "INSERT"
        with self._locked_connection() as connection:
            connection.execute(
                f"{insert_mode} INTO a2a_jobs ({quoted_columns}) VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )

    def append_progress_event(self, job_id: str, event: Dict[str, Any], now: str) -> None:
        with self._locked_connection() as connection:
            row = connection.execute(
                "SELECT status, progress_events_json FROM a2a_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            if str(row["status"] or "").lower() in {"cancelled", "canceled"}:
                raise JobCancelledError(f"Job {job_id} was cancelled.")
            events = _json_loads(row["progress_events_json"]) or []
            if not isinstance(events, list):
                events = []
            events.append(event)
            connection.execute(
                "UPDATE a2a_jobs SET progress_events_json = ?, updated_at = ? WHERE job_id = ?",
                (_json_dumps(events), now, job_id),
            )

    def mark_running(self, job_id: str, now: str) -> None:
        with self._locked_connection() as connection:
            connection.execute(
                """
                UPDATE a2a_jobs
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE job_id = ? AND status NOT IN ('succeeded', 'partial', 'failed', 'cancelled', 'canceled')
                """,
                ("running", now, now, job_id),
            )

    def complete(self, job_id: str, current_status: str, values: Dict[str, Any]) -> None:
        del current_status
        serialized = dict(values)
        for column in ("result_summary_json", "source_usage_json", "error_debug_json"):
            if serialized.get(column) is not None:
                serialized[column] = _json_dumps(serialized[column])
        assignments = ", ".join(f"{column} = ?" for column in serialized)
        parameters = [serialized[column] for column in serialized]
        parameters.append(job_id)
        with self._locked_connection() as connection:
            connection.execute(
                f"""
                UPDATE a2a_jobs SET {assignments}
                WHERE job_id = ? AND status NOT IN ('cancelled', 'canceled')
                """,
                parameters,
            )

    def cancel(self, job_id: str, now: str, reason: str) -> Optional[Dict[str, Any]]:
        with self._locked_connection() as connection:
            connection.execute(
                """
                UPDATE a2a_jobs
                SET status = ?, completed_at = ?, updated_at = ?, error = ?
                WHERE job_id = ? AND status NOT IN ('succeeded', 'partial', 'failed', 'cancelled', 'canceled')
                """,
                ("cancelled", now, now, reason, job_id),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._provider.connect_dbapi()) as connection:
            row = connection.execute(
                "SELECT * FROM a2a_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list(
        self,
        *,
        sender: Optional[str],
        status: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        clauses = []
        parameters: List[Any] = []
        if sender:
            clauses.append("sender = ?")
            parameters.append(sender)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with closing(self._provider.connect_dbapi()) as connection:
            rows = connection.execute(
                f"SELECT * FROM a2a_jobs {where} ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_metric_rows(self, *, created_since: str) -> List[Dict[str, Any]]:
        with closing(self._provider.connect_dbapi()) as connection:
            rows = connection.execute(
                "SELECT job_id, status, created_at FROM a2a_jobs WHERE created_at >= ? ORDER BY created_at",
                (created_since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_status(self, job_id: str, status: str, now: str) -> None:
        with self._locked_connection() as connection:
            connection.execute(
                "UPDATE a2a_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status, now, job_id),
            )

    def list_for_project(self, project_id: str) -> List[Dict[str, Any]]:
        with closing(self._provider.connect_dbapi()) as connection:
            rows = connection.execute("SELECT * FROM a2a_jobs").fetchall()
        return [job for job in (_row_to_dict(row) for row in rows) if _job_matches_project(job, project_id)]

    def delete_for_project(self, project_id: str) -> int:
        jobs = self.list_for_project(project_id)
        if not jobs:
            return 0
        with self._locked_connection() as connection:
            connection.executemany(
                "DELETE FROM a2a_jobs WHERE job_id = ?",
                [(job["job_id"],) for job in jobs],
            )
        return len(jobs)

    def _locked_connection(self) -> "_LockedConnection":
        return _LockedConnection(self._lock, self._provider.connect_dbapi())


class _LockedConnection:
    def __init__(self, lock: threading.Lock, connection: sqlite3.Connection) -> None:
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
            self._connection.close()
        finally:
            self._lock.release()
