from forma_core.config.environment import config
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlparse


ALPHA_GENERATION_UNAVAILABLE_MESSAGE = "Generation is not available in this alpha deployment yet."
HOSTED_CHAT_UNAVAILABLE_MESSAGE = (
    "Forma hosted chat is temporarily under maintenance. "
    "Use the Forma-OSS CLI to build locally and upload completed projects when needed."
)
DATABASE_BACKEND_ENV_NAMES = (
    "DATABASE_BACKEND",
    "DATABASE_PROVIDER",
    "DB_BACKEND",
    "DB_PROVIDER",
)


class AlphaGenerationUnavailableError(RuntimeError):
    """Raised when deployment mode should route users to the alpha signup flow."""


class RuntimeConfigurationError(RuntimeError):
    """Raised when runtime environment values are invalid or unsafe together."""


DeploymentMode = Literal["local", "hosted"]
DEPLOYMENT_MODE_ENV = "FORMA_DEPLOYMENT_MODE"
DEVELOPMENT_MODE_ENV = "FORMA_DEVELOPMENT_MODE"
LEGACY_DEVELOPMENT_MODE_ENV = "FORMA_DEV_MODE"
HOSTED_CHAT_ENABLED_ENV = "FORMA_HOSTED_CHAT_ENABLED"
AUTHORING_MODE_ENABLED_ENV = "FORMA_AUTHORING_MODE_ENABLED"
AGENT_RUNTIMES_ENV = "FORMA_AGENT_RUNTIMES"
DEPLOYMENT_MODES = {"local", "hosted"}
BOOLEAN_VALUES = {"true": True, "false": False}


def env_bool(name: str, default: bool = False) -> bool:
    return config.boolean(name, default)


