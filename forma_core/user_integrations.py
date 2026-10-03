from __future__ import annotations

import json
import logging
import os

from forma_core.config import config as env_config
import re
import hashlib
import base64
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from cryptography.fernet import Fernet, InvalidToken

from forma_core.runtime import (
    forma_dev_mode_enabled,
    deployment_mode_enabled,
    primary_database_backend_from_environment,
)
from forma_core.selectors import parse_llm_selector


logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = ".forma"
DEFAULT_CONFIG_FILENAME = "user-integrations.json"
DEFAULT_ENCRYPTED_CONFIG_FILENAME = "workspace-integrations.enc.json"
WORKSPACE_CONFIG_KEY = "default"
WORKSPACE_CONFIG_CACHE_TTL_SECONDS = 30.0
WORKSPACE_CONFIG_FAILURE_TTL_SECONDS = 60.0
CONFIG_PATH_ENV_NAMES = (
    "FORMA_USER_INTEGRATIONS_PATH",
    "FORMA_USER_CONFIG_PATH",
)
CONFIG_DIR_ENV_NAMES = (
    "FORMA_USER_CONFIG_DIR",
    "FORMA_LOCAL_CONFIG_DIR",
)


@dataclass(frozen=True)
class IntegrationFieldDefinition:
    id: str
    label: str
    env_names: tuple[str, ...]
    secret: bool = False
    placeholder: str = ""
    help: str = ""


@dataclass(frozen=True)
class IntegrationDefinition:
    id: str
    label: str
    description: str
    fields: tuple[IntegrationFieldDefinition, ...]

    def field_by_id(self, field_id: str) -> IntegrationFieldDefinition:
        for field_definition in self.fields:
            if field_definition.id == field_id:
                return field_definition
        raise KeyError(f"Unknown field {field_id!r} for integration {self.id!r}.")


@dataclass(frozen=True)
class HostedByokPolicy:
    hosted_byok: str
    local_byok: str
    self_hosted_byok: str
    note: str
    blocked_secret_fields: tuple[str, ...] = ()
    conditional_secret_fields: tuple[str, ...] = ()


@dataclass
class StoredIntegrationField:
    id: str
    value: str

    @classmethod
    def from_raw(cls, raw: object) -> Optional["StoredIntegrationField"]:
        if not isinstance(raw, dict):
            return None
        field_id = str(raw.get("id") or "").strip()
        value = raw.get("value")
        if not field_id or value is None:
            return None
        return cls(id=field_id, value=str(value))

    def as_json(self) -> dict[str, str]:
        return {"id": self.id, "value": self.value}


@dataclass
class StoredIntegration:
    id: str
    enabled: bool = True
    fields: list[StoredIntegrationField] = field(default_factory=list)
    updated_at: str = ""

    @classmethod
    def from_raw(cls, raw: object) -> Optional["StoredIntegration"]:
        if not isinstance(raw, dict):
            return None
        integration_id = str(raw.get("id") or "").strip()
        if not integration_id:
            return None
        fields = [
            parsed
            for parsed in (StoredIntegrationField.from_raw(item) for item in raw.get("fields") or [])
            if parsed is not None
        ]
        return cls(
            id=integration_id,
            enabled=bool(raw.get("enabled", True)),
            fields=fields,
            updated_at=str(raw.get("updated_at") or ""),
        )

    def field_value(self, field_id: str) -> Optional[str]:
        for stored_field in self.fields:
            if stored_field.id == field_id:
                return stored_field.value
        return None

    def set_field(self, field_id: str, value: str) -> None:
        trimmed_value = value.strip()
        self.clear_field(field_id)
        if trimmed_value:
            self.fields.append(StoredIntegrationField(id=field_id, value=trimmed_value))

    def clear_field(self, field_id: str) -> None:
        self.fields = [stored_field for stored_field in self.fields if stored_field.id != field_id]

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "enabled": self.enabled,
            "fields": [stored_field.as_json() for stored_field in self.fields],
            "updated_at": self.updated_at,
        }


@dataclass
class UserIntegrationConfig:
    version: int = 1
    updated_at: str = ""
    integrations: list[StoredIntegration] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: object) -> "UserIntegrationConfig":
        if not isinstance(raw, dict):
            return cls()
        integrations = [
            parsed
            for parsed in (StoredIntegration.from_raw(item) for item in raw.get("integrations") or [])
            if parsed is not None
        ]
        return cls(
            version=int(raw.get("version") or 1),
            updated_at=str(raw.get("updated_at") or ""),
            integrations=integrations,
        )

    def integration_by_id(self, integration_id: str) -> Optional[StoredIntegration]:
        for integration in self.integrations:
            if integration.id == integration_id:
                return integration
        return None

    def ensure_integration(self, integration_id: str) -> StoredIntegration:
        existing = self.integration_by_id(integration_id)
        if existing is not None:
            return existing
        created = StoredIntegration(id=integration_id)
        self.integrations.append(created)
        return created

    def as_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "integrations": [integration.as_json() for integration in self.integrations],
        }


@dataclass(frozen=True)
class ResolvedIntegrationSettings(Mapping[str, str]):
    """Immutable process-environment snapshot plus one integration overlay."""

    values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.values.get(key, default)

    def as_env(self) -> dict[str, str]:
        """Return a subprocess-safe copy without exposing it in metadata/logs."""
        return dict(self.values)


