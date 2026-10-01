"""Privacy-aware project deletion, contribution consent, and purge orchestration."""

from __future__ import annotations

import asyncio
import logging
from forma_core.config import config
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from apps.api.video_storage import delete_project_videos
from forma_core.database import (
    add_project_deletion_audit,
    anonymize_project_contribution_consent,
    anonymize_project_contribution_snapshot,
    get_generated_project,
    get_project_identity,
    get_project_contribution_consent,
    get_user_settings,
    hard_purge_generated_project,
    list_project_deletion_audits,
    list_due_project_purges,
    publish_project_revision,
    purge_project_contribution_snapshots,
    resolve_project_for_read,
    update_project_deletion_state,
    upsert_project_contribution_consent,
    upsert_project_contribution_snapshot,
    withdraw_project_contribution_consent,
)
from forma_core.jobs.store import JOB_STORE
from forma_core.persistence.images import delete_project_images
from forma_core.persistence.project_artifacts import ProjectArtifactStorage

logger = logging.getLogger(__name__)

DELETION_POLICY_VERSION = "2026-07-31"
SANITIZATION_VERSION = "2026-07-31.1"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_PURGE_INTERVAL_SECONDS = 60
DEFAULT_PURGE_STALE_AFTER_SECONDS = 60 * 60
PERMITTED_CONTRIBUTION_PURPOSES = frozenset(
    {
        "product_research",
        "evaluation",
        "reliability_testing",
        "model_evaluation",
        "ai_system_improvement",
    }
)
_SAFE_ENUM = re.compile(r"^[A-Za-z0-9_.:+/-]{1,40}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).isoformat(timespec="microseconds").replace("+00:00", "Z")


def retention_days() -> int:
    try:
        return max(1, int(config.get("PROJECT_DELETION_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def _attr(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _audit(
    project_id: str,
    acting_user_id: Optional[str],
    action: str,
    status: str,
    details: Optional[Dict[str, Any]] = None,
) -> Any:
    return add_project_deletion_audit(
        {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "acting_user_id": acting_user_id,
            "action": action,
            "status": status,
            "policy_version": DELETION_POLICY_VERSION,
            "details_json": details or {},
            "created_at": iso_timestamp(),
        }
    )


def _safe_category(value: Any) -> str:
    normalized = str(value or "unknown").strip().lower().replace(" ", "_")
    return normalized if _SAFE_ENUM.fullmatch(normalized) else "other"


def sanitize_project_for_contribution(project: Any) -> Dict[str, Any]:
    """Build an aggregate-only snapshot with no prompts, names, URLs, IDs, or free text."""
    hardware_ir = _attr(project, "hardware_ir", {})
    if not isinstance(hardware_ir, dict):
        hardware_ir = {}
    components = hardware_ir.get("components") if isinstance(hardware_ir.get("components"), list) else []
    nets = hardware_ir.get("nets") if isinstance(hardware_ir.get("nets"), list) else []
    validation = hardware_ir.get("validation") if isinstance(hardware_ir.get("validation"), dict) else {}

    component_categories = Counter()
    pin_counts: List[int] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_categories[_safe_category(component.get("category") or component.get("type"))] += 1
        pins = component.get("pins")
        pin_counts.append(len(pins) if isinstance(pins, (list, dict)) else 0)

    connection_counts = []
    for net in nets:
        if not isinstance(net, dict):
            continue
        connections = net.get("connections") or net.get("nodes") or net.get("pins")
        connection_counts.append(len(connections) if isinstance(connections, list) else 0)

    def issue_count(name: str) -> int:
        value = validation.get(name)
        return len(value) if isinstance(value, list) else 0

    return {
        "schema_version": 1,
        "sanitization_version": SANITIZATION_VERSION,
        "hardware_summary": {
            "is_valid": bool(hardware_ir.get("is_valid")),
            "component_count": len(components),
            "component_categories": dict(sorted(component_categories.items())),
            "component_pin_counts": pin_counts,
            "net_count": len(nets),
            "net_connection_counts": connection_counts,
            "validation_counts": {
                "critical": issue_count("critical"),
                "warnings": issue_count("warnings") + issue_count("warning"),
            },
        },
    }


def grant_contribution_consent(
    project_id: str,
    user_id: str,
    *,
    consent_version: str,
    permitted_purposes: Iterable[str],
    workspace_id: Optional[str] = None,
) -> Any:
    try:
        project = resolve_project_for_read(project_id, user_id).project
    except LookupError as exc:
        raise LookupError("Project not found.") from exc
    if not project or _attr(project, "owner_user_id") != user_id:
        raise LookupError("Project not found.")
    purposes = sorted(set(permitted_purposes))
    normalized_consent_version = consent_version.strip()
    if not normalized_consent_version:
        raise ValueError("A consent policy version is required.")
    if not purposes or any(purpose not in PERMITTED_CONTRIBUTION_PURPOSES for purpose in purposes):
        raise ValueError("One or more contribution purposes are not permitted.")
    existing = get_project_contribution_consent(project_id, user_id)
    settings = get_user_settings(user_id)
    if settings and bool(_attr(settings, "model_training_opt_out")):
        raise ValueError("Your account-wide data setting blocks contributions. Change it in Data & Privacy or delete without contributing.")
    now = iso_timestamp()
    consent = upsert_project_contribution_consent(
        {
            "id": _attr(existing, "id") or str(uuid.uuid4()),
            "project_id": project_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "consent_version": normalized_consent_version,
            "permitted_purposes": purposes,
            "granted_at": now,
            "withdrawn_at": None,
            "purged_at": None,
        }
    )
    _audit(project_id, user_id, "contribution_consent_granted", "succeeded", {"purpose_count": len(purposes)})
    return consent


def withdraw_contribution(project_id: str, user_id: str) -> Optional[Any]:
    consent = get_project_contribution_consent(project_id, user_id)
    if not consent:
        return None
    now = iso_timestamp()
    withdrawn = withdraw_project_contribution_consent(project_id, user_id, now)
    removed = purge_project_contribution_snapshots(str(_attr(consent, "id")), now)
    _audit(project_id, user_id, "contribution_consent_withdrawn", "succeeded", {"pending_snapshots_removed": removed})
    return withdrawn


def request_project_deletion(project_id: str, user_id: str) -> Any:
    try:
        resolved = resolve_project_for_read(project_id, user_id, include_deleted=True)
    except LookupError as exc:
        identity = get_project_identity(project_id)
        if not identity or _attr(identity, "owner_user_id") != user_id:
            raise LookupError("Project not found.") from exc
        resolved = None
        project = identity
    else:
        project = resolved.project
    if not project or _attr(project, "owner_user_id") != user_id:
        raise LookupError("Project not found.")
    if _attr(project, "status") != "active":
        return project

    now = utc_now()
    deleted_at = iso_timestamp(now)
    purge_after = iso_timestamp(now + timedelta(days=retention_days()))
    updated = update_project_deletion_state(
        project_id,
        owner_user_id=user_id,
        allowed_statuses=["active"],
        updates={
            "status": "deletion_pending",
            "deleted_at": deleted_at,
            "deletion_requested_by": user_id,
            "purge_after": purge_after,
            "purge_started_at": None,
            "purge_completed_at": None,
            "deletion_error": None,
        },
    )
    if not updated:
        return get_project_identity(project_id)

    try:
        matched_jobs = JOB_STORE.cancel_project_jobs(project_id)
    except Exception as exc:
        matched_jobs = 0
        logger.exception("Project job cancellation failed for project_id=%s", project_id)
        _audit(project_id, user_id, "job_cancellation", "failed", {"error_type": type(exc).__name__})
    consent = get_project_contribution_consent(project_id, user_id)
    snapshot_created = False
    if consent and not _attr(consent, "withdrawn_at"):
        try:
            sanitized_payload = sanitize_project_for_contribution(project)
            sanitized_payload["consent_version"] = str(_attr(consent, "consent_version"))
            sanitized_payload["permitted_purposes"] = list(_attr(consent, "permitted_purposes", []))
            snapshot = upsert_project_contribution_snapshot(
                {
                    "id": str(uuid.uuid4()),
                    "source_project_id": project_id,
                    "consent_record_id": str(_attr(consent, "id")),
                    "sanitization_version": SANITIZATION_VERSION,
                    "contribution_status": "sanitized_pending_anonymization",
                    "payload_json": sanitized_payload,
                    "created_at": deleted_at,
                    "sanitized_at": deleted_at,
                    "anonymized_at": None,
                    "purged_at": None,
                }
            )
            snapshot_created = bool(snapshot)
            _audit(
                project_id,
                user_id,
                "contribution_snapshot",
                "succeeded",
                {"sanitization_version": SANITIZATION_VERSION},
            )
            upsert_project_contribution_consent(
                {
                    "id": str(_attr(consent, "id")),
                    "project_id": project_id,
                    "user_id": user_id,
                    "workspace_id": _attr(consent, "workspace_id"),
                    "consent_version": _attr(consent, "consent_version"),
                    "permitted_purposes": _attr(consent, "permitted_purposes", []),
                    "granted_at": _attr(consent, "granted_at"),
                    "withdrawn_at": None,
                    "snapshot_created_at": deleted_at,
                    "sanitized_at": deleted_at,
                    "anonymized_at": None,
                    "purged_at": None,
                }
            )
        except Exception as exc:
            logger.exception("Sanitized contribution snapshot failed for project_id=%s", project_id)
            _audit(project_id, user_id, "contribution_snapshot", "failed", {"error_type": type(exc).__name__})

    _audit(
        project_id,
        user_id,
        "deletion_requested",
        "succeeded",
        {"retention_days": retention_days(), "jobs_matched": matched_jobs, "snapshot_created": snapshot_created},
    )
    logger.info("Project deletion requested project_id=%s retention_days=%s", project_id, retention_days())
    return updated


def restore_project(project_id: str, user_id: str) -> Any:
    try:
        resolved = resolve_project_for_read(project_id, user_id, include_deleted=True)
    except LookupError as exc:
        identity = get_project_identity(project_id)
        if not identity or _attr(identity, "owner_user_id") != user_id:
            raise LookupError("Project not found.") from exc
        resolved = None
        project = identity
    else:
        project = resolved.project
    if not project or _attr(project, "owner_user_id") != user_id:
        raise LookupError("Project not found.")
    status = _attr(project, "status")
    if status == "active":
        return project
    if status != "deletion_pending" or _attr(project, "purge_started_at"):
        raise RuntimeError("Project purge has started and can no longer be restored.")
    purge_after = _attr(project, "purge_after")
    if purge_after:
        try:
            restore_deadline = datetime.fromisoformat(str(purge_after).replace("Z", "+00:00"))
            if restore_deadline.tzinfo is None:
                restore_deadline = restore_deadline.replace(tzinfo=timezone.utc)
        except ValueError:
            restore_deadline = utc_now()
        if restore_deadline <= utc_now():
            raise RuntimeError("The project retention window has expired.")
    restored = update_project_deletion_state(
        project_id,
        owner_user_id=user_id,
        allowed_statuses=["deletion_pending"],
        updates={
            "status": "active",
            "deleted_at": None,
            "deletion_requested_by": None,
            "purge_after": None,
            "purge_started_at": None,
            "purge_completed_at": None,
            "deletion_error": None,
        },
    )
    if not restored:
        raise RuntimeError("Project could not be restored because its deletion state changed.")
    if resolved is not None and resolved.source == "canonical" and resolved.revision is not None and resolved.design_brief is not None:
        try:
            publish_project_revision(
                resolved.revision,
                resolved.design_brief,
                user_id,
                visibility=resolved.visibility,
            )
        except Exception as exc:
            logger.exception("Project projection rebuild failed for project_id=%s", project_id)
            update_project_deletion_state(
                project_id,
                owner_user_id=user_id,
                allowed_statuses=["active"],
                updates={
                    "status": "deletion_pending",
                    "deleted_at": _attr(project, "deleted_at"),
                    "deletion_requested_by": user_id,
                    "purge_after": _attr(project, "purge_after"),
                    "deletion_error": type(exc).__name__,
                },
            )
            raise RuntimeError("Project could not rebuild its gallery projection.") from exc
    consent = get_project_contribution_consent(project_id, user_id)
    if consent:
        purge_project_contribution_snapshots(str(_attr(consent, "id")), iso_timestamp())
    _audit(project_id, user_id, "deletion_restored", "succeeded")
    return restored


def purge_project(project_id: str) -> Dict[str, Any]:
    # Lifecycle claims must use the same authoritative record as the repository
    # update. The read resolver may return a legacy content projection whose
    # status or lease timestamp differs from the canonical identity.
    project = get_project_identity(project_id)
    if project is None:
        project = get_generated_project(project_id, include_deleted=True)
    if not project:
        return {"project_id": project_id, "status": "purged", "already_absent": True}
    owner_user_id = _attr(project, "owner_user_id")
    status = _attr(project, "status")
    if status not in {"deletion_pending", "deletion_failed", "purging"}:
        raise RuntimeError("Project is not scheduled for deletion.")

    previous_purge_started_at = _attr(project, "purge_started_at")
    if status == "purging" and previous_purge_started_at:
        try:
            parsed_started_at = datetime.fromisoformat(str(previous_purge_started_at).replace("Z", "+00:00"))
            if parsed_started_at.tzinfo is None:
                parsed_started_at = parsed_started_at.replace(tzinfo=timezone.utc)
        except ValueError:
            parsed_started_at = utc_now() - timedelta(hours=1)
        try:
            stale_after = max(60, int(config.get("PROJECT_PURGE_STALE_AFTER_SECONDS", str(DEFAULT_PURGE_STALE_AFTER_SECONDS))))
        except ValueError:
            stale_after = DEFAULT_PURGE_STALE_AFTER_SECONDS
        if parsed_started_at > utc_now() - timedelta(seconds=stale_after):
            raise RuntimeError("Project purge is already running.")

    started_at = iso_timestamp()
    claimed = update_project_deletion_state(
        project_id,
        owner_user_id=owner_user_id,
        allowed_statuses=[status],
        updates={"status": "purging", "purge_started_at": started_at, "deletion_error": None},
        expected_purge_started_at=str(previous_purge_started_at) if status == "purging" and previous_purge_started_at else None,
    )
    if not claimed:
        raise RuntimeError("Project purge was claimed by another worker.")

    counts: Dict[str, int] = {}
    try:
        counts["images"] = delete_project_images(project_id)
        counts["videos"] = delete_project_videos(project_id)
        artifact_storage = ProjectArtifactStorage()
        counts["artifacts"] = (
            artifact_storage.delete_project(project_id)
            if artifact_storage.config.get("enabled")
            else 0
        )
        counts["jobs"] = JOB_STORE.delete_project_jobs(project_id)

        consent = get_project_contribution_consent(project_id, owner_user_id) if owner_user_id else None
        if consent:
            consent_id = str(_attr(consent, "id"))
            if not _attr(consent, "withdrawn_at"):
                anonymize_project_contribution_snapshot(consent_id, iso_timestamp())
            else:
                counts["contribution_snapshots"] = purge_project_contribution_snapshots(consent_id, iso_timestamp())
            anonymize_project_contribution_consent(project_id, owner_user_id, iso_timestamp())

        if not hard_purge_generated_project(project_id, owner_user_id):
            raise RuntimeError("Project row disappeared before database purge completed.")
        _audit(project_id, owner_user_id, "purge_completed", "succeeded", counts)
        return {"project_id": project_id, "status": "purged", "deleted_objects": counts}
    except Exception as exc:
        error_type = type(exc).__name__
        logger.exception("Project purge failed for project_id=%s", project_id)
        update_project_deletion_state(
            project_id,
            owner_user_id=owner_user_id,
            allowed_statuses=["purging"],
            updates={"status": "deletion_failed", "deletion_error": error_type},
            expected_purge_started_at=started_at,
        )
        _audit(project_id, owner_user_id, "purge_completed", "failed", {"error_type": error_type, **counts})
        raise


def purge_due_projects(limit: int = 25) -> List[Dict[str, Any]]:
    results = []
    for project in list_due_project_purges(iso_timestamp(), limit):
        project_id = str(_attr(project, "project_id"))
        try:
            alert_after = max(60, int(config.get("PROJECT_PURGE_ALERT_AFTER_SECONDS", "3600")))
            deadline = datetime.fromisoformat(str(_attr(project, "purge_after")).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline < utc_now() - timedelta(seconds=alert_after):
                logger.error("Project purge exceeded its completion window project_id=%s status=%s", project_id, _attr(project, "status"))
        except (TypeError, ValueError):
            pass
        try:
            results.append(purge_project(project_id))
        except Exception as exc:
            results.append({"project_id": project_id, "status": "deletion_failed", "error_type": type(exc).__name__})
    return results


def deletion_metrics() -> Dict[str, int]:
    """Return content-free lifecycle counters for internal monitoring."""
    events = list_project_deletion_audits(500)
    latest_project_state: Dict[str, str] = {}
    metrics = {
        "pending_purges": 0,
        "completed_purges": 0,
        "failed_purges": 0,
        "completed_sanitizations": 0,
        "failed_sanitizations": 0,
    }
    for event in events:
        action = str(_attr(event, "action") or "")
        event_status = str(_attr(event, "status") or "")
        project_id = str(_attr(event, "project_id") or "")
        if action == "contribution_snapshot":
            key = "completed_sanitizations" if event_status == "succeeded" else "failed_sanitizations"
            metrics[key] += 1
        if project_id in latest_project_state:
            continue
        if action == "purge_completed":
            latest_project_state[project_id] = "completed" if event_status == "succeeded" else "failed"
        elif action == "deletion_restored" and event_status == "succeeded":
            latest_project_state[project_id] = "restored"
        elif action == "deletion_requested" and event_status == "succeeded":
            latest_project_state[project_id] = "pending"
    metrics["pending_purges"] = sum(value == "pending" for value in latest_project_state.values())
    metrics["completed_purges"] = sum(value == "completed" for value in latest_project_state.values())
    metrics["failed_purges"] = sum(value == "failed" for value in latest_project_state.values())
    return metrics


async def purge_worker(stop_event: asyncio.Event) -> None:
    try:
        interval = max(10, int(config.get("PROJECT_PURGE_INTERVAL_SECONDS", str(DEFAULT_PURGE_INTERVAL_SECONDS))))
    except ValueError:
        interval = DEFAULT_PURGE_INTERVAL_SECONDS
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(purge_due_projects)
        except Exception:
            logger.exception("Project purge worker cycle failed.")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
