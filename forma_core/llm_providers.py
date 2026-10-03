import base64
import json
import logging
from forma_core.config import config
from forma_core.config.parti import resolve_parti_model
from forma_core.vertex_auth import build_vertex_credentials
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from dotenv import load_dotenv

from forma_core.runtime import forma_dev_mode_enabled, deployment_mode_enabled

Settings = Mapping[str, str]

load_dotenv()

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None


logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.7-flash"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
DEFAULT_VERTEX_MODEL = DEFAULT_GEMINI_MODEL
DEFAULT_VERTEX_FALLBACK_MODEL = DEFAULT_GEMINI_FALLBACK_MODEL
DEFAULT_VERTEX_LOCATION = "global"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_OPENAI_TIMEOUT_SECONDS = 300.0
DEFAULT_RUNPOD_TIMEOUT_SECONDS = 1200.0
DEFAULT_RUNPOD_POLL_TIMEOUT_SECONDS = 1200.0
DEFAULT_BASETEN_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
DEFAULT_GMI_MODEL = "anthropic/claude-fable-5"
DEFAULT_HUGGINGFACE_MODEL = "Qwen/Qwen2.5-Coder-3B-Instruct:nscale"
DEFAULT_CLOUDFLARE_MODEL = "@cf/google/gemma-4-26b-a4b-it"
DEFAULT_NVIDIA_MODEL = "nvidia/z-ai/glm-5.2"
DEFAULT_XAI_MODEL = "grok-4"
DEFAULT_TOGETHER_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
DEFAULT_BASETEN_BASE_URL = "https://inference.baseten.co/v1"
DEFAULT_GMI_BASE_URL = "https://api.gmi-serving.com/v1"
DEFAULT_HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"
DEFAULT_CLOUDFLARE_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
DEFAULT_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_TOGETHER_BASE_URL = "https://api.together.xyz/v1"
DEFAULT_HTTP_USER_AGENT = "Forma-OSS/0.1"

# Structured-output token budgets. The fine-tuned parti-base emits large JSON
# records (MechanicalNotes runs ~9k+ chars); a low or unset max_tokens truncates
# the completion mid-string and the pydantic parse fails. These bound the budget
# that is ALWAYS sent for structured calls plus the one-retry escalation.
DEFAULT_STRUCTURED_MAX_TOKENS = 8192  # sent when no *_MAX_TOKENS env is set
STRUCTURED_MAX_TOKENS_FLOOR = 6000  # parti-base card minimum for large records
STRUCTURED_MAX_TOKENS_CEILING = 16384  # retry cap; Qwen2.5-3B context is 32k
LARGE_SCHEMA_CHAR_THRESHOLD = 2000  # MechanicalNotes=6656 chars, ProjectOverview=721

# json_repair is imported lazily inside _salvage_json_text so the dependency is
# only required when salvage actually runs. This flag makes the ImportError log
# fire exactly once instead of on every truncated response.
_json_repair_import_warned = False


class LLMProviderConfigError(RuntimeError):
    """Raised when provider configuration prevents live generation."""


class LLMProviderOutputError(RuntimeError):
    """Raised when a live provider returns unusable structured output."""


class LLMProviderPreflightError(LLMProviderConfigError):
    """Raised when a production provider/model has not passed a live preflight."""


class LLMProviderInputError(ValueError):
    """Raised when the selected provider/model cannot consume the supplied input."""


def model_image_input_support(provider_name: str, model_name: str) -> Optional[bool]:
    """Return known image-input support, or ``None`` when it is not known.

    OpenAI-compatible model catalogs do not expose capabilities consistently, so
    unknown models are allowed through and provider errors are normalized later.
    The explicit text-only markers prevent known-incompatible requests from
    starting expensive workflow steps such as web research.
    """
    provider = normalize_llm_provider_name(provider_name) or provider_name.strip().lower()
    model = _normalize_model_name(model_name).lower()

    parti_model = resolve_parti_model(provider, _normalize_model_name(model_name))
    if parti_model is not None:
        return parti_model.supports_images
    if provider in {"gemini", "vertex", "anthropic"}:
        return True
    if provider == "cloudflare" and model == DEFAULT_CLOUDFLARE_MODEL:
        return True
    if any(marker in model for marker in ("-vl", "_vl", "/vl", "vision", "llava")):
        return True
    if provider == "openai" and model.startswith(("gpt-4o", "gpt-4.1", "gpt-5")):
        return True
    if any(marker in model for marker in ("nemotron", "gpt-oss", "coder")):
        return False
    return None


def _image_input_error_message(provider_name: str, model_name: str) -> str:
    return (
        f"The selected model '{provider_name}/{model_name}' cannot read reference images. "
        "Choose a vision-capable model or remove the image attachment."
    )


def _is_image_input_unsupported_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "unknown modality: image",
            "image input is not supported",
            "image inputs are not supported",
            "does not support image input",
            "doesn't support image input",
            "unsupported image input",
            "multimodal input is not supported",
            "only supports text input",
        )
    )


SUPPORTED_LLM_PROVIDERS = {
    "anthropic",
    "baseten",
    "gemini",
    "gmi",
    "huggingface",
    "cloudflare",
    "nvidia",
    "openai",
    "openai-compatible",
    "runpod",
    "runpod-serverless",
    "simulation",
    "together",
    "vertex",
    "xai",
}
SIMULATION_PROVIDER_ALIASES = {"simulation", "simulated", "offline", "none", "mock"}
PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic-claude": "anthropic",
    "baseten-model-apis": "baseten",
    "baseten-frontier": "baseten",
    "build-nvidia": "nvidia",
    "nvidia-build": "nvidia",
    "nvidia-nim": "nvidia",
    "nim": "nvidia",
    "google": "gemini",
    "google-genai": "gemini",
    "google-vertex": "vertex",
    "google-vertex-ai": "vertex",
    "vertex-ai": "vertex",
    "vertexai": "vertex",
    "grok": "xai",
    "x-ai": "xai",
    "xai-grok": "xai",
    "together-ai": "together",
    "togetherai": "together",
    "gmi-cloud": "gmi",
    "gmi_cloud": "gmi",
    "gmicloud": "gmi",
    "gemicloud": "gmi",
    "gmi-serving": "gmi",
    "hf": "huggingface",
    "hugging-face": "huggingface",
    "huggingface-inference": "huggingface",
    "huggingface-router": "huggingface",
    "cloudflare-ai": "cloudflare",
    "cloudflare-workers-ai": "cloudflare",
    "workers-ai": "cloudflare",
    "workers_ai": "cloudflare",
    "compatible": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "runpod-openai": "runpod",
    "runpod-openai-compatible": "runpod",
    "runpod-vllm": "runpod",
    "runpod_serverless": "runpod-serverless",
    **{alias: "simulation" for alias in SIMULATION_PROVIDER_ALIASES},
}


@dataclass
class LLMProviderValidation:
    provider: str
    requested_model: str
    actual_model: Optional[str]
    requested_model_available: bool
    strict_mode: bool
    fallback_active: bool
    fallback_model: Optional[str] = None
    validation_error: Optional[str] = None
    model_availability_checked: bool = False
    live_generation_enabled: bool = True

    def as_debug_dict(self) -> Dict[str, Any]:
        image_support_model = self.actual_model or self.requested_model
        return {
            "provider": self.provider,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "requested_model_available": self.requested_model_available,
            "model_availability_checked": self.model_availability_checked,
            "strict_mode": self.strict_mode,
            "fallback_active": self.fallback_active,
            "fallback_model": self.fallback_model,
            "validation_error": self.validation_error,
            "live_generation_enabled": self.live_generation_enabled,
            "supports_image_input": model_image_input_support(self.provider, image_support_model),
        }


def enforce_production_llm_preflight(validation: LLMProviderValidation) -> LLMProviderValidation:
    """Require a successful live model-availability check outside development."""

    if forma_dev_mode_enabled():
        return validation

    provider_model = f"{validation.provider}/{validation.requested_model}"
    reason = validation.validation_error
    if not reason and not validation.live_generation_enabled:
        reason = "live generation is disabled"
    if not reason and not validation.model_availability_checked:
        reason = "the provider did not perform a live model-availability check"
    if not reason and validation.fallback_active:
        reason = "the selected model fell back to a different model"
    if not reason and not validation.requested_model_available:
        reason = "the selected model was not reported as available"
    if not reason and not validation.actual_model:
        reason = "the provider did not resolve an executable model"

    if reason:
        raise LLMProviderPreflightError(
            f"Production LLM preflight failed for {provider_model}: {reason}. "
            "FORMA_DEV_MODE=false forbids simulation, unchecked providers, and model fallback."
        )
    return validation


@dataclass(frozen=True)
class LLMRuntimeConfig:
    provider: str
    model: str
    requested_provider: Optional[str] = None
    requested_model: Optional[str] = None
    provider_overridden: bool = False
    model_overridden: bool = False
    allowed_providers: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None
    configured_providers: Optional[List[str]] = None

    def as_debug_dict(self) -> Dict[str, Any]:
        return {
            "runtime_provider": self.provider,
            "runtime_model": self.model,
            "requested_provider_override": self.requested_provider,
            "requested_model_override": self.requested_model,
            "provider_overridden": self.provider_overridden,
            "model_overridden": self.model_overridden,
            "allowed_providers": self.allowed_providers,
            "allowed_models": self.allowed_models,
            "configured_providers": self.configured_providers,
        }


