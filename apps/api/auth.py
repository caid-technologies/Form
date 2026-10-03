import base64
from collections import defaultdict, deque
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Set
from urllib import request as urllib_request

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

from apps.api.auth_mode import clerk_auth_required
from apps.api.cli_auth_store import is_cli_access_token, resolve_access_token
from forma_core.config import config
from forma_core.debug import new_error_correlation_id


LOCAL_USER_ID = "local-dev-user"
FORMA_A2A_API_KEY = "FORMA_A2A_API_KEY"
FORMA_MCP_API_KEY = "FORMA_MCP_API_KEY"
MIN_SERVICE_KEY_LENGTH = 32
MCP_SERVICE_PROVIDERS = frozenset({"a2a-api-key", "mcp-api-key"})
_DESTRUCTIVE_RATE_LOCK = threading.Lock()
_DESTRUCTIVE_RATE_EVENTS: Dict[str, deque[float]] = defaultdict(deque)


def _empty_claims() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class UserContext:
    """Provider-neutral identity resolved for one backend request."""

    provider: str
    subject: Optional[str]
    owner_user_id: Optional[str]
    is_authenticated: bool
    is_admin: bool
    claims: Mapping[str, Any] = field(default_factory=_empty_claims)

    def __post_init__(self) -> None:
        # Do not retain a caller-owned mutable claims mapping inside an otherwise
        # immutable request context.
        object.__setattr__(self, "claims", MappingProxyType(dict(self.claims)))


def _csv_env(name: str) -> Set[str]:
    return {
        item.strip()
        for item in (config.get(name) or "").replace("\n", ",").split(",")
        if item.strip()
    }


def deployed_auth_required() -> bool:
    """Backward-compatible name for the explicit Clerk auth-mode check."""
    return clerk_auth_required()


def _issuer_from_publishable_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    key = value.strip()
    for prefix in ("pk_test_", "pk_live_"):
        if not key.startswith(prefix):
            continue
        encoded = key.removeprefix(prefix)
        encoded = encoded.split("$", 1)[0]
        padding = "=" * (-len(encoded) % 4)
        try:
            decoded = base64.b64decode(f"{encoded}{padding}").decode("utf-8").strip().strip("$")
        except Exception:
            return None
        if not decoded:
            return None
        return decoded if decoded.startswith("https://") else f"https://{decoded}"
    return None


def clerk_issuer() -> Optional[str]:
    issuer = (
        config.get("CLERK_JWT_ISSUER")
        or config.get("CLERK_ISSUER")
        or config.get("CLERK_FRONTEND_API_URL")
        or _issuer_from_publishable_key(config.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY") or config.get("CLERK_PUBLISHABLE_KEY"))
    )
    if not issuer:
        return None
    return issuer.rstrip("/")


@lru_cache(maxsize=8)
def _jwk_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


def verify_clerk_bearer_token(token: str) -> Dict[str, Any]:
    issuer = clerk_issuer()
    if not issuer:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth is required, but Clerk issuer is not configured.",
        )
    try:
        signing_key = _jwk_client(f"{issuer}/.well-known/jwks.json").get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired Clerk session.") from exc


def _clerk_secret_key() -> Optional[str]:
    value = config.get("CLERK_SECRET_KEY")
    return value.strip() if value and value.strip() else None


def _primary_email_local_part(user: Dict[str, Any]) -> Optional[str]:
    email = _primary_email_address(user)
    if not email:
        return None
    local_part = email.split("@", 1)[0].strip()
    return local_part or None


def _primary_email_address(user: Dict[str, Any]) -> Optional[str]:
    primary_email_id = user.get("primary_email_address_id")
    email_addresses = user.get("email_addresses")
    if not isinstance(email_addresses, list):
        return None
    primary = None
    for item in email_addresses:
        if not isinstance(item, dict):
            continue
        if primary_email_id and item.get("id") == primary_email_id:
            primary = item
            break
        if primary is None:
            primary = item
    email = primary.get("email_address") if isinstance(primary, dict) else None
    if not isinstance(email, str) or "@" not in email:
        return None
    return email.strip().lower()


def _display_name_from_clerk_user(user: Dict[str, Any]) -> Optional[str]:
    username = user.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()

    first_name = user.get("first_name")
    last_name = user.get("last_name")
    full_name = " ".join(
        part.strip()
        for part in (first_name, last_name)
        if isinstance(part, str) and part.strip()
    )
    if full_name:
        return full_name

    return _primary_email_local_part(user)


