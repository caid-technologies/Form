"""HTTP security policy shared by browser and agent-facing API routes."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from forma_core.config import config


logger = logging.getLogger(__name__)

MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024
MAX_PROMPT_CHARS = 12_000
MAX_IMAGE_ENCODED_CHARS = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096


def cors_origins() -> list[str]:
    """Resolve an explicit browser origin policy; never return wildcard origins."""
    configured = config.get("FORMA_CORS_ORIGINS")
    if configured is None or not configured.strip():
        deployment = (config.get("FORMA_DEPLOYMENT_MODE") or "local").strip().lower()
        return [] if deployment in {"hosted", "production", "prod"} else [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    origins = [item.strip().rstrip("/") for item in configured.replace("\n", ",").split(",") if item.strip()]
    if "*" in origins:
        raise RuntimeError("FORMA_CORS_ORIGINS cannot contain '*' when credentialed CORS is enabled.")
    return origins


def _integer(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(config.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def consume(self, key: str, *, limit: int, window_seconds: int, cost: int = 1) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            cutoff = now - window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) + cost > limit:
                retry_after = max(1, int(events[0] + window_seconds - now)) if events else window_seconds
                return False, retry_after
            for _ in range(cost):
                events.append(now)
            return True, 0


_LIMITER = SlidingWindowLimiter()


def _request_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization:
        identity = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        return f"credential:{identity}"
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return f"ip:{forwarded or (request.client.host if request.client else 'unknown')}"


def _limit_for_path(path: str) -> tuple[str, int, int, int] | None:
    if path == "/api" or path.startswith("/api/"):
        path = path[4:] or "/"
    if path == "/generate":
        return "generation", _integer("FORMA_GENERATION_RATE_LIMIT", 10), _integer("FORMA_GENERATION_RATE_WINDOW_SECONDS", 60), 1
    if path in {"/mcp", "/a2a/mcp"} or path.endswith("/mcp"):
        return "mcp", _integer("FORMA_MCP_RATE_LIMIT", 60), _integer("FORMA_MCP_RATE_WINDOW_SECONDS", 60), 1
    if path in {"/validate", "/validate-circuit"}:
        return "validation", _integer("FORMA_VALIDATION_RATE_LIMIT", 60), _integer("FORMA_VALIDATION_RATE_WINDOW_SECONDS", 60), 1
    return None


def consume_operation_limit(category: str, identity: str, *, cost: int = 1) -> None:
    """Apply limits to WebSocket/TCP actions that do not pass HTTP middleware."""
    if category == "generation":
        rate, window = _integer("FORMA_GENERATION_RATE_LIMIT", 10), _integer("FORMA_GENERATION_RATE_WINDOW_SECONDS", 60)
        quota, quota_window = _integer("FORMA_GENERATION_QUOTA", 100), _integer("FORMA_GENERATION_QUOTA_WINDOW_SECONDS", 86400)
    elif category == "validation":
        rate, window, quota, quota_window = 60, 60, 1000, 86400
    else:
        rate, window, quota, quota_window = 60, 60, 1000, 86400
    allowed, retry_after = _LIMITER.consume(f"{category}:rate:{identity}", limit=rate, window_seconds=window, cost=cost)
    if not allowed:
        raise ValueError(f"Rate limit exceeded; retry after {retry_after} seconds.")
    allowed, retry_after = _LIMITER.consume(f"{category}:quota:{identity}", limit=quota, window_seconds=quota_window, cost=cost)
    if not allowed:
        raise ValueError(f"Usage quota exceeded; retry after {retry_after} seconds.")


class SecurityLimitsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _integer("FORMA_MAX_REQUEST_BODY_BYTES", MAX_REQUEST_BODY_BYTES):
                    return _error(413, "request_too_large", "Request body exceeds the configured limit.")
            except ValueError:
                return _error(400, "invalid_content_length", "Request content length is invalid.")

        policy = _limit_for_path(request.url.path)
        if policy:
            name, limit, window, cost = policy
            identity = _request_key(request)
            allowed, retry_after = _LIMITER.consume(f"{name}:{identity}", limit=limit, window_seconds=window, cost=cost)
            if not allowed:
                logger.warning("Security limit exceeded: category=%s path=%s", name, request.url.path)
                response = _error(429, "rate_limit_exceeded", "Too many requests. Retry later.")
                response.headers["Retry-After"] = str(retry_after)
                return response
            if name == "generation":
                quota, quota_window = _integer("FORMA_GENERATION_QUOTA", 100), _integer("FORMA_GENERATION_QUOTA_WINDOW_SECONDS", 86400)
                allowed, retry_after = _LIMITER.consume(
                    f"{name}:quota:{identity}", limit=quota, window_seconds=quota_window, cost=cost
                )
                if not allowed:
                    logger.warning("Security quota exceeded: category=%s path=%s", name, request.url.path)
                    response = _error(429, "usage_quota_exceeded", "Usage quota exceeded. Retry later.")
                    response.headers["Retry-After"] = str(retry_after)
                    return response

        response = await call_next(request)
        if policy:
            response.headers["X-RateLimit-Policy"] = policy[0]
        return response


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


def validate_image_limits(encoded: str) -> tuple[bytes, str]:
    """Decode and validate an inline image before it reaches storage or a provider."""
    import base64
    import binascii

    if len(encoded) > _integer("FORMA_MAX_IMAGE_ENCODED_CHARS", MAX_IMAGE_ENCODED_CHARS):
        raise ValueError("Reference image exceeds the configured encoded size limit.")
    raw = encoded.strip()
    mime = "image/png"
    if "," in raw:
        header, raw = raw.split(",", 1)
        if not header.lower().startswith("data:") or ";base64" not in header.lower():
            raise ValueError("Reference image must be a base64 data URL.")
        mime = header.split(";", 1)[0][5:].lower()
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("Reference image MIME type is not supported.")
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Reference image could not be decoded.") from exc
    if len(decoded) > _integer("FORMA_MAX_IMAGE_BYTES", MAX_IMAGE_BYTES):
        raise ValueError("Reference image exceeds the configured decoded size limit.")
    try:
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(decoded)) as image:
            image.verify()
        with Image.open(BytesIO(decoded)) as image:
            max_dimension = _integer("FORMA_MAX_IMAGE_DIMENSION", MAX_IMAGE_DIMENSION)
            if image.width > max_dimension or image.height > max_dimension:
                raise ValueError("Reference image dimensions exceed the configured limit.")
    except ImportError:
        logger.warning("Pillow is unavailable; image dimension validation was skipped.")
    except (OSError, SyntaxError) as exc:
        raise ValueError("Reference image is invalid.") from exc
    return decoded, mime


def security_config() -> dict[str, Any]:
    return {
        "max_request_body_bytes": _integer("FORMA_MAX_REQUEST_BODY_BYTES", MAX_REQUEST_BODY_BYTES),
        "max_prompt_chars": _integer("FORMA_MAX_PROMPT_CHARS", MAX_PROMPT_CHARS),
        "max_image_encoded_chars": _integer("FORMA_MAX_IMAGE_ENCODED_CHARS", MAX_IMAGE_ENCODED_CHARS),
        "max_image_bytes": _integer("FORMA_MAX_IMAGE_BYTES", MAX_IMAGE_BYTES),
        "max_image_dimension": _integer("FORMA_MAX_IMAGE_DIMENSION", MAX_IMAGE_DIMENSION),
        "cors_origins": cors_origins(),
    }