INTEGRATION_DEFINITIONS: tuple[IntegrationDefinition, ...] = (
    IntegrationDefinition(
        id="runtime",
        label="Runtime Defaults",
        description="Default provider/model choices used when a job does not pass explicit runtime settings.",
        fields=(
            IntegrationFieldDefinition(
                "llm_selector",
                "Preferred model",
                ("FORMA_LLM_SELECTOR",),
                placeholder="anthropic/claude-opus-5",
                help="Use provider/model. Saving this fills the provider and model runtime settings automatically.",
            ),
            IntegrationFieldDefinition(
                "llm_provider",
                "Provider override",
                ("LLM_PROVIDER",),
                placeholder="anthropic",
                help="Advanced override. Leave blank when Preferred model is set.",
            ),
            IntegrationFieldDefinition(
                "llm_model",
                "Model override",
                ("LLM_MODEL",),
                placeholder="claude-opus-5",
                help="Advanced override. Leave blank when Preferred model is set.",
            ),
            IntegrationFieldDefinition(
                "allowed_providers",
                "Provider allowlist",
                ("LLM_ALLOWED_PROVIDERS",),
                placeholder="auto",
                help="Optional advanced override. Leave blank and Forma allows your configured providers automatically.",
            ),
            IntegrationFieldDefinition("image_provider", "Image provider", ("IMAGE_PROVIDER",), placeholder="openai"),
            IntegrationFieldDefinition("image_model", "Image model", ("IMAGE_MODEL",), placeholder="gpt-image-2"),
            IntegrationFieldDefinition(
                "external_source_provider",
                "Search provider",
                ("EXTERNAL_SOURCE_PROVIDER",),
                placeholder="firecrawl or tavily",
                help="Use firecrawl or tavily. Saved Tool provider credentials are used for the selected source.",
            ),
        ),
    ),
    IntegrationDefinition(
        id="image",
        label="Image Generation",
        description="OpenAI-compatible image generation endpoint and model defaults.",
        fields=(
            IntegrationFieldDefinition("enabled", "Image output enabled", ("IMAGE_OUTPUT_ENABLED", "OPENAI_IMAGE_OUTPUT_ENABLED"), placeholder="true"),
            IntegrationFieldDefinition("provider", "Image provider", ("IMAGE_PROVIDER",), placeholder="openai-compatible"),
            IntegrationFieldDefinition("api_key", "API key", ("IMAGE_API_KEY",), secret=True, placeholder="provider key"),
            IntegrationFieldDefinition("base_url", "Base URL", ("IMAGE_BASE_URL",), placeholder="https://provider.example/v1"),
            IntegrationFieldDefinition("model", "Image model", ("IMAGE_MODEL",), placeholder="gpt-image-2"),
            IntegrationFieldDefinition("size", "Image size", ("IMAGE_SIZE",), placeholder="1024x1024"),
            IntegrationFieldDefinition("quality", "Image quality", ("IMAGE_QUALITY",), placeholder="medium"),
            IntegrationFieldDefinition("output_format", "Output format", ("IMAGE_OUTPUT_FORMAT",), placeholder="png"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("IMAGE_TIMEOUT_SECONDS",), placeholder="120"),
        ),
    ),
    IntegrationDefinition(
        id="anthropic",
        label="Claude",
        description="Anthropic Claude Messages API for structured hardware generation.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"), secret=True, placeholder="sk-ant-..."),
            IntegrationFieldDefinition("base_url", "Base URL", ("ANTHROPIC_BASE_URL", "CLAUDE_BASE_URL"), placeholder="https://api.anthropic.com/v1"),
            IntegrationFieldDefinition("model", "Default model", ("ANTHROPIC_MODEL", "CLAUDE_MODEL"), placeholder="claude-opus-5"),
            IntegrationFieldDefinition("fallback_model", "Fallback model", ("ANTHROPIC_FALLBACK_MODEL", "CLAUDE_FALLBACK_MODEL"), placeholder="claude-haiku-4-5"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("ANTHROPIC_TIMEOUT_SECONDS", "CLAUDE_TIMEOUT_SECONDS"), placeholder="300"),
            IntegrationFieldDefinition("max_tokens", "Max tokens", ("ANTHROPIC_MAX_TOKENS", "CLAUDE_MAX_TOKENS"), placeholder="8192"),
        ),
    ),
    IntegrationDefinition(
        id="openai",
        label="OpenAI",
        description="Text, image, and streaming jobs that use OpenAI-compatible OpenAI endpoints.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("OPENAI_API_KEY",), secret=True, placeholder="sk-..."),
            IntegrationFieldDefinition("base_url", "Base URL", ("OPENAI_BASE_URL", "OPENAI_IMAGE_BASE_URL"), placeholder="https://api.openai.com/v1"),
            IntegrationFieldDefinition("model", "Default text model", ("OPENAI_MODEL", "OPENAI_STREAM_MODEL"), placeholder="gpt-5.6-sol"),
            IntegrationFieldDefinition("image_model", "Default image model", ("OPENAI_IMAGE_MODEL",), placeholder="gpt-image-2"),
            IntegrationFieldDefinition("image_size", "Image size", ("OPENAI_IMAGE_SIZE",), placeholder="1024x1024"),
            IntegrationFieldDefinition("image_quality", "Image quality", ("OPENAI_IMAGE_QUALITY",), placeholder="medium"),
            IntegrationFieldDefinition("image_output_format", "Image output format", ("OPENAI_IMAGE_OUTPUT_FORMAT",), placeholder="png"),
            IntegrationFieldDefinition("image_timeout_seconds", "Image timeout seconds", ("OPENAI_IMAGE_TIMEOUT_SECONDS",), placeholder="120"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("OPENAI_TIMEOUT_SECONDS", "OPENAI_STREAM_TIMEOUT_SECONDS"), placeholder="1200"),
        ),
    ),
    IntegrationDefinition(
        id="baseten",
        label="Baseten",
        description="OpenAI-compatible Baseten Model APIs.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("BASETEN_API_KEY",), secret=True, placeholder="baseten key"),
            IntegrationFieldDefinition("base_url", "Base URL", ("BASETEN_BASE_URL",), placeholder="https://inference.baseten.co/v1"),
            IntegrationFieldDefinition("model", "Default model", ("BASETEN_MODEL", "BASETEN_STREAM_MODEL"), placeholder="deepseek-ai/DeepSeek-V4-Pro"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("BASETEN_TIMEOUT_SECONDS", "BASETEN_STREAM_TIMEOUT_SECONDS"), placeholder="1200"),
            IntegrationFieldDefinition("max_tokens", "Max tokens", ("BASETEN_MAX_TOKENS", "BASETEN_STREAM_MAX_OUTPUT_TOKENS"), placeholder="4000"),
        ),
    ),
    IntegrationDefinition(
        id="cloudflare",
        label="Cloudflare Workers AI",
        description="OpenAI-compatible text generation through Cloudflare Workers AI.",
        fields=(
            IntegrationFieldDefinition("api_key", "API token", ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_AI_API_KEY", "CLOUDFLARE_API_KEY"), secret=True, placeholder="Cloudflare API token"),
            IntegrationFieldDefinition("account_id", "Account ID", ("CLOUDFLARE_ACCOUNT_ID",), placeholder="Cloudflare account ID"),
            IntegrationFieldDefinition("base_url", "Base URL override", ("CLOUDFLARE_BASE_URL",), placeholder="Derived from the account ID"),
            IntegrationFieldDefinition("model", "Default model", ("CLOUDFLARE_MODEL", "CLOUDFLARE_STREAM_MODEL"), placeholder="@cf/google/gemma-4-26b-a4b-it"),
            IntegrationFieldDefinition("fallback_model", "Fallback model", ("CLOUDFLARE_FALLBACK_MODEL",), placeholder="@cf/google/gemma-4-26b-a4b-it"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("CLOUDFLARE_TIMEOUT_SECONDS", "CLOUDFLARE_STREAM_TIMEOUT_SECONDS"), placeholder="300"),
            IntegrationFieldDefinition("max_tokens", "Max tokens", ("CLOUDFLARE_MAX_TOKENS", "CLOUDFLARE_STREAM_MAX_OUTPUT_TOKENS"), placeholder="8192"),
        ),
    ),
    IntegrationDefinition(
        id="runpod",
        label="Runpod",
        description="Runpod OpenAI-compatible endpoints and serverless endpoints.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("RUNPOD_API_KEY",), secret=True, placeholder="rpa_..."),
            IntegrationFieldDefinition("openai_base_url", "OpenAI base URL", ("RUNPOD_OPENAI_BASE_URL", "RUNPOD_BASE_URL"), placeholder="https://api.runpod.ai/v2/<endpoint>/openai/v1"),
            IntegrationFieldDefinition("endpoint_id", "Serverless endpoint ID", ("RUNPOD_ENDPOINT_ID",), placeholder="endpoint id"),
            IntegrationFieldDefinition("endpoint_url", "Serverless endpoint URL", ("RUNPOD_ENDPOINT_URL",), placeholder="https://api.runpod.ai/v2/<endpoint>"),
            IntegrationFieldDefinition("model", "Default model", ("RUNPOD_OPENAI_MODEL", "RUNPOD_MODEL"), placeholder="caid-technologies/parti-base"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("RUNPOD_TIMEOUT_SECONDS", "RUNPOD_POLL_TIMEOUT_SECONDS"), placeholder="1200"),
        ),
    ),
    IntegrationDefinition(
        id="gmi",
        label="GMI Cloud",
        description="GMI/Fable LLM access plus GMI Cloud image/video generation settings.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY"), secret=True, placeholder="gmi key"),
            IntegrationFieldDefinition(
                "key_delegation_confirmation",
                "Key delegation confirmation",
                (),
                placeholder="project-scoped GMI key",
                help="Hosted BYOK requires confirmation that this GMI key is scoped to a dedicated project or organization and may be stored server-side by Forma for your requests.",
            ),
            IntegrationFieldDefinition("llm_base_url", "LLM base URL", ("GMI_BASE_URL",), placeholder="https://api.gmi-serving.com/v1"),
            IntegrationFieldDefinition("video_base_url", "Video base URL", ("GMI_CLOUD_BASE_URL", "GMICLOUD_BASE_URL"), placeholder="https://console.gmicloud.ai"),
            IntegrationFieldDefinition("model", "Default LLM model", ("GMI_MODEL", "GMI_STREAM_MODEL", "GMI_CLOUD_MODEL", "GMICLOUD_MODEL"), placeholder="anthropic/claude-fable-5"),
            IntegrationFieldDefinition(
                "image_base_url",
                "Image base URL",
                ("GMI_IMAGE_BASE_URL", "GMI_CLOUD_IMAGE_BASE_URL", "GMICLOUD_IMAGE_BASE_URL"),
                placeholder="https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/requests/v1",
            ),
            IntegrationFieldDefinition("image_model", "Image model", ("GMI_IMAGE_MODEL", "GMI_CLOUD_IMAGE_MODEL", "GMICLOUD_IMAGE_MODEL"), placeholder="gpt-image-2"),
            IntegrationFieldDefinition(
                "image_size",
                "Image size",
                ("GMI_IMAGE_SIZE", "GMI_CLOUD_IMAGE_SIZE", "GMICLOUD_IMAGE_SIZE"),
                placeholder="1024x1024",
                help="GPT Image size in WxH format. Dimensions must be multiples of 16, the maximum edge is 3840px, and aspect ratio must not exceed 3:1.",
            ),
            IntegrationFieldDefinition("image_quality", "Image quality", ("GMI_IMAGE_QUALITY", "GMI_CLOUD_IMAGE_QUALITY", "GMICLOUD_IMAGE_QUALITY"), placeholder="medium"),
            IntegrationFieldDefinition("image_output_format", "Image output format", ("GMI_IMAGE_OUTPUT_FORMAT", "GMI_CLOUD_IMAGE_OUTPUT_FORMAT", "GMICLOUD_IMAGE_OUTPUT_FORMAT"), placeholder="png"),
            IntegrationFieldDefinition(
                "image_output_compression",
                "Output compression",
                ("GMI_IMAGE_OUTPUT_COMPRESSION", "GMI_CLOUD_IMAGE_OUTPUT_COMPRESSION"),
                placeholder="",
                help="Optional value from 0 to 100. GPT Image 2 applies it to JPEG; GPT Image 1.5 supports it for JPEG and WebP.",
            ),
            IntegrationFieldDefinition(
                "image_background",
                "Background",
                ("GMI_IMAGE_BACKGROUND", "GMI_CLOUD_IMAGE_BACKGROUND"),
                placeholder="auto",
                help="GPT Image background mode. GPT Image 1.5 also supports transparent output.",
            ),
            IntegrationFieldDefinition(
                "image_moderation",
                "Moderation",
                ("GMI_IMAGE_MODERATION", "GMI_CLOUD_IMAGE_MODERATION"),
                placeholder="auto",
                help="GPT Image moderation mode: auto or low.",
            ),
            IntegrationFieldDefinition(
                "image_resolution",
                "Resolution preset",
                ("GMI_IMAGE_RESOLUTION", "GMI_CLOUD_IMAGE_RESOLUTION"),
                placeholder="1K",
                help="Output resolution preset for Gemini or Wan. Suggestions update to the selected model's supported values.",
            ),
            IntegrationFieldDefinition(
                "image_aspect_ratio",
                "Aspect ratio",
                ("GMI_IMAGE_ASPECT_RATIO", "GMI_CLOUD_IMAGE_ASPECT_RATIO"),
                placeholder="1:1",
                help="Model-specific output aspect ratio. The available suggestions update for Gemini and FLUX Kontext.",
            ),
            IntegrationFieldDefinition(
                "image_seed",
                "Seed",
                ("GMI_IMAGE_SEED", "GMI_CLOUD_IMAGE_SEED"),
                placeholder="0",
                help="Optional FLUX Kontext random seed. Use 0 for a random seed.",
            ),
            IntegrationFieldDefinition(
                "image_prompt_upsampling",
                "Prompt upsampling",
                ("GMI_IMAGE_PROMPT_UPSAMPLING", "GMI_CLOUD_IMAGE_PROMPT_UPSAMPLING"),
                placeholder="false",
                help="FLUX Kontext can enhance the prompt before generation.",
            ),
            IntegrationFieldDefinition(
                "image_safety_tolerance",
                "Safety tolerance",
                ("GMI_IMAGE_SAFETY_TOLERANCE", "GMI_CLOUD_IMAGE_SAFETY_TOLERANCE"),
                placeholder="2",
                help="FLUX Kontext moderation level from 0 (strictest) to 6 (most permissive).",
            ),
            IntegrationFieldDefinition(
                "image_sequential_generation",
                "Sequential generation",
                ("GMI_IMAGE_SEQUENTIAL_GENERATION", "GMI_CLOUD_IMAGE_SEQUENTIAL_GENERATION"),
                placeholder="disabled",
                help="Seedream 5.0 Lite supports auto or disabled. Forma requests one output image.",
            ),
            IntegrationFieldDefinition(
                "image_watermark",
                "Watermark",
                ("GMI_IMAGE_WATERMARK", "GMI_CLOUD_IMAGE_WATERMARK"),
                placeholder="false",
                help="Whether Seedream 5.0 Lite should add a watermark.",
            ),
            IntegrationFieldDefinition("image_timeout_seconds", "Image timeout seconds", ("GMI_IMAGE_TIMEOUT_SECONDS", "GMI_CLOUD_IMAGE_TIMEOUT_SECONDS", "GMICLOUD_IMAGE_TIMEOUT_SECONDS"), placeholder="120"),
            IntegrationFieldDefinition("image_to_video_model", "Image-to-video model", ("GMI_CLOUD_IMAGE_TO_VIDEO_MODEL",), placeholder="kling-v3-image-to-video"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("GMI_TIMEOUT_SECONDS", "GMI_STREAM_TIMEOUT_SECONDS", "GMI_CLOUD_TIMEOUT_SECONDS"), placeholder="1200"),
        ),
    ),
    IntegrationDefinition(
        id="together",
        label="Together AI",
        description="Together AI OpenAI-compatible LLM inference and BYOK image generation with project-scoped keys.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("TOGETHER_API_KEY", "TOGETHER_IMAGE_API_KEY"), secret=True, placeholder="tgp_..."),
            IntegrationFieldDefinition(
                "project_key_confirmation",
                "Project key confirmation",
                (),
                placeholder="project-scoped dedicated key",
                help="Hosted BYOK requires a project-scoped Together AI key dedicated to Forma. Legacy or broad account keys are not accepted.",
            ),
            IntegrationFieldDefinition("llm_base_url", "LLM base URL", ("TOGETHER_LLM_BASE_URL",), placeholder="https://api.together.xyz/v1"),
            IntegrationFieldDefinition("model", "Default LLM model", ("TOGETHER_MODEL",), placeholder="meta-llama/Llama-3.3-70B-Instruct-Turbo"),
            IntegrationFieldDefinition("image_base_url", "Image base URL", ("TOGETHER_IMAGE_BASE_URL", "TOGETHER_BASE_URL"), placeholder="https://api.together.ai/v1"),
            IntegrationFieldDefinition("image_model", "Image model", ("TOGETHER_IMAGE_MODEL",), placeholder="openai/gpt-image-2"),
            IntegrationFieldDefinition("image_size", "Image size", ("TOGETHER_IMAGE_SIZE",), placeholder="1024x1024"),
            IntegrationFieldDefinition("image_steps", "Image inference steps", ("TOGETHER_IMAGE_STEPS",), placeholder="4"),
            IntegrationFieldDefinition("image_output_format", "Image output format", ("TOGETHER_IMAGE_OUTPUT_FORMAT",), placeholder="png"),
            IntegrationFieldDefinition("image_timeout_seconds", "Image timeout seconds", ("TOGETHER_IMAGE_TIMEOUT_SECONDS",), placeholder="120"),
        ),
    ),
    IntegrationDefinition(
        id="huggingface",
        label="Hugging Face",
        description="Hugging Face Router models and artifact upload configuration.",
        fields=(
            IntegrationFieldDefinition("api_key", "Token", ("HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN", "HF_API_TOKEN"), secret=True, placeholder="hf_..."),
            IntegrationFieldDefinition(
                "token_scope_confirmation",
                "Token scope confirmation",
                (),
                placeholder="fine-grained inference token",
                help="Hosted BYOK requires a fine-grained token with only Make calls to Inference Providers, or an enterprise service-account token with equivalent scope.",
            ),
            IntegrationFieldDefinition("base_url", "Router base URL", ("HUGGINGFACE_BASE_URL", "HF_BASE_URL"), placeholder="https://router.huggingface.co/v1"),
            IntegrationFieldDefinition("model", "Default model", ("HUGGINGFACE_MODEL", "HF_MODEL"), placeholder="Qwen/Qwen2.5-72B-Instruct:deepinfra"),
            IntegrationFieldDefinition("model_revision", "Model revision", ("HUGGINGFACE_MODEL_REVISION", "HF_MODEL_REVISION"), placeholder="main"),
            IntegrationFieldDefinition("inference_provider", "Inference provider", ("HUGGINGFACE_INFERENCE_PROVIDER", "HF_INFERENCE_PROVIDER"), placeholder="auto"),
            IntegrationFieldDefinition("model_license", "Model license", ("HUGGINGFACE_MODEL_LICENSE", "HF_MODEL_LICENSE"), placeholder="apache-2.0"),
            IntegrationFieldDefinition("image_model", "Image model", ("HUGGINGFACE_IMAGE_MODEL", "HF_IMAGE_MODEL"), placeholder="black-forest-labs/FLUX.1-schnell"),
            IntegrationFieldDefinition("image_inference_provider", "Image inference provider", ("HUGGINGFACE_IMAGE_INFERENCE_PROVIDER", "HF_IMAGE_INFERENCE_PROVIDER"), placeholder="fal-ai"),
            IntegrationFieldDefinition("image_model_revision", "Image model revision", ("HUGGINGFACE_IMAGE_MODEL_REVISION", "HF_IMAGE_MODEL_REVISION"), placeholder="main"),
            IntegrationFieldDefinition("image_model_license", "Image model license", ("HUGGINGFACE_IMAGE_MODEL_LICENSE", "HF_IMAGE_MODEL_LICENSE"), placeholder="apache-2.0"),
            IntegrationFieldDefinition("image_size", "Image size", ("HUGGINGFACE_IMAGE_SIZE", "HF_IMAGE_SIZE"), placeholder="1024x1024"),
            IntegrationFieldDefinition("image_guidance_scale", "Image guidance scale", ("HUGGINGFACE_IMAGE_GUIDANCE_SCALE", "HF_IMAGE_GUIDANCE_SCALE"), placeholder="7.5"),
            IntegrationFieldDefinition("image_steps", "Image inference steps", ("HUGGINGFACE_IMAGE_STEPS", "HF_IMAGE_STEPS"), placeholder="30"),
            IntegrationFieldDefinition("image_output_format", "Image output format", ("HUGGINGFACE_IMAGE_OUTPUT_FORMAT", "HF_IMAGE_OUTPUT_FORMAT"), placeholder="png"),
            IntegrationFieldDefinition("gated_models_enabled", "Gated models enabled", ("HUGGINGFACE_GATED_MODELS_ENABLED", "HF_GATED_MODELS_ENABLED"), placeholder="false"),
            IntegrationFieldDefinition("image_gated_models_enabled", "Image gated models enabled", ("HUGGINGFACE_IMAGE_GATED_MODELS_ENABLED", "HF_IMAGE_GATED_MODELS_ENABLED"), placeholder="false"),
            IntegrationFieldDefinition("artifact_repo_id", "Artifact repo", ("HF_ARTIFACT_REPO_ID", "HUGGINGFACE_ARTIFACT_REPO_ID", "HF_DATASET_REPO_ID"), placeholder="user/dataset"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("HUGGINGFACE_TIMEOUT_SECONDS", "HF_TIMEOUT_SECONDS"), placeholder="1200"),
            IntegrationFieldDefinition("image_timeout_seconds", "Image timeout seconds", ("HUGGINGFACE_IMAGE_TIMEOUT_SECONDS", "HF_IMAGE_TIMEOUT_SECONDS"), placeholder="120"),
        ),
    ),
    IntegrationDefinition(
        id="nvidia",
        label="NVIDIA",
        description="NVIDIA NIM/OpenAI-compatible model routing.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NIM_API_KEY"), secret=True, placeholder="nvapi-..."),
            IntegrationFieldDefinition("base_url", "Base URL", ("NVIDIA_BASE_URL", "NVIDIA_NIM_BASE_URL", "NIM_BASE_URL"), placeholder="https://integrate.api.nvidia.com/v1"),
            IntegrationFieldDefinition("model", "Default model", ("NVIDIA_MODEL", "NVIDIA_NIM_MODEL", "NIM_MODEL"), placeholder="nvidia/z-ai/glm-5.2"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("NVIDIA_TIMEOUT_SECONDS", "NVIDIA_NIM_TIMEOUT_SECONDS", "NIM_TIMEOUT_SECONDS"), placeholder="1200"),
        ),
    ),
    IntegrationDefinition(
        id="xai",
        label="xAI Grok",
        description="xAI Grok models through the OpenAI-compatible API.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("XAI_API_KEY", "GROK_API_KEY"), secret=True, placeholder="xai-..."),
            IntegrationFieldDefinition("base_url", "Base URL", ("XAI_BASE_URL", "GROK_BASE_URL"), placeholder="https://api.x.ai/v1"),
            IntegrationFieldDefinition("model", "Default model", ("XAI_MODEL", "GROK_MODEL"), placeholder="grok-4"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("XAI_TIMEOUT_SECONDS", "GROK_TIMEOUT_SECONDS"), placeholder="300"),
        ),
    ),
    IntegrationDefinition(
        id="gemini",
        label="Gemini",
        description="Google Gemini structured generation.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), secret=True, placeholder="AIza..."),
            IntegrationFieldDefinition("model", "Default model", ("GEMINI_MODEL",), placeholder="gemini-3.7-flash"),
            IntegrationFieldDefinition("fallback_model", "Fallback model", ("GEMINI_FALLBACK_MODEL",), placeholder="gemini-2.5-flash"),
        ),
    ),
    IntegrationDefinition(
        id="vertex",
        label="Google Vertex AI",
        description="Gemini models on Vertex AI using server-side Google Cloud Application Default Credentials.",
        fields=(
            IntegrationFieldDefinition(
                "project",
                "Google Cloud project",
                ("VERTEX_AI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"),
                placeholder="your-gcp-project-id",
                help="The backend must have Vertex AI access through Application Default Credentials.",
            ),
            IntegrationFieldDefinition(
                "location",
                "Vertex AI location",
                ("VERTEX_AI_LOCATION", "GOOGLE_CLOUD_LOCATION"),
                placeholder="global",
            ),
            IntegrationFieldDefinition("model", "Default model", ("VERTEX_AI_MODEL",), placeholder="gemini-3.7-flash"),
            IntegrationFieldDefinition(
                "image_model",
                "Image model",
                ("VERTEX_AI_IMAGE_MODEL", "VERTEX_IMAGE_MODEL"),
                placeholder="gemini-3.1-flash-image",
            ),
            IntegrationFieldDefinition(
                "image_resolution",
                "Image resolution",
                ("VERTEX_AI_IMAGE_RESOLUTION", "VERTEX_IMAGE_RESOLUTION"),
                placeholder="1K",
            ),
            IntegrationFieldDefinition(
                "image_aspect_ratio",
                "Image aspect ratio",
                ("VERTEX_AI_IMAGE_ASPECT_RATIO", "VERTEX_IMAGE_ASPECT_RATIO"),
                placeholder="1:1",
            ),
            IntegrationFieldDefinition(
                "image_output_format",
                "Image output format",
                ("VERTEX_AI_IMAGE_OUTPUT_FORMAT", "VERTEX_IMAGE_OUTPUT_FORMAT"),
                placeholder="png",
            ),
            IntegrationFieldDefinition(
                "image_timeout_seconds",
                "Image timeout seconds",
                ("VERTEX_AI_IMAGE_TIMEOUT_SECONDS", "VERTEX_IMAGE_TIMEOUT_SECONDS"),
                placeholder="120",
            ),
            IntegrationFieldDefinition(
                "fallback_model",
                "Fallback model",
                ("VERTEX_AI_FALLBACK_MODEL",),
                placeholder="gemini-2.5-flash",
            ),
        ),
    ),
    IntegrationDefinition(
        id="firecrawl",
        label="Firecrawl",
        description="Firecrawl MCP search and page extraction.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("FIRECRAWL_API_KEY",), secret=True, placeholder="fc-..."),
            IntegrationFieldDefinition("mcp_command", "MCP command", ("FIRECRAWL_MCP_COMMAND",), placeholder="npx -y firecrawl-mcp"),
            IntegrationFieldDefinition("search_limit", "Search limit", ("FIRECRAWL_SEARCH_LIMIT",), placeholder="3"),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("FIRECRAWL_MCP_TIMEOUT_SECONDS",), placeholder="45"),
        ),
    ),
    IntegrationDefinition(
        id="tavily",
        label="Tavily",
        description="Tavily search, crawl, and research APIs for live web sources.",
        fields=(
            IntegrationFieldDefinition("api_key", "API key", ("TAVILY_API_KEY",), secret=True, placeholder="tvly-..."),
            IntegrationFieldDefinition(
                "search_depth",
                "Search depth",
                ("TAVILY_SEARCH_DEPTH",),
                placeholder="basic",
                help="ultra-fast, fast, basic, or advanced. Advanced costs more credits and returns higher-relevance results.",
            ),
            IntegrationFieldDefinition("search_limit", "Search result limit", ("TAVILY_SEARCH_LIMIT",), placeholder="5"),
            IntegrationFieldDefinition(
                "include_answer",
                "Include search answer",
                ("TAVILY_INCLUDE_ANSWER",),
                placeholder="true",
                help="When true, Tavily Search also returns a short LLM-ready answer summary.",
            ),
            IntegrationFieldDefinition(
                "include_raw_content",
                "Include search page content",
                ("TAVILY_INCLUDE_RAW_CONTENT",),
                placeholder="false",
                help="When true, Tavily Search includes extracted page content with each result.",
            ),
            IntegrationFieldDefinition(
                "crawl_max_depth",
                "Crawl max depth",
                ("TAVILY_CRAWL_MAX_DEPTH",),
                placeholder="1",
                help="How many link levels Tavily Crawl may follow from the start URL (1-5).",
            ),
            IntegrationFieldDefinition(
                "crawl_limit",
                "Crawl page limit",
                ("TAVILY_CRAWL_LIMIT",),
                placeholder="20",
                help="Maximum pages Tavily Crawl will process before stopping.",
            ),
            IntegrationFieldDefinition(
                "crawl_extract_depth",
                "Crawl extract depth",
                ("TAVILY_CRAWL_EXTRACT_DEPTH",),
                placeholder="basic",
                help="basic or advanced. Advanced extracts tables and embedded content.",
            ),
            IntegrationFieldDefinition(
                "research_model",
                "Research model",
                ("TAVILY_RESEARCH_MODEL",),
                placeholder="auto",
                help="Tavily Research agent size: mini, pro, or auto.",
            ),
            IntegrationFieldDefinition(
                "research_output_length",
                "Research output length",
                ("TAVILY_RESEARCH_OUTPUT_LENGTH",),
                placeholder="standard",
                help="short, standard, or long.",
            ),
            IntegrationFieldDefinition("timeout_seconds", "Timeout seconds", ("TAVILY_TIMEOUT_SECONDS",), placeholder="45"),
        ),
    ),
    IntegrationDefinition(
        id="ollama",
        label="Ollama",
        description="Local OpenAI-compatible Ollama endpoints.",
        fields=(
            IntegrationFieldDefinition("base_url", "OpenAI base URL", ("OLLAMA_OPENAI_BASE_URL",), placeholder="http://127.0.0.1:11434/v1"),
            IntegrationFieldDefinition("model", "Default model", ("OLLAMA_FORMA_MODEL",), placeholder="qwen3:0.6b"),
        ),
    ),
)

