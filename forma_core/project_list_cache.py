"""Redis-backed cache for project gallery response payloads.

The cache is deliberately optional in development. It supports either a Redis
connection URL or Upstash's HTTP API, and Redis errors never fail project reads
or writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Optional
from urllib import request

from forma_core.config import config
from forma_core.config.runtime import forma_dev_mode_enabled


logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 60
_DEFAULT_SOCKET_TIMEOUT_SECONDS = 0.25
_FAILURE_COOLDOWN_SECONDS = 30.0
_VERSION_KEY_SUFFIX = "version"

_client: Any = None
_client_identity: Optional[tuple[str, ...]] = None
_client_lock = threading.Lock()
_disabled_until = 0.0


class _UpstashRedisRestClient:
    """Minimal Redis command client for Upstash's REST endpoint."""

    def __init__(self, url: str, token: str, timeout: float) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _command(self, *parts: str) -> Any:
        body = json.dumps(list(parts), separators=(",", ":")).encode("utf-8")
        redis_request = request.Request(
            self._url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(redis_request, timeout=self._timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Upstash Redis returned an invalid response")
        if payload.get("error"):
            raise RuntimeError(f"Upstash Redis command failed: {payload['error']}")
        return payload.get("result")

    def get(self, key: str) -> Any:
        return self._command("GET", key)

    def set(self, key: str, value: str, *, ex: int) -> Any:
        return self._command("SET", key, value, "EX", str(ex))

    def incr(self, key: str) -> Any:
        return self._command("INCR", key)


def require_project_list_cache_config() -> None:
    """Require explicit Redis settings outside development mode."""
    if forma_dev_mode_enabled():
        return

    redis_url = config.get("REDIS_URL", "").strip()
    upstash_url = config.get("UPSTASH_REDIS_REST_URL", "").strip()
    upstash_token = config.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    cache_prefix = config.get("REDIS_CACHE_PREFIX", "").strip()
    if cache_prefix and (redis_url or (upstash_url and upstash_token)):
        return

    message = (
        "FORMA_DEV_MODE=false requires Redis project caching. "
        "Set REDIS_CACHE_PREFIX and either REDIS_URL or both "
        "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN as server-only "
        "environment variables."
    )
    logger.critical(message)
    raise RuntimeError(message)


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _cache_ttl_seconds() -> int:
    return int(_bounded_float("PROJECTS_CACHE_TTL_SECONDS", _DEFAULT_TTL_SECONDS, 1, 3600))


def _cache_prefix() -> str:
    configured = config.get("REDIS_CACHE_PREFIX", "forma").strip().strip(":")
    return configured or "forma"


def _version_key() -> str:
    return f"{_cache_prefix()}:project-lists:{_VERSION_KEY_SUFFIX}"


def _get_redis_client() -> Any:
    """Return a lazy Redis client without importing redis for uncached installs."""
    global _client, _client_identity

    redis_url = config.get("REDIS_URL", "").strip()
    upstash_url = config.get("UPSTASH_REDIS_REST_URL", "").strip()
    upstash_token = config.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if redis_url:
        identity = ("redis", redis_url)
    elif upstash_url and upstash_token:
        identity = ("upstash", upstash_url, upstash_token)
    else:
        return None
    if _client is not None and _client_identity == identity:
        return _client

    with _client_lock:
        if _client is not None and _client_identity == identity:
            return _client
        timeout = _bounded_float(
            "REDIS_SOCKET_TIMEOUT_SECONDS",
            _DEFAULT_SOCKET_TIMEOUT_SECONDS,
            0.05,
            5.0,
        )
        if identity[0] == "upstash":
            _client = _UpstashRedisRestClient(upstash_url, upstash_token, timeout)
            _client_identity = identity
            return _client
        try:
            import redis
        except ImportError:
            logger.warning(
                "REDIS_URL is configured but the redis package is not installed; "
                "project caching is disabled."
            )
            return None

        _client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            health_check_interval=30,
        )
        _client_identity = identity
        return _client


def _available_client() -> Any:
    if time.monotonic() < _disabled_until:
        return None
    return _get_redis_client()


def _record_failure(operation: str, exc: Exception) -> None:
    global _disabled_until
    _disabled_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
    logger.warning(
        "Redis project cache %s failed; using the database for %.0f seconds: %s",
        operation,
        _FAILURE_COOLDOWN_SECONDS,
        exc,
    )


def _current_generation(client: Any) -> str:
    value = client.get(_version_key())
    return str(value) if value is not None else "0"


def _identity_key(owner_user_id: Optional[str]) -> str:
    identity = owner_user_id.strip() if isinstance(owner_user_id, str) and owner_user_id.strip() else "anonymous"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _data_key(scope: str, owner_user_id: Optional[str], generation: str) -> str:
    normalized_scope = "mine" if scope == "mine" else "public"
    return f"{_cache_prefix()}:project-lists:v{generation}:{normalized_scope}:{_identity_key(owner_user_id)}"


def get_cached_project_list(
    scope: str,
    owner_user_id: Optional[str],
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Return a cached response and the generation token used for that lookup."""
    client = _available_client()
    if client is None:
        return None, None
    try:
        generation = _current_generation(client)
        raw_value = client.get(_data_key(scope, owner_user_id, generation))
        if raw_value is None:
            return None, generation
        decoded = json.loads(raw_value)
        if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
            raise ValueError("cached project list has an invalid shape")
        return decoded, generation
    except Exception as exc:
        _record_failure("read", exc)
        return None, None


def cache_project_list(
    scope: str,
    owner_user_id: Optional[str],
    projects: list[dict[str, Any]],
    generation: Optional[str],
) -> None:
    """Cache a project list under the generation observed before its DB read.

    Reusing the lookup generation prevents a concurrent write from placing a
    stale database result into the newly invalidated cache generation.
    """
    if generation is None:
        return
    client = _available_client()
    if client is None:
        return
    try:
        client.set(
            _data_key(scope, owner_user_id, generation),
            json.dumps(projects, separators=(",", ":"), ensure_ascii=False),
            ex=_cache_ttl_seconds(),
        )
    except Exception as exc:
        _record_failure("write", exc)



def _page_data_key(
    scope: str, owner_user_id: Optional[str], generation: str,
    limit: int, offset: int, search: Optional[str],
) -> str:
    # Keep search text and user identifiers out of Redis keys. Callers pass the
    # same normalized pagination/search values to both the cache and the DB.
    query_digest = hashlib.sha256((search or "").encode("utf-8")).hexdigest()
    return f"{_data_key(scope, owner_user_id, generation)}:page:{limit}:{offset}:{query_digest}"


def get_cached_project_page(
    scope: str, owner_user_id: Optional[str], *, limit: int, offset: int,
    search: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Read an unpersonalized page using the existing list invalidation token."""
    client = _available_client()
    if client is None:
        return None, None
    try:
        generation = _current_generation(client)
        raw_value = client.get(_page_data_key(scope, owner_user_id, generation, limit, offset, search))
        if raw_value is None:
            return None, generation
        decoded = json.loads(raw_value)
        if (
            not isinstance(decoded, dict)
            or not isinstance(decoded.get("items"), list)
            or any(not isinstance(item, dict) for item in decoded["items"])
            or type(decoded.get("total")) is not int
            or decoded["total"] < 0
        ):
            raise ValueError("cached project page has an invalid shape")
        return decoded, generation
    except Exception as exc:
        _record_failure("page read", exc)
        return None, None


def cache_project_page(
    scope: str, owner_user_id: Optional[str], items: list[dict[str, Any]],
    total: int, generation: Optional[str], *, limit: int, offset: int,
    search: Optional[str] = None,
) -> None:
    """Cache base records, never response-specific saved/owner capabilities.

    A concurrent invalidation must leave this write in the OLD generation,
    just like cache_project_list. All existing list invalidations cover pages.
    """
    if generation is None:
        return
    client = _available_client()
    if client is None:
        return
    try:
        client.set(
            _page_data_key(scope, owner_user_id, generation, limit, offset, search),
            json.dumps({"items": items, "total": total}, separators=(",", ":"), ensure_ascii=False),
            ex=_cache_ttl_seconds(),
        )
    except Exception as exc:
        _record_failure("page write", exc)


def invalidate_project_lists() -> None:
    """Move all project-list readers to a fresh cache generation."""
    client = _available_client()
    if client is None:
        return
    try:
        client.incr(_version_key())
    except Exception as exc:
        _record_failure("invalidation", exc)