def _env(name: str, default: Optional[str] = None, settings: Optional[Settings] = None) -> Optional[str]:
    value = settings.get(name) if settings is not None else config.get(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped if stripped else default


def _first_env(names: List[str], default: Optional[str] = None, settings: Optional[Settings] = None) -> Optional[str]:
    for name in names:
        value = _env(name, settings=settings)
        if value is not None:
            return value
    return default


def _cloudflare_base_url_from_env(settings: Optional[Settings] = None) -> Optional[str]:
    configured = _first_env(["CLOUDFLARE_BASE_URL"], settings=settings)
    if configured:
        return configured.rstrip("/")
    account_id = _first_env(["CLOUDFLARE_ACCOUNT_ID"], settings=settings)
    if not account_id:
        return None
    encoded_account_id = urllib.parse.quote(account_id, safe="")
    return f"https://api.cloudflare.com/client/v4/accounts/{encoded_account_id}/ai/v1"


def _parse_csv_env(names: List[str], settings: Optional[Settings] = None) -> Optional[List[str]]:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return None
    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    return values


def _parse_json_mapping_env(names: List[str], settings: Optional[Settings] = None) -> Dict[str, Any]:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise LLMProviderConfigError(f"{names[0]} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMProviderConfigError(f"{names[0]} must be a JSON object mapping model names to endpoint ids or URLs.")
    return parsed


def _is_runpod_serverless_endpoint_url(value: Optional[str]) -> bool:
    if not value:
        return False

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.netloc.lower() != "api.runpod.ai":
        return False

    path_parts = [urllib.parse.unquote(part).lower() for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0] != "v2":
        return False
    return "openai" not in path_parts[2:]


def _runpod_base_url_env_names(*, include_generic: bool) -> List[str]:
    names = ["RUNPOD_OPENAI_BASE_URL", "RUNPOD_BASE_URL"]
    if include_generic:
        names.append("LLM_BASE_URL")
    return names


def _runpod_serverless_endpoint_url_from_env(*, include_generic: bool = True, settings: Optional[Settings] = None) -> Optional[str]:
    endpoint_url = _first_env(["RUNPOD_ENDPOINT_URL"], settings=settings)
    if endpoint_url:
        return endpoint_url

    for name in _runpod_base_url_env_names(include_generic=include_generic):
        value = _env(name, settings=settings)
        if _is_runpod_serverless_endpoint_url(value):
            return value
    return None


def _runpod_openai_base_url_from_env(*, include_generic: bool = True, settings: Optional[Settings] = None) -> Optional[str]:
    for name in _runpod_base_url_env_names(include_generic=include_generic):
        value = _env(name, settings=settings)
        if value and not _is_runpod_serverless_endpoint_url(value):
            return value
    return None


def _runpod_serverless_url_env_name(*, include_generic: bool = True, settings: Optional[Settings] = None) -> Optional[str]:
    for name in _runpod_base_url_env_names(include_generic=include_generic):
        if _is_runpod_serverless_endpoint_url(_env(name, settings=settings)):
            return name
    return None


def _runpod_serverless_url_misconfiguration_message(env_name: str) -> str:
    return (
        f"{env_name} points at a Runpod Serverless queue endpoint, but LLM_PROVIDER=runpod uses the "
        "OpenAI-compatible /chat/completions API. Set LLM_PROVIDER=runpod-serverless and put that URL in "
        "RUNPOD_ENDPOINT_URL, or set RUNPOD_OPENAI_BASE_URL to "
        "https://api.runpod.ai/v2/<endpoint-id>/openai/v1."
    )


def normalize_llm_provider_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower().replace("_", "-")
    if not normalized:
        return None
    return PROVIDER_ALIASES.get(normalized, normalized)


def _env_bool(name: str, default: bool = False, settings: Optional[Settings] = None) -> bool:
    value = settings.get(name) if settings is not None else config.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _first_env_bool(names: List[str], default: bool = False, settings: Optional[Settings] = None) -> bool:
    for name in names:
        if (settings is not None and name in settings) or (settings is None and config.get(name) is not None):
            return _env_bool(name, default, settings=settings)
    return default


def _first_env_float(names: List[str], default: float, settings: Optional[Settings] = None) -> float:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Invalid timeout value %r; using %.1fs.", raw_value, default)
        return default


def _first_env_optional_float(names: List[str], default: Optional[float] = None, settings: Optional[Settings] = None) -> Optional[float]:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return default

    if raw_value.strip().lower() in {"default", "none", "omit"}:
        return None

    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Invalid float value %r; using %r.", raw_value, default)
        return default


def _first_env_optional_string(
    names: List[str],
    default: Optional[str] = None,
    omit_values: Optional[List[str]] = None,
    settings: Optional[Settings] = None,
) -> Optional[str]:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return default

    value = raw_value.strip()
    if omit_values and value.lower() in omit_values:
        return None
    return value


def _first_env_int(names: List[str], settings: Optional[Settings] = None) -> Optional[int]:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return None

    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid integer value %r; ignoring it.", raw_value)
        return None


def _scoped_env_helpers(settings: Optional[Settings]):
    """Bind all constructor reads to one explicit settings snapshot."""
    return (
        lambda name, default=None: _env(name, default, settings),
        lambda names, default=None: _first_env(names, default, settings),
        lambda names, default=False: _first_env_bool(names, default, settings),
        lambda names, default=0.0: _first_env_float(names, default, settings),
        lambda names, default=None: _first_env_optional_float(names, default, settings),
        lambda names: _first_env_int(names, settings),
        lambda names, default=None, omit_values=None: _first_env_optional_string(names, default, omit_values, settings),
        lambda include_generic=True: _runpod_openai_base_url_from_env(include_generic=include_generic, settings=settings),
        lambda include_generic=True: _runpod_serverless_endpoint_url_from_env(include_generic=include_generic, settings=settings),
        lambda include_generic=True: _runpod_serverless_url_env_name(include_generic=include_generic, settings=settings),
        lambda: _model_endpoint_map(settings),
        lambda: _cloudflare_base_url_from_env(settings),
    )
def _default_provider_name(settings: Optional[Settings] = None) -> str:
    provider_name = normalize_llm_provider_name(_env("LLM_PROVIDER", settings=settings))
    if provider_name:
        return provider_name
    runpod_api_key = _first_env(["RUNPOD_API_KEY"], settings=settings)
    runpod_openai_base_url = _runpod_openai_base_url_from_env(include_generic=False, settings=settings)
    runpod_serverless_endpoint = _first_env(["RUNPOD_ENDPOINT_ID"], settings=settings) or _runpod_serverless_endpoint_url_from_env(include_generic=False, settings=settings)
    if runpod_api_key and runpod_serverless_endpoint and not runpod_openai_base_url:
        return "runpod-serverless"
    if runpod_api_key and runpod_openai_base_url:
        return "runpod"
    if runpod_api_key and runpod_serverless_endpoint:
        return "runpod-serverless"
    if _first_env(["VERTEX_AI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"], settings=settings):
        return "vertex"
    if _first_env(["GEMINI_API_KEY", "GOOGLE_API_KEY"], settings=settings):
        return "gemini"
    if _first_env(["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"], settings=settings):
        return "anthropic"
    if _first_env(["LLM_BASE_URL", "OPENAI_BASE_URL"], settings=settings):
        return "openai-compatible"
    if _first_env(["OPENAI_API_KEY", "LLM_API_KEY"], settings=settings):
        return "openai"
    if _first_env(["BASETEN_API_KEY"], settings=settings) and _first_env(["BASETEN_BASE_URL"], DEFAULT_BASETEN_BASE_URL, settings=settings):
        return "baseten"
    if _first_env(["GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY"], settings=settings) and _first_env(
        ["GMI_BASE_URL", "GMI_CLOUD_BASE_URL", "GMICLOUD_BASE_URL"],
        DEFAULT_GMI_BASE_URL,
        settings=settings,
    ):
        return "gmi"
    if _first_env(["HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN", "HF_TOKEN", "HF_API_TOKEN"], settings=settings) and _first_env(
        ["HUGGINGFACE_BASE_URL", "HF_BASE_URL"],
        DEFAULT_HUGGINGFACE_BASE_URL,
        settings=settings,
    ):
        return "huggingface"
    if _first_env(["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_API_KEY", "CLOUDFLARE_API_KEY"], settings=settings) and _cloudflare_base_url_from_env(settings):
        return "cloudflare"
    if _first_env(["NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NIM_API_KEY"], settings=settings) and _first_env(
        ["NVIDIA_BASE_URL", "NVIDIA_NIM_BASE_URL", "NIM_BASE_URL"],
        DEFAULT_NVIDIA_BASE_URL,
        settings=settings,
    ):
        return "nvidia"
    if _first_env(["XAI_API_KEY", "GROK_API_KEY"], settings=settings) and _first_env(
        ["XAI_BASE_URL", "GROK_BASE_URL"],
        DEFAULT_XAI_BASE_URL,
        settings=settings,
    ):
        return "xai"
    if _first_env(["TOGETHER_MODEL", "TOGETHER_LLM_BASE_URL"], settings=settings) and _first_env(["TOGETHER_API_KEY", "TOGETHER_IMAGE_API_KEY"], settings=settings):
        return "together"
    return "simulation"


def _configured_provider_names(default_provider: str, settings: Optional[Settings] = None) -> List[str]:
    providers = {default_provider, "simulation"}
    if _first_env(["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"], settings=settings):
        providers.add("anthropic")
    if _first_env(["BASETEN_API_KEY"], settings=settings) and _first_env(["BASETEN_BASE_URL", "LLM_BASE_URL"], DEFAULT_BASETEN_BASE_URL, settings=settings):
        providers.add("baseten")
    if _first_env(["GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY"], settings=settings) and _first_env(
        ["GMI_BASE_URL", "GMI_CLOUD_BASE_URL", "GMICLOUD_BASE_URL"],
        DEFAULT_GMI_BASE_URL,
        settings=settings,
    ):
        providers.add("gmi")
    if _first_env(["HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN", "HF_TOKEN", "HF_API_TOKEN"], settings=settings) and _first_env(
        ["HUGGINGFACE_BASE_URL", "HF_BASE_URL"],
        DEFAULT_HUGGINGFACE_BASE_URL,
        settings=settings,
    ):
        providers.add("huggingface")
    if _first_env(["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_API_KEY", "CLOUDFLARE_API_KEY"], settings=settings) and _cloudflare_base_url_from_env(settings):
        providers.add("cloudflare")
    if _first_env(["NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NIM_API_KEY"], settings=settings) and _first_env(
        ["NVIDIA_BASE_URL", "NVIDIA_NIM_BASE_URL", "NIM_BASE_URL", "LLM_BASE_URL"],
        DEFAULT_NVIDIA_BASE_URL,
        settings=settings,
    ):
        providers.add("nvidia")
    if _first_env(["XAI_API_KEY", "GROK_API_KEY"], settings=settings) and _first_env(
        ["XAI_BASE_URL", "GROK_BASE_URL", "LLM_BASE_URL"],
        DEFAULT_XAI_BASE_URL,
        settings=settings,
    ):
        providers.add("xai")
    if _first_env(["TOGETHER_API_KEY", "TOGETHER_IMAGE_API_KEY"], settings=settings) and _first_env(["TOGETHER_MODEL", "TOGETHER_LLM_BASE_URL"], settings=settings):
        providers.add("together")
    if _first_env(["RUNPOD_API_KEY"], settings=settings) and _runpod_openai_base_url_from_env(settings=settings):
        providers.add("runpod")
    if _first_env(["RUNPOD_API_KEY"], settings=settings) and (_first_env(["RUNPOD_ENDPOINT_ID"], settings=settings) or _runpod_serverless_endpoint_url_from_env(settings=settings)):
        providers.add("runpod-serverless")
    if _first_env(["GEMINI_API_KEY", "GOOGLE_API_KEY"], settings=settings):
        providers.add("gemini")
    if _first_env(["VERTEX_AI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"], settings=settings):
        providers.add("vertex")
    if _first_env(["OPENAI_API_KEY", "LLM_API_KEY"], settings=settings):
        providers.add("openai")
    if _first_env(["LLM_BASE_URL", "OPENAI_BASE_URL"], settings=settings):
        providers.add("openai-compatible")
    return sorted(provider for provider in providers if provider in SUPPORTED_LLM_PROVIDERS)


def _allowed_provider_names(default_provider: str, settings: Optional[Settings] = None) -> Optional[List[str]]:
    configured = _parse_csv_env(["LLM_ALLOWED_PROVIDERS", "ALLOWED_LLM_PROVIDERS"], settings=settings)
    if configured is None:
        return _configured_provider_names(default_provider, settings)
    allowed: List[str] = []
    for provider in configured:
        normalized = normalize_llm_provider_name(provider)
        if normalized:
            allowed.append(normalized)
    return sorted(set(allowed))


def _default_model_for_provider(provider_name: str, *, include_runtime_override: bool = True, settings: Optional[Settings] = None) -> str:
    runtime_model = ["LLM_MODEL"] if include_runtime_override else []
    if provider_name == "anthropic":
        return _first_env(["ANTHROPIC_MODEL", "CLAUDE_MODEL", *runtime_model], DEFAULT_ANTHROPIC_MODEL, settings=settings) or DEFAULT_ANTHROPIC_MODEL
    if provider_name == "baseten":
        return _first_env(["BASETEN_MODEL", *runtime_model], DEFAULT_BASETEN_MODEL, settings=settings) or DEFAULT_BASETEN_MODEL
    if provider_name == "gemini":
        return _first_env([*runtime_model, "GEMINI_MODEL"], DEFAULT_GEMINI_MODEL, settings=settings) or DEFAULT_GEMINI_MODEL
    if provider_name == "vertex":
        return _first_env([*runtime_model, "VERTEX_AI_MODEL", "VERTEX_MODEL"], DEFAULT_VERTEX_MODEL, settings=settings) or DEFAULT_VERTEX_MODEL
    if provider_name == "gmi":
        return _normalize_model_for_provider(
            provider_name,
            _first_env(["GMI_MODEL", "GMI_CLOUD_MODEL", "GMICLOUD_MODEL", *runtime_model], DEFAULT_GMI_MODEL, settings=settings) or DEFAULT_GMI_MODEL,
        )
    if provider_name == "huggingface":
        return _first_env(["HUGGINGFACE_MODEL", "HF_MODEL"], DEFAULT_HUGGINGFACE_MODEL, settings=settings) or DEFAULT_HUGGINGFACE_MODEL
    if provider_name == "cloudflare":
        return _first_env(["CLOUDFLARE_MODEL", *runtime_model], DEFAULT_CLOUDFLARE_MODEL, settings=settings) or DEFAULT_CLOUDFLARE_MODEL
    if provider_name == "nvidia":
        return _first_env(["NVIDIA_MODEL", "NVIDIA_NIM_MODEL", "NIM_MODEL", *runtime_model], DEFAULT_NVIDIA_MODEL, settings=settings) or DEFAULT_NVIDIA_MODEL
    if provider_name == "xai":
        return _first_env(["XAI_MODEL", "GROK_MODEL", *runtime_model], DEFAULT_XAI_MODEL, settings=settings) or DEFAULT_XAI_MODEL
    if provider_name == "together":
        return _first_env(["TOGETHER_MODEL", *runtime_model], DEFAULT_TOGETHER_MODEL, settings=settings) or DEFAULT_TOGETHER_MODEL
    if provider_name == "openai":
        return _first_env(["OPENAI_MODEL", *runtime_model], DEFAULT_OPENAI_MODEL, settings=settings) or DEFAULT_OPENAI_MODEL
    if provider_name == "openai-compatible":
        return _first_env([*runtime_model, "OPENAI_MODEL"], DEFAULT_OPENAI_MODEL, settings=settings) or DEFAULT_OPENAI_MODEL
    if provider_name == "runpod":
        return _first_env(["RUNPOD_OPENAI_MODEL", *runtime_model, "RUNPOD_MODEL", "RUNPOD_PARTI_MODEL"], "runpod-default", settings=settings) or "runpod-default"
    if provider_name == "runpod-serverless":
        return (
            _first_env(["RUNPOD_SERVERLESS_MODEL", "RUNPOD_MODEL", "RUNPOD_OPENAI_MODEL", *runtime_model], "runpod-serverless", settings=settings)
            or "runpod-serverless"
        )
    return "simulation"


def _fallback_model_for_provider(provider_name: str, settings: Optional[Settings] = None) -> Optional[str]:
    if provider_name == "anthropic":
        return _first_env(["ANTHROPIC_FALLBACK_MODEL", "CLAUDE_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "baseten":
        return _first_env(["BASETEN_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "gemini":
        return _first_env(["LLM_FALLBACK_MODEL", "GEMINI_FALLBACK_MODEL"], DEFAULT_GEMINI_FALLBACK_MODEL, settings=settings)
    if provider_name == "vertex":
        return _first_env(
            ["LLM_FALLBACK_MODEL", "VERTEX_AI_FALLBACK_MODEL", "VERTEX_FALLBACK_MODEL"],
            DEFAULT_VERTEX_FALLBACK_MODEL,
            settings=settings,
        )
    if provider_name == "gmi":
        fallback = _first_env(["GMI_FALLBACK_MODEL", "GMI_CLOUD_FALLBACK_MODEL", "GMICLOUD_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
        return _normalize_model_for_provider(provider_name, fallback) if fallback else None
    if provider_name == "huggingface":
        return _first_env(["HUGGINGFACE_FALLBACK_MODEL", "HF_FALLBACK_MODEL"], settings=settings)
    if provider_name == "cloudflare":
        return _first_env(["CLOUDFLARE_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "nvidia":
        return _first_env(["NVIDIA_FALLBACK_MODEL", "NVIDIA_NIM_FALLBACK_MODEL", "NIM_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "xai":
        return _first_env(["XAI_FALLBACK_MODEL", "GROK_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "together":
        return _first_env(["TOGETHER_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "openai":
        return _first_env(["OPENAI_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "openai-compatible":
        return _first_env(["LLM_FALLBACK_MODEL", "OPENAI_FALLBACK_MODEL"], settings=settings)
    if provider_name == "runpod":
        return _first_env(["RUNPOD_OPENAI_FALLBACK_MODEL", "RUNPOD_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    if provider_name == "runpod-serverless":
        return _first_env(["RUNPOD_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"], settings=settings)
    return None


def _model_endpoint_map(settings: Optional[Settings] = None) -> Dict[str, Any]:
    return _parse_json_mapping_env(["RUNPOD_MODEL_ENDPOINTS", "RUNPOD_ENDPOINTS_BY_MODEL"], settings=settings)


def _normalize_model_for_provider(provider_name: str, model_name: Optional[str]) -> str:
    model = (model_name or "").strip()
    if provider_name != "gmi" or not model:
        return model

    lowered = model.lower()
    for prefix in ("gmi/", "gmi-cloud/", "gmicloud/", "gemicloud/"):
        if lowered.startswith(prefix):
            model = model[len(prefix) :]
            lowered = model.lower()
            break

    aliases = {
        "fable": DEFAULT_GMI_MODEL,
        "fable-5": DEFAULT_GMI_MODEL,
        "claude-fable-5": DEFAULT_GMI_MODEL,
    }
    return aliases.get(lowered, model)


def _allowed_model_names(provider_name: str, default_model: str, settings: Optional[Settings] = None) -> Optional[List[str]]:
    env_names = ["LLM_ALLOWED_MODELS", "ALLOWED_LLM_MODELS"]
    if provider_name == "anthropic":
        env_names = ["ANTHROPIC_ALLOWED_MODELS", "CLAUDE_ALLOWED_MODELS", "ALLOWED_ANTHROPIC_MODELS", *env_names]
    elif provider_name == "baseten":
        env_names = ["BASETEN_ALLOWED_MODELS", "ALLOWED_BASETEN_MODELS", *env_names]
    elif provider_name == "gmi":
        env_names = ["GMI_ALLOWED_MODELS", "GMI_CLOUD_ALLOWED_MODELS", "GMICLOUD_ALLOWED_MODELS", "ALLOWED_GMI_MODELS", *env_names]
    elif provider_name == "huggingface":
        env_names = ["HUGGINGFACE_ALLOWED_MODELS", "HF_ALLOWED_MODELS", "ALLOWED_HUGGINGFACE_MODELS", *env_names]
    elif provider_name == "cloudflare":
        env_names = ["CLOUDFLARE_ALLOWED_MODELS", "ALLOWED_CLOUDFLARE_MODELS", *env_names]
    elif provider_name == "openai":
        env_names = ["OPENAI_ALLOWED_MODELS", "ALLOWED_OPENAI_MODELS", *env_names]
    elif provider_name == "openai-compatible":
        env_names = [
            "OPENAI_COMPATIBLE_ALLOWED_MODELS",
            "LLM_COMPATIBLE_ALLOWED_MODELS",
            "ALLOWED_OPENAI_COMPATIBLE_MODELS",
            *env_names,
        ]
    elif provider_name == "gemini":
        env_names = ["GEMINI_ALLOWED_MODELS", "ALLOWED_GEMINI_MODELS", *env_names]
    elif provider_name == "vertex":
        env_names = ["VERTEX_AI_ALLOWED_MODELS", "VERTEX_ALLOWED_MODELS", "ALLOWED_VERTEX_MODELS", *env_names]
    elif provider_name == "nvidia":
        env_names = ["NVIDIA_ALLOWED_MODELS", "NVIDIA_NIM_ALLOWED_MODELS", "NIM_ALLOWED_MODELS", *env_names]
    elif provider_name == "xai":
        env_names = ["XAI_ALLOWED_MODELS", "GROK_ALLOWED_MODELS", *env_names]
    elif provider_name == "together":
        env_names = ["TOGETHER_ALLOWED_MODELS", *env_names]
    elif provider_name in {"runpod", "runpod-serverless"}:
        env_names = ["RUNPOD_ALLOWED_MODELS", "ALLOWED_RUNPOD_MODELS", *env_names]

    configured = _parse_csv_env(env_names, settings=settings)
    if configured is not None:
        return sorted(set(_normalize_model_for_provider(provider_name, model) for model in configured))

    defaults = {default_model}
    fallback = _fallback_model_for_provider(provider_name, settings)
    if fallback:
        defaults.add(fallback)
    if provider_name == "runpod-serverless":
        defaults.update(str(model_name) for model_name in _model_endpoint_map(settings).keys())
    return sorted(model for model in defaults if model)


def resolve_llm_runtime_config(
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> LLMRuntimeConfig:
    default_provider = _default_provider_name(settings)
    provider_requested = bool(provider_name and provider_name.strip())
    provider = normalize_llm_provider_name(provider_name) or default_provider
    if provider not in SUPPORTED_LLM_PROVIDERS:
        raise LLMProviderConfigError(
            f"Unsupported LLM provider '{provider}'. Supported providers are "
            f"{', '.join(sorted(SUPPORTED_LLM_PROVIDERS))}."
        )

    configured_providers = _configured_provider_names(default_provider, settings)
    allowed_providers = _allowed_provider_names(default_provider, settings)
    if allowed_providers is not None and provider not in allowed_providers:
        if provider_requested:
            allowed_providers = sorted({*allowed_providers, provider})
        else:
            raise LLMProviderConfigError(
                f"Provider '{provider}' is not allowed for runtime selection. "
                "Set LLM_ALLOWED_PROVIDERS to include it."
            )

    default_model = _default_model_for_provider(
        provider,
        include_runtime_override=provider == default_provider,
        settings=settings,
    )
    requested_model = model_name.strip() if isinstance(model_name, str) else None
    requested_model = requested_model or None
    model = _normalize_model_for_provider(provider, requested_model or default_model)
    allowed_models = _allowed_model_names(provider, default_model, settings)
    if (
        requested_model
        and allowed_models is not None
        and model not in allowed_models
        and provider_requested
    ):
        allowed_models = sorted({*allowed_models, model})
    if allowed_models is not None and model not in allowed_models:
        raise LLMProviderConfigError(
            f"Model '{model}' is not allowed for provider '{provider}'. "
            f"Set {provider.upper().replace('-', '_')}_ALLOWED_MODELS to include it."
        )

    return LLMRuntimeConfig(
        provider=provider,
        model=model,
        requested_provider=provider_name.strip() if isinstance(provider_name, str) and provider_name.strip() else None,
        requested_model=requested_model,
        provider_overridden=bool(provider_name and provider != default_provider),
        model_overridden=bool(requested_model),
        allowed_providers=allowed_providers,
        allowed_models=allowed_models,
        configured_providers=configured_providers,
    )


def get_llm_runtime_debug_config(settings: Optional[Settings] = None) -> Dict[str, Any]:
    return resolve_llm_runtime_config(settings=settings).as_debug_dict()


def _normalize_model_name(model_name: str) -> str:
    normalized = model_name.strip().removeprefix("models/")
    if "/models/" in normalized:
        normalized = normalized.rsplit("/models/", 1)[-1]
    return normalized


def _model_is_available(model_name: str, available_models: List[str]) -> bool:
    requested = _normalize_model_name(model_name)
    return any(_normalize_model_name(candidate) == requested for candidate in available_models)


def _schema_name(schema_class: Any) -> str:
    raw_name = getattr(schema_class, "__name__", "StructuredResponse")
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in raw_name)


def _schema_with_closed_objects(schema: Any) -> Any:
    """Return a JSON schema copy with every object type explicitly closed.

    Anthropic and GMI's structured JSON schema endpoints reject object schemas
    unless `additionalProperties` is explicitly `false` on each object node.
    """
    if isinstance(schema, list):
        return [_schema_with_closed_objects(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    normalized = {key: _schema_with_closed_objects(value) for key, value in schema.items()}
    if normalized.get("type") == "object":
        normalized["additionalProperties"] = False
    return normalized


def _is_anthropic_grammar_timeout(exc: Exception) -> bool:
    return "Grammar compilation timed out" in str(exc)


def _strip_json_markdown(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_document(text: str) -> Optional[str]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in {"{", "["}:
            continue
        try:
            _, end_index = decoder.raw_decode(text[index:])
            return text[index:index + end_index]
        except json.JSONDecodeError:
            continue
    return None


def _salvage_json_text(text: str) -> tuple[Optional[dict], List[str]]:
    """Repair a truncated/malformed JSON object from the first '{' onward.

    Uses json_repair to close unbalanced structure left behind when the model
    hits its token budget mid-object. It only closes brackets/quotes and drops
    trailing garbage; it never invents field content, so a semantically wrong
    record still fails the pydantic validation that gates the result.

    Returns (object-or-None, notes). json_repair is imported lazily so the
    dependency is only needed when salvage runs; on ImportError we log once and
    return (None, [note]).
    """
    global _json_repair_import_warned
    start = text.find("{")
    if start == -1:
        return None, ["no JSON object found to salvage"]
    try:
        import json_repair
    except ImportError:
        if not _json_repair_import_warned:
            logger.warning(
                "json_repair is not installed; truncated structured output cannot be salvaged. "
                "Install json-repair>=0.30.0 to enable JSON salvage."
            )
            _json_repair_import_warned = True
        return None, ["json_repair unavailable"]
    obj = json_repair.loads(text[start:])
    if not isinstance(obj, dict):
        return None, [f"salvage produced {type(obj).__name__}, not an object"]
    return obj, ["closed truncated/malformed JSON structure"]


def _prune_truncated_tail(obj: Any) -> tuple[Any, List[str]]:
    """Drop the half-written trailing list item truncation leaves behind.

    After json_repair closes a truncated record, the LAST element of the list
    that was being written is usually incomplete (a dict missing keys its
    siblings all have). Detect exactly that - last item of a list of >= 2 dicts,
    key set a proper subset of the first item's - and drop it. Only the tail of
    each list is considered, so legitimately sparse items elsewhere are kept.
    """
    notes: List[str] = []

    def _prune(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _prune(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list) and len(node) >= 2:
            first, last = node[0], node[-1]
            if (isinstance(first, dict) and isinstance(last, dict)
                    and set(last) < set(first)):
                node.pop()
                notes.append(f"dropped incomplete trailing item in {path}")
            for i, v in enumerate(node):
                _prune(v, f"{path}[{i}]")

    _prune(obj, "")
    return obj, notes


def _validate_structured_json(response_text: str, schema_class: Any) -> Any:
    try:
        return schema_class.model_validate_json(response_text)
    except Exception as first_error:
        cleaned = _strip_json_markdown(response_text)
        if cleaned != response_text:
            try:
                return schema_class.model_validate_json(cleaned)
            except Exception:
                pass

        extracted = _extract_json_document(cleaned)
        if extracted:
            try:
                return schema_class.model_validate_json(extracted)
            except Exception:
                pass

        # Fourth attempt: salvage a truncated completion. json_repair closes the
        # structure, the pruner deletes the half-written trailing item, and full
        # pydantic validation still gates the result - salvage cannot invent
        # content, only recover a record the model finished but got cut off.
        salvaged, notes = _salvage_json_text(cleaned)
        if salvaged is not None:
            salvaged, prune_notes = _prune_truncated_tail(salvaged)
            notes = notes + prune_notes
            try:
                result = schema_class.model_validate(salvaged)
                logger.warning(
                    "Structured output for %s required JSON salvage (%d chars): %s",
                    _schema_name(schema_class),
                    len(response_text),
                    "; ".join(notes) or "no changes",
                )
                return result
            except Exception:
                pass

        raise first_error


class StructuredLLMProvider:
    provider_name = "base"
    requested_model = "simulation"
    fallback_model: Optional[str] = None
    strict_mode = False
    model_name = "simulation"
    is_configured = False

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        raise NotImplementedError

    def get_debug_config(self) -> Dict[str, Any]:
        validation = self.validate_configured_model(raise_on_strict=False)
        return enforce_production_llm_preflight(validation).as_debug_dict()

    def validate_image_input(self) -> None:
        if model_image_input_support(self.provider_name, self.model_name) is False:
            raise LLMProviderInputError(_image_input_error_message(self.provider_name, self.model_name))

    def generate_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Any:
        raise NotImplementedError


class SimulationProvider(StructuredLLMProvider):
    provider_name = "simulation"
    requested_model = "simulation"
    model_name = "simulation"
    is_configured = False

    def __init__(self, validation_error: Optional[str] = None):
        self.validation_error = validation_error or "No live LLM provider is configured; simulation mode is active."
        self._validation: Optional[LLMProviderValidation] = None

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        if not self._validation:
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=False,
                fallback_active=False,
                fallback_model=None,
                validation_error=self.validation_error,
                live_generation_enabled=False,
            )
        return self._validation

    def generate_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Any:
        raise RuntimeError("Simulation provider cannot generate live structured responses.")


class GeminiProvider(StructuredLLMProvider):
    provider_name = "gemini"
    provider_label = "Gemini"

    def __init__(self, model_name: Optional[str] = None, settings: Optional[Settings] = None):
        self.settings = settings
        (_env, _first_env, _first_env_bool, _first_env_float, _first_env_optional_float, _first_env_int,
         _first_env_optional_string, _runpod_openai_base_url_from_env, _runpod_serverless_endpoint_url_from_env,
         _runpod_serverless_url_env_name, _model_endpoint_map, _cloudflare_base_url_from_env) = _scoped_env_helpers(settings)
        self.api_key = _first_env(["GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"])
        self.requested_model = model_name or _first_env(["LLM_MODEL", "GEMINI_MODEL"], DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL
        self.fallback_model = (
            _first_env(["LLM_FALLBACK_MODEL", "GEMINI_FALLBACK_MODEL"], DEFAULT_GEMINI_FALLBACK_MODEL)
            or DEFAULT_GEMINI_FALLBACK_MODEL
        )
        self.strict_mode = _first_env_bool(["STRICT_LLM", "STRICT_GEMINI"], default=True)
        self.timeout_seconds = _first_env_float(
            ["GEMINI_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"],
            DEFAULT_TIMEOUT_SECONDS,
        )
        self.model_name = self.requested_model
        self.client = None
        self.init_error: Optional[str] = None
        self._validation: Optional[LLMProviderValidation] = None

        if self.api_key and genai:
            try:
                self.client = genai.Client(
                    api_key=self.api_key,
                    http_options=genai_types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
                )
                logger.info("%s LLM provider initialized successfully.", self.provider_label)
            except Exception as exc:
                self.init_error = f"Error initializing {self.provider_label} provider: {exc}"
                logger.error(self.init_error)
        elif self.api_key and not genai:
            self.init_error = f"{self.provider_label} credentials are set, but google-genai is unavailable."
            logger.warning("%s Running in simulated/fallback mode.", self.init_error)
        else:
            self.init_error = "No Gemini API key found."
            logger.warning("%s Live %s generation is disabled.", self.init_error, self.provider_label)

        self.is_configured = self.client is not None

    def _list_generate_content_models(self) -> List[str]:
        if self.client is None:
            return []

        available_models: List[str] = []
        for model in self.client.models.list():
            name = getattr(model, "name", None)
            if not name:
                continue

            supported_actions = getattr(model, "supported_actions", None)
            if supported_actions is None and isinstance(model, dict):
                supported_actions = model.get("supportedActions") or model.get("supported_actions")
            if supported_actions is None and self.provider_name == "vertex":
                # Vertex publisher-model listings currently omit capability
                # metadata even for Gemini generateContent models.
                available_models.append(name)
                continue
            supported_actions = supported_actions or []

            if "generateContent" in supported_actions:
                available_models.append(name)

        return available_models

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        if self._validation:
            if raise_on_strict and self._validation.validation_error and self.strict_mode:
                raise LLMProviderConfigError(self._validation.validation_error)
            return self._validation

        if self.client is None:
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=f"{self.init_error or f'{self.provider_label} provider is not configured'} Simulation mode is active.",
                live_generation_enabled=False,
            )
            return self._validation

        try:
            available_models = self._list_generate_content_models()
        except Exception as exc:
            validation_error = f"Unable to validate {self.provider_label} model availability: {exc}"
            actual_model = self.fallback_model if not self.strict_mode else None
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=actual_model,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=not self.strict_mode,
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            if self.strict_mode and raise_on_strict:
                raise LLMProviderConfigError(validation_error)
            self.model_name = actual_model or self.requested_model
            return self._validation

        requested_available = _model_is_available(self.requested_model, available_models)
        if requested_available:
            self.model_name = self.requested_model
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=self.model_name,
                requested_model_available=True,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                model_availability_checked=True,
            )
            return self._validation

        if self.strict_mode:
            validation_error = (
                f"Configured {self.provider_label} model {self.requested_model} is not available for this account/provider. "
                f"Check available models or configure a valid {self.provider_label} model ID."
            )
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=True,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            if raise_on_strict:
                raise LLMProviderConfigError(validation_error)
            return self._validation

        fallback_available = _model_is_available(self.fallback_model, available_models)
        if not fallback_available:
            validation_error = (
                f"Configured {self.provider_label} model {self.requested_model} is not available, and fallback model "
                f"{self.fallback_model} is not available for this API key/provider."
            )
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=False,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            raise LLMProviderConfigError(validation_error)

        self.model_name = self.fallback_model
        self._validation = LLMProviderValidation(
            provider=self.provider_name,
            requested_model=self.requested_model,
            actual_model=self.model_name,
            requested_model_available=False,
            strict_mode=False,
            fallback_active=True,
            fallback_model=self.fallback_model,
            model_availability_checked=True,
        )
        return self._validation

    def generate_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Any:
        if self.client is None or genai_types is None:
            raise RuntimeError(f"{self.provider_label} provider is not configured.")

        contents = []
        if image_bytes and image_mime_type:
            contents.append(genai_types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type))
        contents.append(prompt)

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema_class,
                temperature=0.2,
            ),
        )
        return _validate_structured_json(response.text, schema_class)


class VertexAIProvider(GeminiProvider):
    """Gemini on Vertex AI using Google Cloud Application Default Credentials."""

    provider_name = "vertex"
    provider_label = "Vertex AI"

    def __init__(self, model_name: Optional[str] = None, settings: Optional[Settings] = None):
        (_env, _first_env, _first_env_bool, _first_env_float, _first_env_optional_float, _first_env_int,
         _first_env_optional_string, _runpod_openai_base_url_from_env, _runpod_serverless_endpoint_url_from_env,
         _runpod_serverless_url_env_name, _model_endpoint_map, _cloudflare_base_url_from_env) = _scoped_env_helpers(settings)
        self.project = _first_env(
            ["VERTEX_AI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"]
        )
        self.location = (
            _first_env(["VERTEX_AI_LOCATION", "GOOGLE_CLOUD_LOCATION"], DEFAULT_VERTEX_LOCATION)
            or DEFAULT_VERTEX_LOCATION
        )
        self.api_key = None
        self.requested_model = (
            model_name
            or _first_env(["LLM_MODEL", "VERTEX_AI_MODEL", "VERTEX_MODEL"], DEFAULT_VERTEX_MODEL)
            or DEFAULT_VERTEX_MODEL
        )
        self.fallback_model = (
            _first_env(
                ["LLM_FALLBACK_MODEL", "VERTEX_AI_FALLBACK_MODEL", "VERTEX_FALLBACK_MODEL"],
                DEFAULT_VERTEX_FALLBACK_MODEL,
            )
            or DEFAULT_VERTEX_FALLBACK_MODEL
        )
        self.strict_mode = _first_env_bool(["STRICT_LLM", "STRICT_VERTEX_AI", "STRICT_VERTEX"], default=True)
        self.timeout_seconds = _first_env_float(
            ["VERTEX_AI_TIMEOUT_SECONDS", "VERTEX_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"],
            DEFAULT_TIMEOUT_SECONDS,
        )
        self.model_name = self.requested_model
        self.client = None
        self.init_error: Optional[str] = None
        self._validation: Optional[LLMProviderValidation] = None

        if self.project and genai:
            try:
                client_config: Dict[str, Any] = {
                    "vertexai": True,
                    "project": self.project,
                    "location": self.location,
                    "http_options": genai_types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
                }
                credentials = build_vertex_credentials()
                if credentials is not None:
                    client_config["credentials"] = credentials
                self.client = genai.Client(
                    **client_config,
                )
                logger.info(
                    "Vertex AI LLM provider initialized for project %s in %s.",
                    self.project,
                    self.location,
                )
            except Exception as exc:
                self.init_error = f"Error initializing Vertex AI provider: {exc}"
                logger.error(self.init_error)
        elif self.project and not genai:
            self.init_error = "Vertex AI project is set, but google-genai is unavailable."
            logger.warning("%s Running in simulated/fallback mode.", self.init_error)
        else:
            self.init_error = (
                "No Vertex AI project found. Set VERTEX_AI_PROJECT or GOOGLE_CLOUD_PROJECT "
                "and configure Application Default Credentials."
            )
            logger.warning("%s Live Vertex AI generation is disabled.", self.init_error)

        self.is_configured = self.client is not None


class AnthropicProvider(StructuredLLMProvider):
    provider_name = "anthropic"

    def __init__(self, model_name: Optional[str] = None, settings: Optional[Settings] = None):
        (_env, _first_env, _first_env_bool, _first_env_float, _first_env_optional_float, _first_env_int,
         _first_env_optional_string, _runpod_openai_base_url_from_env, _runpod_serverless_endpoint_url_from_env,
         _runpod_serverless_url_env_name, _model_endpoint_map, _cloudflare_base_url_from_env) = _scoped_env_helpers(settings)
        self.api_key = _first_env(["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
        self.base_url = (
            _first_env(["ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL"], DEFAULT_ANTHROPIC_BASE_URL)
            or DEFAULT_ANTHROPIC_BASE_URL
        ).rstrip("/")
        self.requested_model = model_name or _first_env(
            ["ANTHROPIC_MODEL", "CLAUDE_MODEL", "LLM_MODEL"],
            DEFAULT_ANTHROPIC_MODEL,
        ) or DEFAULT_ANTHROPIC_MODEL
        self.fallback_model = _first_env(["ANTHROPIC_FALLBACK_MODEL", "CLAUDE_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"])
        self.strict_mode = _first_env_bool(["STRICT_ANTHROPIC", "STRICT_CLAUDE", "STRICT_LLM"], default=True)
        self.validate_models = not forma_dev_mode_enabled() or _first_env_bool(
            ["ANTHROPIC_VALIDATE_MODELS", "CLAUDE_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"],
            default=False,
        )
        self.timeout_seconds = _first_env_float(["ANTHROPIC_TIMEOUT_SECONDS", "CLAUDE_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"], DEFAULT_OPENAI_TIMEOUT_SECONDS)
        self.max_tokens = _first_env_int(["ANTHROPIC_MAX_TOKENS", "CLAUDE_MAX_TOKENS", "LLM_MAX_TOKENS"])
        self.temperature = _first_env_optional_float(["ANTHROPIC_TEMPERATURE", "CLAUDE_TEMPERATURE", "LLM_TEMPERATURE"], default=None)
        self.anthropic_version = _first_env(["ANTHROPIC_VERSION", "CLAUDE_API_VERSION"], DEFAULT_ANTHROPIC_VERSION) or DEFAULT_ANTHROPIC_VERSION
        self.user_agent = _env("LLM_USER_AGENT", DEFAULT_HTTP_USER_AGENT) or DEFAULT_HTTP_USER_AGENT
        self.use_output_config = _first_env_bool(["ANTHROPIC_JSON_SCHEMA_OUTPUT", "CLAUDE_JSON_SCHEMA_OUTPUT"], default=True)
        self.model_name = self.requested_model
        self._validation: Optional[LLMProviderValidation] = None
        self.is_configured = bool(self.api_key and self.base_url)
        if self.is_configured:
            logger.info("Anthropic provider initialized for model %s.", self.requested_model)
        else:
            logger.warning("Anthropic provider is missing ANTHROPIC_API_KEY/CLAUDE_API_KEY or base URL.")

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "x-api-key": self.api_key or "",
            "anthropic-version": self.anthropic_version,
        }

    def _request_json(self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"anthropic request failed with HTTP {exc.code}: {detail[:500]}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RuntimeError(
                f"anthropic request timed out after {self.timeout_seconds:.1f}s. "
                "Increase ANTHROPIC_TIMEOUT_SECONDS/CLAUDE_TIMEOUT_SECONDS or use a lower-latency model."
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                raise RuntimeError(
                    f"anthropic request timed out after {self.timeout_seconds:.1f}s. "
                    "Increase ANTHROPIC_TIMEOUT_SECONDS/CLAUDE_TIMEOUT_SECONDS or use a lower-latency model."
                ) from exc
            raise RuntimeError(f"anthropic request failed: {exc}") from exc

        if not body.strip():
            return {}
        return json.loads(body)

    def _list_models(self) -> List[str]:
        payload = self._request_json("models")
        data = payload.get("data", [])
        if not isinstance(data, list):
            return []
        models: List[str] = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    models.append(str(model_id))
            elif isinstance(item, str):
                models.append(item)
        return models

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        if self._validation:
            if raise_on_strict and self._validation.validation_error and self.strict_mode:
                raise LLMProviderConfigError(self._validation.validation_error)
            return self._validation

        if not self.is_configured:
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error="anthropic provider is not configured. Set ANTHROPIC_API_KEY or CLAUDE_API_KEY.",
                live_generation_enabled=False,
            )
            return self._validation

        if not self.validate_models:
            self.model_name = self.requested_model
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=self.model_name,
                requested_model_available=True,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                model_availability_checked=False,
            )
            return self._validation

        try:
            available_models = self._list_models()
        except Exception as exc:
            validation_error = f"Unable to validate Anthropic model availability: {exc}"
            actual_model = self.fallback_model if (self.fallback_model and not self.strict_mode) else None
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=actual_model,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=bool(actual_model),
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            if self.strict_mode and raise_on_strict:
                raise LLMProviderConfigError(validation_error)
            self.model_name = actual_model or self.requested_model
            return self._validation

        requested_available = _model_is_available(self.requested_model, available_models)
        if requested_available:
            self.model_name = self.requested_model
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=self.model_name,
                requested_model_available=True,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                model_availability_checked=True,
            )
            return self._validation

        if self.strict_mode or not self.fallback_model:
            validation_error = f"Configured Anthropic model {self.requested_model} is not available for this API key."
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            if raise_on_strict:
                raise LLMProviderConfigError(validation_error)
            return self._validation

        self.model_name = self.fallback_model
        self._validation = LLMProviderValidation(
            provider=self.provider_name,
            requested_model=self.requested_model,
            actual_model=self.model_name,
            requested_model_available=False,
            strict_mode=False,
            fallback_active=True,
            fallback_model=self.fallback_model,
            model_availability_checked=True,
        )
        return self._validation

    def _build_structured_prompt(self, prompt: str, schema_class: Any, *, use_output_config: Optional[bool] = None) -> str:
        should_use_output_config = self.use_output_config if use_output_config is None else use_output_config
        if should_use_output_config:
            return prompt
        return (
            f"{prompt}\n\n"
            "Return only valid JSON. The JSON must conform to this schema:\n"
            f"{json.dumps(schema_class.model_json_schema(), indent=2)}"
        )

    def _build_content(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
        use_output_config: Optional[bool] = None,
    ) -> Any:
        structured_prompt = self._build_structured_prompt(prompt, schema_class, use_output_config=use_output_config)
        content: List[Dict[str, Any]] = []
        if image_bytes and image_mime_type:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": structured_prompt})
        return content

    def _structured_max_tokens(self, schema_class: Any) -> int:
        budget = self.max_tokens or DEFAULT_STRUCTURED_MAX_TOKENS
        schema_chars = len(json.dumps(schema_class.model_json_schema()))
        if schema_chars >= LARGE_SCHEMA_CHAR_THRESHOLD and budget < STRUCTURED_MAX_TOKENS_FLOOR:
            return STRUCTURED_MAX_TOKENS_FLOOR
        return budget

    def _extract_text(self, response: Dict[str, Any]) -> str:
        content = response.get("content") or []
        if not isinstance(content, list):
            return ""
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()

    def generate_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Any:
        if not self.is_configured:
            raise RuntimeError("anthropic provider is not configured.")

        budget = self._structured_max_tokens(schema_class)
        using_output_config = self.use_output_config
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": budget,
            "system": "You produce concise, valid JSON only. Do not include markdown or commentary.",
            "messages": [
                {
                    "role": "user",
                    "content": self._build_content(
                        prompt,
                        schema_class,
                        image_bytes,
                        image_mime_type,
                        use_output_config=using_output_config,
                    ),
                }
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if using_output_config:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": _schema_with_closed_objects(schema_class.model_json_schema()),
                }
            }

        last_error: Optional[Exception] = None
        stop_reason: Optional[str] = None
        attempt = 0
        while attempt < 2:
            payload["max_tokens"] = budget
            try:
                response = self._request_json("messages", method="POST", payload=payload)
            except RuntimeError as exc:
                if using_output_config and _is_anthropic_grammar_timeout(exc):
                    using_output_config = False
                    payload.pop("output_config", None)
                    payload["messages"] = [
                        {
                            "role": "user",
                            "content": self._build_content(
                                prompt,
                                schema_class,
                                image_bytes,
                                image_mime_type,
                                use_output_config=False,
                            ),
                        }
                    ]
                    logger.warning(
                        "anthropic output_config grammar compilation timed out for %s; retrying with prompt-embedded schema.",
                        _schema_name(schema_class),
                    )
                    continue
                raise
            stop_reason = response.get("stop_reason") if isinstance(response.get("stop_reason"), str) else None
            content = self._extract_text(response)
            if not content:
                raise RuntimeError("anthropic response did not include text content.")
            try:
                return _validate_structured_json(content, schema_class)
            except Exception as validation_error:
                last_error = validation_error
                attempt += 1
                if attempt < 2:
                    budget = min(max(budget * 2, STRUCTURED_MAX_TOKENS_FLOOR), STRUCTURED_MAX_TOKENS_CEILING)
                    logger.warning(
                        "anthropic produced unusable %s (stop_reason=%s, content=%d chars); retrying once with max_tokens=%d.",
                        _schema_name(schema_class),
                        stop_reason,
                        len(content),
                        budget,
                    )

        raise LLMProviderOutputError(
            f"anthropic returned unusable structured output for {_schema_name(schema_class)} "
            f"after retry (stop_reason={stop_reason}, max_tokens={budget}): {last_error}"
        ) from last_error


class OpenAICompatibleProvider(StructuredLLMProvider):
    def __init__(self, provider_name: str = "openai", model_name: Optional[str] = None, settings: Optional[Settings] = None):
        (_env, _first_env, _first_env_bool, _first_env_float, _first_env_optional_float, _first_env_int,
         _first_env_optional_string, _runpod_openai_base_url_from_env, _runpod_serverless_endpoint_url_from_env,
         _runpod_serverless_url_env_name, _model_endpoint_map, _cloudflare_base_url_from_env) = _scoped_env_helpers(settings)
        normalized_provider = normalize_llm_provider_name(provider_name) or "openai"
        if normalized_provider in {"baseten", "gmi", "huggingface", "cloudflare", "nvidia", "openai", "runpod", "together", "xai"}:
            self.provider_name = normalized_provider
        else:
            self.provider_name = "openai-compatible"
        self.configuration_error: Optional[str] = None
        if self.provider_name == "baseten":
            api_key_names = ["BASETEN_API_KEY", "LLM_API_KEY"]
            base_url_names = ["BASETEN_BASE_URL", "LLM_BASE_URL"]
            model_names = ["BASETEN_MODEL", "LLM_MODEL"]
            fallback_model_names = ["BASETEN_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_BASETEN", "STRICT_LLM"]
            validate_model_names = ["BASETEN_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["BASETEN_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["BASETEN_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["BASETEN_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["BASETEN_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["BASETEN_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["BASETEN_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_BASETEN_MODEL
            default_base_url = DEFAULT_BASETEN_BASE_URL
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "gmi":
            api_key_names = ["GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY", "LLM_API_KEY"]
            base_url_names = ["GMI_BASE_URL", "GMI_CLOUD_BASE_URL", "GMICLOUD_BASE_URL"]
            model_names = ["GMI_MODEL", "GMI_CLOUD_MODEL", "GMICLOUD_MODEL", "LLM_MODEL"]
            fallback_model_names = ["GMI_FALLBACK_MODEL", "GMI_CLOUD_FALLBACK_MODEL", "GMICLOUD_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_GMI", "STRICT_GMI_CLOUD", "STRICT_GMICLOUD", "STRICT_LLM"]
            validate_model_names = ["GMI_VALIDATE_MODELS", "GMI_CLOUD_VALIDATE_MODELS", "GMICLOUD_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["GMI_RESPONSE_FORMAT", "GMI_CLOUD_RESPONSE_FORMAT", "GMICLOUD_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["GMI_TIMEOUT_SECONDS", "GMI_CLOUD_TIMEOUT_SECONDS", "GMICLOUD_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["GMI_MAX_TOKENS", "GMI_CLOUD_MAX_TOKENS", "GMICLOUD_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["GMI_TEMPERATURE", "GMI_CLOUD_TEMPERATURE", "GMICLOUD_TEMPERATURE"]
            reasoning_effort_names = ["GMI_REASONING_EFFORT", "GMI_CLOUD_REASONING_EFFORT", "GMICLOUD_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["GMI_ALLOW_NO_API_KEY", "GMI_CLOUD_ALLOW_NO_API_KEY", "GMICLOUD_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_GMI_MODEL
            default_base_url = DEFAULT_GMI_BASE_URL
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "huggingface":
            api_key_names = ["HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN", "HF_TOKEN", "HF_API_TOKEN"]
            base_url_names = ["HUGGINGFACE_BASE_URL", "HF_BASE_URL"]
            model_names = ["HUGGINGFACE_MODEL", "HF_MODEL"]
            fallback_model_names = ["HUGGINGFACE_FALLBACK_MODEL", "HF_FALLBACK_MODEL"]
            strict_names = ["STRICT_HUGGINGFACE", "STRICT_HF", "STRICT_LLM"]
            validate_model_names = ["HUGGINGFACE_VALIDATE_MODELS", "HF_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["HUGGINGFACE_RESPONSE_FORMAT", "HF_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["HUGGINGFACE_TIMEOUT_SECONDS", "HF_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["HUGGINGFACE_MAX_TOKENS", "HF_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["HUGGINGFACE_TEMPERATURE", "HF_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["HUGGINGFACE_REASONING_EFFORT", "HF_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["HUGGINGFACE_ALLOW_NO_API_KEY", "HF_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_HUGGINGFACE_MODEL
            default_base_url = DEFAULT_HUGGINGFACE_BASE_URL
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "cloudflare":
            api_key_names = ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_API_KEY", "CLOUDFLARE_API_KEY", "LLM_API_KEY"]
            base_url_names = ["CLOUDFLARE_BASE_URL"]
            model_names = ["CLOUDFLARE_MODEL", "LLM_MODEL"]
            fallback_model_names = ["CLOUDFLARE_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_CLOUDFLARE", "STRICT_LLM"]
            validate_model_names = ["CLOUDFLARE_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["CLOUDFLARE_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["CLOUDFLARE_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["CLOUDFLARE_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["CLOUDFLARE_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["CLOUDFLARE_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["CLOUDFLARE_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_CLOUDFLARE_MODEL
            default_base_url = None
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "nvidia":
            api_key_names = ["NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NIM_API_KEY", "LLM_API_KEY"]
            base_url_names = ["NVIDIA_BASE_URL", "NVIDIA_NIM_BASE_URL", "NIM_BASE_URL", "LLM_BASE_URL"]
            model_names = ["NVIDIA_MODEL", "NVIDIA_NIM_MODEL", "NIM_MODEL", "LLM_MODEL"]
            fallback_model_names = ["NVIDIA_FALLBACK_MODEL", "NVIDIA_NIM_FALLBACK_MODEL", "NIM_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_NVIDIA", "STRICT_NVIDIA_NIM", "STRICT_NIM", "STRICT_LLM"]
            validate_model_names = ["NVIDIA_VALIDATE_MODELS", "NVIDIA_NIM_VALIDATE_MODELS", "NIM_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["NVIDIA_RESPONSE_FORMAT", "NVIDIA_NIM_RESPONSE_FORMAT", "NIM_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["NVIDIA_TIMEOUT_SECONDS", "NVIDIA_NIM_TIMEOUT_SECONDS", "NIM_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["NVIDIA_MAX_TOKENS", "NVIDIA_NIM_MAX_TOKENS", "NIM_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["NVIDIA_TEMPERATURE", "NVIDIA_NIM_TEMPERATURE", "NIM_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["NVIDIA_REASONING_EFFORT", "NVIDIA_NIM_REASONING_EFFORT", "NIM_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["NVIDIA_ALLOW_NO_API_KEY", "NVIDIA_NIM_ALLOW_NO_API_KEY", "NIM_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_NVIDIA_MODEL
            default_base_url = DEFAULT_NVIDIA_BASE_URL
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "xai":
            api_key_names = ["XAI_API_KEY", "GROK_API_KEY", "LLM_API_KEY"]
            base_url_names = ["XAI_BASE_URL", "GROK_BASE_URL", "LLM_BASE_URL"]
            model_names = ["XAI_MODEL", "GROK_MODEL", "LLM_MODEL"]
            fallback_model_names = ["XAI_FALLBACK_MODEL", "GROK_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_XAI", "STRICT_GROK", "STRICT_LLM"]
            validate_model_names = ["XAI_VALIDATE_MODELS", "GROK_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["XAI_RESPONSE_FORMAT", "GROK_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["XAI_TIMEOUT_SECONDS", "GROK_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["XAI_MAX_TOKENS", "GROK_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["XAI_TEMPERATURE", "GROK_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["XAI_REASONING_EFFORT", "GROK_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["XAI_ALLOW_NO_API_KEY", "GROK_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_XAI_MODEL
            default_base_url = DEFAULT_XAI_BASE_URL
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "together":
            api_key_names = ["TOGETHER_API_KEY", "TOGETHER_IMAGE_API_KEY", "LLM_API_KEY"]
            base_url_names = ["TOGETHER_LLM_BASE_URL", "TOGETHER_BASE_URL", "LLM_BASE_URL"]
            model_names = ["TOGETHER_MODEL", "LLM_MODEL"]
            fallback_model_names = ["TOGETHER_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_TOGETHER", "STRICT_LLM"]
            validate_model_names = ["TOGETHER_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["TOGETHER_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["TOGETHER_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["TOGETHER_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["TOGETHER_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["TOGETHER_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["TOGETHER_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_TOGETHER_MODEL
            default_base_url = DEFAULT_TOGETHER_BASE_URL
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "openai":
            api_key_names = ["OPENAI_API_KEY", "LLM_API_KEY"]
            base_url_names = ["OPENAI_BASE_URL"]
            model_names = ["OPENAI_MODEL", "LLM_MODEL"]
            fallback_model_names = ["OPENAI_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_OPENAI", "STRICT_LLM"]
            validate_model_names = ["OPENAI_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["OPENAI_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["OPENAI_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["OPENAI_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["OPENAI_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["OPENAI_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["OPENAI_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_OPENAI_MODEL
            default_base_url = "https://api.openai.com/v1"
            default_timeout_seconds = DEFAULT_OPENAI_TIMEOUT_SECONDS
        elif self.provider_name == "runpod":
            api_key_names = ["RUNPOD_API_KEY", "LLM_API_KEY"]
            base_url_names = ["RUNPOD_OPENAI_BASE_URL", "RUNPOD_BASE_URL", "LLM_BASE_URL"]
            model_names = ["RUNPOD_OPENAI_MODEL", "LLM_MODEL", "RUNPOD_MODEL", "RUNPOD_PARTI_MODEL"]
            fallback_model_names = ["RUNPOD_OPENAI_FALLBACK_MODEL", "RUNPOD_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"]
            strict_names = ["STRICT_RUNPOD", "STRICT_LLM"]
            validate_model_names = ["RUNPOD_VALIDATE_MODELS", "LLM_VALIDATE_MODELS"]
            response_format_names = ["RUNPOD_RESPONSE_FORMAT", "LLM_RESPONSE_FORMAT"]
            timeout_names = ["RUNPOD_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"]
            max_tokens_names = ["RUNPOD_MAX_TOKENS", "LLM_MAX_TOKENS"]
            temperature_names = ["RUNPOD_TEMPERATURE", "LLM_TEMPERATURE"]
            reasoning_effort_names = ["RUNPOD_REASONING_EFFORT", "LLM_REASONING_EFFORT"]
            allow_no_api_key_names = ["RUNPOD_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]
            default_model_name = "runpod-default"
            default_base_url = None
            default_timeout_seconds = DEFAULT_RUNPOD_TIMEOUT_SECONDS
        else:
            api_key_names = ["LLM_API_KEY", "OPENAI_API_KEY"]
            base_url_names = ["LLM_BASE_URL", "OPENAI_BASE_URL"]
            model_names = ["LLM_MODEL", "OPENAI_MODEL"]
            fallback_model_names = ["LLM_FALLBACK_MODEL", "OPENAI_FALLBACK_MODEL"]
            strict_names = ["STRICT_LLM", "STRICT_OPENAI"]
            validate_model_names = ["LLM_VALIDATE_MODELS", "OPENAI_VALIDATE_MODELS"]
            response_format_names = ["LLM_RESPONSE_FORMAT", "OPENAI_RESPONSE_FORMAT"]
            timeout_names = ["LLM_TIMEOUT_SECONDS", "OPENAI_TIMEOUT_SECONDS"]
            max_tokens_names = ["LLM_MAX_TOKENS", "OPENAI_MAX_TOKENS"]
            temperature_names = ["LLM_TEMPERATURE", "OPENAI_TEMPERATURE"]
            reasoning_effort_names = ["LLM_REASONING_EFFORT", "OPENAI_REASONING_EFFORT"]
            allow_no_api_key_names = ["LLM_ALLOW_NO_API_KEY", "OPENAI_ALLOW_NO_API_KEY"]
            default_model_name = DEFAULT_OPENAI_MODEL
            default_base_url = None
            default_timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        self.api_key = _first_env(api_key_names)
        self.organization_id = _first_env(["OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_ORGANIZATION_ID"])
        self.project_id = _first_env(["OPENAI_PROJECT_ID", "OPENAI_PROJECT"])
        if self.provider_name == "runpod":
            configured_base_url = _runpod_openai_base_url_from_env()
        elif self.provider_name == "cloudflare":
            configured_base_url = _cloudflare_base_url_from_env()
        else:
            configured_base_url = _first_env(base_url_names)
        if self.provider_name == "runpod" and configured_base_url is None:
            runpod_serverless_url_env_name = _runpod_serverless_url_env_name()
            if runpod_serverless_url_env_name:
                configured_base_url = _env(runpod_serverless_url_env_name)
                self.configuration_error = _runpod_serverless_url_misconfiguration_message(runpod_serverless_url_env_name)
        self.base_url = (configured_base_url or default_base_url or "").rstrip("/")
        parsed_base_url = urllib.parse.urlparse(self.base_url)
        base_host = (parsed_base_url.hostname or "").lower()
        self.ollama_native_chat = bool(
            self.provider_name == "openai-compatible"
            and base_host in {"localhost", "127.0.0.1", "::1"}
            and (parsed_base_url.port == 11434 or _first_env_bool(["OLLAMA_NATIVE_CHAT"], default=False))
        )
        self.ollama_context_length = _first_env_int(["OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_CTX", "LLM_CONTEXT_LENGTH"])
        if self.ollama_native_chat and self.ollama_context_length is None:
            self.ollama_context_length = 16384
        self.ollama_think = _first_env_bool(["OLLAMA_THINK"], default=False)
        self.requested_model = _normalize_model_for_provider(
            self.provider_name,
            model_name or _first_env(model_names, default_model_name) or default_model_name,
        )
        raw_fallback_model = _first_env(fallback_model_names)
        self.fallback_model = _normalize_model_for_provider(self.provider_name, raw_fallback_model) if raw_fallback_model else None
        self.strict_mode = _first_env_bool(strict_names, default=True)
        self.validate_models = not forma_dev_mode_enabled() or _first_env_bool(
            validate_model_names,
            default=False,
        )
        default_response_format = "json_schema" if self.provider_name in {"cloudflare", "openai"} else "json_object"
        self.response_format = (
            _first_env(response_format_names, default_response_format)
            or default_response_format
        ).strip().lower().replace("-", "_")
        self.timeout_seconds = _first_env_float(timeout_names, default_timeout_seconds)
        self.max_tokens = _first_env_int(max_tokens_names)
        self.temperature = _first_env_optional_float(
            temperature_names,
            default=None if self.provider_name in {"gmi", "openai"} else 0.2,
        )
        self.reasoning_effort = _first_env_optional_string(
            reasoning_effort_names,
            omit_values=["default", "omit"],
        )
        self.cloudflare_enable_thinking = (
            _first_env_bool(["CLOUDFLARE_ENABLE_THINKING"], default=False) if self.provider_name == "cloudflare" else None
        )
        self.user_agent = _env("LLM_USER_AGENT", DEFAULT_HTTP_USER_AGENT) or DEFAULT_HTTP_USER_AGENT
        self.allow_no_api_key = _first_env_bool(
            allow_no_api_key_names,
            default=self.provider_name == "openai-compatible" and configured_base_url is not None,
        )
        self.model_name = self.requested_model
        self._validation: Optional[LLMProviderValidation] = None

        self.is_configured = bool(self.base_url and (self.api_key or self.allow_no_api_key))
        if self.is_configured:
            logger.info("%s LLM provider initialized for model %s.", self.provider_name, self.requested_model)
        else:
            logger.warning("%s LLM provider is missing an API key or base URL.", self.provider_name)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider_name == "openai":
            if self.organization_id:
                headers["OpenAI-Organization"] = self.organization_id
            if self.project_id:
                headers["OpenAI-Project"] = self.project_id
        return headers

    def _request_json(self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        use_ollama_chat = bool(
            self.ollama_native_chat
            and method == "POST"
            and path.lstrip("/") == "chat/completions"
            and payload is not None
        )
        request_payload = payload
        if use_ollama_chat:
            parsed = urllib.parse.urlparse(self.base_url)
            url = f"{parsed.scheme}://{parsed.netloc}/api/chat"
            response_format = payload.get("response_format") if isinstance(payload, dict) else None
            native_format: Any = None
            if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
                json_schema = response_format.get("json_schema")
                if isinstance(json_schema, dict):
                    native_format = json_schema.get("schema")
            elif isinstance(response_format, dict) and response_format.get("type") == "json_object":
                native_format = "json"

            options: Dict[str, Any] = {
                "num_predict": payload.get("max_tokens") or DEFAULT_STRUCTURED_MAX_TOKENS,
            }
            if self.ollama_context_length:
                options["num_ctx"] = self.ollama_context_length
            if payload.get("temperature") is not None:
                options["temperature"] = payload["temperature"]
            request_payload = {
                "model": payload.get("model"),
                "messages": payload.get("messages") or [],
                "stream": False,
                "think": self.ollama_think,
                "options": options,
            }
            if native_format is not None:
                request_payload["format"] = native_format
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(request_payload).encode("utf-8") if request_payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{self.provider_name} request failed with HTTP {exc.code}: {detail[:500]}"
                f"{self._http_error_hint(exc.code, path)}"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            timeout_hint = self._timeout_hint()
            raise RuntimeError(
                f"{self.provider_name} request timed out after {self.timeout_seconds:.1f}s while reading {path}. "
                f"{timeout_hint}"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                timeout_hint = self._timeout_hint()
                raise RuntimeError(
                    f"{self.provider_name} request timed out after {self.timeout_seconds:.1f}s while reading {path}. "
                    f"{timeout_hint}"
                ) from exc
            raise RuntimeError(f"{self.provider_name} request failed: {exc}") from exc

        if not body.strip():
            return {}
        decoded = json.loads(body)
        if not use_ollama_chat:
            return decoded

        message = decoded.get("message") if isinstance(decoded.get("message"), dict) else {}
        return {
            "choices": [
                {
                    "message": message,
                    "finish_reason": decoded.get("done_reason") or ("stop" if decoded.get("done") else None),
                }
            ],
            "usage": {
                "prompt_tokens": decoded.get("prompt_eval_count"),
                "completion_tokens": decoded.get("eval_count"),
            },
            "model": decoded.get("model"),
        }

    def _http_error_hint(self, status_code: int, path: str) -> str:
        if self.provider_name == "runpod" and status_code == 404 and path.lstrip("/") == "chat/completions":
            return (
                " Check RUNPOD_OPENAI_BASE_URL: runpod requires an OpenAI-compatible URL like "
                "https://api.runpod.ai/v2/<endpoint-id>/openai/v1. For queue-style Serverless endpoints, "
                "use LLM_PROVIDER=runpod-serverless with RUNPOD_ENDPOINT_ID or RUNPOD_ENDPOINT_URL instead."
            )
        return ""

    def _timeout_hint(self) -> str:
        if self.provider_name == "baseten":
            return "Increase BASETEN_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "huggingface":
            return "Increase HUGGINGFACE_TIMEOUT_SECONDS/HF_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "gmi":
            return "Increase GMI_TIMEOUT_SECONDS/GMI_CLOUD_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "cloudflare":
            return "Increase CLOUDFLARE_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "nvidia":
            return "Increase NVIDIA_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "xai":
            return "Increase XAI_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "together":
            return "Increase TOGETHER_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."
        if self.provider_name == "runpod":
            return "Increase RUNPOD_TIMEOUT_SECONDS for long Runpod jobs."
        return "Increase OPENAI_TIMEOUT_SECONDS/LLM_TIMEOUT_SECONDS or use a lower-latency model/settings."

    def _configuration_hint(self) -> str:
        if self.provider_name == "baseten":
            return "Set BASETEN_API_KEY. BASETEN_BASE_URL defaults to https://inference.baseten.co/v1."
        if self.provider_name == "huggingface":
            return "Set HF_TOKEN, HUGGINGFACE_API_KEY, or HUGGINGFACE_HUB_TOKEN. HUGGINGFACE_BASE_URL defaults to https://router.huggingface.co/v1."
        if self.provider_name == "gmi":
            return "Set GMI_API_KEY or GMI_CLOUD_API_KEY. GMI_BASE_URL defaults to https://api.gmi-serving.com/v1."
        if self.provider_name == "cloudflare":
            return "Set CLOUDFLARE_API_TOKEN plus CLOUDFLARE_ACCOUNT_ID, or set CLOUDFLARE_BASE_URL explicitly."
        if self.provider_name == "nvidia":
            return "Set NVIDIA_API_KEY. NVIDIA_BASE_URL defaults to https://integrate.api.nvidia.com/v1."
        if self.provider_name == "xai":
            return "Set XAI_API_KEY. XAI_BASE_URL defaults to https://api.x.ai/v1."
        if self.provider_name == "together":
            return "Set TOGETHER_API_KEY. TOGETHER_LLM_BASE_URL defaults to https://api.together.xyz/v1."
        if self.provider_name == "runpod":
            return "Set RUNPOD_API_KEY plus RUNPOD_OPENAI_BASE_URL or RUNPOD_BASE_URL."
        if self.provider_name == "openai":
            return "Set OPENAI_API_KEY."
        return "Set LLM_API_KEY and LLM_BASE_URL, or set LLM_ALLOW_NO_API_KEY=true for a local OpenAI-compatible endpoint."

    def _list_models(self) -> List[str]:
        payload = self._request_json("models")
        data = payload.get("data", [])
        if not isinstance(data, list):
            return []

        models = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    models.append(model_id)
            elif isinstance(item, str):
                models.append(item)
        return models

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        if self._validation:
            if raise_on_strict and self._validation.validation_error and self.strict_mode:
                raise LLMProviderConfigError(self._validation.validation_error)
            return self._validation

        if self.configuration_error:
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=self.configuration_error,
                live_generation_enabled=False,
            )
            if self.strict_mode and raise_on_strict:
                raise LLMProviderConfigError(self.configuration_error)
            return self._validation

        if not self.is_configured:
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=f"{self.provider_name} provider is not configured. {self._configuration_hint()}",
                live_generation_enabled=False,
            )
            return self._validation

        if not self.validate_models:
            self.model_name = self.requested_model
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=self.model_name,
                requested_model_available=True,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                model_availability_checked=False,
            )
            return self._validation

        try:
            available_models = self._list_models()
        except Exception as exc:
            validation_error = f"Unable to validate {self.provider_name} model availability: {exc}"
            actual_model = self.fallback_model if (self.fallback_model and not self.strict_mode) else None
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=actual_model,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=bool(actual_model),
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            if self.strict_mode and raise_on_strict:
                raise LLMProviderConfigError(validation_error)
            self.model_name = actual_model or self.requested_model
            return self._validation

        requested_available = _model_is_available(self.requested_model, available_models)
        if requested_available:
            self.model_name = self.requested_model
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=self.model_name,
                requested_model_available=True,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                model_availability_checked=True,
            )
            return self._validation

        if self.strict_mode or not self.fallback_model:
            validation_error = (
                f"Configured {self.provider_name} model {self.requested_model} is not available for this endpoint."
            )
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            if raise_on_strict:
                raise LLMProviderConfigError(validation_error)
            return self._validation

        fallback_available = _model_is_available(self.fallback_model, available_models)
        if not fallback_available:
            validation_error = (
                f"Configured {self.provider_name} model {self.requested_model} is not available, and fallback model "
                f"{self.fallback_model} is not available for this endpoint."
            )
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=False,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=validation_error,
                model_availability_checked=True,
            )
            raise LLMProviderConfigError(validation_error)

        self.model_name = self.fallback_model
        self._validation = LLMProviderValidation(
            provider=self.provider_name,
            requested_model=self.requested_model,
            actual_model=self.model_name,
            requested_model_available=False,
            strict_mode=False,
            fallback_active=True,
            fallback_model=self.fallback_model,
            model_availability_checked=True,
        )
        return self._validation

    def _build_structured_prompt(self, prompt: str, schema_class: Any) -> str:
        schema_json = json.dumps(_schema_with_closed_objects(schema_class.model_json_schema()), indent=2)
        return (
            f"{prompt}\n\n"
            "Return only valid JSON. The JSON must conform to this schema:\n"
            f"{schema_json}"
        )

    def _build_user_content(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> Any:
        structured_prompt = self._build_structured_prompt(prompt, schema_class)
        if not image_bytes or not image_mime_type:
            return structured_prompt

        data = base64.b64encode(image_bytes).decode("ascii")
        return [
            {"type": "text", "text": structured_prompt},
            {"type": "image_url", "image_url": {"url": f"data:{image_mime_type};base64,{data}"}},
        ]

    def _structured_max_tokens(self, schema_class: Any) -> int:
        """Token budget always sent for a structured call.

        Uses the configured max_tokens, or DEFAULT_STRUCTURED_MAX_TOKENS when no
        *_MAX_TOKENS env is set. Large schemas (MechanicalNotes=6656 chars) whose
        configured budget sits below the parti-base floor are raised to the floor
        so big records are not truncated; small schemas keep the configured value.
        """
        budget = self.max_tokens or DEFAULT_STRUCTURED_MAX_TOKENS
        schema_chars = len(json.dumps(schema_class.model_json_schema()))
        if schema_chars >= LARGE_SCHEMA_CHAR_THRESHOLD and budget < STRUCTURED_MAX_TOKENS_FLOOR:
            logger.warning(
                "Raising max_tokens for %s from %d to floor %d (schema is %d chars; "
                "large records truncate below the floor)",
                _schema_name(schema_class),
                budget,
                STRUCTURED_MAX_TOKENS_FLOOR,
                schema_chars,
            )
            return STRUCTURED_MAX_TOKENS_FLOOR
        return budget

    def generate_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Any:
        if not self.is_configured:
            raise RuntimeError(f"{self.provider_name} provider is not configured.")

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": "You produce concise, valid JSON only. Do not include markdown or commentary.",
                },
                {
                    "role": "user",
                    "content": self._build_user_content(prompt, schema_class, image_bytes, image_mime_type),
                },
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        if self.provider_name == "cloudflare":
            payload["chat_template_kwargs"] = {
                "enable_thinking": bool(self.cloudflare_enable_thinking),
            }

        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        if self.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(schema_class),
                    "schema": _schema_with_closed_objects(schema_class.model_json_schema()),
                    "strict": False,
                },
            }
        elif self.response_format != "none":
            payload["response_format"] = {"type": "json_object"}

        # A token budget is ALWAYS sent for structured calls (see Layer A). An
        # unset or too-small budget truncated large records mid-string and the
        # pydantic parse failed with a user-facing 500.
        tokens_key = "max_completion_tokens" if self.provider_name == "openai" else "max_tokens"
        budget = self._structured_max_tokens(schema_class)

        # Exactly two attempts: the first at the computed budget, and on a
        # validation failure one retry at a larger budget. Transport/HTTP errors
        # from _request_json are NOT retried here - they raise immediately,
        # preserving current behavior.
        last_error: Optional[Exception] = None
        finish_reason: Optional[str] = None
        for attempt in range(2):
            payload[tokens_key] = budget
            try:
                response = self._request_json("chat/completions", method="POST", payload=payload)
            except RuntimeError as exc:
                if image_bytes and _is_image_input_unsupported_error(exc):
                    raise LLMProviderInputError(
                        _image_input_error_message(self.provider_name, self.model_name)
                    ) from exc
                raise
            choices = response.get("choices") or []
            if not choices:
                raise RuntimeError(f"{self.provider_name} response did not include any choices.")

            choice = choices[0]
            finish_reason = choice.get("finish_reason")
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if not isinstance(content, str) or not content.strip():
                choice_text = choice.get("text")
                if isinstance(choice_text, str) and choice_text.strip():
                    content = choice_text
            if not isinstance(content, str) or not content.strip():
                refusal = message.get("refusal")
                if isinstance(refusal, str) and refusal.strip():
                    raise LLMProviderOutputError(
                        f"{self.provider_name} refused to produce structured output for {_schema_name(schema_class)}."
                    )

                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                completion_tokens = usage.get("completion_tokens")
                reasoning_content = message.get("reasoning_content")
                has_reasoning_content = isinstance(reasoning_content, str) and bool(reasoning_content.strip())
                last_error = LLMProviderOutputError(
                    f"{self.provider_name} response did not include text content "
                    f"(finish_reason={finish_reason}, max_tokens={budget}, "
                    f"completion_tokens={completion_tokens}, reasoning_content={has_reasoning_content})."
                )
                if attempt == 0:
                    budget = min(
                        max(budget * 2, STRUCTURED_MAX_TOKENS_FLOOR),
                        STRUCTURED_MAX_TOKENS_CEILING,
                    )
                    if self.provider_name == "cloudflare" and has_reasoning_content and not self.reasoning_effort:
                        payload["reasoning_effort"] = "low"
                    logger.warning(
                        "%s returned no visible %s content (finish_reason=%s, completion_tokens=%s, "
                        "reasoning_content=%s); retrying once with max_tokens=%d.",
                        self.provider_name,
                        _schema_name(schema_class),
                        finish_reason,
                        completion_tokens,
                        has_reasoning_content,
                        budget,
                    )
                    continue
                break

            try:
                result = _validate_structured_json(content, schema_class)
                if finish_reason == "length":
                    logger.warning(
                        "%s output for %s hit the token cap (max_tokens=%d, finish_reason=length) "
                        "but was salvaged into a valid record.",
                        self.provider_name,
                        _schema_name(schema_class),
                        budget,
                    )
                return result
            except Exception as validation_error:
                last_error = validation_error
                if attempt == 0:
                    budget = min(
                        max(budget * 2, STRUCTURED_MAX_TOKENS_FLOOR),
                        STRUCTURED_MAX_TOKENS_CEILING,
                    )
                    logger.warning(
                        "%s produced unusable %s (finish_reason=%s, content=%d chars); "
                        "retrying once with max_tokens=%d.",
                        self.provider_name,
                        _schema_name(schema_class),
                        finish_reason,
                        len(content),
                        budget,
                    )

        raise LLMProviderOutputError(
            f"{self.provider_name} returned unusable structured output for "
            f"{_schema_name(schema_class)} after retry (finish_reason={finish_reason}, "
            f"max_tokens={budget}): {last_error}"
        ) from last_error


class RunpodServerlessProvider(StructuredLLMProvider):
    provider_name = "runpod-serverless"

    def __init__(self, model_name: Optional[str] = None, settings: Optional[Settings] = None):
        self.settings = settings
        (_env, _first_env, _first_env_bool, _first_env_float, _first_env_optional_float, _first_env_int,
         _first_env_optional_string, _runpod_openai_base_url_from_env, _runpod_serverless_endpoint_url_from_env,
         _runpod_serverless_url_env_name, _model_endpoint_map, _cloudflare_base_url_from_env) = _scoped_env_helpers(settings)
        self.api_key = _first_env(["RUNPOD_API_KEY"])
        self.requested_model = (
            model_name
            or _first_env(["RUNPOD_SERVERLESS_MODEL", "RUNPOD_MODEL", "RUNPOD_OPENAI_MODEL", "LLM_MODEL"], "runpod-default")
            or "runpod-default"
        )
        self.fallback_model = _first_env(["RUNPOD_FALLBACK_MODEL", "LLM_FALLBACK_MODEL"])
        self.strict_mode = _first_env_bool(["STRICT_RUNPOD", "STRICT_LLM"], default=True)
        self.timeout_seconds = _first_env_float(["RUNPOD_TIMEOUT_SECONDS"], DEFAULT_RUNPOD_TIMEOUT_SECONDS)
        self.wait_ms = _first_env_int(["RUNPOD_WAIT_MS", "RUNPOD_RUNSYNC_WAIT_MS"]) or 90_000
        self.poll_timeout_seconds = _first_env_float(["RUNPOD_POLL_TIMEOUT_SECONDS"], DEFAULT_RUNPOD_POLL_TIMEOUT_SECONDS)
        self.poll_interval_seconds = max(1.0, _first_env_float(["RUNPOD_POLL_INTERVAL_SECONDS"], 5.0))
        self.execution_timeout_ms = _first_env_int(["RUNPOD_EXECUTION_TIMEOUT_MS"])
        self.ttl_ms = _first_env_int(["RUNPOD_TTL_MS"])
        self.temperature = _first_env_optional_float(["RUNPOD_TEMPERATURE", "LLM_TEMPERATURE"], default=None)
        self.input_template = _first_env(["RUNPOD_INPUT_TEMPLATE"])
        self.model_endpoint_map = _model_endpoint_map()
        self.endpoint_base_url = self._resolve_endpoint_base_url(self.requested_model)
        self.model_name = self.requested_model
        self._validation: Optional[LLMProviderValidation] = None
        self.is_configured = bool(self.api_key and self.endpoint_base_url)
        if self.is_configured:
            logger.info("Runpod Serverless provider initialized for model %s.", self.requested_model)
        else:
            logger.warning("Runpod Serverless provider is missing RUNPOD_API_KEY or endpoint configuration.")

    def _resolve_endpoint_base_url(self, model_name: str) -> str:
        endpoint_config = self.model_endpoint_map.get(model_name)
        endpoint_url: Optional[str] = None
        endpoint_id: Optional[str] = None
        if isinstance(endpoint_config, str):
            if endpoint_config.startswith("http://") or endpoint_config.startswith("https://"):
                endpoint_url = endpoint_config
            else:
                endpoint_id = endpoint_config
        elif isinstance(endpoint_config, dict):
            raw_url = endpoint_config.get("endpoint_url") or endpoint_config.get("url")
            raw_id = endpoint_config.get("endpoint_id") or endpoint_config.get("id")
            endpoint_url = str(raw_url) if raw_url else None
            endpoint_id = str(raw_id) if raw_id else None

        endpoint_url = endpoint_url or _runpod_serverless_endpoint_url_from_env(settings=self.settings)
        endpoint_id = endpoint_id or _first_env(["RUNPOD_ENDPOINT_ID"], settings=self.settings)

        if endpoint_url:
            parsed = urllib.parse.urlparse(endpoint_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise LLMProviderConfigError(f"RUNPOD_ENDPOINT_URL must be absolute, got {endpoint_url!r}.")
            path_parts = [part for part in parsed.path.split("/") if part]
            if len(path_parts) >= 2 and path_parts[0] == "v2":
                runpod_endpoint_id = urllib.parse.unquote(path_parts[1])
                return urllib.parse.urlunparse(
                    (parsed.scheme, parsed.netloc, f"/v2/{urllib.parse.quote(runpod_endpoint_id, safe='')}", "", "", "")
                ).rstrip("/")
            return endpoint_url.rstrip("/")

        if endpoint_id:
            return f"https://api.runpod.ai/v2/{urllib.parse.quote(endpoint_id, safe='')}"
        return ""

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        if self._validation:
            if raise_on_strict and self._validation.validation_error and self.strict_mode:
                raise LLMProviderConfigError(self._validation.validation_error)
            return self._validation

        if not self.is_configured:
            self._validation = LLMProviderValidation(
                provider=self.provider_name,
                requested_model=self.requested_model,
                actual_model=None,
                requested_model_available=False,
                strict_mode=self.strict_mode,
                fallback_active=False,
                fallback_model=self.fallback_model,
                validation_error=(
                    "Runpod provider is not configured. Set RUNPOD_API_KEY plus RUNPOD_ENDPOINT_ID, "
                    "RUNPOD_ENDPOINT_URL, or RUNPOD_MODEL_ENDPOINTS."
                ),
                live_generation_enabled=False,
            )
            return self._validation

        self.model_name = self.requested_model
        self._validation = LLMProviderValidation(
            provider=self.provider_name,
            requested_model=self.requested_model,
            actual_model=self.model_name,
            requested_model_available=True,
            strict_mode=self.strict_mode,
            fallback_active=False,
            fallback_model=self.fallback_model,
            model_availability_checked=False,
        )
        return self._validation

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request_json(self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.endpoint_base_url}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Runpod request failed with HTTP {exc.code}: {detail[:500]}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RuntimeError(
                f"Runpod request timed out after {self.timeout_seconds:.1f}s while reading {path}. "
                "Increase RUNPOD_TIMEOUT_SECONDS. For queued jobs, also increase RUNPOD_POLL_TIMEOUT_SECONDS, "
                "RUNPOD_EXECUTION_TIMEOUT_MS, and RUNPOD_TTL_MS."
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                raise RuntimeError(
                    f"Runpod request timed out after {self.timeout_seconds:.1f}s while reading {path}. "
                    "Increase RUNPOD_TIMEOUT_SECONDS. For queued jobs, also increase RUNPOD_POLL_TIMEOUT_SECONDS, "
                    "RUNPOD_EXECUTION_TIMEOUT_MS, and RUNPOD_TTL_MS."
                ) from exc
            raise RuntimeError(f"Runpod request failed: {exc}") from exc

        if not body.strip():
            return {}
        return json.loads(body)

    def _apply_template(self, value: Any, *, prompt: str, schema_json: str) -> Any:
        if isinstance(value, dict):
            return {key: self._apply_template(item, prompt=prompt, schema_json=schema_json) for key, item in value.items()}
        if isinstance(value, list):
            return [self._apply_template(item, prompt=prompt, schema_json=schema_json) for item in value]
        if isinstance(value, str):
            return value.replace("{prompt}", prompt).replace("{model}", self.model_name).replace("{schema}", schema_json)
        return value

    def _build_run_payload(self, prompt: str, schema_class: Any) -> Dict[str, Any]:
        schema_json = json.dumps(schema_class.model_json_schema(), indent=2)
        if self.input_template:
            try:
                parsed_template = json.loads(self.input_template)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"RUNPOD_INPUT_TEMPLATE must be valid JSON: {exc}") from exc
            parsed_template = self._apply_template(parsed_template, prompt=prompt, schema_json=schema_json)
            payload = parsed_template if isinstance(parsed_template, dict) and "input" in parsed_template else {"input": parsed_template}
        else:
            payload = {
                "input": {
                    "prompt": prompt,
                    "model": self.model_name,
                }
            }
            if self.temperature is not None:
                payload["input"]["temperature"] = self.temperature

        policy: Dict[str, Any] = {}
        if isinstance(payload.get("policy"), dict):
            policy.update(payload["policy"])
        if self.execution_timeout_ms is not None:
            policy.setdefault("executionTimeout", self.execution_timeout_ms)
        if self.ttl_ms is not None:
            policy.setdefault("ttl", self.ttl_ms)
        if policy:
            payload["policy"] = policy
        return payload

    def _poll_status(self, job_id: str) -> Dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        last_result: Dict[str, Any] = {"id": job_id, "status": "IN_QUEUE"}
        while time.monotonic() < deadline:
            time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
            last_result = self._request_json(f"status/{urllib.parse.quote(job_id, safe='')}")
            status = str(last_result.get("status") or "")
            if status in {"COMPLETED", "FAILED", "ERROR", "TIMED_OUT", "CANCELLED"}:
                return last_result
        return last_result

    def _extract_structured_output(self, value: Any) -> Any:
        if isinstance(value, dict):
            if "choices" in value:
                choices = value.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    message = choices[0].get("message") or {}
                    if isinstance(message, dict) and "content" in message:
                        return self._extract_structured_output(message["content"])
            if "output_text" in value:
                return value["output_text"]
            if "content" in value:
                return self._extract_structured_output(value["content"])
            if "text" in value:
                return value["text"]
            if "response" in value:
                return self._extract_structured_output(value["response"])
            if "result" in value:
                return self._extract_structured_output(value["result"])
            return value
        if isinstance(value, list):
            if len(value) == 1:
                return self._extract_structured_output(value[0])
            text_parts = []
            for item in value:
                extracted = self._extract_structured_output(item)
                if isinstance(extracted, str):
                    text_parts.append(extracted)
            if text_parts:
                return "".join(text_parts)
            return value
        return value

    def generate_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Any:
        if image_bytes or image_mime_type:
            raise RuntimeError("Runpod provider does not support reference images through the generic adapter.")
        if not self.is_configured:
            raise RuntimeError("Runpod provider is not configured.")

        structured_prompt = (
            f"{prompt}\n\n"
            "Return only valid JSON. The JSON must conform to this schema:\n"
            f"{json.dumps(schema_class.model_json_schema(), indent=2)}"
        )
        payload = self._build_run_payload(structured_prompt, schema_class)
        wait_ms = max(1_000, min(300_000, self.wait_ms))
        result = self._request_json(f"runsync?wait={wait_ms}", method="POST", payload=payload)
        status = str(result.get("status") or "")
        if status in {"IN_QUEUE", "IN_PROGRESS"} and result.get("id"):
            result = self._poll_status(str(result["id"]))
            status = str(result.get("status") or "")
        if status != "COMPLETED":
            raise RuntimeError(f"Runpod job did not complete successfully; status={status or 'unknown'}.")

        output = self._extract_structured_output(result.get("output"))
        if isinstance(output, str):
            return _validate_structured_json(output, schema_class)
        return schema_class.model_validate(output)


def build_llm_provider(
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    runtime_config: Optional[LLMRuntimeConfig] = None,
    settings: Optional[Settings] = None,
) -> StructuredLLMProvider:
    runtime = runtime_config or resolve_llm_runtime_config(provider_name=provider_name, model_name=model_name, settings=settings)
    if runtime.provider == "anthropic":
        return AnthropicProvider(model_name=runtime.model, settings=settings)
    if runtime.provider == "gemini":
        return GeminiProvider(model_name=runtime.model, settings=settings)
    if runtime.provider == "vertex":
        return VertexAIProvider(model_name=runtime.model, settings=settings)
    if runtime.provider in {"baseten", "gmi", "huggingface", "cloudflare", "nvidia", "openai", "openai-compatible", "together", "xai"}:
        return OpenAICompatibleProvider(provider_name=runtime.provider, model_name=runtime.model, settings=settings)
    if runtime.provider == "runpod":
        return OpenAICompatibleProvider(provider_name="runpod", model_name=runtime.model, settings=settings)
    if runtime.provider == "runpod-serverless":
        return RunpodServerlessProvider(model_name=runtime.model, settings=settings)
    if runtime.provider == "simulation":
        return SimulationProvider()

    message = (
        f"Unsupported LLM_PROVIDER '{runtime.provider}'. Supported providers are "
        "anthropic, baseten, gemini, gmi, huggingface, cloudflare, nvidia, openai, openai-compatible, "
        "runpod, runpod-serverless, simulation, together, vertex, and xai."
    )
    logger.warning(message)
    return SimulationProvider(validation_error=message)
