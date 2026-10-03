from __future__ import annotations

import logging
import re
import traceback
import uuid
from typing import Any, Dict, Optional

from forma_core.config import config
from forma_core.config.runtime import HOSTED_CHAT_UNAVAILABLE_MESSAGE, forma_dev_mode_enabled


DEBUG_ENV_VARS = ("FORMA_DEBUG", "FORMA_DEBUG_MODE", "API_DEBUG", "DEBUG")
SECRET_KEY_PATTERN = re.compile(r"(api[_-]?key|authorization|bearer|password|secret|token|credential)", re.IGNORECASE)
SECRET_TEXT_PATTERNS = [
    re.compile(r"\b(sk-proj-[A-Za-z0-9._-]{8,})"),
    re.compile(r"\b(sk-[A-Za-z0-9._-]{8,})"),
    re.compile(r"\b(sk-lf-[A-Za-z0-9._-]{8,})"),
    re.compile(r"\b(pk-lf-[A-Za-z0-9._-]{8,})"),
    re.compile(r"\b(rpa_[A-Za-z0-9._-]{8,})"),
    re.compile(r"\b(nvapi-[A-Za-z0-9._-]{8,})"),
    re.compile(r"\b(fc-[A-Za-z0-9._-]{8,})"),
    re.compile(r"(?i)(api[_-]?key|authorization|bearer|password|secret|token)\s*[:=]\s*([^\s,'\"]{8,})"),
]
RUNTIME_CONTEXT_PATTERN = re.compile(
    r"\s+for\s+provider=[^\s:]+(?:\s+model=[^\s:]+)?\s*:",
    re.IGNORECASE,
)
FILE_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)(?:[^\s\"'<>]+[\\/])*[^\s\"'<>]+"
    r"|(?<![\w])/(?:[^\s\"'<>]+/)*[^\s\"'<>]+"
)
DATABASE_DETAIL_PATTERN = re.compile(
    r"(?is)\b(?:sqlite(?:3)?|postgres(?:ql)?|psycopg|sqlalchemy)\b[^\r\n]{0,500}"
    r"|\bsupabase\s+(?:response|error|exception|database)\b[^\r\n]{0,500}"
    r"|\bdatabase\s+(?:detail|error|exception|response)\b\s*[:=][^\r\n]{0,500}"
)
PROVIDER_RESPONSE_PATTERN = re.compile(
    r"(?is)\b(?:provider\s+response|response\s+(?:body|payload))\s*[:=]\s*[^\r\n]{0,1000}"
)

PUBLIC_ERROR_MESSAGES = {
    "authentication_required": "Authentication is required.",
    "authorization_required": "You are not authorized to perform this action.",
    "request_invalid": "The request is invalid.",
    "request_validation_failed": "The request failed validation.",
    "invalid_params": "The request parameters are invalid.",
    "method_not_found": "The requested method is not available.",
    "mcp_tool_failed": "The MCP tool could not complete the request.",
    "a2a_protocol_error": "The A2A message could not be processed.",
    "a2a_action_failed": "The A2A action could not be completed.",
    "a2a_invalid_request": "The A2A request is invalid.",
    "a2a_submit_failed": "The A2A request could not be submitted.",
    "mcp_invalid_request": "The MCP request is invalid.",
    "mcp_invalid_params": "The MCP request parameters are invalid.",
    "mcp_method_not_found": "The requested MCP method is not available.",
    "mcp_request_failed": "The MCP request could not be completed.",
    "hosted_chat_unavailable": HOSTED_CHAT_UNAVAILABLE_MESSAGE,
    "generation_input_invalid": "The generation request is missing valid input.",
    "generation_request_invalid": "The generation request is invalid.",
    "generation_cancelled": "Generation was stopped by the user.",
    "generation_failed": "Generation could not be completed.",
    "generation_root_failed": "A required generation stage failed.",
    "llm_config_invalid": "The requested model configuration is invalid.",
    "llm_output_invalid": "The model provider returned an invalid response.",
    "llm_generation_unavailable": "Generation is temporarily unavailable.",
    "alpha_generation_unavailable": "Generation is currently unavailable.",
    "debug_config_failed": "Runtime configuration could not be loaded.",
    "runtime_config_failed": "Runtime configuration could not be resolved.",
    "video_provider_failed": "The video provider could not complete the request.",
    "video_review_config_invalid": "The video review configuration is invalid.",
    "video_review_output_invalid": "The video review provider returned an invalid response.",
    "image_model_test_failed": "The image model test could not be completed.",
    "image_generation_failed": "Image generation could not be completed.",
    "internal_error": "The server could not complete the request.",
    "storage_failed": "The requested storage operation could not be completed.",
    "project_not_found": "Project not found.",
    "design_brief_not_found": "DesignBrief not found.",
    "chat_not_found": "Chat not found.",
    "invalid_workflow_transition": "The project workflow cannot perform that transition.",
    "readiness_not_ready": "The project is not ready for this action.",
}

ERROR_CONTENT_KEYS = {
    "authorization_header",
    "body",
    "content",
    "image_data",
    "prompt",
    "prompt_text",
    "project_prompt",
    "provider_body",
    "provider_response",
    "raw_provider_response",
    "request_body",
    "raw_body",
    "raw_response",
    "response",
    "response_body",
    "response_payload",
    "response_text",
    "source_url",
    "source_prompt",
    "text",
    "user_prompt",
}
ERROR_DETAIL_KEYS = {
    "error",
    "error_detail",
    "error_message",
    "error_text",
    "exception",
    "failure",
    "failure_detail",
    "traceback",
}


def _env_bool(name: str, default: bool = False) -> bool:
    return config.boolean(name, default)


def debug_mode_enabled() -> bool:
    return any(_env_bool(name) for name in DEBUG_ENV_VARS)


def new_error_correlation_id() -> str:
    return f"err_{uuid.uuid4().hex}"


def _normalized_error_code(code: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(code or "").strip())
    return normalized[:80] or "request_failed"


def public_error_message(code: str, fallback: Optional[str] = None) -> str:
    del fallback
    return PUBLIC_ERROR_MESSAGES.get(_normalized_error_code(code), "The request could not be completed.")


def get_debug_mode_config() -> Dict[str, Any]:
    return {
        "enabled": debug_mode_enabled(),
        "env_vars": [name for name in DEBUG_ENV_VARS if config.get(name) is not None],
    }


def redact_debug_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_PATTERN.search(key_text):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_debug_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_debug_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_debug_value(item) for item in value]
    if isinstance(value, str):
        return redact_debug_text(value)
    return value


