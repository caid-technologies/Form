"""Durable storage for OpenCode sessions, leased commands, and public events."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from forma_core.database import get_database_provider
from forma_core.opencode.models import (
    CommandContext,
    ConnectorEventInput,
    ConnectorCommand,
    OpenCodeCommandStatus,
    OpenCodeOperation,
    OpenCodeSessionStatus,
    OpenCodeEventKind,
    ProjectHistoryMessage,
    PublicEvent,
)
from forma_core.persistence.providers import SQLiteProvider, SupabaseProvider, create_sqlite_provider
from forma_core.opencode.public_events import project_public_event

from forma_core.user_integrations import decrypt_user_secret_text, encrypt_user_secret_text


class LeaseError(PermissionError):
    """A lease rejection safe to classify without exposing its token."""

    def __init__(self, code: str):
        super().__init__("The command lease is no longer valid.")
        self.code = code


class SessionClosedError(ValueError):
    """The session cannot accept more authoring work."""


class CommandConflictError(ValueError):
    """A retry key was reused with a different command payload."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class StoredSession:
    session_id: str
    connector_id: str
    owner_user_id: str
    project_id: str
    status: OpenCodeSessionStatus
    capability_nonce: str
    created_at: str
    updated_at: str
    last_heartbeat_at: str | None
    next_event_sequence: int

    def as_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "connector_id": self.connector_id,
            "owner_user_id": self.owner_user_id,
            "project_id": self.project_id,
            "status": self.status.value,
            "capability_nonce": self.capability_nonce,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "next_event_sequence": self.next_event_sequence,
        }


@dataclass(frozen=True, slots=True)
class StoredCommand:
    command_id: str
    session_id: str
    connector_id: str
    owner_user_id: str
    project_id: str
    operation: OpenCodeOperation
    idempotency_key: str
    status: OpenCodeCommandStatus
    message_digest: str
    message_ciphertext: str | None
    message_key_id: str | None
    attempt_count: int
    lease_expires_at: str | None
    lease_token_hash: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    model: str | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "connector_id": self.connector_id,
            "owner_user_id": self.owner_user_id,
            "project_id": self.project_id,
            "operation": self.operation.value,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "message_digest": self.message_digest,
            "message_ciphertext": self.message_ciphertext,
            "message_key_id": self.message_key_id,
            "attempt_count": self.attempt_count,
            "lease_expires_at": self.lease_expires_at,
            "lease_token_hash": self.lease_token_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "model": self.model,
        }


