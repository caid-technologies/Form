"""Short-lived, session-scoped HMAC capabilities for connector calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import FrozenSet

from forma_core.config import config


class CapabilityError(PermissionError):
    """The connector capability was rejected with a stable, public category."""

    code = "opencode_capability_invalid"


class CapabilityExpiredError(CapabilityError):
    code = "opencode_capability_expired"


class CapabilityScopeError(CapabilityError):
    code = "opencode_scope_mismatch"


CONNECTOR_CAPABILITY_TTL_SECONDS = 20 * 60


@dataclass(frozen=True, slots=True)
class ConnectorCapability:
    connector_id: str
    session_id: str
    project_id: str
    owner_user_id: str
    expires_at: int
    nonce: str
    scopes: FrozenSet[str]


def _secret() -> bytes:
    value = (config.get("FORMA_OPENCODE_CAPABILITY_SECRET") or config.get("FORMA_OPENCODE_CONNECTOR_SECRET") or "").strip()
    if len(value) < 32:
        raise CapabilityError("OpenCode connector capability signing is not configured.")
    return value.encode("utf-8")


def issue_capability(
    *,
    connector_id: str,
    session_id: str,
    project_id: str,
    owner_user_id: str,
    scopes: FrozenSet[str],
    ttl_seconds: int = CONNECTOR_CAPABILITY_TTL_SECONDS,
) -> str:
    expires_at = int(time.time()) + max(30, min(ttl_seconds, CONNECTOR_CAPABILITY_TTL_SECONDS))
    payload = {
        "connector_id": connector_id,
        "session_id": session_id,
        "project_id": project_id,
        "owner_user_id": owner_user_id,
        "expires_at": expires_at,
        "nonce": secrets.token_urlsafe(18),
        "scopes": sorted(scopes),
    }
    encoded = _encode(payload)
    signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{_b64encode(signature)}"


def verify_capability(
    token: str,
    *,
    connector_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    owner_user_id: str | None = None,
    scope: str | None = None,
) -> ConnectorCapability:
    try:
        encoded_text, supplied_signature = token.split(".", 1)
        encoded = encoded_text.encode("ascii")
        expected_signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
        actual_signature = base64.urlsafe_b64decode(supplied_signature + "=" * (-len(supplied_signature) % 4))
        if _b64encode(actual_signature) != supplied_signature or not hmac.compare_digest(actual_signature, expected_signature):
            raise CapabilityError("The connector capability is invalid.")
        payload_bytes = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        payload = json.loads(payload_bytes.decode("utf-8"))
        capability = ConnectorCapability(
            connector_id=str(payload["connector_id"]),
            session_id=str(payload["session_id"]),
            project_id=str(payload["project_id"]),
            owner_user_id=str(payload["owner_user_id"]),
            expires_at=int(payload["expires_at"]),
            nonce=str(payload["nonce"]),
            scopes=frozenset(str(item) for item in payload["scopes"]),
        )
    except CapabilityError:
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise CapabilityError("The connector capability is invalid.") from exc

    if capability.expires_at <= int(time.time()):
        raise CapabilityExpiredError("The connector capability has expired.")
    if connector_id is not None and capability.connector_id != connector_id:
        raise CapabilityScopeError("The connector capability is outside its connector scope.")
    if session_id is not None and capability.session_id != session_id:
        raise CapabilityScopeError("The connector capability is outside its session scope.")
    if project_id is not None and capability.project_id != project_id:
        raise CapabilityScopeError("The connector capability is outside its project scope.")
    if owner_user_id is not None and capability.owner_user_id != owner_user_id:
        raise CapabilityScopeError("The connector capability is outside its owner scope.")
    if scope is not None and scope not in capability.scopes:
        raise CapabilityScopeError("The connector capability does not grant this operation.")
    return capability


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _encode(payload: dict[str, object]) -> bytes:
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