def redact_debug_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
        else:
            redacted = pattern.sub("<redacted>", redacted)
    redacted = PROVIDER_RESPONSE_PATTERN.sub(
        lambda match: re.sub(r"\s*[:=].*", ": <redacted>", match.group(0), count=1),
        redacted,
    )
    redacted = DATABASE_DETAIL_PATTERN.sub("database detail=<redacted>", redacted)
    redacted = FILE_PATH_PATTERN.sub("<redacted path>", redacted)
    return redacted


def redact_error_value(value: Any) -> Any:
    """Redact content-bearing fields before errors enter durable or operator output."""
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.casefold()
            if (
                normalized_key in ERROR_DETAIL_KEYS
                or normalized_key.endswith("_error")
                or normalized_key.endswith("_failure")
            ):
                redacted[key] = (
                    item
                    if isinstance(item, str) and item in PUBLIC_ERROR_MESSAGES.values()
                    else "<redacted>"
                )
            elif SECRET_KEY_PATTERN.search(key_text) or normalized_key in ERROR_CONTENT_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_error_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_error_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_error_value(item) for item in value]
    if isinstance(value, str):
        return redact_debug_text(value)
    return value


def runtime_safe_error_message(
    value: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    message = redact_debug_text(value)
    if forma_dev_mode_enabled():
        return message

    message = RUNTIME_CONTEXT_PATTERN.sub(":", message)
    replacements = (
        (model, "configured model"),
        (provider, "model provider"),
    )
    for identifier, replacement in replacements:
        if not identifier:
            continue
        identifier_pattern = re.compile(
            rf"(?<![\w-]){re.escape(identifier)}(?![\w-])",
            re.IGNORECASE,
        )
        message = identifier_pattern.sub(replacement, message)

    message = re.sub(r"\bmodel\s*=\s*[^\s,:;]+", "model=<redacted>", message, flags=re.IGNORECASE)
    message = re.sub(r"\bprovider\s*=\s*[^\s,:;]+", "provider=<redacted>", message, flags=re.IGNORECASE)
    return message


def exception_debug_payload(
    exc: BaseException,
    *,
    context: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "error_type": exc.__class__.__name__,
        "error": redact_debug_text(str(exc)),
        "traceback": redact_debug_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
    }
    if correlation_id:
        payload["correlation_id"] = correlation_id
    if context:
        payload["context"] = redact_error_value(context)
    return payload


def log_exception(
    logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    correlation_id: str,
    context: Optional[Dict[str, Any]] = None,
    level: int = logging.ERROR,
) -> None:
    """Write searchable, redacted diagnostics without emitting a raw traceback."""
    diagnostics = exception_debug_payload(exc, context=context, correlation_id=correlation_id)
    logger.log(level, "%s diagnostics=%s", message, diagnostics)


def api_error_detail(
    *,
    code: str,
    message: str,
    exc: Optional[BaseException] = None,
    job_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    public: bool = False,
) -> Dict[str, Any]:
    # Public transport callers must opt into the stable, diagnostic-free contract.
    if not public:
        runtime_details_visible = forma_dev_mode_enabled()
        detail: Dict[str, Any] = {
            "code": code,
            "message": runtime_safe_error_message(message, provider=provider, model=model),
        }
        if runtime_details_visible and provider:
            detail["provider"] = provider
        if runtime_details_visible and model:
            detail["model"] = model
        if runtime_details_visible and debug_mode_enabled() and exc is not None:
            detail["debug"] = exception_debug_payload(exc, context=context)
        elif runtime_details_visible and debug_mode_enabled() and context:
            detail["debug"] = {"context": redact_debug_value(context)}
        if job_id:
            detail["job_id"] = job_id
        return detail

    normalized_code = _normalized_error_code(code)
    detail: Dict[str, Any] = {
        "code": normalized_code,
        "message": public_error_message(normalized_code, message),
        "correlation_id": correlation_id or new_error_correlation_id(),
    }
    if job_id:
        detail["job_id"] = job_id
    return detail