def _github_username_from_clerk_user(user: Dict[str, Any]) -> Optional[str]:
    external_accounts = user.get("external_accounts")
    if not isinstance(external_accounts, list):
        return None
    for account in external_accounts:
        if not isinstance(account, dict):
            continue
        provider = str(account.get("provider") or "").strip().lower()
        username = account.get("username")
        if provider in {"github", "oauth_github"} and isinstance(username, str) and username.strip():
            return username.strip().lstrip("@") or None
    return None


@lru_cache(maxsize=512)
def clerk_user_profile(user_id: str) -> Optional[Dict[str, Optional[str]]]:
    normalized_user_id = str(user_id or "").strip()
    secret_key = _clerk_secret_key()
    if not normalized_user_id or not secret_key:
        return None

    url = f"https://api.clerk.com/v1/users/{normalized_user_id}"
    request = urllib_request.Request(
        url,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Accept": "application/json",
            "User-Agent": "Forma/1.0 (+https://github.com/caid-technologies/forma-oss)",
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    image_url = payload.get("image_url")
    return {
        "display_name": _display_name_from_clerk_user(payload),
        "email": _primary_email_address(payload),
        "github_username": _github_username_from_clerk_user(payload),
        "image_url": image_url.strip() if isinstance(image_url, str) and image_url.strip().startswith(("http://", "https://")) else None,
    }


def clerk_user_display_name(user_id: str) -> Optional[str]:
    profile = clerk_user_profile(user_id)
    return profile.get("display_name") if profile else None


def clerk_user_image_url(user_id: str) -> Optional[str]:
    profile = clerk_user_profile(user_id)
    return profile.get("image_url") if profile else None


def clerk_user_email(user_id: str) -> Optional[str]:
    profile = clerk_user_profile(user_id)
    return profile.get("email") if profile else None


def has_opencode_authoring_access(user: Optional[UserContext]) -> bool:
    """Allow signed-in user accounts to author their own projects."""
    if user is None or not user.is_authenticated or not user.owner_user_id:
        return False
    if user.provider == "local":
        return (config.get("FORMA_OPENCODE_ALLOW_LOCAL") or "").strip().lower() in {"1", "true", "yes", "on"}
    return user.provider in {"clerk", "forma-cli"}


async def require_opencode_authoring_access(request: Request) -> UserContext:
    """Require a signed-in hosted Clerk or Forma CLI user.

    All user accounts are eligible; service API keys do not count as sign-in.
    Project and session ownership are checked separately.
    """
    deployment = (config.get("FORMA_DEPLOYMENT_MODE") or "").strip().lower()
    try:
        clerk_required = deployed_auth_required()
    except RuntimeError:
        clerk_required = False
    local_allowed = (config.get("FORMA_OPENCODE_ALLOW_LOCAL") or "").strip().lower() in {"1", "true", "yes", "on"} and deployment == "local"
    if (deployment not in {"hosted", "production", "prod"} or not clerk_required) and not local_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_opencode_auth_error("opencode_hosted_only", "Hosted OpenCode access is unavailable."),
        )
    context = await require_user_context(request)
    if (context.provider not in {"clerk", "forma-cli"} and not local_allowed) or not has_opencode_authoring_access(context):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_opencode_auth_error("opencode_user_required", "A Clerk or Forma CLI user session is required."),
        )
    return context


def _opencode_auth_error(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message, "correlation_id": new_error_correlation_id()}


def _request_bearer_token(request: Any) -> Optional[str]:
    authorization = getattr(request, "headers", {}).get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    normalized = token.strip()
    return normalized or None


def clerk_user_id(auth_claims: Optional[Dict[str, Any]]) -> Optional[str]:
    if not auth_claims:
        return None
    value = auth_claims.get("sub")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def clerk_user_is_admin(auth_claims: Optional[Dict[str, Any]]) -> bool:
    if not deployed_auth_required():
        return True

    user_id = clerk_user_id(auth_claims)
    if not user_id:
        return False

    admin_user_ids = _csv_env("FORMA_ADMIN_USER_IDS") | _csv_env("CLERK_ADMIN_USER_IDS")
    if user_id in admin_user_ids:
        return True

    public_metadata = auth_claims.get("public_metadata") if isinstance(auth_claims, dict) else None
    private_metadata = auth_claims.get("private_metadata") if isinstance(auth_claims, dict) else None
    for metadata in (public_metadata, private_metadata):
        if isinstance(metadata, dict) and metadata.get("role") == "admin":
            return True
        if isinstance(metadata, dict) and metadata.get("admin") is True:
            return True

    admin_emails = {email.lower() for email in (_csv_env("FORMA_ADMIN_EMAILS") | _csv_env("CLERK_ADMIN_EMAILS"))}
    if not admin_emails:
        return False
    email = clerk_user_email(user_id)
    return bool(email and email.lower() in admin_emails)