def _strict_env_bool(name: str, default: bool) -> bool:
    value = config.optional(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized not in BOOLEAN_VALUES:
        raise RuntimeConfigurationError(
            f"Invalid {name}={value!r}. Expected 'true' or 'false'."
        )
    return BOOLEAN_VALUES[normalized]


def deployment_mode() -> DeploymentMode:
    """Resolve the deployment mode, defaulting to the safe local mode."""
    value = config.optional(DEPLOYMENT_MODE_ENV)
    if value is not None:
        normalized = value.lower()
        if normalized not in DEPLOYMENT_MODES:
            raise RuntimeConfigurationError(
                f"Invalid {DEPLOYMENT_MODE_ENV}={value!r}. Expected 'local' or 'hosted'."
            )
        return normalized  # type: ignore[return-value]

    # Preserve older boolean deployment aliases while callers migrate.
    for name in (
        "FORMA_DEPLOYMENT",
        "DEPLOYMENT",
        "DEPLOYMENT_MODE",
        "NEXT_PUBLIC_FORMA_DEPLOYMENT",
    ):
        value = config.optional(name)
        if value is not None:
            return "hosted" if value.lower() in {"1", "true", "yes", "on"} else "local"
    return "local"


def development_mode_enabled() -> bool:
    """Resolve strict development mode, using FORMA_DEV_MODE as a legacy alias."""
    if config.optional(DEVELOPMENT_MODE_ENV) is not None:
        return _strict_env_bool(DEVELOPMENT_MODE_ENV, False)
    return env_bool(LEGACY_DEVELOPMENT_MODE_ENV, False)


def forma_dev_mode_enabled() -> bool:
    """Compatibility alias for the canonical development-mode resolver."""
    return development_mode_enabled()


def runtime_state() -> Dict[str, Any]:
    """Resolve and validate deployment/development state."""
    mode = deployment_mode()
    development = development_mode_enabled()
    if mode == "hosted" and development:
        raise RuntimeConfigurationError(
            f"{DEPLOYMENT_MODE_ENV}=hosted cannot be combined with "
            f"{DEVELOPMENT_MODE_ENV}=true. Disable development mode or use local deployment mode."
        )
    return {
        "deployment_mode": mode,
        "development_mode": development,
        "legacy_development_mode": config.optional(LEGACY_DEVELOPMENT_MODE_ENV),
    }


def validate_runtime_configuration() -> Dict[str, Any]:
    """Validate runtime state before database, provider, or worker startup."""
    return runtime_state()


def primary_database_backend_from_environment() -> str:
    """Resolve the primary persistence backend without initializing a client."""
    aliases = {
        "sqlite": "sqlite",
        "sqlite3": "sqlite",
        "supabase": "supabase",
    }
    configured_backend = None
    for name in DATABASE_BACKEND_ENV_NAMES:
        value = config.get(name)
        if not value or not value.strip():
            continue
        normalized = aliases.get(value.strip().lower())
        if normalized:
            configured_backend = normalized
            break

    supabase_url = config.get("SUPABASE_URL") or config.get("NEXT_PUBLIC_SUPABASE_URL")
    supabase_key = config.get("SUPABASE_SERVICE_ROLE_KEY") or config.get("SUPABASE_SECRET_KEY")

    if forma_dev_mode_enabled():
        parsed = urlparse(supabase_url or "")
        host = (parsed.hostname or "").strip().lower()
        local_supabase = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
        if configured_backend == "supabase" and local_supabase and supabase_key and supabase_key.strip():
            return "supabase"
        return "sqlite"

    if configured_backend:
        return configured_backend
    return "supabase" if supabase_url and supabase_url.strip() and supabase_key and supabase_key.strip() else "sqlite"


def deployment_mode_enabled() -> bool:
    return deployment_mode() == "hosted"


def hosted_chat_enabled() -> bool:
    """Resolve hosted chat availability with a safe hosted-deployment default."""
    return env_bool(HOSTED_CHAT_ENABLED_ENV, default=not deployment_mode_enabled())


def authoring_mode_enabled() -> bool:
    """Resolve external FormaAgent authoring mode, disabled by default."""
    return env_bool(AUTHORING_MODE_ENABLED_ENV, default=False)


def configured_agent_runtimes() -> list[Dict[str, str]]:
    """Return the credential-safe Forma Agent runtime inventory.

    ``FORMA_AGENT_RUNTIMES`` accepts comma-separated ``connector-id=Display Name``
    entries. The legacy ``FORMA_OPENCODE_CONNECTOR_ID`` remains the default and
    is automatically included when it is not listed explicitly.
    """

    runtimes: list[Dict[str, str]] = []
    seen: set[str] = set()
    configured = (config.get(AGENT_RUNTIMES_ENV) or "").strip()
    for raw_entry in configured.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        connector_id, separator, display_name = entry.partition("=")
        connector_id = connector_id.strip()
        if not connector_id or connector_id in seen:
            continue
        label = display_name.strip() if separator else ""
        if not label:
            label = connector_id.replace("-", " ").replace("_", " ").strip().title()
        runtimes.append({"id": connector_id, "label": label})
        seen.add(connector_id)

    default_connector_id = (config.get("FORMA_OPENCODE_CONNECTOR_ID") or "").strip()
    if default_connector_id and default_connector_id not in seen:
        label = default_connector_id.replace("-", " ").replace("_", " ").strip().title()
        runtimes.insert(0, {"id": default_connector_id, "label": label})

    return runtimes


class HostedChatUnavailableError(RuntimeError):
    """Raised when hosted chat is disabled by deployment configuration."""


def ensure_hosted_chat_enabled() -> None:
    if not hosted_chat_enabled():
        raise HostedChatUnavailableError(HOSTED_CHAT_UNAVAILABLE_MESSAGE)


def deployment_runtime_config(
    llm_config: Dict[str, Any],
    *,
    signup_storage: Optional[str] = None,
    authoring_access: bool = False,
) -> Dict[str, Any]:
    state = runtime_state()
    deployment_enabled = state["deployment_mode"] == "hosted"
    live_generation_enabled = bool(llm_config.get("live_generation_enabled"))
    default_connector_id = (config.get("FORMA_OPENCODE_CONNECTOR_ID") or "").strip() or None
    contract = {
        "enabled": deployment_enabled,
        "mode": state["deployment_mode"],
        "development_mode": state["development_mode"],
        "hosted_chat_enabled": hosted_chat_enabled(),
        "authoring_mode_enabled": authoring_mode_enabled(),
        "authoring_access": authoring_access,
        "opencode_connector_id": default_connector_id,
        "authoring_runtimes": configured_agent_runtimes(),
        "alpha_generation_gate_active": deployment_enabled and not live_generation_enabled,
        "generation_available": (not deployment_enabled) or live_generation_enabled,
    }
    if signup_storage:
        contract["signup_storage"] = signup_storage
    reason = generation_unavailable_reason(llm_config)
    if reason:
        contract["generation_unavailable_reason"] = reason
    return contract


def _runtime_value(llm_config: Dict[str, Any], key: str) -> Any:
    runtime = llm_config.get("runtime")
    if isinstance(runtime, dict) and runtime.get(key) is not None:
        return runtime.get(key)
    return llm_config.get(key)


def generation_unavailable_reason(llm_config: Dict[str, Any]) -> Optional[str]:
    validation_error = llm_config.get("validation_error")
    if isinstance(validation_error, str) and validation_error.strip():
        return validation_error.strip()

    if not bool(llm_config.get("live_generation_enabled")):
        provider = _runtime_value(llm_config, "runtime_provider") or llm_config.get("provider") or "selected provider"
        model = _runtime_value(llm_config, "runtime_model") or llm_config.get("requested_model") or "selected model"
        return f"{provider}/{model} is not configured for live generation."

    return None


def generation_unavailable_message(llm_config: Dict[str, Any]) -> str:
    reason = generation_unavailable_reason(llm_config)
    if reason:
        provider = _runtime_value(llm_config, "runtime_provider") or llm_config.get("provider") or "selected provider"
        model = _runtime_value(llm_config, "runtime_model") or llm_config.get("requested_model") or "selected model"
        return f"Generation cannot run with {provider}/{model}: {reason}"
    return ALPHA_GENERATION_UNAVAILABLE_MESSAGE


def generation_unavailable_detail(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    reason = generation_unavailable_reason(llm_config)
    provider = _runtime_value(llm_config, "runtime_provider") or llm_config.get("provider")
    model = _runtime_value(llm_config, "runtime_model") or llm_config.get("requested_model")
    return {
        "code": "llm_generation_unavailable" if reason else "alpha_generation_unavailable",
        "message": generation_unavailable_message(llm_config),
        "reason": reason,
        "provider": provider,
        "model": model,
        "live_generation_enabled": bool(llm_config.get("live_generation_enabled")),
    }


__all__ = [
    "AGENT_RUNTIMES_ENV",
    "ALPHA_GENERATION_UNAVAILABLE_MESSAGE",
    "AlphaGenerationUnavailableError",
    "AUTHORING_MODE_ENABLED_ENV",
    "BOOLEAN_VALUES",
    "DEPLOYMENT_MODE_ENV",
    "DEVELOPMENT_MODE_ENV",
    "DeploymentMode",
    "HOSTED_CHAT_ENABLED_ENV",
    "HOSTED_CHAT_UNAVAILABLE_MESSAGE",
    "HostedChatUnavailableError",
    "LEGACY_DEVELOPMENT_MODE_ENV",
    "RuntimeConfigurationError",
    "authoring_mode_enabled",
    "configured_agent_runtimes",
    "deployment_mode",
    "deployment_mode_enabled",
    "deployment_runtime_config",
    "development_mode_enabled",
    "ensure_hosted_chat_enabled",
    "env_bool",
    "forma_dev_mode_enabled",
    "generation_unavailable_detail",
    "generation_unavailable_message",
    "generation_unavailable_reason",
    "hosted_chat_enabled",
    "primary_database_backend_from_environment",
    "runtime_state",
    "validate_runtime_configuration",
]