_DEFINITION_BY_ID = {definition.id: definition for definition in INTEGRATION_DEFINITIONS}
_APPLIED_ENV_VALUES: dict[str, str] = {}
_ORIGINAL_ENV_VALUES: dict[str, Optional[str]] = {}
_WORKSPACE_CONFIG_CACHE: dict[str, tuple[float, UserIntegrationConfig]] = {}
_WORKSPACE_CONFIG_FAILURE_CACHE: dict[str, tuple[float, UserIntegrationConfig, str]] = {}
EXTRA_MANAGED_ENV_NAMES = {
    "OPENAI_IMAGE_API_KEY",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_ALLOWED_PROVIDERS",
    "ANTHROPIC_ALLOWED_MODELS",
    "BASETEN_ALLOWED_MODELS",
    "GMI_ALLOWED_MODELS",
    "HUGGINGFACE_ALLOWED_MODELS",
    "CLOUDFLARE_ALLOWED_MODELS",
    "NVIDIA_ALLOWED_MODELS",
    "OPENAI_ALLOWED_MODELS",
    "RUNPOD_ALLOWED_MODELS",
    "TOGETHER_ALLOWED_MODELS",
    "VERTEX_AI_ALLOWED_MODELS",
    "XAI_ALLOWED_MODELS",
}
LLM_PROVIDER_INTEGRATION_IDS = {
    "anthropic",
    "baseten",
    "gemini",
    "vertex",
    "gmi",
    "huggingface",
    "cloudflare",
    "nvidia",
    "openai",
    "runpod",
    "together",
    "xai",
}
PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic-claude": "anthropic",
    "hf": "huggingface",
    "hugging-face": "huggingface",
    "cloudflare-ai": "cloudflare",
    "cloudflare-workers-ai": "cloudflare",
    "workers-ai": "cloudflare",
    "workers_ai": "cloudflare",
    "nvidia-build": "nvidia",
    "nvidia-nim": "nvidia",
    "nim": "nvidia",
    "google": "gemini",
    "google-vertex": "vertex",
    "google-vertex-ai": "vertex",
    "vertex-ai": "vertex",
    "vertexai": "vertex",
    "grok": "xai",
    "x-ai": "xai",
    "xai-grok": "xai",
    "together-ai": "together",
    "togetherai": "together",
}
MODEL_PREFIX_PROVIDERS = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("text-", "openai"),
    ("meta/", "nvidia"),
    ("nvidia/", "nvidia"),
    ("grok-", "xai"),
)
PROVIDER_ALLOWED_MODEL_ENV = {
    "anthropic": "ANTHROPIC_ALLOWED_MODELS",
    "baseten": "BASETEN_ALLOWED_MODELS",
    "gemini": "GEMINI_ALLOWED_MODELS",
    "vertex": "VERTEX_AI_ALLOWED_MODELS",
    "gmi": "GMI_ALLOWED_MODELS",
    "huggingface": "HUGGINGFACE_ALLOWED_MODELS",
    "cloudflare": "CLOUDFLARE_ALLOWED_MODELS",
    "nvidia": "NVIDIA_ALLOWED_MODELS",
    "openai": "OPENAI_ALLOWED_MODELS",
    "runpod": "RUNPOD_ALLOWED_MODELS",
    "together": "TOGETHER_ALLOWED_MODELS",
    "xai": "XAI_ALLOWED_MODELS",
}
HOSTED_BYOK_POLICIES: dict[str, HostedByokPolicy] = {
    "openai": HostedByokPolicy(
        hosted_byok="disabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        blocked_secret_fields=("api_key",),
        note=(
            "Forma Cloud does not accept user-supplied OpenAI API keys. "
            "Use local or self-hosted Forma for OpenAI BYOK; hosted cloud uses a platform-managed key."
        ),
    ),
    "image": HostedByokPolicy(
        hosted_byok="disabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        blocked_secret_fields=("api_key",),
        note=(
            "Forma Cloud does not accept generic user-supplied image provider API keys by default. "
            "Use local or self-hosted Forma for image BYOK until the provider terms and credential scopes are reviewed."
        ),
    ),
    "anthropic": HostedByokPolicy(
        hosted_byok="conditional",
        local_byok="enabled",
        self_hosted_byok="enabled",
        conditional_secret_fields=("api_key",),
        note="Hosted Anthropic BYOK must use an Anthropic Console API key, not Claude.ai credentials or subscription tokens.",
    ),
    "baseten": HostedByokPolicy(
        hosted_byok="enabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        conditional_secret_fields=("api_key",),
        note="Hosted Baseten BYOK requires a team API key with inference-only permissions and environment scoping.",
    ),
    "gmi": HostedByokPolicy(
        hosted_byok="conditional",
        local_byok="enabled",
        self_hosted_byok="enabled",
        conditional_secret_fields=("api_key",),
        note=(
            "Hosted GMI Cloud BYOK is conditional: use an organization or project-specific API key, confirm "
            "third-party server-side key delegation, keep credentials server-side only, and avoid sensitive serverless "
            "workloads unless retention, no-training, deletion, region, and data-processing terms are reviewed."
        ),
    ),
    "huggingface": HostedByokPolicy(
        hosted_byok="enabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        conditional_secret_fields=("api_key",),
        note=(
            "Hosted Hugging Face BYOK requires a fine-grained token with only Make calls to Inference Providers, "
            "or an enterprise service-account token with equivalent scope. Broad repository, organization, billing, "
            "deployment, or unrestricted account tokens are not accepted."
        ),
    ),
    "cloudflare": HostedByokPolicy(
        hosted_byok="disabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        blocked_secret_fields=("api_key",),
        note=(
            "Forma Cloud does not accept user-supplied Cloudflare API tokens until provider terms and "
            "credential scopes have been reviewed. Use local or self-hosted Forma for Cloudflare BYOK."
        ),
    ),
    "together": HostedByokPolicy(
        hosted_byok="enabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        conditional_secret_fields=("api_key",),
        note=(
            "Hosted Together AI image BYOK requires a project-scoped API key dedicated to Forma. "
            "Legacy or broad account keys are not accepted; credentials stay backend-only and encrypted, "
            "model IDs are recorded, output storage is enabled, and model-specific terms must be enforced."
        ),
    ),
    "nvidia": HostedByokPolicy(
        hosted_byok="disabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        blocked_secret_fields=("api_key",),
        note=(
            "Forma Cloud does not accept user-supplied NVIDIA Build/API Catalog keys. NVIDIA hosted endpoints are for "
            "account-holder trial, evaluation, and developer use unless a separate paid NVIDIA or authorized-provider "
            "agreement allows production, customer-facing application use, storage, and distribution."
        ),
    ),
    "xai": HostedByokPolicy(
        hosted_byok="disabled",
        local_byok="enabled",
        self_hosted_byok="enabled",
        blocked_secret_fields=("api_key",),
        note=(
            "Forma Cloud does not accept user-supplied xAI API keys. "
            "Use local or self-hosted Forma for xAI BYOK; hosted cloud uses a platform-managed key."
        ),
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _configured_path() -> Optional[Path]:
    for env_name in CONFIG_PATH_ENV_NAMES:
        raw_value = env_config.get(env_name)
        if raw_value and raw_value.strip():
            return Path(raw_value.strip()).expanduser()
    for env_name in CONFIG_DIR_ENV_NAMES:
        raw_value = env_config.get(env_name)
        if raw_value and raw_value.strip():
            return Path(raw_value.strip()).expanduser() / DEFAULT_CONFIG_FILENAME
    return None


def default_user_integrations_path() -> Path:
    return _configured_path() or (_repo_root() / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILENAME)


def user_integrations_path_for_user(user_id: str) -> Path:
    safe_user_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_id.strip())[:160]
    if not safe_user_id:
        raise ValueError("user_id is required")
    return _repo_root() / DEFAULT_CONFIG_DIR / "users" / safe_user_id / DEFAULT_CONFIG_FILENAME


def encrypted_workspace_integrations_path() -> Path:
    configured = env_config.get("FORMA_WORKSPACE_INTEGRATIONS_PATH")
    if configured and configured.strip():
        return Path(configured.strip()).expanduser()
    return _repo_root() / DEFAULT_CONFIG_DIR / DEFAULT_ENCRYPTED_CONFIG_FILENAME


def encrypted_user_integrations_path(user_id: str) -> Path:
    safe_user_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_id.strip())[:160]
    if not safe_user_id:
        raise ValueError("user_id is required")
    return _repo_root() / DEFAULT_CONFIG_DIR / "users" / safe_user_id / "integrations.enc.json"


