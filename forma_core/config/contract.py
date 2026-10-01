"""Canonical, credential-safe runtime configuration consumed by API clients."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from forma_core.agents.orchestrator import HardwarePipelineOrchestrator
from forma_core.agents.workflows import list_workflows
from forma_core.config.environment import config
from forma_core.image_providers import get_image_output_debug_config
from forma_core.llm_providers import (
    LLMProviderConfigError,
    LLMRuntimeConfig,
    model_image_input_support,
    resolve_llm_runtime_config,
)
from forma_core.runtime import forma_dev_mode_enabled
from forma_core.config.runtime import deployment_runtime_config


RUNTIME_CONTRACT_VERSION = 1

_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "baseten": "Baseten",
    "cloudflare": "Cloudflare",
    "gemini": "Google Gemini",
    "gmi": "GMI",
    "huggingface": "Hugging Face",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "openai-compatible": "OpenAI Compatible",
    "runpod": "Runpod",
    "runpod-serverless": "Runpod Serverless",
    "vertex": "Google Vertex AI",
}

_MODEL_LABELS = {
    ("cloudflare", "@cf/google/gemma-4-26b-a4b-it"): "Cloudflare Gemma 4 26B A4B",
    ("gmi", "anthropic/claude-fable-5"): "GMI Claude Fable 5",
    ("anthropic", "claude-opus-5"): "Claude Opus 5",
    ("anthropic", "claude-sonnet-5"): "Claude Sonnet 5",
    ("gemini", "gemini-3.7-flash"): "Gemini 3.7 Flash",
    ("vertex", "gemini-3.7-flash"): "Vertex Gemini 3.7 Flash",
    ("baseten", "zai-org/GLM-5.2"): "GLM 5.2",
    ("runpod", "caid-technologies/parti-base"): "Runpod Parti Base",
    ("runpod-serverless", "caid-technologies/parti-base"): "caid-technologies/parti-base",
}


def llm_display_label(provider: str, model: str) -> str:
    explicit = _MODEL_LABELS.get((provider, model))
    if explicit:
        return explicit
    provider_label = _PROVIDER_LABELS.get(provider, provider.replace("-", " ").title())
    return f"{provider_label} {model}".strip()


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _llm_option(provider: str, model: str, selected: LLMRuntimeConfig) -> Dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "label": llm_display_label(provider, model),
        "supports_image_input": model_image_input_support(provider, model),
        "selected": provider == selected.provider and model == selected.model,
        "configured": True,
    }


def _resolved_llm_options(
    runtime: LLMRuntimeConfig,
    settings: Optional[Mapping[str, str]] = None,
) -> list[Dict[str, Any]]:
    configured = set(runtime.configured_providers or [])
    allowed = set(runtime.allowed_providers or configured)
    providers = _ordered_unique([runtime.provider, *sorted(configured)])
    options: list[Dict[str, Any]] = []

    for provider in providers:
        if provider == "simulation" or provider not in configured or provider not in allowed:
            continue
        try:
            provider_runtime = resolve_llm_runtime_config(provider_name=provider, settings=settings)
        except LLMProviderConfigError:
            continue
        models = _ordered_unique([
            runtime.model if provider == runtime.provider else provider_runtime.model,
            *(provider_runtime.allowed_models or []),
        ])
        options.extend(_llm_option(provider, model, runtime) for model in models)

    options.sort(key=lambda option: (not option["selected"], option["label"].lower()))
    return options


def resolve_runtime_contract(
    *,
    llm_config: Optional[Dict[str, Any]] = None,
    image_config: Optional[Dict[str, Any]] = None,
    workflows: Optional[list[Dict[str, Any]]] = None,
    signup_storage: Optional[str] = None,
    settings: Optional[Mapping[str, str]] = None,
    authoring_access: bool = False,
) -> Dict[str, Any]:
    """Resolve all client-facing generation decisions from the active environment."""
    resolved_llm_config = llm_config or HardwarePipelineOrchestrator(settings=settings).get_debug_config(raise_on_preflight=False)
    runtime = resolve_llm_runtime_config(settings=settings)
    llm_options = _resolved_llm_options(runtime, settings=settings)
    selected_llm = next((option for option in llm_options if option["selected"]), None)
    if selected_llm is None and runtime.provider != "simulation":
        selected_llm = _llm_option(runtime.provider, runtime.model, runtime)

    resolved_image = image_config or get_image_output_debug_config(settings=settings)
    image_capable = bool(resolved_image.get("request_capable"))
    resolved_workflows = workflows if workflows is not None else list_workflows()
    workflow_ids = {str(item.get("id")) for item in resolved_workflows if isinstance(item, dict)}
    default_workflow = config.default_generation_workflow
    if default_workflow not in workflow_ids and resolved_workflows:
        default_workflow = (
            "web_research"
            if "web_research" in workflow_ids
            else str(resolved_workflows[0].get("id") or "default")
        )

    deployment = deployment_runtime_config(
        resolved_llm_config,
        signup_storage=signup_storage,
        authoring_access=authoring_access,
    )
    llm_ready = bool(resolved_llm_config.get("live_generation_enabled"))
    llm_reason = resolved_llm_config.get("validation_error")
    image_reason = resolved_image.get("reason")

    return {
        "contract_version": RUNTIME_CONTRACT_VERSION,
        "authority": "backend",
        "forma_dev_mode": forma_dev_mode_enabled(),
        "precedence": ["request_override", "saved_integration", "environment", "provider_default"],
        "generation": {
            "ready": llm_ready,
            "available": bool(deployment.get("generation_available")) and llm_ready,
            "reason": llm_reason or deployment.get("generation_unavailable_reason"),
            "selected_llm": selected_llm,
            "llm_options": llm_options,
        },
        "images": {
            "enabled": bool(resolved_image.get("default_enabled")),
            "configured": image_capable,
            "request_capable": image_capable,
            "provider": resolved_image.get("request_provider") or resolved_image.get("provider"),
            "model": resolved_image.get("request_model_name") or resolved_image.get("model_name"),
            "generate_by_default": image_capable,
            "reason": image_reason,
        },
        "workflow": {
            "default_id": default_workflow,
            "options": resolved_workflows,
        },
        "provider_setup": {
            "required": not llm_ready or not image_capable,
            "llm_required": not llm_ready,
            "image_required": not image_capable,
        },
        "deployment": deployment,
    }


__all__ = [
    "RUNTIME_CONTRACT_VERSION",
    "llm_display_label",
    "resolve_runtime_contract",
]