class OpenCodeStore:
    """Use the configured primary provider; standalone SQLite is test-friendly."""

    def __init__(self, db_path: str | None = None, provider: Any = None) -> None:
        self._provider = provider
        self._standalone = db_path is not None
        self._db_path = db_path
        self._lock = threading.RLock()
        self._initialized = False

    def _ensure_provider(self) -> Any:
        if self._provider is None:
            if self._standalone:
                database_url = "sqlite:///:memory:" if self._db_path == ":memory:" else f"sqlite:///{self._db_path}"
                self._provider = create_sqlite_provider(
                    source="OpenCode test/store path",
                    url=database_url,
                    import_legacy_jobs=False,
                )
            else:
                self._provider = get_database_provider()
        if not self._initialized:
            self._provider.initialize()
            self._initialized = True
        return self._provider

    @property
    def provider(self) -> Any:
        return self._ensure_provider()

    def close(self) -> None:
        if self._standalone and isinstance(self._provider, SQLiteProvider):
            self._provider.dispose()

    def create_session(self, *, session_id: str, connector_id: str, owner_user_id: str, project_id: str) -> StoredSession:
        now = _timestamp()
        session = StoredSession(
            session_id=session_id,
            connector_id=connector_id,
            owner_user_id=owner_user_id,
            project_id=project_id,
            status=OpenCodeSessionStatus.ACTIVE,
            capability_nonce=secrets.token_urlsafe(18),
            created_at=now,
            updated_at=now,
            last_heartbeat_at=None,
            next_event_sequence=1,
        )
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_sessions").insert(session.as_record()).execute()
            return session
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO opencode_sessions "
                "(session_id, connector_id, owner_user_id, project_id, status, capability_nonce, created_at, "
                "updated_at, last_heartbeat_at, next_event_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(session.as_record().values()),
            )
        return session

    def get_session(self, session_id: str) -> StoredSession | None:
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = provider.client.table("opencode_sessions").select("*").eq("session_id", session_id).limit(1).execute().data or []
            return _session_from_record(rows[0]) if rows else None
        with closing(provider.connect_dbapi()) as connection:
            row = connection.execute("SELECT * FROM opencode_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return _session_from_record(dict(row)) if row else None

    def reconcile_session(self, session_id: str, *, cancel: bool = False) -> StoredSession | None:
        """Atomically settle an outage (120s warning, 300s cutoff) or cancellation.

        Only pending work starts the outage clock. A newly submitted command on
        an idle session gets a full grace period. Connector requests reconcile
        before touching the heartbeat, so late reconnects cannot revive work.
        """
        provider = self._ensure_provider()
        now = _now()
        if isinstance(provider, SupabaseProvider):
            provider.client.rpc("reconcile_opencode_session", {
                "p_session_id": session_id, "p_now": _timestamp(now), "p_cancel": cancel,
            }).execute()
        else:
            with self._connection(begin_immediate=True) as connection:
                row = connection.execute("SELECT * FROM opencode_sessions WHERE session_id = ?", (session_id,)).fetchone()
                if row is None or row["status"] != OpenCodeSessionStatus.ACTIVE.value:
                    return _session_from_record(dict(row)) if row else None
                session = _session_from_record(dict(row))
                commands = connection.execute(
                    "SELECT * FROM opencode_commands WHERE session_id = ? AND status IN ('queued', 'leased', 'running') ORDER BY created_at",
                    (session_id,),
                ).fetchall()
                if not commands and not cancel:
                    return session
                anchor = max(
                    _parse_timestamp(session.last_heartbeat_at) or _parse_timestamp(session.created_at) or now,
                    (_parse_timestamp(commands[0]["created_at"]) or now) if commands else now,
                )
                age = (now - anchor).total_seconds()
                if not cancel and age < 120:
                    return session
                terminal = cancel or age >= 300
                sequence = session.next_event_sequence
                events = []
                if terminal:
                    command_status = "cancelled" if cancel else "failed"
                    connection.execute(
                        "UPDATE opencode_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                        (command_status, _timestamp(now), session_id),
                    )
                    connection.execute(
                        "UPDATE opencode_commands SET status = ?, completed_at = ?, updated_at = ?, lease_token_hash = NULL, lease_expires_at = NULL "
                        "WHERE session_id = ? AND status IN ('queued', 'leased', 'running')",
                        (command_status, _timestamp(now), _timestamp(now), session_id),
                    )
                    events = [ConnectorEventInput(
                        event_id=f"{command['command_id']}:terminal", kind=command_status, status=command_status,
                        error_code=None if cancel else "connector_timeout",
                    ) for command in commands]
                    if cancel:
                        events.append(ConnectorEventInput(event_id=f"cancelled_{session_id}", kind="cancelled", status="cancelled"))
                else:
                    events = [ConnectorEventInput(event_id=f"{commands[0]['command_id']}:unavailable", kind="connector_unavailable")]
                for event in events:
                    if connection.execute("SELECT 1 FROM opencode_events WHERE event_id = ?", (event.event_id,)).fetchone():
                        continue
                    public = project_public_event(event, sequence=sequence, session_id=session_id,
                                                  project_id=UUID(session.project_id), created_at=now)
                    connection.execute(
                        "INSERT INTO opencode_events (event_id, session_id, owner_user_id, project_id, sequence, event_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (public.event_id, session_id, session.owner_user_id, session.project_id, sequence,
                         public.model_dump_json(), _timestamp(now)),
                    )
                    sequence += 1
                connection.execute("UPDATE opencode_sessions SET next_event_sequence = ? WHERE session_id = ?", (sequence, session_id))
        return self.get_session(session_id)

    def touch_session(self, session_id: str) -> None:
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_sessions").update({"last_heartbeat_at": _timestamp()}).eq("session_id", session_id).execute()
            return
        with self._connection() as connection:
            connection.execute("UPDATE opencode_sessions SET last_heartbeat_at = ? WHERE session_id = ?", (_timestamp(), session_id))

    def touch_session_activity(self, session_id: str) -> None:
        now = _timestamp()
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_sessions").update({"updated_at": now}).eq("session_id", session_id).execute()
            return
        with self._connection() as connection:
            connection.execute("UPDATE opencode_sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))

    def create_command(
        self,
        *,
        command_id: str,
        session: StoredSession,
        operation: OpenCodeOperation,
        idempotency_key: str,
        message: str,
        model: str | None = None,
    ) -> StoredCommand:
        existing = self.find_command(session.session_id, idempotency_key)
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if existing:
            if existing.message_digest != digest or existing.model != model:
                raise CommandConflictError("The command idempotency key was already used with another message or model.")
            if existing.message_ciphertext is None or existing.message_key_id is None:
                ciphertext, key_id = encrypt_user_secret_text(message)
                self._update_command_message(existing.command_id, ciphertext, key_id)
            self.touch_session_activity(session.session_id)
            return existing
        now = _timestamp()
        ciphertext, key_id = encrypt_user_secret_text(message)
        command = StoredCommand(
            command_id=command_id,
            session_id=session.session_id,
            connector_id=session.connector_id,
            owner_user_id=session.owner_user_id,
            project_id=session.project_id,
            operation=operation,
            idempotency_key=idempotency_key,
            status=OpenCodeCommandStatus.QUEUED,
            message_digest=digest,
            message_ciphertext=ciphertext,
            message_key_id=key_id,
            attempt_count=0,
            lease_expires_at=None,
            lease_token_hash=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            model=model,
        )
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            try:
                provider.client.table("opencode_commands").insert(command.as_record()).execute()
            except Exception as exc:
                if getattr(exc, "code", None) == "23514":
                    raise SessionClosedError("The OpenCode session is no longer active.") from exc
                raise
        else:
            with self._connection(begin_immediate=True) as connection:
                parent = connection.execute("SELECT status FROM opencode_sessions WHERE session_id = ?", (session.session_id,)).fetchone()
                if parent is None or parent["status"] != "active":
                    raise SessionClosedError("The OpenCode session is no longer active.")
                connection.execute(
                    "INSERT INTO opencode_commands "
                    "(command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, "
                     "status, message_digest, message_ciphertext, message_key_id, attempt_count, lease_expires_at, lease_token_hash, created_at, updated_at, completed_at, model) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(command.as_record().values()),
                )
        self.touch_session_activity(session.session_id)
        return command

    def find_command(self, session_id: str, idempotency_key: str) -> StoredCommand | None:
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = (
                provider.client.table("opencode_commands")
                .select("*")
                .eq("session_id", session_id)
                .eq("idempotency_key", idempotency_key)
                .limit(1)
                .execute()
                .data
                or []
            )
            return _command_from_record(rows[0]) if rows else None
        with closing(provider.connect_dbapi()) as connection:
            row = connection.execute(
                "SELECT * FROM opencode_commands WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
        return _command_from_record(dict(row)) if row else None

    def get_command(self, command_id: str) -> StoredCommand | None:
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = provider.client.table("opencode_commands").select("*").eq("command_id", command_id).limit(1).execute().data or []
            return _command_from_record(rows[0]) if rows else None
        with closing(provider.connect_dbapi()) as connection:
            row = connection.execute("SELECT * FROM opencode_commands WHERE command_id = ?", (command_id,)).fetchone()
        return _command_from_record(dict(row)) if row else None

    def list_connector_sessions(self, connector_id: str, *, idle_after_seconds: int = 900) -> list[StoredSession]:
        cutoff = _now() - timedelta(seconds=max(1, idle_after_seconds))
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = (
                provider.client.table("opencode_sessions")
                .select("*")
                .eq("connector_id", connector_id)
                .eq("status", OpenCodeSessionStatus.ACTIVE.value)
                .order("created_at")
                .execute()
                .data
                or []
            )
            sessions = [_session_from_record(row) for row in rows]
            return [session for session in sessions if (_parse_timestamp(session.updated_at) or cutoff) >= cutoff]
        with closing(provider.connect_dbapi()) as connection:
            rows = connection.execute(
                "SELECT * FROM opencode_sessions WHERE connector_id = ? AND status = ? ORDER BY created_at",
                (connector_id, OpenCodeSessionStatus.ACTIVE.value),
            ).fetchall()
        sessions = [_session_from_record(dict(row)) for row in rows]
        return [session for session in sessions if (_parse_timestamp(session.updated_at) or cutoff) >= cutoff]

    def claim_next(self, *, connector_id: str, session_id: str, lease_seconds: int = 60) -> ConnectorCommand | None:
        """Atomically claim FIFO work only when this session has no live lease."""
        now = _now()
        now_text = _timestamp(now)
        expiry = _timestamp(now + timedelta(seconds=max(15, min(lease_seconds, 300))))
        lease_token = secrets.token_urlsafe(24)
        lease_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = provider.client.rpc("claim_opencode_command", {
                "p_connector_id": connector_id, "p_session_id": session_id,
                "p_lease_hash": lease_hash, "p_lease_seconds": lease_seconds, "p_now": now_text,
            }).execute().data or []
            if not rows:
                return None
            record = rows[0]
        else:
            with self._connection(begin_immediate=True) as connection:
                session = connection.execute(
                    "SELECT 1 FROM opencode_sessions WHERE session_id = ? AND connector_id = ? AND status = 'active'",
                    (session_id, connector_id),
                ).fetchone()
                if session is None:
                    return None
                active = connection.execute(
                    "SELECT 1 FROM opencode_commands WHERE connector_id = ? AND session_id = ? "
                    "AND status IN ('leased', 'running') AND lease_expires_at > ? LIMIT 1",
                    (connector_id, session_id, now_text),
                ).fetchone()
                if active is not None:
                    return None
                row = connection.execute(
                    "SELECT * FROM opencode_commands WHERE connector_id = ? AND session_id = ? AND "
                    "(status = 'queued' OR (status IN ('leased', 'running') AND lease_expires_at <= ?)) "
                    "ORDER BY created_at, command_id LIMIT 1",
                    (connector_id, session_id, now_text),
                ).fetchone()
                if row is None:
                    return None
                record = dict(row)
                attempt_count = int(record["attempt_count"] or 0) + 1
                connection.execute(
                    "UPDATE opencode_commands SET status = ?, attempt_count = ?, lease_expires_at = ?, "
                    "lease_token_hash = ?, updated_at = ? WHERE command_id = ?",
                    (OpenCodeCommandStatus.LEASED.value, attempt_count, expiry, lease_hash, now_text, record["command_id"]),
                )
                record.update(status=OpenCodeCommandStatus.LEASED.value, attempt_count=attempt_count,
                              lease_expires_at=expiry, lease_token_hash=lease_hash, updated_at=now_text)
        command = _command_from_record(record)
        message = self._command_message(command)
        if message is None:
            self.cancel_command(command)
            return self.claim_next(connector_id=connector_id, session_id=session_id, lease_seconds=lease_seconds)
        return self._with_context(command, lease_token, message)

    def _with_context(self, command: StoredCommand, lease_token: str, message: str) -> ConnectorCommand:
        """Rehydrate earlier user requests for the same owner, project and runtime.

        Include the original brief plus the 15 most recent requests. Cancelled attempts
        still contain requirements; queued future commands must never enter this prompt.
        Browser reloads create new sessions, not new project requirements.
        Nothing is written back as plaintext or added to public events.
        """
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            def query():
                return (provider.client.table("opencode_commands").select("*")
                        .eq("project_id", command.project_id)
                        .eq("owner_user_id", command.owner_user_id).eq("connector_id", command.connector_id)
                        .eq("operation", OpenCodeOperation.PROJECT_MESSAGE.value)
                        .lt("created_at", command.created_at))
            first = query().order("created_at").limit(1).execute().data or []
            recent = query().order("created_at", desc=True).limit(15).execute().data or []
        else:
            sql = ("SELECT * FROM opencode_commands WHERE project_id = ? "
                   "AND owner_user_id = ? AND connector_id = ? AND operation = ? AND created_at < ? ")
            params = (command.project_id, command.owner_user_id, command.connector_id,
                      OpenCodeOperation.PROJECT_MESSAGE.value, command.created_at)
            with closing(provider.connect_dbapi()) as connection:
                first = [dict(row) for row in connection.execute(sql + "ORDER BY created_at LIMIT 1", params).fetchall()]
                recent = [dict(row) for row in connection.execute(sql + "ORDER BY created_at DESC LIMIT 15", params).fetchall()]
        rows = {row["command_id"]: row for row in [*first, *recent]}
        context = []
        for row in sorted(rows.values(), key=lambda item: item["created_at"]):
            previous = _command_from_record(row)
            text = self._command_message(previous)
            if text:
                context.append(CommandContext(message=text, status=previous.status))
        result = _connector_command(command, lease_token, message)
        result.conversation_context = tuple(context)
        return result

    def project_history(self, project_id: str, owner_user_id: str) -> tuple[ProjectHistoryMessage, ...]:
        """Recover the last 40 turns without publishing ciphertext or internal events.

        Callers must authorize project access first. Both queries also enforce
        owner/project scope; recovery never writes a replacement chat record.
        """
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = (provider.client.table("opencode_commands").select("*")
                    .eq("project_id", project_id).eq("owner_user_id", owner_user_id)
                    .eq("operation", OpenCodeOperation.PROJECT_MESSAGE.value)
                    .order("created_at", desc=True).limit(40).execute().data or [])
        else:
            with closing(provider.connect_dbapi()) as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT * FROM opencode_commands WHERE project_id = ? AND owner_user_id = ? "
                    "AND operation = ? ORDER BY created_at DESC LIMIT 40",
                    (project_id, owner_user_id, OpenCodeOperation.PROJECT_MESSAGE.value),
                ).fetchall()]
        if not rows:
            return ()
        commands = sorted((_command_from_record(row) for row in rows), key=lambda command: command.created_at)
        if isinstance(provider, SupabaseProvider):
            events = (provider.client.table("opencode_events").select("event_json")
                      .eq("project_id", project_id).eq("owner_user_id", owner_user_id)
                      .gte("created_at", commands[0].created_at)
                      .in_("event_json->>kind", ["assistant_message", "completed", "failed", "cancelled"])
                      .order("created_at", desc=True).limit(2000).execute().data or [])
        else:
            with closing(provider.connect_dbapi()) as connection:
                events = [dict(row) for row in connection.execute(
                    "SELECT event_json FROM opencode_events WHERE project_id = ? AND owner_user_id = ? "
                    "AND created_at >= ? AND json_extract(event_json, '$.kind') IN ('assistant_message','completed','failed','cancelled') "
                    "ORDER BY created_at DESC LIMIT 2000", (project_id, owner_user_id, commands[0].created_at),
                ).fetchall()]
        answers: dict[str, PublicEvent] = {}
        failures: dict[str, PublicEvent] = {}
        completions: dict[str, PublicEvent] = {}
        for row in events:
            payload = row["event_json"]
            event = PublicEvent.model_validate(json.loads(payload) if isinstance(payload, str) else payload)
            command_id = event.event_id.partition(":")[0]
            if event.kind == OpenCodeEventKind.ASSISTANT_MESSAGE and event.message:
                answers.setdefault(command_id, event)
            elif event.kind == OpenCodeEventKind.FAILED:
                failures.setdefault(command_id, event)
            elif event.kind == OpenCodeEventKind.COMPLETED:
                completions.setdefault(command_id, event)
        messages = []
        for command in commands:
            text = self._command_message(command)
            if text:
                messages.append(ProjectHistoryMessage(
                    id=f"{command.command_id}:user", role="user", content=text, status="idle",
                    timestamp=command.created_at, projectId=project_id,
                ))
            answer = answers.get(command.command_id)
            if command.status == OpenCodeCommandStatus.FAILED:
                failure = failures.get(command.command_id)
                content = failure.error.message if failure and failure.error else "Forma Agent could not complete the project request."
                status = "error"
            elif command.status == OpenCodeCommandStatus.CANCELLED:
                content, status = "Forma Agent was stopped.", "cancelled"
            elif command.status == OpenCodeCommandStatus.SUCCEEDED:
                content = answer.message if answer else "Forma Agent finished responding."
                status = "success"
            else:
                # Active turns are observed through the live event stream.
                continue
            messages.append(ProjectHistoryMessage(
                id=f"{command.command_id}:assistant", role="assistant", content=content, status=status,
                timestamp=command.completed_at or command.updated_at, projectId=project_id,
                revisionId=completions[command.command_id].revision_id
                if status == "success" and command.command_id in completions else None,
            ))
        return tuple(messages)

    def heartbeat(self, command: StoredCommand, lease_token: str) -> StoredCommand:
        command = self._require_lease(command, lease_token)
        now = _timestamp()
        current_expiry = _parse_timestamp(command.lease_expires_at) or _now()
        expiry = _timestamp(max(_now() + timedelta(seconds=60), current_expiry + timedelta(seconds=1)))
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_commands").update({"status": OpenCodeCommandStatus.RUNNING.value, "lease_expires_at": expiry, "updated_at": now}).eq("command_id", command.command_id).eq("lease_token_hash", command.lease_token_hash).in_("status", ["leased", "running"]).gt("lease_expires_at", now).execute()
            provider.client.table("opencode_sessions").update({"last_heartbeat_at": now, "updated_at": now}).eq("session_id", command.session_id).execute()
        else:
            with self._connection() as connection:
                connection.execute("UPDATE opencode_commands SET status = ?, lease_expires_at = ?, updated_at = ? WHERE command_id = ? AND lease_token_hash = ? AND status IN ('leased', 'running') AND lease_expires_at > ?", (OpenCodeCommandStatus.RUNNING.value, expiry, now, command.command_id, command.lease_token_hash, now))
                connection.execute("UPDATE opencode_sessions SET last_heartbeat_at = ?, updated_at = ? WHERE session_id = ?", (now, now, command.session_id))
        result = self.get_command(command.command_id) or command
        return self._require_lease(result, lease_token)

    def complete(self, command: StoredCommand, lease_token: str, status: OpenCodeCommandStatus) -> StoredCommand:
        if status not in {OpenCodeCommandStatus.SUCCEEDED, OpenCodeCommandStatus.FAILED, OpenCodeCommandStatus.CANCELLED}:
            raise ValueError("A connector may only complete a command with a terminal status.")
        if command.status in {OpenCodeCommandStatus.SUCCEEDED, OpenCodeCommandStatus.FAILED, OpenCodeCommandStatus.CANCELLED}:
            if command.status == status:
                return command
            raise LeaseError("opencode_command_closed")
        command = self._require_lease(command, lease_token)
        now = _timestamp()
        values = {"status": status.value, "completed_at": now, "updated_at": now, "lease_token_hash": None, "lease_expires_at": None}
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_commands").update(values).eq("command_id", command.command_id).eq("lease_token_hash", command.lease_token_hash).in_("status", ["leased", "running"]).gt("lease_expires_at", now).execute()
        else:
            with self._connection() as connection:
                connection.execute("UPDATE opencode_commands SET status = ?, completed_at = ?, updated_at = ?, lease_token_hash = NULL, lease_expires_at = NULL WHERE command_id = ? AND lease_token_hash = ? AND status IN ('leased', 'running') AND lease_expires_at > ?", (status.value, now, now, command.command_id, command.lease_token_hash, now))
        result = self.get_command(command.command_id) or command
        if result.status != status:
            raise LeaseError("opencode_command_closed")
        return result

    def validate_lease(self, command: StoredCommand, lease_token: str) -> None:
        """Validate an event's lease without changing command state."""
        self._require_lease(command, lease_token)

    def cancel_command(self, command: StoredCommand) -> StoredCommand:
        if command.status in {OpenCodeCommandStatus.SUCCEEDED, OpenCodeCommandStatus.FAILED, OpenCodeCommandStatus.CANCELLED}:
            return command
        now = _timestamp()
        values = (OpenCodeCommandStatus.CANCELLED.value, now, now, command.command_id)
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_commands").update({"status": values[0], "completed_at": now, "updated_at": now, "lease_token_hash": None, "lease_expires_at": None}).eq("command_id", command.command_id).execute()
        else:
            with self._connection() as connection:
                connection.execute("UPDATE opencode_commands SET status = ?, completed_at = ?, updated_at = ?, lease_token_hash = NULL, lease_expires_at = NULL WHERE command_id = ?", values)
        return self.get_command(command.command_id) or command

    def cancel_session_commands(self, session_id: str) -> None:
        provider = self._ensure_provider()
        terminal = {
            OpenCodeCommandStatus.SUCCEEDED.value,
            OpenCodeCommandStatus.FAILED.value,
            OpenCodeCommandStatus.CANCELLED.value,
        }
        if isinstance(provider, SupabaseProvider):
            rows = provider.client.table("opencode_commands").select("command_id, status").eq("session_id", session_id).execute().data or []
            command_ids = [str(row["command_id"]) for row in rows if str(row.get("status") or "") not in terminal]
        else:
            with closing(provider.connect_dbapi()) as connection:
                rows = connection.execute("SELECT command_id FROM opencode_commands WHERE session_id = ? AND status NOT IN (?, ?, ?)", (session_id, *terminal)).fetchall()
            command_ids = [str(row["command_id"]) for row in rows]
        for command_id in command_ids:
            command = self.get_command(command_id)
            if command is not None:
                self.cancel_command(command)

    def add_event(self, event: PublicEvent) -> PublicEvent:
        provider = self._ensure_provider()
        payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        if isinstance(provider, SupabaseProvider):
            existing = provider.client.table("opencode_events").select("event_json").eq("event_id", event.event_id).limit(1).execute().data or []
            if existing:
                return PublicEvent.model_validate(existing[0]["event_json"])
            for _ in range(3):
                session = self.get_session(event.session_id)
                if session is None:
                    raise ValueError("OpenCode session not found.")
                latest = provider.client.table("opencode_events").select("sequence").eq("session_id", event.session_id).order("sequence", desc=True).limit(1).execute().data or []
                latest_sequence = int(latest[0]["sequence"]) + 1 if latest else 1
                candidate = event.model_copy(update={"sequence": max(session.next_event_sequence, latest_sequence)})
                try:
                    provider.client.table("opencode_events").insert({"event_id": candidate.event_id, "session_id": candidate.session_id, "owner_user_id": session.owner_user_id, "project_id": str(candidate.project_id), "sequence": candidate.sequence, "event_json": candidate.model_dump(mode="json"), "created_at": _timestamp(candidate.created_at)}).execute()
                    provider.client.table("opencode_sessions").update({"next_event_sequence": candidate.sequence + 1, "updated_at": _timestamp()}).eq("session_id", event.session_id).lt("next_event_sequence", candidate.sequence + 1).execute()
                    return candidate
                except Exception as exc:
                    if getattr(exc, "code", None) != "23505" and getattr(exc, "status_code", None) != 409:
                        raise
                    existing = provider.client.table("opencode_events").select("event_json").eq("event_id", event.event_id).limit(1).execute().data or []
                    if existing:
                        return PublicEvent.model_validate(existing[0]["event_json"])
            raise RuntimeError("Unable to allocate an OpenCode event sequence.")
        with self._connection() as connection:
            existing = connection.execute("SELECT event_json FROM opencode_events WHERE event_id = ?", (event.event_id,)).fetchone()
            if existing:
                return PublicEvent.model_validate(json.loads(existing["event_json"]))
            session = connection.execute("SELECT owner_user_id FROM opencode_sessions WHERE session_id = ?", (event.session_id,)).fetchone()
            if session is None:
                raise ValueError("OpenCode session not found.")
            connection.execute("INSERT INTO opencode_events (event_id, session_id, owner_user_id, project_id, sequence, event_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (event.event_id, event.session_id, session["owner_user_id"], str(event.project_id), event.sequence, payload, _timestamp(event.created_at)))
            connection.execute("UPDATE opencode_sessions SET next_event_sequence = MAX(next_event_sequence, ?), updated_at = ? WHERE session_id = ?", (event.sequence + 1, _timestamp(), event.session_id))
        return event

    def next_event_sequence(self, session_id: str) -> int:
        session = self.get_session(session_id)
        if session is None:
            raise ValueError("OpenCode session not found.")
        return session.next_event_sequence

    def get_event(self, session_id: str, event_id: str) -> PublicEvent | None:
        """Return an acknowledged event only within its authorized session."""
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = provider.client.table("opencode_events").select("event_json").eq("session_id", session_id).eq("event_id", event_id).limit(1).execute().data or []
            return PublicEvent.model_validate(rows[0]["event_json"]) if rows else None
        with closing(provider.connect_dbapi()) as connection:
            row = connection.execute("SELECT event_json FROM opencode_events WHERE session_id = ? AND event_id = ?", (session_id, event_id)).fetchone()
        return PublicEvent.model_validate(json.loads(row["event_json"])) if row else None

    def list_events(self, session_id: str, after: int, limit: int) -> list[PublicEvent]:
        provider = self._ensure_provider()
        if isinstance(provider, SupabaseProvider):
            rows = provider.client.table("opencode_events").select("event_json").eq("session_id", session_id).gt("sequence", after).order("sequence").limit(limit).execute().data or []
            return [PublicEvent.model_validate(row["event_json"]) for row in rows]
        with closing(provider.connect_dbapi()) as connection:
            rows = connection.execute("SELECT event_json FROM opencode_events WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT ?", (session_id, after, limit)).fetchall()
        return [PublicEvent.model_validate(json.loads(row["event_json"])) for row in rows]

    def _require_lease(self, command: StoredCommand, lease_token: str) -> StoredCommand:
        current = self.get_command(command.command_id)
        if current is None or current.status not in {OpenCodeCommandStatus.LEASED, OpenCodeCommandStatus.RUNNING}:
            raise LeaseError("opencode_command_closed")
        command = current
        digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        if not command.lease_token_hash or not secrets.compare_digest(digest, command.lease_token_hash):
            raise LeaseError("opencode_lease_invalid")
        expiry = _parse_timestamp(command.lease_expires_at)
        if expiry is None or expiry <= _now():
            self.fail_expired_lease(command, digest)
            raise LeaseError("opencode_lease_expired")
        return command

    def fail_expired_lease(self, command: StoredCommand, lease_digest: str) -> None:
        """Settle only the matching expired lease, preserving any newer claimant."""
        provider = self._ensure_provider()
        now = _now()
        if isinstance(provider, SupabaseProvider):
            provider.client.rpc("fail_expired_opencode_lease", {
                "p_command_id": command.command_id, "p_lease_hash": lease_digest, "p_now": _timestamp(now),
            }).execute()
            return
        with self._connection(begin_immediate=True) as connection:
            row = connection.execute("SELECT * FROM opencode_commands WHERE command_id = ?", (command.command_id,)).fetchone()
            if row is None or row["status"] not in {"leased", "running"} or row["lease_token_hash"] != lease_digest:
                return
            expiry = _parse_timestamp(row["lease_expires_at"])
            if expiry is not None and expiry > now:
                return
            session = connection.execute("SELECT * FROM opencode_sessions WHERE session_id = ?", (row["session_id"],)).fetchone()
            sequence = session["next_event_sequence"]
            event = project_public_event(
                ConnectorEventInput(event_id=f"{command.command_id}:terminal", kind="failed", status="failed", error_code="opencode_lease_expired"),
                sequence=sequence, session_id=row["session_id"], project_id=UUID(row["project_id"]), created_at=now,
            )
            connection.execute("UPDATE opencode_commands SET status = 'failed', completed_at = ?, updated_at = ?, lease_token_hash = NULL, lease_expires_at = NULL WHERE command_id = ?",
                               (_timestamp(now), _timestamp(now), command.command_id))
            # An already-delivered canonical outcome must not be replaced.
            cursor = connection.execute("INSERT OR IGNORE INTO opencode_events (event_id, session_id, owner_user_id, project_id, sequence, event_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                        (event.event_id, row["session_id"], row["owner_user_id"], row["project_id"], sequence, event.model_dump_json(), _timestamp(now)))
            if cursor.rowcount:
                connection.execute("UPDATE opencode_sessions SET next_event_sequence = ? WHERE session_id = ?", (sequence + 1, row["session_id"]))

    def _command_message(self, command: StoredCommand) -> str | None:
        if command.message_ciphertext is None or command.message_key_id is None:
            return None
        return decrypt_user_secret_text(command.message_ciphertext, command.message_key_id)

    def _update_command_message(self, command_id: str, ciphertext: str, key_id: str) -> None:
        provider = self._ensure_provider()
        values = {"message_ciphertext": ciphertext, "message_key_id": key_id}
        if isinstance(provider, SupabaseProvider):
            provider.client.table("opencode_commands").update(values).eq("command_id", command_id).execute()
            return
        with self._connection() as connection:
            connection.execute(
                "UPDATE opencode_commands SET message_ciphertext = ?, message_key_id = ? WHERE command_id = ?",
                (ciphertext, key_id, command_id),
            )

    def _connection(self, *, begin_immediate: bool = False) -> Any:
        provider = self._ensure_provider()
        if not isinstance(provider, SQLiteProvider):
            raise TypeError("A SQLite connection was requested from a non-SQLite OpenCode provider.")
        connection = provider.connect_dbapi()
        if begin_immediate:
            self._lock.acquire()
            connection.execute("BEGIN IMMEDIATE")
            return _LockedConnection(connection, self._lock, pre_acquired=True)
        return _LockedConnection(connection, self._lock, pre_acquired=False)


class _LockedConnection:
    def __init__(self, connection: Any, lock: threading.RLock, *, pre_acquired: bool) -> None:
        self.connection = connection
        self.lock = lock
        self.pre_acquired = pre_acquired

    def __enter__(self) -> Any:
        if not self.pre_acquired:
            self.lock.acquire()
        return self.connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            self.connection.close()
        finally:
            self.lock.release()


def _session_from_record(record: dict[str, Any]) -> StoredSession:
    return StoredSession(
        session_id=str(record["session_id"]), connector_id=str(record["connector_id"]), owner_user_id=str(record["owner_user_id"]),
        project_id=str(record["project_id"]), status=OpenCodeSessionStatus(str(record["status"])), capability_nonce=str(record["capability_nonce"]),
        created_at=str(record["created_at"]), updated_at=str(record["updated_at"]), last_heartbeat_at=record.get("last_heartbeat_at"),
        next_event_sequence=int(record.get("next_event_sequence") or 1),
    )


def _command_from_record(record: dict[str, Any]) -> StoredCommand:
    return StoredCommand(
        command_id=str(record["command_id"]), session_id=str(record["session_id"]), connector_id=str(record["connector_id"]),
        owner_user_id=str(record["owner_user_id"]), project_id=str(record["project_id"]), operation=OpenCodeOperation(str(record["operation"])),
        idempotency_key=str(record["idempotency_key"]), status=OpenCodeCommandStatus(str(record["status"])), message_digest=str(record["message_digest"]),
        message_ciphertext=record.get("message_ciphertext"), message_key_id=record.get("message_key_id"),
        attempt_count=int(record.get("attempt_count") or 0), lease_expires_at=record.get("lease_expires_at"), lease_token_hash=record.get("lease_token_hash"),
        created_at=str(record["created_at"]), updated_at=str(record["updated_at"]), completed_at=record.get("completed_at"),
        model=record.get("model"),
    )


def _connector_command(command: StoredCommand, lease_token: str, message: str | None) -> ConnectorCommand:
    return ConnectorCommand(
        command_id=command.command_id, session_id=command.session_id, connector_id=command.connector_id,
        owner_user_id=command.owner_user_id, project_id=command.project_id, operation=command.operation,
        message=message, attempt_count=command.attempt_count, lease_expires_at=_parse_timestamp(command.lease_expires_at) or _now(),
        lease_token=lease_token,
        model=command.model,
    )