def integration_definition_by_id(integration_id: str) -> IntegrationDefinition:
    definition = _DEFINITION_BY_ID.get(integration_id)
    if definition is None:
        raise KeyError(f"Unknown integration {integration_id!r}.")
    return definition


def list_integration_definitions() -> tuple[IntegrationDefinition, ...]:
    return INTEGRATION_DEFINITIONS


class UserIntegrationStore:
    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_user_integrations_path()

    @classmethod
    def for_user(cls, user_id: Optional[str]) -> "UserIntegrationStore":
        if not user_id:
            return default_integration_store()
        if forma_dev_mode_enabled():
            return EncryptedFileIntegrationStore(
                encrypted_user_integrations_path(user_id),
                secret_scope="user",
            )
        backend = _user_integration_backend()
        if backend in {"file", "local", "json"}:
            return EncryptedFileIntegrationStore(
                encrypted_user_integrations_path(user_id),
                secret_scope="user",
            )
        if backend in {"supabase", "db", "database"}:
            return SupabaseUserIntegrationStore(user_id)
        if primary_database_backend_from_environment() == "supabase":
            return SupabaseUserIntegrationStore(user_id)
        return EncryptedFileIntegrationStore(
            encrypted_user_integrations_path(user_id),
            secret_scope="user",
        )

    def load(self) -> UserIntegrationConfig:
        if not self.path.exists():
            return UserIntegrationConfig()
        try:
            with self.path.open("r", encoding="utf-8") as file:
                raw = json.load(file)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"User integrations config is invalid JSON: {self.path}") from exc
        return UserIntegrationConfig.from_raw(raw)

    def save(self, config: UserIntegrationConfig) -> UserIntegrationConfig:
        config.updated_at = _utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(config.as_json(), file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temp_path, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return config

    def update_integration(
        self,
        integration_id: str,
        *,
        enabled: Optional[bool] = None,
        field_values: Optional[dict[str, Optional[str]]] = None,
        clear_fields: Iterable[str] = (),
    ) -> UserIntegrationConfig:
        definition = integration_definition_by_id(integration_id)
        config = self.load()
        integration = config.ensure_integration(definition.id)
        if enabled is not None:
            integration.enabled = bool(enabled)

        for field_id in clear_fields:
            definition.field_by_id(field_id)
            integration.clear_field(field_id)

        for field_id, value in (field_values or {}).items():
            definition.field_by_id(field_id)
            if value is None or str(value).strip() == "":
                integration.clear_field(field_id)
            else:
                integration.set_field(field_id, str(value))

        integration.updated_at = _utc_now()
        return self.save(config)

    def clear_integration(self, integration_id: str) -> UserIntegrationConfig:
        integration_definition_by_id(integration_id)
        config = self.load()
        config.integrations = [integration for integration in config.integrations if integration.id != integration_id]
        return self.save(config)


def _workspace_integration_backend() -> str:
    return (
        env_config.get("FORMA_WORKSPACE_INTEGRATIONS_BACKEND")
        or env_config.get("FORMA_INTEGRATIONS_BACKEND")
        or ""
    ).strip().lower()


def _user_integration_backend() -> str:
    return (
        env_config.get("FORMA_USER_INTEGRATIONS_BACKEND")
        or env_config.get("FORMA_INTEGRATIONS_BACKEND")
        or ""
    ).strip().lower()


def _supabase_url() -> Optional[str]:
    value = env_config.get("SUPABASE_URL") or env_config.get("NEXT_PUBLIC_SUPABASE_URL")
    return value.strip() if value and value.strip() else None


def _supabase_service_key() -> Optional[str]:
    for name in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"):
        value = env_config.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _env_float(name: str, default: float) -> float:
    value = env_config.get(name)
    if not value or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _workspace_config_cache_ttl_seconds() -> float:
    return max(0.0, _env_float("FORMA_WORKSPACE_CONFIG_CACHE_TTL_SECONDS", WORKSPACE_CONFIG_CACHE_TTL_SECONDS))


def _workspace_config_failure_ttl_seconds() -> float:
    return max(0.0, _env_float("FORMA_WORKSPACE_CONFIG_FAILURE_TTL_SECONDS", WORKSPACE_CONFIG_FAILURE_TTL_SECONDS))


def _clone_config(config: UserIntegrationConfig) -> UserIntegrationConfig:
    return UserIntegrationConfig.from_raw(config.as_json())


def default_integration_store() -> UserIntegrationStore:
    if forma_dev_mode_enabled():
        return EncryptedFileIntegrationStore(encrypted_workspace_integrations_path())
    backend = _workspace_integration_backend()
    if backend in {"file", "local", "json"}:
        return EncryptedFileIntegrationStore(encrypted_workspace_integrations_path())
    if backend in {"supabase", "db", "database"}:
        return SupabaseWorkspaceIntegrationStore()
    if primary_database_backend_from_environment() == "supabase":
        return SupabaseWorkspaceIntegrationStore()
    return EncryptedFileIntegrationStore(encrypted_workspace_integrations_path())


def _hosted_byok_policy(integration_id: str) -> Optional[HostedByokPolicy]:
    return HOSTED_BYOK_POLICIES.get(integration_id)


def _hosted_byok_policy_active() -> bool:
    return deployment_mode_enabled()


def _active_hosted_byok_policy(integration_id: str) -> Optional[HostedByokPolicy]:
    if not _hosted_byok_policy_active():
        return None
    return _hosted_byok_policy(integration_id)


def _is_hosted_blocked_secret_field(integration_id: str, field_id: str) -> bool:
    policy = _active_hosted_byok_policy(integration_id)
    return bool(policy and field_id in policy.blocked_secret_fields)


def _sanitize_hosted_user_config(config: UserIntegrationConfig) -> UserIntegrationConfig:
    if not _hosted_byok_policy_active():
        return config
    for integration in config.integrations:
        policy = _hosted_byok_policy(integration.id)
        if not policy or not policy.blocked_secret_fields:
            continue
        blocked = set(policy.blocked_secret_fields)
        integration.fields = [field for field in integration.fields if field.id not in blocked]
    return config


def _truthy_confirmation(value: Optional[str]) -> bool:
    return bool(
        value
        and value.strip().lower()
        in {
            "1",
            "true",
            "yes",
            "y",
            "confirmed",
            "fine-grained",
            "service-account",
            "project-scoped",
            "organization-scoped",
            "dedicated-project",
            "dedicated-to-forma",
        }
    )


def _requires_hosted_huggingface_confirmation(integration_id: str, field_values: Optional[dict[str, Optional[str]]]) -> bool:
    if integration_id != "huggingface":
        return False
    value = (field_values or {}).get("api_key")
    return value is not None and str(value).strip() != ""


def _requires_hosted_gmi_confirmation(integration_id: str, field_values: Optional[dict[str, Optional[str]]]) -> bool:
    if integration_id != "gmi":
        return False
    value = (field_values or {}).get("api_key")
    return value is not None and str(value).strip() != ""


def _requires_hosted_together_confirmation(integration_id: str, field_values: Optional[dict[str, Optional[str]]]) -> bool:
    if integration_id != "together":
        return False
    value = (field_values or {}).get("api_key")
    return value is not None and str(value).strip() != ""


def require_user_secrets_key() -> str:
    value = env_config.get("FORMA_USER_SECRETS_KEY")
    if not value or not value.strip():
        message = (
            "FORMA_USER_SECRETS_KEY is required at runtime for encrypted integration settings. "
            "Generate a high-entropy server-only secret and keep it out of NEXT_PUBLIC_* variables."
        )
        logger.critical(message)
        raise RuntimeError(message)
    return value.strip()


def _integration_encryption_secret() -> str:
    return require_user_secrets_key()


def _integration_encryption_key_id(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _fernet_for_secret(secret: str) -> Fernet:
    try:
        return Fernet(secret.encode("utf-8"))
    except Exception:
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def _workspace_encryption_secret() -> str:
    value = (
        env_config.get("FORMA_WORKSPACE_SECRETS_KEY")
        or env_config.get("FORMA_WORKSPACE_INTEGRATIONS_ENCRYPTION_KEY")
        or env_config.get("WORKSPACE_INTEGRATIONS_ENCRYPTION_KEY")
    )
    if value and value.strip():
        return value.strip()
    return _integration_encryption_secret()


def _encrypt_config_with_secret(config: UserIntegrationConfig, secret: str) -> tuple[str, str]:
    token = _fernet_for_secret(secret).encrypt(json.dumps(config.as_json(), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return token.decode("ascii"), _integration_encryption_key_id(secret)


def _decrypt_config_with_secret(token: str, secret: str) -> UserIntegrationConfig:
    try:
        plaintext = _fernet_for_secret(secret).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError("Stored user integrations could not be decrypted with the configured key.") from exc
    return UserIntegrationConfig.from_raw(json.loads(plaintext.decode("utf-8")))


def _encrypt_config(config: UserIntegrationConfig) -> tuple[str, str]:
    return _encrypt_config_with_secret(config, _integration_encryption_secret())


def _decrypt_config(token: str) -> UserIntegrationConfig:
    return _decrypt_config_with_secret(token, _integration_encryption_secret())


def encrypt_user_secret_text(value: str) -> tuple[str, str]:
    """Encrypt short server-only payloads with the configured user secret key."""
    secret = _integration_encryption_secret()
    token = _fernet_for_secret(secret).encrypt(value.encode("utf-8"))
    return token.decode("ascii"), _integration_encryption_key_id(secret)


def decrypt_user_secret_text(token: str, key_id: str) -> str:
    """Decrypt a payload and reject values encrypted with another configured key."""
    secret = _integration_encryption_secret()
    if not secrets.compare_digest(key_id, _integration_encryption_key_id(secret)):
        raise RuntimeError("Stored secret text was encrypted with a different FORMA_USER_SECRETS_KEY.")
    try:
        plaintext = _fernet_for_secret(secret).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError("Stored secret text could not be decrypted with the configured key.") from exc
    return plaintext.decode("utf-8")


class EncryptedFileIntegrationStore(UserIntegrationStore):
    """Fernet-encrypted file storage for local workspace or user settings."""

    def __init__(self, path: Path | str, *, secret_scope: str = "workspace") -> None:
        self.path = Path(path).expanduser()
        self.secret_scope = secret_scope
        self.user_scoped = secret_scope == "user"
        self.storage_label = f"encrypted-file:{self.path}"

    def _secret(self) -> str:
        if self.secret_scope == "user":
            return _integration_encryption_secret()
        return _workspace_encryption_secret()

    def load(self) -> UserIntegrationConfig:
        if not self.path.exists():
            return UserIntegrationConfig()
        try:
            with self.path.open("r", encoding="utf-8") as file:
                raw = json.load(file)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Encrypted integration config is invalid JSON: {self.path}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"Encrypted integration config has an invalid shape: {self.path}")
        encrypted_config = raw.get("encrypted_config")
        if not isinstance(encrypted_config, str) or not encrypted_config.strip():
            raise RuntimeError(f"Encrypted integration config has no ciphertext: {self.path}")
        secret = self._secret()
        configured_key_id = _integration_encryption_key_id(secret)
        stored_key_id = raw.get("encryption_key_id")
        if stored_key_id and stored_key_id != configured_key_id:
            logger.error(
                "Encrypted file integration key mismatch: storage=%s stored_key_id=%s configured_key_id=%s",
                self.storage_label,
                stored_key_id,
                configured_key_id,
            )
            raise RuntimeError(
                "Stored provider settings were encrypted with a different integration secrets key."
            )
        return _decrypt_config_with_secret(encrypted_config, secret)

    def save(self, config: UserIntegrationConfig) -> UserIntegrationConfig:
        config.updated_at = _utc_now()
        encrypted_config, key_id = _encrypt_config_with_secret(config, self._secret())
        record = {
            "encrypted_config": encrypted_config,
            "encryption_key_id": key_id,
            "version": config.version,
            "updated_at": config.updated_at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(record, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temp_path, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return config


class SupabaseUserIntegrationStore(UserIntegrationStore):
    table_name = "user_integration_configs"

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id.strip()
        if not self.user_id:
            raise ValueError("user_id is required")
        self.path = Path(f".forma/supabase/users/{re.sub(r'[^A-Za-z0-9_.-]+', '_', self.user_id)[:160]}/encrypted")
        self.storage_label = f"supabase:{self.table_name}/{self.user_id}"

    def _client(self) -> Any:
        try:
            from forma_core.database import get_supabase_client

            return get_supabase_client()
        except Exception:
            url = _supabase_url()
            key = _supabase_service_key()
            if not url or not key:
                raise
            try:
                from supabase import create_client
            except ImportError as exc:
                raise RuntimeError("Supabase client is not installed. Run pip install -r apps/api/requirements.txt.") from exc
            return create_client(url, key)

    def load(self) -> UserIntegrationConfig:
        try:
            rows = (
                self._client()
                .table(self.table_name)
                .select("encrypted_config,encryption_key_id,version,updated_at")
                .eq("owner_user_id", self.user_id)
                .limit(1)
                .execute()
                .data
                or []
            )
        except Exception:
            logger.exception(
                "Supabase user integration read failed: owner_user_id=%s storage=%s",
                self.user_id,
                self.storage_label,
            )
            raise
        if not rows:
            logger.info(
                "Supabase user integration config not found: owner_user_id=%s storage=%s",
                self.user_id,
                self.storage_label,
            )
            return UserIntegrationConfig()
        encrypted_config = rows[0].get("encrypted_config")
        if not isinstance(encrypted_config, str) or not encrypted_config.strip():
            logger.error(
                "Supabase user integration config is empty: owner_user_id=%s storage=%s",
                self.user_id,
                self.storage_label,
            )
            return UserIntegrationConfig()
        stored_key_id = rows[0].get("encryption_key_id")
        configured_key_id = _integration_encryption_key_id(_integration_encryption_secret())
        if stored_key_id and stored_key_id != configured_key_id:
            logger.error(
                "Supabase user integration encryption key mismatch: owner_user_id=%s storage=%s "
                "stored_key_id=%s configured_key_id=%s",
                self.user_id,
                self.storage_label,
                stored_key_id,
                configured_key_id,
            )
            raise RuntimeError(
                "Stored provider settings were encrypted with a different FORMA_USER_SECRETS_KEY."
            )
        try:
            config = _decrypt_config(encrypted_config)
        except Exception:
            logger.exception(
                "Supabase user integration decrypt failed: owner_user_id=%s storage=%s "
                "stored_key_id=%s configured_key_id=%s",
                self.user_id,
                self.storage_label,
                stored_key_id,
                configured_key_id,
            )
            raise
        logger.info(
            "Supabase user integration config loaded: owner_user_id=%s storage=%s version=%s updated_at=%s",
            self.user_id,
            self.storage_label,
            rows[0].get("version"),
            rows[0].get("updated_at"),
        )
        return _sanitize_hosted_user_config(config)

    def update_integration(
        self,
        integration_id: str,
        *,
        enabled: Optional[bool] = None,
        field_values: Optional[dict[str, Optional[str]]] = None,
        clear_fields: Iterable[str] = (),
    ) -> UserIntegrationConfig:
        blocked_fields = [
            field_id
            for field_id, value in (field_values or {}).items()
            if value is not None and str(value).strip() and _is_hosted_blocked_secret_field(integration_id, field_id)
        ]
        if blocked_fields:
            policy = _active_hosted_byok_policy(integration_id)
            raise ValueError(policy.note if policy else "This hosted BYOK credential is not allowed.")
        if _hosted_byok_policy_active() and _requires_hosted_huggingface_confirmation(integration_id, field_values):
            confirmation = (field_values or {}).get("token_scope_confirmation")
            existing_confirmation = self.load().integration_by_id("huggingface")
            existing_value = existing_confirmation.field_value("token_scope_confirmation") if existing_confirmation else None
            if not (_truthy_confirmation(str(confirmation) if confirmation is not None else None) or _truthy_confirmation(existing_value)):
                raise ValueError(
                    "Hosted Hugging Face BYOK requires confirmation that the token is fine-grained with only "
                    "Make calls to Inference Providers, or an enterprise service-account token with equivalent scope."
                )
        if _hosted_byok_policy_active() and _requires_hosted_gmi_confirmation(integration_id, field_values):
            confirmation = (field_values or {}).get("key_delegation_confirmation")
            existing_confirmation = self.load().integration_by_id("gmi")
            existing_value = existing_confirmation.field_value("key_delegation_confirmation") if existing_confirmation else None
            if not (_truthy_confirmation(str(confirmation) if confirmation is not None else None) or _truthy_confirmation(existing_value)):
                raise ValueError(
                    "Hosted GMI Cloud BYOK requires confirmation that the key is scoped to a dedicated project or "
                    "organization and that third-party server-side key storage by Forma is permitted for your requests."
                )
        if _hosted_byok_policy_active() and _requires_hosted_together_confirmation(integration_id, field_values):
            confirmation = (field_values or {}).get("project_key_confirmation")
            existing_confirmation = self.load().integration_by_id("together")
            existing_value = existing_confirmation.field_value("project_key_confirmation") if existing_confirmation else None
            if not (_truthy_confirmation(str(confirmation) if confirmation is not None else None) or _truthy_confirmation(existing_value)):
                raise ValueError(
                    "Hosted Together AI BYOK requires confirmation that the API key is project-scoped, "
                    "dedicated to Forma, and is not a legacy or broad account key."
                )
        return super().update_integration(
            integration_id,
            enabled=enabled,
            field_values=field_values,
            clear_fields=clear_fields,
        )

    def save(self, config: UserIntegrationConfig) -> UserIntegrationConfig:
        config.updated_at = _utc_now()
        encrypted_config, key_id = _encrypt_config(config)
        record = {
            "owner_user_id": self.user_id,
            "encrypted_config": encrypted_config,
            "encryption_key_id": key_id,
            "version": config.version,
            "updated_at": config.updated_at,
        }
        try:
            self._client().table(self.table_name).upsert(record, on_conflict="owner_user_id").execute()
        except Exception:
            logger.exception(
                "Supabase user integration upsert failed: owner_user_id=%s storage=%s "
                "encryption_key_id=%s version=%s updated_at=%s",
                self.user_id,
                self.storage_label,
                key_id,
                config.version,
                config.updated_at,
            )
            raise
        logger.info(
            "Supabase user integration config saved: owner_user_id=%s storage=%s "
            "encryption_key_id=%s version=%s updated_at=%s",
            self.user_id,
            self.storage_label,
            key_id,
            config.version,
            config.updated_at,
        )
        return config


class SupabaseWorkspaceIntegrationStore(UserIntegrationStore):
    table_name = "workspace_integration_configs"

    def __init__(self, config_key: str = WORKSPACE_CONFIG_KEY) -> None:
        self.config_key = (config_key or WORKSPACE_CONFIG_KEY).strip()
        if not self.config_key:
            raise ValueError("config_key is required")
        self.path = Path(f".forma/supabase/workspace/{re.sub(r'[^A-Za-z0-9_.-]+', '_', self.config_key)[:160]}/encrypted")
        self.storage_label = f"supabase:{self.table_name}/{self.config_key}"

    def _client(self) -> Any:
        try:
            from forma_core.database import get_supabase_client

            return get_supabase_client()
        except Exception:
            url = _supabase_url()
            key = _supabase_service_key()
            if not url or not key:
                raise
            try:
                from supabase import create_client
            except ImportError as exc:
                raise RuntimeError("Supabase client is not installed. Run pip install -r apps/api/requirements.txt.") from exc
            return create_client(url, key)

    def load(self) -> UserIntegrationConfig:
        cache_key = self.storage_label
        now = time.monotonic()
        success_ttl = _workspace_config_cache_ttl_seconds()
        if success_ttl:
            cached = _WORKSPACE_CONFIG_CACHE.get(cache_key)
            if cached and now - cached[0] < success_ttl:
                return _clone_config(cached[1])

        failure_ttl = _workspace_config_failure_ttl_seconds()
        if failure_ttl:
            failed = _WORKSPACE_CONFIG_FAILURE_CACHE.get(cache_key)
            if failed and now - failed[0] < failure_ttl:
                return _clone_config(failed[1])

        try:
            rows = (
                self._client()
                .table(self.table_name)
                .select("encrypted_config")
                .eq("config_key", self.config_key)
                .limit(1)
                .execute()
                .data
                or []
            )
        except Exception as exc:
            if deployment_mode_enabled():
                logger.error(
                    "Supabase workspace integration config load failed for %s; refusing an empty hosted fallback: %s",
                    self.storage_label,
                    exc,
                )
                raise
            fallback = UserIntegrationConfig()
            logger.warning(
                "Supabase workspace integration config load failed for %s; using empty runtime config until Supabase is ready: %s",
                self.storage_label,
                exc,
            )
            _WORKSPACE_CONFIG_FAILURE_CACHE[cache_key] = (now, fallback, str(exc))
            return _clone_config(fallback)
        if not rows:
            config = UserIntegrationConfig()
            _WORKSPACE_CONFIG_CACHE[cache_key] = (now, config)
            _WORKSPACE_CONFIG_FAILURE_CACHE.pop(cache_key, None)
            return _clone_config(config)
        encrypted_config = rows[0].get("encrypted_config")
        if not isinstance(encrypted_config, str) or not encrypted_config.strip():
            config = UserIntegrationConfig()
        else:
            config = _decrypt_config_with_secret(encrypted_config, _workspace_encryption_secret())
        _WORKSPACE_CONFIG_CACHE[cache_key] = (now, config)
        _WORKSPACE_CONFIG_FAILURE_CACHE.pop(cache_key, None)
        return _clone_config(config)

    def save(self, config: UserIntegrationConfig) -> UserIntegrationConfig:
        config.updated_at = _utc_now()
        encrypted_config, key_id = _encrypt_config_with_secret(config, _workspace_encryption_secret())
        record = {
            "config_key": self.config_key,
            "encrypted_config": encrypted_config,
            "encryption_key_id": key_id,
            "version": config.version,
            "updated_at": config.updated_at,
        }
        self._client().table(self.table_name).upsert(record, on_conflict="config_key").execute()
        _WORKSPACE_CONFIG_CACHE[self.storage_label] = (time.monotonic(), _clone_config(config))
        _WORKSPACE_CONFIG_FAILURE_CACHE.pop(self.storage_label, None)
        return config


def mask_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _first_env(env_names: Iterable[str]) -> Optional[str]:
    for env_name in env_names:
        value = env_config.get(env_name)
        if value and value.strip():
            return value.strip()
    return None


def _managed_environment_names() -> set[str]:
    names = set(EXTRA_MANAGED_ENV_NAMES)
    for definition in INTEGRATION_DEFINITIONS:
        for field_definition in definition.fields:
            names.update(field_definition.env_names)
    names.update(PROVIDER_ALLOWED_MODEL_ENV.values())
    return names


def _capture_original_environment(env_names: Iterable[str]) -> None:
    """Remember deployment/local env values before applying a saved BYOK overlay."""
    for env_name in env_names:
        if env_name not in _ORIGINAL_ENV_VALUES:
            _ORIGINAL_ENV_VALUES[env_name] = env_config.get(env_name)


def _environment_value(
    env_names: Iterable[str],
    environment: Mapping[str, Optional[str]],
) -> Optional[str]:
    for env_name in env_names:
        value = environment.get(env_name)
        if value and value.strip():
            return value.strip()
    return None


def _original_environment_value(env_names: Iterable[str]) -> Optional[str]:
    return _environment_value(env_names, _ORIGINAL_ENV_VALUES)


def _disabled_flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off"}


@lru_cache(maxsize=1)
def _google_cloud_auth_available() -> bool:
    """True when Vertex/Gemini can authenticate with Google Cloud ADC or Vercel OIDC."""

    try:
        from forma_core.vertex_auth import build_vertex_credentials

        if build_vertex_credentials() is not None:
            return True
    except Exception:
        logger.debug("Vercel Vertex credentials are unavailable.", exc_info=True)

    try:
        from google.auth import default as google_auth_default

        credentials, _project = google_auth_default()
        return credentials is not None
    except Exception:
        logger.debug("Google Application Default Credentials are unavailable.", exc_info=True)
        return False


def _values_configure_integration(
    definition: IntegrationDefinition,
    values: dict[str, str],
    *,
    enabled: bool = True,
    runtime_provider: Optional[str] = None,
    generic_key: Optional[str] = None,
) -> bool:
    if definition.id in {"gemini", "vertex"} and _google_cloud_auth_available():
        return True
    if not values:
        return False
    if definition.id == "image":
        if not enabled or _disabled_flag(values.get("enabled")):
            return False
        return bool(values.get("api_key"))
    if definition.id == "tavily":
        return bool(values.get("api_key"))
    if definition.id not in LLM_PROVIDER_INTEGRATION_IDS:
        return True
    if definition.id == "vertex":
        return "project" in values
    secret_ids = {field.id for field in definition.fields if field.secret}
    if any(field_id in values for field_id in secret_ids):
        return True
    return runtime_provider == definition.id and bool((generic_key or "").strip())


def _environment_configures_integration(
    definition: IntegrationDefinition,
    environment: Optional[Mapping[str, Optional[str]]] = None,
) -> bool:
    source = environment if environment is not None else _ORIGINAL_ENV_VALUES
    values = {
        field.id: value
        for field in definition.fields
        if (value := (_environment_value(field.env_names, source) or "").strip())
    }
    return _values_configure_integration(
        definition,
        values,
        runtime_provider=_normalize_provider_id(source.get("LLM_PROVIDER") or ""),
        generic_key=source.get("LLM_API_KEY"),
    )


def _store_configures_integration(definition: IntegrationDefinition, stored: Optional[StoredIntegration]) -> bool:
    if not stored:
        return False
    values = {
        field.id: value
        for field in definition.fields
        if (value := (stored.field_value(field.id) or "").strip())
    }
    return _values_configure_integration(definition, values, enabled=bool(stored.enabled))


def _normalize_provider_id(value: str) -> Optional[str]:
    normalized = value.strip().lower().replace("_", "-")
    if not normalized:
        return None
    normalized = PROVIDER_ALIASES.get(normalized, normalized)
    return normalized if normalized in LLM_PROVIDER_INTEGRATION_IDS else None


def _infer_provider_for_model(model: str) -> Optional[str]:
    lowered = model.strip().lower()
    for prefix, provider in MODEL_PREFIX_PROVIDERS:
        if lowered.startswith(prefix):
            return provider
    return None


def _parse_preferred_llm_selector(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    raw_value = (value or "").strip()
    if not raw_value:
        return None, None

    if "/" in raw_value:
        try:
            selector = parse_llm_selector(raw_value)
        except ValueError:
            selector = None
        if selector:
            provider = _normalize_provider_id(selector.provider)
            if provider:
                return provider, selector.model

    inferred_provider = _infer_provider_for_model(raw_value)
    if inferred_provider:
        return inferred_provider, raw_value
    return None, raw_value


def _model_env_names_for_provider(provider: str) -> tuple[str, ...]:
    definition = _DEFINITION_BY_ID.get(provider)
    if definition is None:
        return ()
    try:
        return definition.field_by_id("model").env_names
    except KeyError:
        return ()


def _merge_csv_value(existing: Optional[str], values: Iterable[str]) -> str:
    merged = []
    seen = set()
    for item in [*(existing or "").split(","), *values]:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return ",".join(merged)


def _desired_environment(
    config: UserIntegrationConfig,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    desired: dict[str, str] = {}
    allowed_providers: set[str] = set()
    allowed_models_by_provider: dict[str, set[str]] = {}
    runtime_image_provider: Optional[str] = None
    generic_image_provider: Optional[str] = None
    inferred_image_provider: Optional[str] = None
    inferred_image_provider_updated_at = ""

    def remember_provider_model(provider: str, model: Optional[str]) -> None:
        normalized_provider = _normalize_provider_id(provider)
        if not normalized_provider:
            return
        allowed_providers.add(normalized_provider)
        if model and model.strip():
            allowed_models_by_provider.setdefault(normalized_provider, set()).add(model.strip())

    def remember_image_provider(provider: str, integration: StoredIntegration) -> None:
        nonlocal inferred_image_provider, inferred_image_provider_updated_at
        updated_at = integration.updated_at or ""
        if not inferred_image_provider or updated_at >= inferred_image_provider_updated_at:
            inferred_image_provider = provider
            inferred_image_provider_updated_at = updated_at

    for integration in config.integrations:
        if not integration.enabled:
            continue
        definition = _DEFINITION_BY_ID.get(integration.id)
        if definition is None:
            continue

        if integration.id in LLM_PROVIDER_INTEGRATION_IDS:
            has_config = any(integration.field_value(field.id) for field in definition.fields)
            if has_config:
                remember_provider_model(integration.id, integration.field_value("model"))
            if integration.id == "huggingface" and integration.field_value("api_key") and integration.field_value("image_model"):
                remember_image_provider("huggingface", integration)
            elif integration.id == "openai" and integration.field_value("image_model"):
                remember_image_provider("openai", integration)
            elif integration.id == "gmi" and integration.field_value("api_key"):
                remember_image_provider("gmi", integration)
            elif integration.id == "vertex" and integration.field_value("project") and integration.field_value("image_model"):
                remember_image_provider("vertex", integration)

        if integration.id == "together" and integration.field_value("api_key"):
            remember_image_provider("together", integration)

        for field_definition in definition.fields:
            value = integration.field_value(field_definition.id)
            if not value:
                continue
            for env_name in field_definition.env_names:
                if env_name == "IMAGE_PROVIDER" and integration.id in {"runtime", "image"}:
                    continue
                desired[env_name] = value

        if integration.id == "runtime":
            selector_provider, selector_model = _parse_preferred_llm_selector(integration.field_value("llm_selector"))
            if selector_provider:
                desired["LLM_PROVIDER"] = selector_provider
                remember_provider_model(selector_provider, selector_model)
                if selector_model:
                    desired["LLM_MODEL"] = selector_model
                    for env_name in _model_env_names_for_provider(selector_provider):
                        desired[env_name] = selector_model
            elif selector_model:
                desired["LLM_MODEL"] = selector_model
            if integration.field_value("image_provider"):
                runtime_image_provider = integration.field_value("image_provider")
        elif integration.id == "image" and integration.field_value("provider"):
            generic_image_provider = integration.field_value("provider")

    if generic_image_provider and not desired.get("IMAGE_PROVIDER"):
        desired["IMAGE_PROVIDER"] = generic_image_provider
    elif runtime_image_provider and not desired.get("IMAGE_PROVIDER"):
        desired["IMAGE_PROVIDER"] = runtime_image_provider
    # Provider credentials advertise an available fallback; they are not an
    # explicit image-provider selection. Keep a platform-selected provider
    # (for example production Vertex AI) unless the user chose a provider in
    # the runtime or image integration.
    elif (
        inferred_image_provider
        and not desired.get("IMAGE_PROVIDER")
        and not (environment or _ORIGINAL_ENV_VALUES).get("IMAGE_PROVIDER")
    ):
        desired["IMAGE_PROVIDER"] = inferred_image_provider

    if allowed_providers and not desired.get("LLM_ALLOWED_PROVIDERS"):
        desired["LLM_ALLOWED_PROVIDERS"] = ",".join(sorted(allowed_providers))
    elif allowed_providers:
        desired["LLM_ALLOWED_PROVIDERS"] = _merge_csv_value(desired.get("LLM_ALLOWED_PROVIDERS"), sorted(allowed_providers))

    for provider, models in sorted(allowed_models_by_provider.items()):
        env_name = PROVIDER_ALLOWED_MODEL_ENV.get(provider)
        if env_name and models:
            desired[env_name] = _merge_csv_value(desired.get(env_name), sorted(models))
    return desired


def resolve_user_integration_settings(
    store: Optional[UserIntegrationStore] = None,
    *,
    fail_open: bool = True,
) -> ResolvedIntegrationSettings:
    """Resolve one request's provider settings without mutating the process environment."""
    resolved_store = store or default_integration_store()
    try:
        integration_config = resolved_store.load()
    except Exception as exc:
        if not fail_open or deployment_mode_enabled():
            raise
        logger.warning(
            "Integration config load failed from %s; using empty runtime config: %s",
            getattr(resolved_store, "storage_label", getattr(resolved_store, "path", "unknown")),
            exc,
        )
        integration_config = UserIntegrationConfig()

    environment = env_config.snapshot()
    desired = _desired_environment(integration_config, environment=environment)
    additive_env_names = {"LLM_ALLOWED_PROVIDERS", *PROVIDER_ALLOWED_MODEL_ENV.values()}
    for env_name in additive_env_names:
        desired_value = desired.get(env_name)
        if desired_value:
            desired[env_name] = _merge_csv_value(environment.get(env_name), desired_value.split(","))
    environment.update(desired)
    return ResolvedIntegrationSettings(environment)


def apply_user_integrations_to_environment(
    store: Optional[UserIntegrationStore] = None,
    *,
    fail_open: bool = True,
) -> UserIntegrationConfig:
    resolved_store = store or default_integration_store()
    try:
        config = resolved_store.load()
    except Exception as exc:
        if not fail_open or deployment_mode_enabled():
            raise
        logger.warning(
            "Integration config load failed from %s; using empty runtime config: %s",
            getattr(resolved_store, "storage_label", getattr(resolved_store, "path", "unknown")),
            exc,
        )
        config = UserIntegrationConfig()
    managed_env_names = _managed_environment_names()
    _capture_original_environment(managed_env_names)
    desired = _desired_environment(config)

    # Allowlists describe availability, so BYOK entries extend platform/env
    # entries instead of replacing them. Direct settings (keys, endpoints,
    # selected provider/model) retain normal saved-over-environment precedence.
    additive_env_names = {"LLM_ALLOWED_PROVIDERS", *PROVIDER_ALLOWED_MODEL_ENV.values()}
    for env_name in additive_env_names:
        desired_value = desired.get(env_name)
        if not desired_value:
            continue
        desired[env_name] = _merge_csv_value(
            _ORIGINAL_ENV_VALUES.get(env_name),
            desired_value.split(","),
        )

    for env_name in managed_env_names:
        if env_name in desired:
            continue
        original_value = _ORIGINAL_ENV_VALUES.get(env_name)
        if original_value is None:
            env_config.unset(env_name)
        else:
            env_config.set(env_name, original_value)
        _APPLIED_ENV_VALUES.pop(env_name, None)

    for env_name, desired_value in desired.items():
        env_config.set(env_name, desired_value)
        _APPLIED_ENV_VALUES[env_name] = desired_value

    return config


def _field_status_payload(
    integration: Optional[StoredIntegration],
    field_definition: IntegrationFieldDefinition,
    *,
    integration_id: str,
    hosted_user_store: bool = False,
    environment: Optional[Mapping[str, Optional[str]]] = None,
) -> dict[str, object]:
    stored_value = integration.field_value(field_definition.id) if integration else None
    saved_value = stored_value if integration and integration.enabled else None
    environment_value = _environment_value(
        field_definition.env_names,
        environment if environment is not None else _ORIGINAL_ENV_VALUES,
    )
    policy = _active_hosted_byok_policy(integration_id) if hosted_user_store else None
    blocked_by_policy = bool(policy and field_definition.id in policy.blocked_secret_fields)
    conditional_by_policy = bool(policy and field_definition.id in policy.conditional_secret_fields)
    if blocked_by_policy:
        saved_value = None
    active_value = saved_value or environment_value
    source = "saved" if saved_value else "environment" if environment_value else "unset"
    payload: dict[str, object] = {
        "id": field_definition.id,
        "label": field_definition.label,
        "env_names": list(field_definition.env_names),
        "secret": field_definition.secret,
        "placeholder": field_definition.placeholder,
        "help": field_definition.help,
        "configured": bool(active_value),
        "saved": bool(stored_value),
        "source": source,
        "editable": not blocked_by_policy,
        "policy_status": policy.hosted_byok if policy else "enabled",
        "policy_blocked": blocked_by_policy,
        "policy_conditional": conditional_by_policy,
        "policy_notice": policy.note if policy and (blocked_by_policy or conditional_by_policy) else "",
    }
    if field_definition.secret:
        payload["masked_value"] = mask_secret(active_value)
        payload["value"] = None
    else:
        payload["masked_value"] = None
        payload["value"] = active_value
    return payload


def integration_status_payload(store: Optional[UserIntegrationStore] = None) -> dict[str, object]:
    resolved_store = store or default_integration_store()
    config = resolved_store.load()
    environment = env_config.snapshot()
    hosted_user_store = isinstance(resolved_store, SupabaseUserIntegrationStore) or bool(
        getattr(resolved_store, "user_scoped", False)
    )
    integrations: list[dict[str, object]] = []
    for definition in INTEGRATION_DEFINITIONS:
        stored = config.integration_by_id(definition.id)
        environment_configured = _environment_configures_integration(definition, environment)
        configured_fields = [
            _field_status_payload(
                stored,
                field_definition,
                integration_id=definition.id,
                hosted_user_store=hosted_user_store,
                environment=environment,
            )
            for field_definition in definition.fields
        ]
        enabled = stored.enabled if stored else True
        saved_configured = _store_configures_integration(definition, stored)
        configured = environment_configured or saved_configured
        policy = _active_hosted_byok_policy(definition.id) if hosted_user_store else None
        integrations.append(
            {
                "id": definition.id,
                "label": definition.label,
                "description": definition.description,
                "policy_status": policy.hosted_byok if policy else "enabled",
                "policy_notice": policy.note if policy else "",
                "enabled": enabled,
                "environment_configured": environment_configured,
                "available": environment_configured or (enabled and saved_configured),
                "saved": stored is not None,
                "updated_at": stored.updated_at if stored else None,
                "configured": configured,
                "fields": configured_fields,
            }
        )
    return {
        "version": config.version,
        "config_path": str(resolved_store.path),
        "storage": getattr(resolved_store, "storage_label", str(resolved_store.path)),
        "updated_at": config.updated_at,
        "integrations": integrations,
    }


__all__ = [
    "EncryptedFileIntegrationStore",
    "IntegrationDefinition",
    "IntegrationFieldDefinition",
    "StoredIntegration",
    "StoredIntegrationField",
    "ResolvedIntegrationSettings",
    "SupabaseUserIntegrationStore",
    "SupabaseWorkspaceIntegrationStore",
    "UserIntegrationConfig",
    "UserIntegrationStore",
    "apply_user_integrations_to_environment",
    "resolve_user_integration_settings",
    "default_integration_store",
    "default_user_integrations_path",
    "encrypted_user_integrations_path",
    "encrypted_workspace_integrations_path",
    "integration_definition_by_id",
    "integration_status_payload",
    "list_integration_definitions",
    "mask_secret",
    "encrypt_user_secret_text",
    "decrypt_user_secret_text",
    "require_user_secrets_key",
    "user_integrations_path_for_user",
]