def _local_user_context() -> UserContext:
    return UserContext(
        provider="local",
        subject=LOCAL_USER_ID,
        owner_user_id=LOCAL_USER_ID,
        is_authenticated=True,
        is_admin=True,
    )


def _anonymous_clerk_context() -> UserContext:
    return UserContext(
        provider="clerk",
        subject=None,
        owner_user_id=None,
        is_authenticated=False,
        is_admin=False,
    )


def _authenticated_clerk_context(auth_claims: Dict[str, Any]) -> UserContext:
    subject = clerk_user_id(auth_claims)
    return UserContext(
        provider="clerk",
        subject=subject,
        owner_user_id=subject,
        is_authenticated=subject is not None,
        is_admin=bool(subject and clerk_user_is_admin(auth_claims)),
        claims=auth_claims,
    )


async def optional_user_context(request: Request) -> UserContext:
    """Resolve a request identity without requiring the caller to be signed in."""
    token = _request_bearer_token(request)
    if token:
        cli_identity = resolve_access_token(token)
        if cli_identity is not None:
            claims = {
                "sub": cli_identity.subject,
                "email": cli_identity.email,
                "name": cli_identity.display_name,
            }
            return UserContext(
                provider="forma-cli",
                subject=cli_identity.subject,
                owner_user_id=cli_identity.subject,
                is_authenticated=True,
                is_admin=False,
                claims=claims,
            )
        if not deployed_auth_required() and is_cli_access_token(token):
            return _anonymous_clerk_context()

    if not deployed_auth_required():
        return _local_user_context()
    if not token:
        return _anonymous_clerk_context()
    return _authenticated_clerk_context(verify_clerk_bearer_token(token))


async def require_user_context(request: Request) -> UserContext:
    """Resolve a request identity and reject anonymous Clerk requests."""
    context = await optional_user_context(request)
    if context.is_authenticated:
        return context
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to use Forma generation.")


def _service_context_for_token(token: Optional[str], *, a2a: bool) -> Optional[UserContext]:
    """Resolve a configured service credential without treating it as a user session."""
    if not token:
        return None

    credentials = (
        (
            (FORMA_A2A_API_KEY, "a2a-api-key", "a2a-service", False),
            (FORMA_MCP_API_KEY, "mcp-api-key", "mcp-service", True),
        )
        if a2a
        else (
            (FORMA_MCP_API_KEY, "mcp-api-key", "mcp-service", True),
            (FORMA_A2A_API_KEY, "a2a-api-key", "a2a-service", False),
        )
    )
    for environment_name, provider, subject, is_admin in credentials:
        configured_key = (config.get(environment_name) or "").strip()
        if (
            len(configured_key) >= MIN_SERVICE_KEY_LENGTH
            and secrets.compare_digest(token, configured_key)
        ):
            return UserContext(
                provider=provider,
                subject=subject,
                owner_user_id=None,
                is_authenticated=True,
                is_admin=is_admin,
            )
    return None


def resolve_a2a_service_context(token: Optional[str]) -> Optional[UserContext]:
    """Resolve the service identity accepted by non-HTTP A2A transports."""
    normalized = (token or "").strip()
    if normalized.lower().startswith("bearer "):
        normalized = normalized[7:].strip()
    return _service_context_for_token(normalized or None, a2a=True)


def a2a_service_credentials_configured() -> bool:
    """Return whether an A2A-capable service credential is configured safely."""
    return any(
        len((config.get(name) or "").strip()) >= MIN_SERVICE_KEY_LENGTH
        for name in (FORMA_A2A_API_KEY, FORMA_MCP_API_KEY)
    )


def mcp_context_is_authorized(user_context: Optional[UserContext]) -> bool:
    """Return whether an already-resolved context may use MCP tools."""
    return bool(
        user_context
        and user_context.is_authenticated
        and (
            user_context.is_admin
            or user_context.provider in MCP_SERVICE_PROVIDERS
            or user_context.provider in {"clerk", "forma-cli"}
        )
    )


def a2a_local_development_allowed() -> bool:
    """Allow unauthenticated A2A only for explicitly local development."""
    try:
        return (
            (config.get("FORMA_DEPLOYMENT_MODE") or "").strip().lower() == "local"
            and (config.get("FORMA_AUTH_MODE") or "").strip().lower() == "local"
        )
    except RuntimeError:
        # Missing or invalid auth configuration must never fall back to local access.
        return False


