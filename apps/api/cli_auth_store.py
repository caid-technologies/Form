"""Short-lived CLI device sessions and opaque bearer-token state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
import threading
import time
from typing import Any, Optional

from forma_core.config import config


DEVICE_LIFETIME_SECONDS = 600
ACCESS_LIFETIME_SECONDS = 3600
REFRESH_LIFETIME_SECONDS = 60 * 60 * 24 * 30


@dataclass(frozen=True)
class CliIdentity:
    subject: str
    provider: str
    email: str | None = None
    display_name: str | None = None


@dataclass
class _DeviceSession:
    device_code: str
    user_code: str
    expires_at: float
    status: str = "pending"
    identity: CliIdentity | None = None
    consumed: bool = False


@dataclass
class _RefreshSession:
    identity: CliIdentity
    expires_at: float


@dataclass
class _AccessSession:
    identity: CliIdentity
    expires_at: float
    refresh_token: str


_LOCK = threading.RLock()
_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False
_DEVICES: dict[str, _DeviceSession] = {}
_DEVICES_BY_USER_CODE: dict[str, str] = {}
_REFRESH_TOKENS: dict[str, _RefreshSession] = {}
_ACCESS_TOKENS: dict[str, _AccessSession] = {}
_REVOKED_ACCESS_TOKENS: set[str] = set()


def _use_memory_store() -> bool:
    return (config.optional("FORMA_CLI_AUTH_STORAGE") or "database").strip().lower() == "memory"


def _database() -> Any:
    global _DB_INITIALIZED
    from forma_core import database

    if not _DB_INITIALIZED:
        with _DB_INIT_LOCK:
            if not _DB_INITIALIZED:
                database.init_db()
                _DB_INITIALIZED = True
    return database


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_from_record(record: Any) -> CliIdentity | None:
    subject = str(getattr(record, "owner_user_id", "") or "").strip()
    provider = str(getattr(record, "provider", "") or "").strip()
    if not subject or not provider:
        return None
    return CliIdentity(
        subject=subject,
        provider=provider,
        email=getattr(record, "email", None),
        display_name=getattr(record, "display_name", None),
    )


def _identity_fields(identity: CliIdentity) -> dict[str, str | None]:
    return {
        "owner_user_id": identity.subject,
        "provider": identity.provider,
        "email": identity.email,
        "display_name": identity.display_name,
    }


def _cleanup_memory(now: float | None = None) -> None:
    current = now or time.time()
    for code, session in list(_DEVICES.items()):
        if session.expires_at <= current:
            _DEVICES.pop(code, None)
            _DEVICES_BY_USER_CODE.pop(session.user_code, None)
    for token, session in list(_REFRESH_TOKENS.items()):
        if session.expires_at <= current:
            _REFRESH_TOKENS.pop(token, None)
    for token, session in list(_ACCESS_TOKENS.items()):
        if session.expires_at <= current:
            _ACCESS_TOKENS.pop(token, None)


def create_device_session() -> _DeviceSession:
    with _LOCK:
        if _use_memory_store():
            _cleanup_memory()
            while True:
                user_code = "-".join(
                    "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
                    for _ in range(2)
                )
                if user_code not in _DEVICES_BY_USER_CODE:
                    break
            session = _DeviceSession(
                device_code=secrets.token_urlsafe(32),
                user_code=user_code,
                expires_at=time.time() + DEVICE_LIFETIME_SECONDS,
            )
            _DEVICES[session.device_code] = session
            _DEVICES_BY_USER_CODE[session.user_code] = session.device_code
            return session

        while True:
            user_code = "-".join(
                "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))
                for _ in range(2)
            )
            device_code = secrets.token_urlsafe(32)
            try:
                _database().insert_cli_device_authorization({
                    "device_code_hash": _hash(device_code),
                    "user_code_hash": _hash(user_code),
                    "status": "pending",
                    "expires_at": time.time() + DEVICE_LIFETIME_SECONDS,
                    "owner_user_id": None,
                    "provider": None,
                    "email": None,
                    "display_name": None,
                    "consumed": False,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            except Exception as exc:
                if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
                    raise
                continue
            return _DeviceSession(device_code=device_code, user_code=user_code, expires_at=time.time() + DEVICE_LIFETIME_SECONDS)


def device_status(device_code: str) -> str:
    with _LOCK:
        now = time.time()
        if _use_memory_store():
            _cleanup_memory(now)
            session = _DEVICES.get(device_code)
            return "expired" if session is None else session.status
        record = _database().get_cli_device_authorization(_hash(device_code))
        if record is None or float(record.expires_at) <= now:
            return "expired"
        return str(record.status)


def approve_device(user_code: str, identity: CliIdentity) -> bool:
    with _LOCK:
        if _use_memory_store():
            _cleanup_memory()
            device_code = _DEVICES_BY_USER_CODE.get(user_code.strip().upper())
            session = _DEVICES.get(device_code or "")
            if session is None or session.status != "pending":
                return False
            session.status = "approved"
            session.identity = identity
            return True
        record = _database().get_cli_device_authorization(user_code_hash=_hash(user_code.strip().upper()))
        if record is None or float(record.expires_at) <= time.time():
            return False
        updated = _database().update_cli_device_authorization(
            record.device_code_hash,
            _identity_fields(identity) | {"status": "approved"},
            expected_status="pending",
            expected_consumed=False,
        )
        return updated is not None


def deny_device(user_code: str) -> bool:
    with _LOCK:
        if _use_memory_store():
            session = _DEVICES.get(_DEVICES_BY_USER_CODE.get(user_code.strip().upper(), ""))
            if session is None:
                return False
            session.status = "denied"
            return True
        record = _database().get_cli_device_authorization(user_code_hash=_hash(user_code.strip().upper()))
        if record is None or float(record.expires_at) <= time.time():
            return False
        return _database().update_cli_device_authorization(
            record.device_code_hash,
            {"status": "denied"},
            expected_status="pending",
            expected_consumed=False,
        ) is not None


def exchange_device(device_code: str) -> tuple[str, str, int] | None:
    with _LOCK:
        now = time.time()
        if _use_memory_store():
            _cleanup_memory(now)
            session = _DEVICES.get(device_code)
            if session is None or session.status != "approved" or session.consumed or session.identity is None:
                return None
            session.consumed = True
            refresh_token = secrets.token_urlsafe(48)
            access_token = secrets.token_urlsafe(48)
            identity = session.identity
            _REFRESH_TOKENS[refresh_token] = _RefreshSession(identity=identity, expires_at=now + REFRESH_LIFETIME_SECONDS)
            _ACCESS_TOKENS[access_token] = _AccessSession(
                identity=identity, expires_at=now + ACCESS_LIFETIME_SECONDS, refresh_token=refresh_token
            )
            return access_token, refresh_token, ACCESS_LIFETIME_SECONDS

        record = _database().get_cli_device_authorization(_hash(device_code))
        identity = _identity_from_record(record) if record is not None else None
        if (
            record is None
            or float(record.expires_at) <= now
            or str(record.status) != "approved"
            or bool(record.consumed)
            or identity is None
        ):
            return None
        consumed = _database().update_cli_device_authorization(
            record.device_code_hash,
            {"consumed": True},
            expected_status="approved",
            expected_consumed=False,
        )
        if consumed is None:
            return None
        return _issue_tokens(identity, now)


def _issue_tokens(identity: CliIdentity, now: float) -> tuple[str, str, int]:
    refresh_token = secrets.token_urlsafe(48)
    access_token = secrets.token_urlsafe(48)
    database = _database()
    refresh_hash = _hash(refresh_token)
    fields = _identity_fields(identity)
    database.insert_cli_token_session({
        "token_hash": refresh_hash,
        "token_type": "refresh",
        "refresh_token_hash": refresh_hash,
        **fields,
        "expires_at": now + REFRESH_LIFETIME_SECONDS,
        "revoked_at": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    })
    database.insert_cli_token_session({
        "token_hash": _hash(access_token),
        "token_type": "access",
        "refresh_token_hash": refresh_hash,
        **fields,
        "expires_at": now + ACCESS_LIFETIME_SECONDS,
        "revoked_at": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    })
    return access_token, refresh_token, ACCESS_LIFETIME_SECONDS


def refresh_access(refresh_token: str) -> tuple[str, str, int] | None:
    with _LOCK:
        now = time.time()
        if _use_memory_store():
            _cleanup_memory(now)
            session = _REFRESH_TOKENS.get(refresh_token)
            if session is None:
                return None
            access_token = secrets.token_urlsafe(48)
            _ACCESS_TOKENS[access_token] = _AccessSession(
                identity=session.identity, expires_at=now + ACCESS_LIFETIME_SECONDS, refresh_token=refresh_token
            )
            return access_token, refresh_token, ACCESS_LIFETIME_SECONDS
        record = _database().get_cli_token_session(_hash(refresh_token))
        identity = _identity_from_record(record) if record is not None else None
        if (
            record is None
            or str(record.token_type) != "refresh"
            or record.revoked_at is not None
            or float(record.expires_at) <= now
            or identity is None
        ):
            return None
        return _issue_access_token(identity, _hash(refresh_token), now), refresh_token, ACCESS_LIFETIME_SECONDS


def _issue_access_token(identity: CliIdentity, refresh_token_hash: str, now: float) -> str:
    access_token = secrets.token_urlsafe(48)
    _database().insert_cli_token_session({
        "token_hash": _hash(access_token),
        "token_type": "access",
        "refresh_token_hash": refresh_token_hash,
        **_identity_fields(identity),
        "expires_at": now + ACCESS_LIFETIME_SECONDS,
        "revoked_at": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    })
    return access_token


def resolve_access_token(access_token: str) -> Optional[CliIdentity]:
    with _LOCK:
        now = time.time()
        if _use_memory_store():
            _cleanup_memory(now)
            session = _ACCESS_TOKENS.get(access_token)
            return session.identity if session is not None else None
        record = _database().get_cli_token_session(_hash(access_token))
        if (
            record is None
            or str(record.token_type) != "access"
            or record.revoked_at is not None
            or float(record.expires_at) <= now
        ):
            return None
        return _identity_from_record(record)


def is_cli_access_token(access_token: str) -> bool:
    """Distinguish an invalid CLI token from an unrelated local-mode bearer value."""
    with _LOCK:
        if _use_memory_store():
            return access_token in _ACCESS_TOKENS or access_token in _REVOKED_ACCESS_TOKENS
        return _database().get_cli_token_session(_hash(access_token)) is not None


def revoke_tokens(refresh_token: str | None = None, access_token: str | None = None) -> None:
    with _LOCK:
        if _use_memory_store():
            if refresh_token:
                _REFRESH_TOKENS.pop(refresh_token, None)
            if access_token:
                _REVOKED_ACCESS_TOKENS.add(access_token)
                session = _ACCESS_TOKENS.pop(access_token, None)
                if session and not refresh_token:
                    _REFRESH_TOKENS.pop(session.refresh_token, None)
            if refresh_token:
                for token, session in list(_ACCESS_TOKENS.items()):
                    if session.refresh_token == refresh_token:
                        _REVOKED_ACCESS_TOKENS.add(token)
                        _ACCESS_TOKENS.pop(token, None)
            return
        database = _database()
        refresh_hash = _hash(refresh_token) if refresh_token else None
        if access_token and not refresh_hash:
            access_record = database.get_cli_token_session(_hash(access_token))
            refresh_hash = getattr(access_record, "refresh_token_hash", None)
        database.revoke_cli_token_sessions(
            token_hash=_hash(access_token) if access_token else None,
            refresh_token_hash=refresh_hash,
            revoked_at=time.time(),
        )


__all__ = [
    "CliIdentity",
    "approve_device",
    "create_device_session",
    "deny_device",
    "device_status",
    "exchange_device",
    "is_cli_access_token",
    "refresh_access",
    "resolve_access_token",
    "revoke_tokens",
]
