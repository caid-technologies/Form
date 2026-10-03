"""OS credential-store integration for CLI and local provider secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import warnings

from forma_cli.config import config_dir


SERVICE_NAME = "forma-oss"
SESSION_KEY = "cli-session"
_PLAINTEXT_ENV = "FORMA_ALLOW_PLAINTEXT_CREDENTIALS"


class CredentialStoreError(RuntimeError):
    """Raised when secure credential storage cannot be used."""


def _plaintext_enabled() -> bool:
    return os.environ.get(_PLAINTEXT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _keyring_module():
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreError(
            "The OS credential store is unavailable because keyring is not installed. "
            f"Install the CLI dependencies or explicitly set {_PLAINTEXT_ENV}=1 for a warned plaintext fallback."
        ) from exc
    return keyring


def _plaintext_path() -> Path:
    configured = os.environ.get("FORMA_CLI_PLAINTEXT_CREDENTIALS_PATH")
    if configured:
        return Path(configured).expanduser()
    return config_dir() / "credentials.json"


class CredentialStore:
    """Store values in keyring, with an opt-in insecure fallback for CI/dev."""

    def __init__(self, *, service: str = SERVICE_NAME, keyring_backend: Any = None) -> None:
        self.service = service
        self._keyring_backend = keyring_backend
        if _plaintext_enabled():
            warnings.warn(
                f"{_PLAINTEXT_ENV}=1 is enabled; Forma CLI credentials are being stored in plaintext.",
                UserWarning,
                stacklevel=2,
            )

    @property
    def using_plaintext_fallback(self) -> bool:
        return _plaintext_enabled()

    def _backend(self) -> Any:
        return self._keyring_backend or _keyring_module()

    def _load_plaintext(self) -> dict[str, str]:
        path = _plaintext_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialStoreError(f"Could not read plaintext credential fallback: {path}") from exc
        return {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}

    def _save_plaintext(self, values: dict[str, str]) -> None:
        path = _plaintext_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        if _plaintext_enabled():
            return self._load_plaintext().get(name)
        try:
            return self._backend().get_password(self.service, name)
        except Exception as exc:
            raise CredentialStoreError(
                "The OS credential store rejected the credential lookup. "
                f"Set {_PLAINTEXT_ENV}=1 only if an explicit insecure fallback is acceptable."
            ) from exc

    def set(self, name: str, value: str) -> None:
        if not str(value).strip():
            raise CredentialStoreError("Credential value cannot be empty.")
        if _plaintext_enabled():
            values = self._load_plaintext()
            values[name] = value
            self._save_plaintext(values)
            return
        try:
            self._backend().set_password(self.service, name, value)
        except Exception as exc:
            raise CredentialStoreError(
                "The OS credential store rejected the credential write. "
                f"Set {_PLAINTEXT_ENV}=1 only if an explicit insecure fallback is acceptable."
            ) from exc

    def delete(self, name: str) -> None:
        if _plaintext_enabled():
            values = self._load_plaintext()
            values.pop(name, None)
            self._save_plaintext(values)
            return
        try:
            self._backend().delete_password(self.service, name)
        except Exception as exc:
            # keyring backends commonly report missing entries as errors; deletion is idempotent.
            message = str(exc).lower()
            if not any(token in message for token in ("not found", "does not exist", "no such", "missing")):
                raise CredentialStoreError("The OS credential store rejected credential removal.") from exc

    def get_json(self, name: str) -> dict[str, Any] | None:
        raw = self.get(name)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise CredentialStoreError(f"Stored credential {name!r} is invalid.") from exc
        return payload if isinstance(payload, dict) else None

    def set_json(self, name: str, value: dict[str, Any]) -> None:
        self.set(name, json.dumps(value, sort_keys=True))


__all__ = ["CredentialStore", "CredentialStoreError", "SERVICE_NAME", "SESSION_KEY"]