def authenticated_principal(user_context: Optional[UserContext]) -> Optional[str]:
    """Return a stable, non-secret principal key for ownership checks."""
    if user_context is None or not user_context.is_authenticated:
        return None
    owner_user_id = str(user_context.owner_user_id or "").strip()
    if owner_user_id:
        raw_principal = f"user:{owner_user_id}"
    else:
        subject = str(user_context.subject or "").strip()
        provider = str(user_context.provider or "service").strip() or "service"
        raw_principal = f"service:{provider}:{subject}" if subject else f"service:{provider}"
    digest = hashlib.sha256(raw_principal.encode("utf-8")).hexdigest()
    return f"principal:{digest}"


async def require_a2a_user_context(request: Request) -> UserContext:
    """Require a user or scoped service identity for an HTTP A2A transport."""
    service_context = _service_context_for_token(_request_bearer_token(request), a2a=True)
    if service_context is not None:
        return service_context

    if not _clerk_auth_mode_enabled() and not a2a_local_development_allowed():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required for A2A transports.",
        )
    return await require_user_context(request)


async def require_a2a_websocket_context(websocket: Any) -> UserContext:
    """Resolve an authenticated identity from a WebSocket handshake."""
    service_context = _service_context_for_token(_request_bearer_token(websocket), a2a=True)
    if service_context is not None:
        return service_context

    if not _clerk_auth_mode_enabled() and not a2a_local_development_allowed():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required for A2A transports.",
        )
    context = await optional_user_context(websocket)
    if context.is_authenticated:
        return context
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication is required for A2A transports.",
    )


async def require_a2a_admin_user_context(request: Request) -> UserContext:
    """Require an administrator while preserving the A2A transport auth policy."""
    context = await require_a2a_user_context(request)
    if context.is_admin and not context.provider.endswith("-api-key"):
        return context
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required.")


def _clerk_auth_mode_enabled() -> bool:
    try:
        return clerk_auth_required()
    except RuntimeError:
        return False


async def require_recent_user_context(request: Request) -> UserContext:
    """Require a fresh session for destructive or consent-changing operations."""
    context = await require_user_context(request)
    if context.provider == "local":
        return context
    issued_at = context.claims.get("auth_time") or context.claims.get("iat")
    try:
        age_seconds = time.time() - float(issued_at)
        max_age_seconds = max(60, int(config.get("DESTRUCTIVE_AUTH_MAX_AGE_SECONDS", "600")))
    except (TypeError, ValueError):
        age_seconds = float("inf")
        max_age_seconds = 600
    if 0 <= age_seconds <= max_age_seconds:
        return context
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Recent authentication is required for this action. Sign in again and retry.",
    )


async def require_destructive_user_context(request: Request) -> UserContext:
    """Require recent authentication and apply a per-user destructive-action limit."""
    context = await require_recent_user_context(request)
    user_id = context.owner_user_id or context.subject or "anonymous"
    now = time.time()
    try:
        window_seconds = max(60, int(config.get("DESTRUCTIVE_RATE_LIMIT_WINDOW_SECONDS", "300")))
        request_limit = max(1, int(config.get("DESTRUCTIVE_RATE_LIMIT_REQUESTS", "20")))
    except ValueError:
        window_seconds, request_limit = 300, 20
    with _DESTRUCTIVE_RATE_LOCK:
        events = _DESTRUCTIVE_RATE_EVENTS[user_id]
        while events and events[0] <= now - window_seconds:
            events.popleft()
        if len(events) >= request_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many destructive actions. Wait and retry.",
            )
        events.append(now)
    return context


async def require_admin_user_context(request: Request) -> UserContext:
    """Resolve a request identity and require administrative access."""
    context = await require_user_context(request)
    if context.is_admin:
        return context
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required.")


async def require_mcp_user_context(request: Request) -> UserContext:
    """Authorize MCP with a dedicated API key or the normal admin identity."""
    service_context = _service_context_for_token(_request_bearer_token(request), a2a=False)
    if service_context is not None:
        return service_context
    if not _clerk_auth_mode_enabled() and not a2a_local_development_allowed():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required for MCP transports.",
        )
    return await require_user_context(request)


async def require_deployed_clerk_auth(request: Request) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper for routes that still consume raw Clerk claims."""
    if not deployed_auth_required():
        return None

    context = await optional_user_context(request)
    if _request_bearer_token(request):
        return dict(context.claims)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to use Forma generation.")


async def optional_deployed_clerk_auth(request: Request) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper for routes that still consume raw Clerk claims."""
    context = await optional_user_context(request)
    if not deployed_auth_required() or context.provider == "local" or not _request_bearer_token(request):
        return None
    return dict(context.claims)


async def require_deployed_admin_auth(request: Request) -> Optional[Dict[str, Any]]:
    auth_claims = await require_deployed_clerk_auth(request)
    if clerk_user_is_admin(auth_claims):
        return auth_claims
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required.")
