"""Project untrusted connector activity into a small public event contract."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

from forma_core.opencode.models import (
    ConnectorEventInput,
    OpenCodeCommandStatus,
    OpenCodeEventKind,
    PublicError,
    PublicEvent,
)


_SECRET_PATTERN = re.compile(r"(?i)(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+")
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|/Users/|/home/|/tmp/|\\\\|\.{1,2}[\\/])")
_INTERNAL_PATTERN = re.compile(r"(?i)(?:^|\s)(?:diff|patch|shell|command|tool|reasoning|traceback|exception)\s*[:=]")
_PUBLIC_KINDS = {kind.value for kind in OpenCodeEventKind}
_ERROR_MESSAGES = {
    "opencode_command_stalled": "Forma Agent could not start or resume this request after three attempts. Check the runtime and retry the request.",
    "opencode_lease_expired": "Forma Agent lost its command lease. Submit the request again.",
    "connector_timeout": "Forma Agent did not reconnect within five minutes. Retry this request when it is available.",
    "connector_unavailable": "Forma Agent is unavailable.",
    "command_failed": "Forma Agent could not complete the project request.",
    "validation_failed": "The project failed Forma validation.",
}
_SAFE_WORKER_ERROR_PATTERN = re.compile(r"^opencode_(?:session_)?http_[45]\d{2}$")


def project_public_event(
    event: ConnectorEventInput,
    *,
    sequence: int,
    session_id: str,
    project_id: UUID,
    created_at: datetime | None = None,
) -> PublicEvent:
    kind = event.kind.strip().lower()
    if kind not in _PUBLIC_KINDS:
        kind = OpenCodeEventKind.PROGRESS.value
    safe_message = _safe_message(event.message) if kind == OpenCodeEventKind.ASSISTANT_MESSAGE.value else None
    error = _public_error(event) if kind == OpenCodeEventKind.FAILED.value else None
    if kind == OpenCodeEventKind.FAILED.value and error is None:
        error = PublicError(
            code="command_failed",
            message=_ERROR_MESSAGES["command_failed"],
            correlation_id=event.correlation_id or f"event_{event.event_id}",
        )
    return PublicEvent(
        event_id=event.event_id,
        sequence=sequence,
        session_id=session_id,
        project_id=project_id,
        kind=OpenCodeEventKind(kind),
        status=event.status,
        message=safe_message,
        revision_id=event.revision_id if kind == OpenCodeEventKind.COMPLETED.value else None,
        validation=event.validation if kind in {OpenCodeEventKind.COMPLETED.value, OpenCodeEventKind.VALIDATING.value} else None,
        artifact_ids=event.artifact_ids if kind == OpenCodeEventKind.COMPLETED.value else (),
        error=error,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _safe_message(value: str | None) -> str | None:
    if not value:
        return None
    if _PATH_PATTERN.search(value) or _INTERNAL_PATTERN.search(value):
        return "Forma Agent is working on the project."
    redacted = _SECRET_PATTERN.sub("[redacted]", value).strip()
    return redacted[:2000] if redacted else None


def _public_error(event: ConnectorEventInput) -> PublicError:
    code = event.error_code if event.error_code in _ERROR_MESSAGES else "command_failed"
    if event.error_code == "opencode_fetch_failed":
        code = event.error_code
        message = "Forma Agent is offline. The runtime assigned to this workspace could not be reached."
    elif event.error_code and _SAFE_WORKER_ERROR_PATTERN.fullmatch(event.error_code):
        code = event.error_code
        message = f"Forma Agent runtime request failed (HTTP {event.error_code[-3:]})."
    else:
        message = _ERROR_MESSAGES.get(code, "Forma Agent could not complete the project request.")
    return PublicError(
        code=code,
        message=message,
        correlation_id=event.correlation_id or f"event_{event.event_id}",
    )
