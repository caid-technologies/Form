"""Typed, dependency-light Forma API client used by the CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from forma_cli.config import api_url
from forma_cli.credentials import CredentialStore, SESSION_KEY
from forma_core._version import __version__
from forma_core.config.compatibility import (
    CURRENT_PROTOCOL_VERSION,
    CompatibilityMetadata,
    CompatibilityResult,
    CompatibilityStatus,
    ensure_supported_hardware_ir_version,
    evaluate_compatibility,
    unavailable_result,
)


class FormaAPIError(RuntimeError):
    """An HTTP or transport failure returned by the Forma API."""

    def __init__(self, message: str, *, status_code: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class TokenSet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(default=3600, ge=1)


class DeviceAuthorization(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = Field(ge=1)
    interval: int = Field(default=5, ge=1)


class DevicePollResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    message: str | None = None


class AccountIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    subject: str
    provider: str
    email: str | None = None
    display_name: str | None = None
    api_url: str


class CloudProjectSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    owner_user_id: str | None = None
    workspace_id: str | None = None
    creation_channel: str = "cli"
    title: str = ""
    prompt: str = ""
    visibility: str = "public"
    status: str = "active"
    revision_id: str | None = None
    revision: int | None = None
    parent_revision_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CloudProjectRevision(BaseModel):
    model_config = ConfigDict(extra="allow")

    revision_id: str
    project_id: str
    revision: int
    parent_revision_id: str | None = None
    manifest: dict[str, Any]
    created_at: str | None = None


class DeliveryProjectRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    revision_id: str
    revision: int
    parent_revision_id: str | None = None
    visibility: str = "private"


class DeliveryArtifactStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str | None = None
    sha256: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    status: str = "pending"


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")

    delivery_id: str
    status: str
    project: DeliveryProjectRef
    artifacts: list[DeliveryArtifactStatus] = field(default_factory=list)
    artifact_summary: dict[str, int] = {}
    created_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class ProjectArtifactDownload:
    """Binary artifact response returned by a cloud project revision."""

    content: bytes
    content_type: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


class ManagedCredentialMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    credential_id: str | None = None
    label: str | None = None
    masked_value: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    configured: bool = False


@dataclass
class FormaAPIClient:
    """Call Forma endpoints without importing backend handlers or Supabase."""

    base_url: str | None = None
    credential_store: CredentialStore | None = None
    timeout_seconds: float = 30.0
    _compatibility_result: CompatibilityResult | None = field(default=None, init=False, repr=False)
    _compatibility_notice_emitted: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or api_url()).rstrip("/")
        self.credential_store = self.credential_store or CredentialStore()

    @property
    def _store(self) -> CredentialStore:
        assert self.credential_store is not None
        return self.credential_store

    def _saved_tokens(self) -> TokenSet | None:
        payload = self._store.get_json(SESSION_KEY)
        return TokenSet.model_validate(payload) if payload else None

    def _save_tokens(self, tokens: TokenSet) -> TokenSet:
        self._store.set_json(SESSION_KEY, tokens.model_dump(mode="json"))
        return tokens

    @staticmethod
    def _is_local_endpoint(base_url: str) -> bool:
        host = (urlparse(base_url).hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")

    def check_compatibility(self, *, force: bool = False) -> CompatibilityResult:
        """Fetch and cache hosted compatibility metadata for this client."""
        if self._compatibility_result is not None and not force:
            return self._compatibility_result
        if self._is_local_endpoint(self.base_url or ""):
            result = unavailable_result(__version__)
        else:
            try:
                raw, _headers = self._request_wire(
                    "/forma/version",
                    authenticated=False,
                    retry_refresh=False,
                    _skip_compatibility=True,
                )
                metadata = CompatibilityMetadata.model_validate(self._decode_json(raw))
                result = evaluate_compatibility(
                    __version__,
                    metadata,
                    client_protocol_version=CURRENT_PROTOCOL_VERSION,
                )
            except (FormaAPIError, ValueError):
                result = unavailable_result(__version__)
        self._compatibility_result = result
        return result

    def compatibility_notice(self) -> str | None:
        """Return the update notice once per client instance."""
        result = self.check_compatibility()
        if result.status != CompatibilityStatus.UPDATE_AVAILABLE or self._compatibility_notice_emitted:
            return None
        self._compatibility_notice_emitted = True
        return f"WARNING: {result.message}"

    def _request_wire(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
        authenticated: bool = False,
        retry_refresh: bool = True,
        _skip_compatibility: bool = False,
    ) -> tuple[bytes, dict[str, str]]:
        if payload is not None and body is not None:
            raise ValueError("A Forma API request cannot contain both JSON and binary payloads.")
        if not _skip_compatibility:
            result = self.check_compatibility()
            if result.is_blocking:
                raise FormaAPIError(result.message, status_code=426, detail=result.model_dump(mode="json"))
        headers = {
            "Accept": accept,
            "User-Agent": f"forma-oss-cli/{__version__}",
            "X-Forma-Client-Version": __version__,
            "X-Forma-Protocol-Version": str(CURRENT_PROTOCOL_VERSION),
        }
        tokens = self._saved_tokens() if authenticated else None
        if authenticated:
            if tokens is None:
                raise FormaAPIError("Sign in with `forma-oss login` before using cloud commands.")
            headers["Authorization"] = f"{tokens.token_type} {tokens.access_token}"
        request_body = json.dumps(dict(payload)).encode("utf-8") if payload is not None else body
        if request_body is not None:
            headers["Content-Type"] = content_type or "application/json"
        request = Request(f"{self.base_url}/{path.lstrip('/')}", data=request_body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                response_headers = {
                    str(key).lower(): str(value)
                    for key, value in getattr(response, "headers", {}).items()
                }
        except HTTPError as exc:
            raw = exc.read()

            try:
                detail = self._decode_json(raw)
            except FormaAPIError:
                detail = {
                    "raw_body": raw.decode("utf-8", errors="replace").strip() or None,
                    "content_type": exc.headers.get("Content-Type"),
                }

            if (
                exc.code == 401
                and authenticated
                and retry_refresh
                and tokens
                and tokens.refresh_token
            ):
                try:
                    self.refresh(tokens.refresh_token)
                except FormaAPIError:
                    pass
                else:
                    return self._request_wire(
                        path,
                        method=method,
                        payload=payload,
                        body=body,
                        content_type=content_type,
                        accept=accept,
                        authenticated=authenticated,
                        retry_refresh=False,
                        _skip_compatibility=_skip_compatibility,
                    )

            raise FormaAPIError(
                self._error_message(
                    detail,
                    fallback=f"Forma API request failed ({exc.code}).",
                ),
                status_code=exc.code,
                detail=detail,
            ) from exc
        except URLError as exc:
            raise FormaAPIError(f"Could not reach Forma API at {self.base_url}: {exc.reason}") from exc
        except OSError as exc:
            raise FormaAPIError(f"Could not reach Forma API at {self.base_url}: {exc}") from exc
        return raw, response_headers

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        authenticated: bool = False,
        retry_refresh: bool = True,
    ) -> Any:
        raw, _headers = self._request_wire(
            path,
            method=method,
            payload=payload,
            authenticated=authenticated,
            retry_refresh=retry_refresh,
        )
        return self._decode_json(raw)

    def _request_bytes(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
        authenticated: bool = False,
        retry_refresh: bool = True,
    ) -> ProjectArtifactDownload:
        raw, headers = self._request_wire(
            path,
            method=method,
            body=body,
            content_type=content_type,
            accept="application/octet-stream",
            authenticated=authenticated,
            retry_refresh=retry_refresh,
        )
        try:
            size_bytes = int(headers["x-forma-artifact-size"]) if headers.get("x-forma-artifact-size") else None
        except ValueError:
            size_bytes = None
        return ProjectArtifactDownload(
            content=raw,
            content_type=headers.get("content-type"),
            sha256=headers.get("x-forma-artifact-sha256"),
            size_bytes=size_bytes,
        )

    @staticmethod
    def _decode_json(raw: bytes) -> Any:
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise FormaAPIError("Forma API returned an invalid JSON response.") from exc

    @staticmethod
    def _error_message(detail: Any, *, fallback: str) -> str:
        if isinstance(detail, dict):
            nested = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
            message = nested.get("message") or nested.get("detail")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return fallback

    def request_device_authorization(self) -> DeviceAuthorization:
        return DeviceAuthorization.model_validate(
            self._request("/cli/device/authorize", method="POST", payload={"client_name": "forma-oss"})
        )

    def poll_device_authorization(self, device_code: str) -> DevicePollResult:
        return DevicePollResult.model_validate(
            self._request("/cli/device/poll", method="POST", payload={"device_code": device_code})
        )

    def exchange_device_code(self, device_code: str) -> TokenSet:
        return self._save_tokens(
            TokenSet.model_validate(
                self._request("/cli/device/token", method="POST", payload={"device_code": device_code})
            )
        )

    def refresh(self, refresh_token: str | None = None) -> TokenSet:
        token = refresh_token or (self._saved_tokens().refresh_token if self._saved_tokens() else "")
        if not token:
            raise FormaAPIError("No refresh token is available; sign in again.")
        return self._save_tokens(
            TokenSet.model_validate(
                self._request("/cli/token/refresh", method="POST", payload={"refresh_token": token})
            )
        )

    def revoke(self) -> None:
        tokens = self._saved_tokens()
        if tokens:
            try:
                self._request(
                    "/cli/token/revoke",
                    method="POST",
                    payload={"refresh_token": tokens.refresh_token},
                    authenticated=True,
                    retry_refresh=False,
                )
            finally:
                self._store.delete(SESSION_KEY)

    def whoami(self) -> AccountIdentity:
        return AccountIdentity.model_validate(self._request("/cli/whoami", authenticated=True))

    def list_projects(self) -> list[CloudProjectSummary]:
        payload = self._request("/cli/projects", authenticated=True)
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        return [CloudProjectSummary.model_validate(item) for item in items or []]

    def push_project(
        self,
        manifest: Mapping[str, Any],
        *,
        parent_revision_id: str | None = None,
        idempotency_key: str | None = None,
        visibility: str | None = None,
    ) -> DeliveryReceipt:
        ensure_supported_hardware_ir_version(manifest)
        body: dict[str, Any] = {"manifest": dict(manifest)}
        if parent_revision_id is not None:
            body["parent_revision_id"] = parent_revision_id
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        if visibility is not None:
            body["visibility"] = visibility
        payload = self._request(
            "/cli/projects/push",
            method="POST",
            payload=body,
            authenticated=True,
        )
        return DeliveryReceipt.model_validate(payload)

    def upload_project_artifact(
        self,
        project_id: str,
        revision_id: str,
        sha256: str,
        content: bytes,
        media_type: str,
    ) -> dict[str, Any]:
        path = (
            f"/cli/projects/{quote(project_id, safe='')}/revisions/"
            f"{quote(revision_id, safe='')}/artifacts/{quote(sha256, safe='')}"
        )
        raw, _headers = self._request_wire(
            path,
            method="PUT",
            body=content,
            content_type=media_type,
            authenticated=True,
        )
        payload = self._decode_json(raw)
        return payload if isinstance(payload, dict) else {}

    def download_project_artifact(
        self,
        project_id: str,
        revision_id: str,
        sha256: str,
    ) -> ProjectArtifactDownload:
        path = (
            f"/cli/projects/{quote(project_id, safe='')}/revisions/"
            f"{quote(revision_id, safe='')}/artifacts/{quote(sha256, safe='')}"
        )
        return self._request_bytes(path, authenticated=True)

    def pull_project(self, project_id: str, revision_id: str | None = None) -> CloudProjectRevision:
        path = f"/cli/projects/{quote(project_id, safe='')}"
        if revision_id:
            path += f"/revisions/{quote(revision_id, safe='')}"
        payload = self._request(path, authenticated=True)
        return CloudProjectRevision.model_validate(payload)

    def deliver_project(
        self,
        manifest: Mapping[str, Any],
        *,
        idempotency_key: str,
        parent_revision_id: str | None = None,
        visibility: str | None = None,
    ) -> DeliveryReceipt:
        ensure_supported_hardware_ir_version(manifest)
        body: dict[str, Any] = {
            "manifest": dict(manifest),
            "idempotency_key": idempotency_key,
        }
        if parent_revision_id is not None:
            body["parent_revision_id"] = parent_revision_id
        if visibility is not None:
            body["visibility"] = visibility
        payload = self._request(
            "/cli/projects/deliver",
            method="POST",
            payload=body,
            authenticated=True,
        )
        return DeliveryReceipt.model_validate(payload)

    def complete_delivery(self, delivery_id: str) -> DeliveryReceipt:
        payload = self._request(
            f"/cli/projects/deliver/{quote(delivery_id, safe='')}/complete",
            method="POST",
            payload={},
            authenticated=True,
        )
        return DeliveryReceipt.model_validate(payload)

    def publish_project(self, project_id: str) -> dict[str, Any]:
        payload = self._request(
            f"/cli/projects/{quote(project_id, safe='')}/publish",
            method="POST",
            payload={},
            authenticated=True,
        )
        return payload if isinstance(payload, dict) else {}

    def list_keys(self) -> list[ManagedCredentialMetadata]:
        payload = self._request("/cli/credentials", authenticated=True)
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        return [ManagedCredentialMetadata.model_validate(item) for item in items or []]

    def set_key(self, provider: str, value: str, *, label: str | None = None) -> ManagedCredentialMetadata:
        payload = self._request(
            "/cli/credentials",
            method="POST",
            payload={"provider": provider, "value": value, "label": label},
            authenticated=True,
        )
        return ManagedCredentialMetadata.model_validate(payload)

    def remove_key(self, provider: str) -> None:
        self._request(f"/cli/credentials/{quote(provider, safe='')}", method="DELETE", authenticated=True)


__all__ = [
    "AccountIdentity",
    "CloudProjectRevision",
    "CloudProjectSummary",
    "CompatibilityMetadata",
    "CompatibilityResult",
    "CompatibilityStatus",
    "DeliveryArtifactStatus",
    "DeliveryProjectRef",
    "DeliveryReceipt",
    "DeviceAuthorization",
    "DevicePollResult",
    "FormaAPIClient",
    "FormaAPIError",
    "ManagedCredentialMetadata",
    "ProjectArtifactDownload",
    "TokenSet",
]
