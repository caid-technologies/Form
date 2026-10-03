import base64
import html
import io
import json
import logging
from forma_core.config import config
from forma_core.vertex_auth import build_vertex_credentials
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - exercised when the optional Vertex dependency is absent.
    genai = None
    genai_types = None

from forma_core.agents.prompt_compaction import (
    DEFAULT_IMAGE_PROMPT_TARGET_CHARS,
    OPENAI_IMAGE_PROMPT_MAX_CHARS,
    PromptCompactionAgent,
    PromptCompactionResult,
)

load_dotenv()

logger = logging.getLogger(__name__)

Settings = Mapping[str, str]

DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2"
DEFAULT_VERTEX_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_VERTEX_IMAGE_LOCATION = "global"
DEFAULT_GMI_IMAGE_BASE_URL = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/requests/v1"
DEFAULT_GMI_IMAGE_MODEL = "gpt-image-2"
DEFAULT_GMI_IMAGE_QUALITY = "medium"
DEFAULT_GMI_IMAGE_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_TOGETHER_IMAGE_BASE_URL = "https://api.together.ai/v1"
DEFAULT_TOGETHER_IMAGE_MODEL = "openai/gpt-image-2"
DEFAULT_TOGETHER_IMAGE_STEPS = 4
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_TIMEOUT_SECONDS = 120.0
HUGGINGFACE_IMAGE_PROMPT_MAX_CHARS = 6000
HUGGINGFACE_IMAGE_PROMPT_TARGET_CHARS = 4000

GMI_GEMINI_IMAGE_MODELS = {
    "gemini-2.5-flash-image",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
}
GMI_WAN_IMAGE_MODELS = {"wan2.7-image", "wan2.7-image-pro"}
GMI_SEEDREAM_5_IMAGE_MODELS = {"seedream-5.0-lite", "seedream-5.0-pro"}


@dataclass
class GeneratedImage:
    data_url: str
    provider: str
    model: str
    size: str
    prompt: str
    output_format: str = "png"
    view_id: str = "product"
    label: str = "Product render"
    reference_view_id: Optional[str] = None
    prompt_original_length: Optional[int] = None
    prompt_final_length: Optional[int] = None
    prompt_compacted: bool = False
    prompt_compaction_strategy: str = "none"
    model_revision: Optional[str] = None
    inference_provider: Optional[str] = None
    model_license: Optional[str] = None


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
        logger.warning("Invalid image timeout value %r; using %.1fs.", raw_value, default)
        return default


def _first_env_int(names: List[str], default: int, settings: Optional[Settings] = None) -> int:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid image integer config value %r; using %s.", raw_value, default)
        return default


def _first_env_optional_int(names: List[str], settings: Optional[Settings] = None) -> Optional[int]:
    raw_value = _first_env(names, settings=settings)
    if raw_value is None:
        return None

    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid optional image integer config value %r; ignoring it.", raw_value)
        return None


def _scoped_image_env_helpers(settings: Optional[Settings]):
    return (
        lambda name, default=None: _env(name, default, settings),
        lambda names, default=None: _first_env(names, default, settings),
        lambda names, default=False: _first_env_bool(names, default, settings),
        lambda names, default=0.0: _first_env_float(names, default, settings),
        lambda names, default=0: _first_env_int(names, default, settings),
        lambda names: _first_env_optional_int(names, settings),
    )
def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _limit_list(values: List[Any], limit: int, item_limit: int = 140) -> List[str]:
    result: List[str] = []
    for value in values:
        text = _truncate(value, item_limit)
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _model_supports_gpt_image_params(model_name: str) -> bool:
    return model_name.strip().lower().startswith("gpt-image")


def _together_model_supports_flux_params(model_name: str) -> bool:
    return model_name.strip().lower().startswith("black-forest-labs/flux")


def _mime_for_output_format(output_format: str) -> str:
    normalized = (output_format or "png").strip().lower()
    if normalized == "jpg":
        normalized = "jpeg"
    if normalized in {"jpeg", "png", "webp"}:
        return f"image/{normalized}"
    return "image/png"


class ImageProvider:
    provider_name = "none"
    model_name = "none"
    is_configured = False
    enabled = False

    def get_debug_config(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_name,
            "enabled": self.enabled,
            "configured": self.is_configured,
            "model_name": self.model_name,
        }

    def generate_project_image(self, user_prompt: str, ir: Any) -> Optional[GeneratedImage]:
        raise NotImplementedError

    def generate_project_image_sequence(self, user_prompt: str, ir: Any) -> List[GeneratedImage]:
        image = self.generate_project_image(user_prompt, ir)
        return [image] if image else []

    def generate_test_image(self, prompt: str) -> GeneratedImage:
        """Generate one image directly, without running the project or agent pipeline."""
        if not self.is_configured:
            reason = self.get_debug_config().get("reason") or "Image provider is not configured."
            raise RuntimeError(str(reason))
        generator = getattr(self, "_generate_image_from_prompt", None)
        if not callable(generator):
            raise RuntimeError(f"{self.provider_name} does not support direct image model tests.")
        return generator(
            prompt,
            view_id="image-model-test",
            label="Image model test",
            reference_view_id=None,
        )


class NoImageProvider(ImageProvider):
    provider_name = "none"
    model_name = "none"

    def __init__(self, reason: str = "Image output is disabled.") -> None:
        self.reason = reason
        self.enabled = False
        self.is_configured = False

    def get_debug_config(self) -> Dict[str, Any]:
        return {
            **super().get_debug_config(),
            "reason": self.reason,
        }

    def generate_project_image(self, user_prompt: str, ir: Any) -> Optional[GeneratedImage]:
        return None


class OpenAIImageProvider(ImageProvider):
    def __init__(self, provider_name: str = "openai", enabled: bool = True, force_enabled: bool = False, settings: Optional[Settings] = None) -> None:
        _env, _first_env, _first_env_bool, _first_env_float, _first_env_int, _first_env_optional_int = _scoped_image_env_helpers(settings)
        normalized_provider = provider_name.strip().lower().replace("_", "-")
        self.provider_name = "openai-compatible" if normalized_provider != "openai" else "openai"
        self.enabled = enabled or force_enabled

        if self.provider_name == "openai":
            api_key_names = ["OPENAI_IMAGE_API_KEY", "OPENAI_API_KEY"]
            base_url_names = ["OPENAI_IMAGE_BASE_URL", "OPENAI_BASE_URL"]
            model_names = ["OPENAI_IMAGE_MODEL", "IMAGE_MODEL"]
            timeout_names = ["OPENAI_IMAGE_TIMEOUT_SECONDS", "IMAGE_TIMEOUT_SECONDS"]
            allow_no_api_key_names = ["OPENAI_IMAGE_ALLOW_NO_API_KEY"]
        else:
            api_key_names = ["IMAGE_API_KEY", "OPENAI_IMAGE_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"]
            base_url_names = ["IMAGE_BASE_URL", "OPENAI_IMAGE_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL"]
            model_names = ["IMAGE_MODEL", "OPENAI_IMAGE_MODEL"]
            timeout_names = ["IMAGE_TIMEOUT_SECONDS", "OPENAI_IMAGE_TIMEOUT_SECONDS"]
            allow_no_api_key_names = ["IMAGE_ALLOW_NO_API_KEY", "OPENAI_IMAGE_ALLOW_NO_API_KEY", "LLM_ALLOW_NO_API_KEY"]

        configured_base_url = _first_env(base_url_names)
        default_base_url = "https://api.openai.com/v1" if self.provider_name == "openai" else None
        self.base_url = (configured_base_url or default_base_url or "").rstrip("/")
        self.api_key = _first_env(api_key_names)
        self.organization_id = _first_env(["OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_ORGANIZATION_ID"])
        self.project_id = _first_env(["OPENAI_PROJECT_ID", "OPENAI_PROJECT"])
        self.model_name = _first_env(model_names, DEFAULT_OPENAI_IMAGE_MODEL) or DEFAULT_OPENAI_IMAGE_MODEL
        self.size = _first_env(["OPENAI_IMAGE_SIZE", "IMAGE_SIZE"], DEFAULT_IMAGE_SIZE) or DEFAULT_IMAGE_SIZE
        self.quality = _first_env(["OPENAI_IMAGE_QUALITY", "IMAGE_QUALITY"])
        self.output_format = _first_env(["OPENAI_IMAGE_OUTPUT_FORMAT", "IMAGE_OUTPUT_FORMAT"], "png") or "png"
        self.timeout_seconds = _first_env_float(timeout_names, DEFAULT_IMAGE_TIMEOUT_SECONDS)
        self.prompt_max_chars = _first_env_int(
            ["OPENAI_IMAGE_PROMPT_MAX_CHARS", "IMAGE_PROMPT_MAX_CHARS"],
            OPENAI_IMAGE_PROMPT_MAX_CHARS,
        )
        self.prompt_target_chars = _first_env_int(
            ["OPENAI_IMAGE_PROMPT_TARGET_CHARS", "IMAGE_PROMPT_TARGET_CHARS"],
            DEFAULT_IMAGE_PROMPT_TARGET_CHARS,
        )
        self.prompt_compactor = PromptCompactionAgent()
        self.allow_no_api_key = _first_env_bool(
            allow_no_api_key_names,
            default=self.provider_name != "openai" and configured_base_url is not None,
        )
        self.is_configured = bool(self.enabled and self.base_url and (self.api_key or self.allow_no_api_key))

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.enabled:
            reason = "Image output is disabled."
        elif not self.base_url:
            reason = "Image provider base URL is missing."
        elif not self.api_key and not self.allow_no_api_key:
            reason = "Image provider API key is missing."

        return {
            **super().get_debug_config(),
            "base_url": self.base_url,
            "size": self.size,
            "quality": self.quality,
            "output_format": self.output_format,
            "prompt_max_chars": self.prompt_max_chars,
            "prompt_target_chars": self.prompt_target_chars,
            "reason": reason,
        }

    def _headers(self, content_type: Optional[str] = "application/json") -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "Forma-OSS/1.0",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider_name == "openai":
            if self.organization_id:
                headers["OpenAI-Organization"] = self.organization_id
            if self.project_id:
                headers["OpenAI-Project"] = self.project_id
        return headers

    def _request_json(self, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{self.provider_name} image request failed with HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{self.provider_name} image request failed: {exc}") from exc

        if not body.strip():
            return {}
        return json.loads(body)

    def _request_multipart(
        self,
        path: str,
        fields: Dict[str, Any],
        files: List[Tuple[str, str, bytes, str]],
    ) -> Dict[str, Any]:
        boundary = f"----FormaImageBoundary{uuid.uuid4().hex}"
        body_parts: List[bytes] = []

        for name, value in fields.items():
            if value is None:
                continue
            body_parts.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )

        for field_name, filename, content, content_type in files:
            body_parts.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    (
                        f'Content-Disposition: form-data; name="{field_name}"; '
                        f'filename="{filename}"\r\n'
                    ).encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                    content,
                    b"\r\n",
                ]
            )

        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(body_parts)
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=body,
            headers=self._headers(f"multipart/form-data; boundary={boundary}"),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{self.provider_name} image edit request failed with HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{self.provider_name} image edit request failed: {exc}") from exc

        return json.loads(raw_body) if raw_body.strip() else {}

    def generate_project_image(self, user_prompt: str, ir: Any) -> Optional[GeneratedImage]:
        if not self.is_configured:
            return None

        image_prompt = build_project_image_prompt(user_prompt, ir)
        return self._generate_image_from_prompt(
            image_prompt,
            view_id="case",
            label="Product exterior render",
            reference_view_id=None,
        )

    def generate_project_image_sequence(self, user_prompt: str, ir: Any) -> List[GeneratedImage]:
        if not self.is_configured:
            return []

        prompts = build_project_image_sequence_prompts(user_prompt, ir)
        sequence: List[GeneratedImage] = []
        previous: Optional[GeneratedImage] = None

        for item in prompts:
            view_id = item["view_id"]
            label = item["label"]
            prompt = item["prompt"]
            if item.get("generation_mode") == "deterministic_svg":
                prompt_result = self._compact_image_prompt(prompt, view_id=view_id)
                image = build_project_layout_diagram_image(
                    user_prompt,
                    ir,
                    prompt=prompt_result.prompt,
                    reference_view_id="visual_spec",
                    prompt_compaction_result=prompt_result,
                )
                sequence.append(image)
                previous = image
                continue

            reference_view_id = previous.view_id if previous else None
            try:
                if previous:
                    image = self._edit_image_from_prompt(
                        prompt,
                        previous,
                        view_id=view_id,
                        label=label,
                        reference_view_id=reference_view_id,
                    )
                else:
                    image = self._generate_image_from_prompt(
                        prompt,
                        view_id=view_id,
                        label=label,
                        reference_view_id=None,
                    )
            except Exception as exc:
                if not previous:
                    raise
                logger.warning("Image-to-image stage %s failed; falling back to text generation: %s", view_id, exc)
                image = self._generate_image_from_prompt(
                    prompt,
                    view_id=view_id,
                    label=label,
                    reference_view_id=reference_view_id,
                )
            sequence.append(image)
            previous = image

        return sequence

    def _generate_image_from_prompt(
        self,
        image_prompt: str,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        prompt_result = self._compact_image_prompt(image_prompt, view_id=view_id)
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt_result.prompt,
            "size": self.size,
            "n": 1,
        }

        if _model_supports_gpt_image_params(self.model_name):
            if self.quality:
                payload["quality"] = self.quality
            if self.output_format:
                payload["output_format"] = self.output_format

        response = self._request_json("images/generations", method="POST", payload=payload)
        item = _first_image_item(response)
        if not item:
            raise RuntimeError(f"{self.provider_name} image response did not include image data.")

        return GeneratedImage(
            data_url=_image_data_url_from_item(item, self.output_format, self.provider_name),
            provider=self.provider_name,
            model=self.model_name,
            size=self.size,
            prompt=prompt_result.prompt,
            output_format=self.output_format,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            prompt_original_length=prompt_result.original_length,
            prompt_final_length=prompt_result.final_length,
            prompt_compacted=prompt_result.was_compacted,
            prompt_compaction_strategy=prompt_result.strategy,
        )

    def _edit_image_from_prompt(
        self,
        image_prompt: str,
        reference_image: GeneratedImage,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        prompt_result = self._compact_image_prompt(image_prompt, view_id=view_id)
        reference_bytes, reference_content_type = _image_bytes_from_data(reference_image.data_url)
        fields: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt_result.prompt,
            "size": self.size,
            "n": 1,
        }
        if _model_supports_gpt_image_params(self.model_name):
            if self.quality:
                fields["quality"] = self.quality
            if self.output_format:
                fields["output_format"] = self.output_format

        response = self._request_multipart(
            "images/edits",
            fields,
            [("image", f"{reference_image.view_id or 'reference'}.png", reference_bytes, reference_content_type)],
        )
        item = _first_image_item(response)
        if not item:
            raise RuntimeError(f"{self.provider_name} image edit response did not include image data.")

        return GeneratedImage(
            data_url=_image_data_url_from_item(item, self.output_format, self.provider_name),
            provider=self.provider_name,
            model=self.model_name,
            size=self.size,
            prompt=prompt_result.prompt,
            output_format=self.output_format,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            prompt_original_length=prompt_result.original_length,
            prompt_final_length=prompt_result.final_length,
            prompt_compacted=prompt_result.was_compacted,
            prompt_compaction_strategy=prompt_result.strategy,
        )

    def _compact_image_prompt(self, image_prompt: str, *, view_id: str) -> PromptCompactionResult:
        result = self.prompt_compactor.compact_if_needed(
            image_prompt,
            max_chars=self.prompt_max_chars,
            target_chars=self.prompt_target_chars,
            label=f"image prompt {view_id}",
        )
        if result.was_compacted:
            logger.info(
                "Image prompt for %s compacted from %s to %s characters.",
                view_id,
                result.original_length,
                result.final_length,
        )
        return result


class VertexAIImageProvider(OpenAIImageProvider):
    """Nano Banana image generation on Vertex AI using Application Default Credentials."""

    def __init__(self, enabled: bool = True, force_enabled: bool = False, settings: Optional[Settings] = None) -> None:
        _env, _first_env, _first_env_bool, _first_env_float, _first_env_int, _first_env_optional_int = _scoped_image_env_helpers(settings)
        ImageProvider.__init__(self)
        self.provider_name = "vertex"
        self.enabled = enabled or force_enabled
        self.project = _first_env(
            ["VERTEX_AI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"]
        )
        self.location = (
            _first_env(["VERTEX_AI_LOCATION", "GOOGLE_CLOUD_LOCATION"], DEFAULT_VERTEX_IMAGE_LOCATION)
            or DEFAULT_VERTEX_IMAGE_LOCATION
        )
        self.model_name = (
            _first_env(["VERTEX_AI_IMAGE_MODEL", "VERTEX_IMAGE_MODEL", "IMAGE_MODEL"], DEFAULT_VERTEX_IMAGE_MODEL)
            or DEFAULT_VERTEX_IMAGE_MODEL
        )
        self.aspect_ratio = (
            _first_env(["VERTEX_AI_IMAGE_ASPECT_RATIO", "VERTEX_IMAGE_ASPECT_RATIO", "IMAGE_ASPECT_RATIO"], "1:1")
            or "1:1"
        )
        self.resolution = (
            _first_env(["VERTEX_AI_IMAGE_RESOLUTION", "VERTEX_IMAGE_RESOLUTION", "IMAGE_RESOLUTION"], "1K")
            or "1K"
        ).upper()
        self.size = self.resolution
        self.output_format = (
            _first_env(["VERTEX_AI_IMAGE_OUTPUT_FORMAT", "VERTEX_IMAGE_OUTPUT_FORMAT", "IMAGE_OUTPUT_FORMAT"], "png")
            or "png"
        ).lower().replace("jpg", "jpeg")
        self.timeout_seconds = _first_env_float(
            ["VERTEX_AI_IMAGE_TIMEOUT_SECONDS", "VERTEX_IMAGE_TIMEOUT_SECONDS", "IMAGE_TIMEOUT_SECONDS"],
            DEFAULT_IMAGE_TIMEOUT_SECONDS,
        )
        self.prompt_max_chars = _first_env_int(
            ["VERTEX_AI_IMAGE_PROMPT_MAX_CHARS", "VERTEX_IMAGE_PROMPT_MAX_CHARS", "IMAGE_PROMPT_MAX_CHARS"],
            OPENAI_IMAGE_PROMPT_MAX_CHARS,
        )
        self.prompt_target_chars = _first_env_int(
            ["VERTEX_AI_IMAGE_PROMPT_TARGET_CHARS", "VERTEX_IMAGE_PROMPT_TARGET_CHARS", "IMAGE_PROMPT_TARGET_CHARS"],
            DEFAULT_IMAGE_PROMPT_TARGET_CHARS,
        )
        self.prompt_compactor = PromptCompactionAgent()
        self.client = None
        self.init_error: Optional[str] = None

        if self.enabled and self.project and genai:
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
                self.client = genai.Client(**client_config)
            except Exception as exc:
                self.init_error = f"Error initializing Vertex AI image provider: {exc}"
                logger.error(self.init_error)
        elif not self.project:
            self.init_error = (
                "No Vertex AI project found. Set VERTEX_AI_PROJECT or GOOGLE_CLOUD_PROJECT "
                "and configure Application Default Credentials."
            )
        elif not genai:
            self.init_error = "Vertex AI image generation requires the google-genai package."
        self.is_configured = bool(self.enabled and self.client is not None)

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.enabled:
            reason = "Image output is disabled."
        elif self.init_error:
            reason = self.init_error
        return {
            **ImageProvider.get_debug_config(self),
            "project": self.project,
            "location": self.location,
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "output_format": self.output_format,
            "reason": reason,
        }

    def _generate_image_from_prompt(
        self,
        image_prompt: str,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        return self._generate_vertex_image(
            image_prompt,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
        )

    def _edit_image_from_prompt(
        self,
        image_prompt: str,
        reference_image: GeneratedImage,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        reference_bytes, reference_mime_type = _image_bytes_from_data(reference_image.data_url)
        return self._generate_vertex_image(
            image_prompt,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            reference_bytes=reference_bytes,
            reference_mime_type=reference_mime_type,
        )

    def _generate_vertex_image(
        self,
        image_prompt: str,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
        reference_bytes: Optional[bytes] = None,
        reference_mime_type: str = "image/png",
    ) -> GeneratedImage:
        if not self.client or genai_types is None:
            raise RuntimeError(self.init_error or "Vertex AI image provider is not configured.")

        prompt_result = self._compact_image_prompt(image_prompt, view_id=view_id)
        contents: Any = prompt_result.prompt
        if reference_bytes is not None:
            contents = [
                genai_types.Part.from_bytes(data=reference_bytes, mime_type=reference_mime_type),
                prompt_result.prompt,
            ]
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=genai_types.ImageConfig(
                    aspect_ratio=self.aspect_ratio,
                    image_size=self.resolution,
                    output_mime_type=_mime_for_output_format(self.output_format),
                ),
            ),
        )
        image_bytes, image_mime_type = self._first_inline_image(response)
        actual_format = image_mime_type.removeprefix("image/").replace("jpg", "jpeg")
        return GeneratedImage(
            data_url=f"data:{image_mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}",
            provider=self.provider_name,
            model=self.model_name,
            size=self.resolution,
            prompt=prompt_result.prompt,
            output_format=actual_format,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            prompt_original_length=prompt_result.original_length,
            prompt_final_length=prompt_result.final_length,
            prompt_compacted=prompt_result.was_compacted,
            prompt_compaction_strategy=prompt_result.strategy,
        )

    @staticmethod
    def _first_inline_image(response: Any) -> Tuple[bytes, str]:
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline_data = getattr(part, "inline_data", None)
                data = getattr(inline_data, "data", None)
                if data:
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    mime_type = str(getattr(inline_data, "mime_type", None) or "image/png")
                    return bytes(data), mime_type
        raise RuntimeError("Vertex AI image response did not include image data.")


class GMIImageProvider(OpenAIImageProvider):
    def __init__(self, enabled: bool = True, force_enabled: bool = False, settings: Optional[Settings] = None) -> None:
        _env, _first_env, _first_env_bool, _first_env_float, _first_env_int, _first_env_optional_int = _scoped_image_env_helpers(settings)
        ImageProvider.__init__(self)
        self.provider_name = "gmi"
        self.enabled = enabled or force_enabled
        self.base_url = (
            _first_env(
                [
                    "GMI_IMAGE_BASE_URL",
                    "GMI_CLOUD_IMAGE_BASE_URL",
                    "GMICLOUD_IMAGE_BASE_URL",
                    "GMI_BASE_URL",
                ],
                DEFAULT_GMI_IMAGE_BASE_URL,
            )
            or DEFAULT_GMI_IMAGE_BASE_URL
        ).rstrip("/")
        self.api_key = _first_env(["GMI_IMAGE_API_KEY", "GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY"])
        self.organization_id = None
        self.project_id = None
        self.model_name = (
            _first_env(["GMI_IMAGE_MODEL", "GMI_CLOUD_IMAGE_MODEL", "GMICLOUD_IMAGE_MODEL", "IMAGE_MODEL"], DEFAULT_GMI_IMAGE_MODEL)
            or DEFAULT_GMI_IMAGE_MODEL
        )
        default_size = "2048x2048" if self.model_name in GMI_SEEDREAM_5_IMAGE_MODELS else DEFAULT_IMAGE_SIZE
        self.size = _first_env(
            ["GMI_IMAGE_SIZE", "GMI_CLOUD_IMAGE_SIZE", "GMICLOUD_IMAGE_SIZE", "IMAGE_SIZE"],
            default_size,
        ) or default_size
        self.quality = _first_env(
            ["GMI_IMAGE_QUALITY", "GMI_CLOUD_IMAGE_QUALITY", "GMICLOUD_IMAGE_QUALITY", "IMAGE_QUALITY"],
            DEFAULT_GMI_IMAGE_QUALITY,
        )
        default_output_format = "jpeg" if self.model_name == "seedream-5.0-pro" else "png"
        self.output_format = _first_env(
            ["GMI_IMAGE_OUTPUT_FORMAT", "GMI_CLOUD_IMAGE_OUTPUT_FORMAT", "GMICLOUD_IMAGE_OUTPUT_FORMAT", "IMAGE_OUTPUT_FORMAT"],
            default_output_format,
        ) or default_output_format
        self.output_compression = _first_env_optional_int(
            ["GMI_IMAGE_OUTPUT_COMPRESSION", "GMI_CLOUD_IMAGE_OUTPUT_COMPRESSION"]
        )
        if self.output_compression is not None and not 0 <= self.output_compression <= 100:
            logger.warning("GMI image output compression must be between 0 and 100; ignoring %s.", self.output_compression)
            self.output_compression = None
        self.background = _first_env(["GMI_IMAGE_BACKGROUND", "GMI_CLOUD_IMAGE_BACKGROUND"], "auto") or "auto"
        self.moderation = _first_env(["GMI_IMAGE_MODERATION", "GMI_CLOUD_IMAGE_MODERATION"], "auto") or "auto"
        default_resolution = "2K" if self.model_name in GMI_WAN_IMAGE_MODELS else "1K"
        self.resolution = (
            _first_env(["GMI_IMAGE_RESOLUTION", "GMI_CLOUD_IMAGE_RESOLUTION"], default_resolution)
            or default_resolution
        )
        self.aspect_ratio = _first_env(["GMI_IMAGE_ASPECT_RATIO", "GMI_CLOUD_IMAGE_ASPECT_RATIO"], "1:1") or "1:1"
        self.seed = _first_env_optional_int(["GMI_IMAGE_SEED", "GMI_CLOUD_IMAGE_SEED"])
        self.prompt_upsampling = _first_env_bool(["GMI_IMAGE_PROMPT_UPSAMPLING", "GMI_CLOUD_IMAGE_PROMPT_UPSAMPLING"], default=False)
        self.safety_tolerance = _first_env_int(["GMI_IMAGE_SAFETY_TOLERANCE", "GMI_CLOUD_IMAGE_SAFETY_TOLERANCE"], 2)
        if not 0 <= self.safety_tolerance <= 6:
            logger.warning("GMI image safety tolerance must be between 0 and 6; using 2.")
            self.safety_tolerance = 2
        self.sequential_generation = (
            _first_env(["GMI_IMAGE_SEQUENTIAL_GENERATION", "GMI_CLOUD_IMAGE_SEQUENTIAL_GENERATION"], "disabled") or "disabled"
        )
        self.watermark = _first_env_bool(["GMI_IMAGE_WATERMARK", "GMI_CLOUD_IMAGE_WATERMARK"], default=False)
        self.timeout_seconds = _first_env_float(
            ["GMI_IMAGE_TIMEOUT_SECONDS", "GMI_CLOUD_IMAGE_TIMEOUT_SECONDS", "GMICLOUD_IMAGE_TIMEOUT_SECONDS", "IMAGE_TIMEOUT_SECONDS"],
            DEFAULT_IMAGE_TIMEOUT_SECONDS,
        )
        self.poll_interval_seconds = _first_env_float(
            ["GMI_IMAGE_POLL_INTERVAL_SECONDS", "GMI_CLOUD_IMAGE_POLL_INTERVAL_SECONDS"],
            DEFAULT_GMI_IMAGE_POLL_INTERVAL_SECONDS,
        )
        documented_prompt_max = 2000 if self.model_name == "flux-kontext-pro" else 5000 if self.model_name in GMI_WAN_IMAGE_MODELS else OPENAI_IMAGE_PROMPT_MAX_CHARS
        self.prompt_max_chars = _first_env_int(
            ["GMI_IMAGE_PROMPT_MAX_CHARS", "IMAGE_PROMPT_MAX_CHARS"],
            documented_prompt_max,
        )
        self.prompt_target_chars = _first_env_int(
            ["GMI_IMAGE_PROMPT_TARGET_CHARS", "IMAGE_PROMPT_TARGET_CHARS"],
            DEFAULT_IMAGE_PROMPT_TARGET_CHARS,
        )
        self.prompt_compactor = PromptCompactionAgent()
        self.allow_no_api_key = _first_env_bool(["GMI_IMAGE_ALLOW_NO_API_KEY", "IMAGE_ALLOW_NO_API_KEY"], default=False)
        self.is_configured = bool(self.enabled and self.base_url and self.model_name and (self.api_key or self.allow_no_api_key))

    def _queue_base_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        marker = "/api/v1/ie/requestqueue/apikey/requests"
        if marker in base_url:
            return f"{base_url.split(marker, 1)[0]}{marker}"
        if base_url.endswith("/v1"):
            return base_url[:-3]
        return base_url

    def _request_queue_json(
        self,
        *,
        method: str,
        request_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self._queue_base_url()
        if request_id:
            url = f"{url}/{urllib.parse.quote(request_id, safe='')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GMI image queue request failed with HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GMI image queue request failed: {exc}") from exc
        return json.loads(body) if body.strip() else {}

    def _wait_for_queue_image(self, response: Dict[str, Any]) -> Dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        current = response
        while True:
            status = str(current.get("status") or "").strip().lower()
            if status == "success" or _first_image_item(current):
                return current
            if status in {"failed", "cancelled"}:
                detail = current.get("error") or current.get("message") or current.get("outcome") or status
                raise RuntimeError(f"GMI image queue request {status}: {_truncate(detail, 500)}")
            request_id = str(current.get("request_id") or "").strip()
            if not request_id:
                raise RuntimeError("GMI image queue response did not include request_id or image output.")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"GMI image queue request {request_id} timed out after {self.timeout_seconds:.1f}s.")
            time.sleep(max(0.1, min(self.poll_interval_seconds, deadline - time.monotonic())))
            current = self._request_queue_json(method="GET", request_id=request_id)

    def _queue_payload(self, prompt: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"prompt": prompt}
        if self.model_name in GMI_GEMINI_IMAGE_MODELS:
            payload["image_size"] = self.resolution
            payload["aspect_ratio"] = self.aspect_ratio
            payload["image_output_format"] = self.output_format
        elif self.model_name == "gpt-image-2-generate":
            payload["size"] = self.size
            payload["quality"] = self.quality
            payload["output_format"] = self.output_format
            payload["n"] = 1
        elif self.model_name == "gpt-image-1.5":
            payload["size"] = self.size
            payload["quality"] = self.quality
            payload["output_format"] = self.output_format
            payload["background"] = self.background
            payload["n"] = 1
            if self.output_format.lower() in {"jpeg", "jpg", "webp"} and self.output_compression is not None:
                payload["output_compression"] = self.output_compression
        elif self.model_name == "flux-kontext-pro":
            payload["aspect_ratio"] = self.aspect_ratio
            payload["prompt_upsampling"] = self.prompt_upsampling
            payload["safety_tolerance"] = self.safety_tolerance
            payload["output_format"] = self.output_format
            if self.seed is not None:
                payload["seed"] = self.seed
        elif self.model_name in GMI_SEEDREAM_5_IMAGE_MODELS:
            payload["size"] = self.size
            payload["output_format"] = self.output_format
            payload["max_images"] = 1
            payload["sequential_image_generation"] = self.sequential_generation
            payload["watermark"] = self.watermark
        elif self.model_name in GMI_WAN_IMAGE_MODELS:
            payload = {"text": prompt, "size": self.resolution, "n": 1}
        return payload

    def _generate_image_from_prompt(
        self,
        image_prompt: str,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        prompt_result = self._compact_image_prompt(image_prompt, view_id=view_id)
        if self.model_name == "gpt-image-2":
            payload: Dict[str, Any] = {
                "model": self.model_name,
                "prompt": prompt_result.prompt,
                "size": self.size,
                "quality": self.quality,
                "output_format": self.output_format,
                "background": self.background,
                "moderation": self.moderation,
                "n": 1,
            }
            if self.output_format.lower() in {"jpeg", "jpg"} and self.output_compression is not None:
                payload["output_compression"] = self.output_compression
            response = self._request_json("images/generations", method="POST", payload=payload)
            reported_size = self.size
        else:
            response = self._request_queue_json(
                method="POST",
                payload={"model": self.model_name, "payload": self._queue_payload(prompt_result.prompt)},
            )
            response = self._wait_for_queue_image(response)
            if self.model_name in GMI_GEMINI_IMAGE_MODELS:
                reported_size = f"{self.resolution} {self.aspect_ratio}"
            elif self.model_name in GMI_WAN_IMAGE_MODELS:
                reported_size = self.resolution
            elif self.model_name == "flux-kontext-pro":
                reported_size = self.aspect_ratio
            else:
                reported_size = self.size

        item = _first_image_item(response)
        if not item:
            raise RuntimeError("GMI image response did not include image data.")
        return GeneratedImage(
            data_url=_image_data_url_from_item(item, self.output_format, self.provider_name),
            provider=self.provider_name,
            model=self.model_name,
            size=reported_size,
            prompt=prompt_result.prompt,
            output_format=self.output_format,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            prompt_original_length=prompt_result.original_length,
            prompt_final_length=prompt_result.final_length,
            prompt_compacted=prompt_result.was_compacted,
            prompt_compaction_strategy=prompt_result.strategy,
        )

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.enabled:
            reason = "Image output is disabled."
        elif not self.base_url:
            reason = "GMI image base URL is missing."
        elif not self.model_name:
            reason = "GMI image model is missing."
        elif not self.api_key and not self.allow_no_api_key:
            reason = "GMI image API key is missing."

        return {
            **ImageProvider.get_debug_config(self),
            "base_url": self.base_url,
            "size": self.size,
            "quality": self.quality,
            "output_format": self.output_format,
            "output_compression": self.output_compression,
            "background": self.background,
            "moderation": self.moderation,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "seed": self.seed,
            "prompt_upsampling": self.prompt_upsampling,
            "safety_tolerance": self.safety_tolerance,
            "sequential_generation": self.sequential_generation,
            "watermark": self.watermark,
            "request_mode": "native-openai" if self.model_name == "gpt-image-2" else "gmi-request-queue",
            "prompt_max_chars": self.prompt_max_chars,
            "prompt_target_chars": self.prompt_target_chars,
            "reason": reason,
        }


class TogetherImageProvider(OpenAIImageProvider):
    def __init__(self, enabled: bool = True, force_enabled: bool = False, settings: Optional[Settings] = None) -> None:
        _env, _first_env, _first_env_bool, _first_env_float, _first_env_int, _first_env_optional_int = _scoped_image_env_helpers(settings)
        ImageProvider.__init__(self)
        self.provider_name = "together"
        self.enabled = enabled or force_enabled
        self.base_url = (
            _first_env(["TOGETHER_IMAGE_BASE_URL", "TOGETHER_BASE_URL"], DEFAULT_TOGETHER_IMAGE_BASE_URL)
            or DEFAULT_TOGETHER_IMAGE_BASE_URL
        ).rstrip("/")
        self.api_key = _first_env(["TOGETHER_IMAGE_API_KEY", "TOGETHER_API_KEY"])
        self.organization_id = None
        self.project_id = None
        self.model_name = (
            _first_env(["TOGETHER_IMAGE_MODEL", "TOGETHER_MODEL", "IMAGE_MODEL"], DEFAULT_TOGETHER_IMAGE_MODEL)
            or DEFAULT_TOGETHER_IMAGE_MODEL
        )
        self.size = _first_env(["TOGETHER_IMAGE_SIZE", "IMAGE_SIZE"], DEFAULT_IMAGE_SIZE) or DEFAULT_IMAGE_SIZE
        default_steps = DEFAULT_TOGETHER_IMAGE_STEPS if _together_model_supports_flux_params(self.model_name) else 0
        self.num_inference_steps = _first_env_int(["TOGETHER_IMAGE_STEPS", "TOGETHER_IMAGE_NUM_INFERENCE_STEPS", "IMAGE_STEPS"], default_steps)
        self.output_format = _first_env(["TOGETHER_IMAGE_OUTPUT_FORMAT", "IMAGE_OUTPUT_FORMAT"], "png") or "png"
        self.timeout_seconds = _first_env_float(["TOGETHER_IMAGE_TIMEOUT_SECONDS", "IMAGE_TIMEOUT_SECONDS"], DEFAULT_IMAGE_TIMEOUT_SECONDS)
        self.prompt_max_chars = _first_env_int(["TOGETHER_IMAGE_PROMPT_MAX_CHARS", "IMAGE_PROMPT_MAX_CHARS"], HUGGINGFACE_IMAGE_PROMPT_MAX_CHARS)
        self.prompt_target_chars = _first_env_int(
            ["TOGETHER_IMAGE_PROMPT_TARGET_CHARS", "IMAGE_PROMPT_TARGET_CHARS"],
            HUGGINGFACE_IMAGE_PROMPT_TARGET_CHARS,
        )
        self.prompt_compactor = PromptCompactionAgent()
        self.allow_no_api_key = _first_env_bool(["TOGETHER_IMAGE_ALLOW_NO_API_KEY", "IMAGE_ALLOW_NO_API_KEY"], default=False)
        self.is_configured = bool(self.enabled and self.base_url and self.model_name and (self.api_key or self.allow_no_api_key))

    def _size_dimensions(self) -> Tuple[Optional[int], Optional[int]]:
        normalized = (self.size or "").strip().lower()
        if "x" not in normalized:
            return None, None
        width_raw, height_raw = normalized.split("x", 1)
        try:
            return int(width_raw), int(height_raw)
        except ValueError:
            return None, None

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.enabled:
            reason = "Image output is disabled."
        elif not self.base_url:
            reason = "Together AI image base URL is missing."
        elif not self.model_name:
            reason = "Together AI image model is missing."
        elif not self.api_key and not self.allow_no_api_key:
            reason = "Together AI image API key is missing."

        return {
            **ImageProvider.get_debug_config(self),
            "base_url": self.base_url,
            "size": self.size,
            "steps": self.num_inference_steps if _together_model_supports_flux_params(self.model_name) else None,
            "output_format": self.output_format,
            "prompt_max_chars": self.prompt_max_chars,
            "prompt_target_chars": self.prompt_target_chars,
            "reason": reason,
        }

    def _generate_image_from_prompt(
        self,
        image_prompt: str,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        prompt_result = self._compact_image_prompt(image_prompt, view_id=view_id)
        width, height = self._size_dimensions()
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt_result.prompt,
        }
        if _together_model_supports_flux_params(self.model_name):
            payload["n"] = 1
            payload["response_format"] = "base64"
            payload["output_format"] = self.output_format
            if width and height:
                payload["width"] = width
                payload["height"] = height
            if self.num_inference_steps:
                payload["steps"] = self.num_inference_steps

        response = self._request_json("images/generations", method="POST", payload=payload)
        item = _first_image_item(response)
        if not item:
            raise RuntimeError("Together AI image response did not include image data.")

        return GeneratedImage(
            data_url=_image_data_url_from_item(item, self.output_format, self.provider_name),
            provider=self.provider_name,
            model=self.model_name,
            size=self.size,
            prompt=prompt_result.prompt,
            output_format=self.output_format,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            prompt_original_length=prompt_result.original_length,
            prompt_final_length=prompt_result.final_length,
            prompt_compacted=prompt_result.was_compacted,
            prompt_compaction_strategy=prompt_result.strategy,
        )

    def generate_project_image_sequence(self, user_prompt: str, ir: Any) -> List[GeneratedImage]:
        if not self.is_configured:
            return []
        sequence: List[GeneratedImage] = []
        for item in build_project_image_sequence_prompts(user_prompt, ir):
            if item.get("generation_mode") == "deterministic_svg":
                prompt_result = self._compact_image_prompt(item["prompt"], view_id=item["view_id"])
                sequence.append(
                    build_project_layout_diagram_image(
                        user_prompt,
                        ir,
                        prompt=prompt_result.prompt,
                        reference_view_id="visual_spec",
                        prompt_compaction_result=prompt_result,
                    )
                )
                continue
            sequence.append(
                self._generate_image_from_prompt(
                    item["prompt"],
                    view_id=item["view_id"],
                    label=item["label"],
                    reference_view_id=None,
                )
            )
        return sequence


class HuggingFaceImageProvider(ImageProvider):
    provider_name = "huggingface"

    def __init__(self, enabled: bool = True, force_enabled: bool = False, settings: Optional[Settings] = None) -> None:
        _env, _first_env, _first_env_bool, _first_env_float, _first_env_int, _first_env_optional_int = _scoped_image_env_helpers(settings)
        self.enabled = enabled or force_enabled
        self.api_key = _first_env(["HUGGINGFACE_IMAGE_API_KEY", "HF_IMAGE_TOKEN", "HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGINGFACE_HUB_TOKEN", "HF_API_TOKEN"])
        self.model_name = _first_env(["HUGGINGFACE_IMAGE_MODEL", "HF_IMAGE_MODEL", "IMAGE_MODEL"], "black-forest-labs/FLUX.1-schnell") or "black-forest-labs/FLUX.1-schnell"
        self.inference_provider = _first_env(["HUGGINGFACE_IMAGE_INFERENCE_PROVIDER", "HF_IMAGE_INFERENCE_PROVIDER", "HUGGINGFACE_INFERENCE_PROVIDER", "HF_INFERENCE_PROVIDER"])
        self.model_revision = _first_env(["HUGGINGFACE_IMAGE_MODEL_REVISION", "HF_IMAGE_MODEL_REVISION", "HUGGINGFACE_MODEL_REVISION", "HF_MODEL_REVISION"])
        self.model_license = _first_env(["HUGGINGFACE_IMAGE_MODEL_LICENSE", "HF_IMAGE_MODEL_LICENSE", "HUGGINGFACE_MODEL_LICENSE", "HF_MODEL_LICENSE"])
        self.gated_models_enabled = _first_env_bool(["HUGGINGFACE_IMAGE_GATED_MODELS_ENABLED", "HF_IMAGE_GATED_MODELS_ENABLED", "HUGGINGFACE_GATED_MODELS_ENABLED", "HF_GATED_MODELS_ENABLED"])
        self.size = _first_env(["HUGGINGFACE_IMAGE_SIZE", "HF_IMAGE_SIZE", "IMAGE_SIZE"], DEFAULT_IMAGE_SIZE) or DEFAULT_IMAGE_SIZE
        self.guidance_scale = _first_env_float(["HUGGINGFACE_IMAGE_GUIDANCE_SCALE", "HF_IMAGE_GUIDANCE_SCALE"], 0.0)
        self.num_inference_steps = _first_env_int(["HUGGINGFACE_IMAGE_STEPS", "HF_IMAGE_STEPS"], 0)
        self.negative_prompt = _first_env(["HUGGINGFACE_IMAGE_NEGATIVE_PROMPT", "HF_IMAGE_NEGATIVE_PROMPT"])
        self.seed = _first_env_int(["HUGGINGFACE_IMAGE_SEED", "HF_IMAGE_SEED"], 0)
        self.output_format = _first_env(["HUGGINGFACE_IMAGE_OUTPUT_FORMAT", "HF_IMAGE_OUTPUT_FORMAT", "IMAGE_OUTPUT_FORMAT"], "png") or "png"
        self.timeout_seconds = _first_env_float(["HUGGINGFACE_IMAGE_TIMEOUT_SECONDS", "HF_IMAGE_TIMEOUT_SECONDS", "IMAGE_TIMEOUT_SECONDS"], DEFAULT_IMAGE_TIMEOUT_SECONDS)
        self.prompt_max_chars = _first_env_int(
            ["HUGGINGFACE_IMAGE_PROMPT_MAX_CHARS", "HF_IMAGE_PROMPT_MAX_CHARS", "IMAGE_PROMPT_MAX_CHARS"],
            HUGGINGFACE_IMAGE_PROMPT_MAX_CHARS,
        )
        self.prompt_target_chars = _first_env_int(
            ["HUGGINGFACE_IMAGE_PROMPT_TARGET_CHARS", "HF_IMAGE_PROMPT_TARGET_CHARS", "IMAGE_PROMPT_TARGET_CHARS"],
            HUGGINGFACE_IMAGE_PROMPT_TARGET_CHARS,
        )
        self.prompt_compactor = PromptCompactionAgent()
        self.is_configured = bool(self.enabled and self.api_key and self.model_name and (self.gated_models_enabled or not self._looks_gated_model()))

    def _looks_gated_model(self) -> bool:
        model = self.model_name.strip().lower()
        return "flux.1-dev" in model or "gated" in model

    def _size_dimensions(self) -> Tuple[Optional[int], Optional[int]]:
        normalized = (self.size or "").strip().lower()
        if "x" not in normalized:
            return None, None
        width_raw, height_raw = normalized.split("x", 1)
        try:
            return int(width_raw), int(height_raw)
        except ValueError:
            return None, None

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.enabled:
            reason = "Image output is disabled."
        elif not self.api_key:
            reason = "Hugging Face image token is missing."
        elif not self.model_name:
            reason = "Hugging Face image model is missing."
        elif self._looks_gated_model() and not self.gated_models_enabled:
            reason = "Hugging Face gated image models are disabled until user access, terms, and license checks are confirmed."

        return {
            **super().get_debug_config(),
            "size": self.size,
            "output_format": self.output_format,
            "inference_provider": self.inference_provider,
            "model_revision": self.model_revision,
            "model_license": self.model_license,
            "gated_models_enabled": self.gated_models_enabled,
            "prompt_max_chars": self.prompt_max_chars,
            "prompt_target_chars": self.prompt_target_chars,
            "reason": reason,
        }

    def _compact_image_prompt(self, image_prompt: str, *, view_id: str) -> PromptCompactionResult:
        return self.prompt_compactor.compact_if_needed(
            image_prompt,
            max_chars=self.prompt_max_chars,
            target_chars=self.prompt_target_chars,
            label=f"huggingface image prompt {view_id}",
        )

    def _client(self) -> Any:
        try:
            from huggingface_hub import InferenceClient
        except ImportError as exc:
            raise RuntimeError("Hugging Face image generation requires huggingface-hub. Install the huggingface extra.") from exc
        kwargs: Dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout_seconds}
        if self.inference_provider:
            kwargs["provider"] = self.inference_provider
        return InferenceClient(**kwargs)

    def _image_to_data_url(self, image: Any) -> str:
        if isinstance(image, bytes):
            encoded = base64.b64encode(image).decode("ascii")
            return f"data:{_mime_for_output_format(self.output_format)};base64,{encoded}"
        if isinstance(image, str):
            if image.startswith("data:"):
                return image
            return f"data:{_mime_for_output_format(self.output_format)};base64,{image}"
        if hasattr(image, "save"):
            buffer = io.BytesIO()
            pil_format = "JPEG" if self.output_format.lower() in {"jpg", "jpeg"} else self.output_format.upper()
            image.save(buffer, format=pil_format)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:{_mime_for_output_format(self.output_format)};base64,{encoded}"
        raise RuntimeError("Hugging Face image response did not include image bytes.")

    def _generate_image_from_prompt(
        self,
        image_prompt: str,
        *,
        view_id: str,
        label: str,
        reference_view_id: Optional[str],
    ) -> GeneratedImage:
        prompt_result = self._compact_image_prompt(image_prompt, view_id=view_id)
        width, height = self._size_dimensions()
        parameters: Dict[str, Any] = {}
        if width and height:
            parameters["width"] = width
            parameters["height"] = height
        if self.guidance_scale:
            parameters["guidance_scale"] = self.guidance_scale
        if self.num_inference_steps:
            parameters["num_inference_steps"] = self.num_inference_steps
        if self.negative_prompt:
            parameters["negative_prompt"] = self.negative_prompt
        if self.seed:
            parameters["seed"] = self.seed

        logger.info(
            "Requesting Hugging Face image: model=%s provider=%s view=%s size=%s timeout=%s",
            self.model_name,
            self.inference_provider or "auto",
            view_id,
            self.size,
            self.timeout_seconds,
        )
        image = self._client().text_to_image(
            prompt_result.prompt,
            model=self.model_name,
            **parameters,
        )
        logger.info(
            "Hugging Face image response received: model=%s provider=%s view=%s response_type=%s",
            self.model_name,
            self.inference_provider or "auto",
            view_id,
            type(image).__name__,
        )
        return GeneratedImage(
            data_url=self._image_to_data_url(image),
            provider=self.provider_name,
            model=self.model_name,
            size=self.size,
            prompt=prompt_result.prompt,
            output_format=self.output_format,
            view_id=view_id,
            label=label,
            reference_view_id=reference_view_id,
            prompt_original_length=prompt_result.original_length,
            prompt_final_length=prompt_result.final_length,
            prompt_compacted=prompt_result.was_compacted,
            prompt_compaction_strategy=prompt_result.strategy,
            model_revision=self.model_revision,
            inference_provider=self.inference_provider,
            model_license=self.model_license,
        )

    def generate_project_image(self, user_prompt: str, ir: Any) -> Optional[GeneratedImage]:
        if not self.is_configured:
            return None
        image_prompt = build_project_image_prompt(user_prompt, ir)
        return self._generate_image_from_prompt(
            image_prompt,
            view_id="case",
            label="Product exterior render",
            reference_view_id=None,
        )

    def generate_project_image_sequence(self, user_prompt: str, ir: Any) -> List[GeneratedImage]:
        if not self.is_configured:
            return []
        sequence: List[GeneratedImage] = []
        for item in build_project_image_sequence_prompts(user_prompt, ir):
            if item.get("generation_mode") == "deterministic_svg":
                prompt_result = self._compact_image_prompt(item["prompt"], view_id=item["view_id"])
                sequence.append(
                    build_project_layout_diagram_image(
                        user_prompt,
                        ir,
                        prompt=prompt_result.prompt,
                        reference_view_id="visual_spec",
                        prompt_compaction_result=prompt_result,
                    )
                )
                continue
            sequence.append(
                self._generate_image_from_prompt(
                    item["prompt"],
                    view_id=item["view_id"],
                    label=item["label"],
                    reference_view_id=None,
                )
            )
        return sequence


def _image_data_url_from_item(item: Dict[str, Any], output_format: str, provider_name: str) -> str:
    b64_json = item.get("b64_json") or item.get("base64") or item.get("image_base64")
    if isinstance(b64_json, str) and b64_json.strip():
        mime_type = _mime_for_output_format(output_format)
        return f"data:{mime_type};base64,{b64_json.strip()}"

    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError(f"{provider_name} image response did not include b64_json or url.")
    return url.strip()


def _image_bytes_from_data(image_data: str) -> Tuple[bytes, str]:
    image_data = (image_data or "").strip()
    if not image_data:
        raise ValueError("Reference image data is empty.")

    if image_data.startswith(("http://", "https://")):
        request = urllib.request.Request(image_data, headers={"User-Agent": "Forma-OSS/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read(), response.headers.get_content_type() or "image/png"

    content_type = "image/png"
    base64_data = image_data
    if "," in image_data:
        header, base64_data = image_data.split(",", 1)
        if header.startswith("data:") and ";base64" in header:
            content_type = header.removeprefix("data:").split(";", 1)[0] or content_type

    return base64.b64decode(base64_data.strip()), content_type


def _first_image_item(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = response.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else None

    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and any(key in item for key in {"b64_json", "base64", "image_base64", "url"}):
                return item
            if isinstance(item, dict):
                nested = item.get("content")
                if isinstance(nested, list):
                    for nested_item in nested:
                        if isinstance(nested_item, dict) and any(
                            key in nested_item for key in {"b64_json", "base64", "image_base64", "url"}
                        ):
                            return nested_item

    outcome = response.get("outcome")
    if isinstance(outcome, dict):
        media_urls = outcome.get("media_urls")
        if isinstance(media_urls, list):
            for media_item in media_urls:
                if isinstance(media_item, dict) and isinstance(media_item.get("url"), str):
                    return media_item
                if isinstance(media_item, str) and media_item.strip():
                    return {"url": media_item.strip()}
        thumbnail_url = outcome.get("thumbnail_image_url")
        if isinstance(thumbnail_url, str) and thumbnail_url.strip():
            return {"url": thumbnail_url.strip()}
    return None


def build_project_image_prompt(user_prompt: str, ir: Any) -> str:
    overview = getattr(ir, "overview", None)
    mechanical = getattr(ir, "mechanical", None)
    title = getattr(overview, "title", "Hardware concept")
    description = getattr(overview, "description", user_prompt)
    components = getattr(ir, "components", []) or []
    constraints = getattr(ir, "constraints", []) or []
    fabrication_notes = getattr(ir, "fabrication_notes", []) or []
    physical_form = getattr(mechanical, "physical_form", "") if mechanical else ""

    component_lines = _limit_list(
        [
            " ".join(
                item
                for item in [
                    getattr(component, "ref_des", ""),
                    getattr(component, "name", ""),
                    f"({getattr(component, 'category', '')})" if getattr(component, "category", "") else "",
                ]
                if item
            )
            for component in components
        ],
        limit=12,
    )

    dimensions = ""
    render_dimensions = getattr(mechanical, "render_dimensions", None)
    if render_dimensions:
        dimensions = (
            f"{getattr(render_dimensions, 'x_mm', '?')}mm wide x "
            f"{getattr(render_dimensions, 'y_mm', '?')}mm deep x "
            f"{getattr(render_dimensions, 'z_mm', '?')}mm tall"
        )

    prompt_parts = [
        "Create a clean realistic render of the existing hardware design described below.",
        "Preserve the saved design's identity, silhouette, geometry, part count, relative proportions, materials, and mechanical relationships. This is a visualization, not a redesign.",
        "Render only parts and features explicitly specified in the saved project. A purely mechanical design must remain purely mechanical.",
        "Do not add circuit boards, displays, sensors, wiring, batteries, motors, lights, ports, or decorative technology unless they are explicitly part of this design. Do not infer electronics from the word hardware.",
        "Use a closed shell only when the requirements call for one. Do not default to a rectangular project box; curved, cylindrical, radial, wearable, folded, structural, and open-frame forms are equally valid.",
        "The rendered pixels must contain no text: no dimension lines or values, labels, annotations, captions, legends, watermarks, or logos.",
        "Do not include hands, people, wiring diagrams, schematic symbols, high-voltage equipment, medical devices, or weapons.",
        "Use a neutral studio background, believable materials, and a three-quarter product view.",
        f"Project title: {_truncate(title, 120)}",
        f"Project description: {_truncate(description, 2000)}",
        f"Requested view or presentation: {_truncate(user_prompt, 4000)}",
    ]

    if physical_form and str(physical_form).strip().lower() != "unspecified":
        prompt_parts.append(f"Authoritative physical form and silhouette: {_truncate(physical_form, 800)}")
    metadata = getattr(ir, "assembly_metadata", None) or {}
    source_prompt = metadata.get("source_prompt") if isinstance(metadata, dict) else None
    if source_prompt and source_prompt != "OpenCode project":
        prompt_parts.append(f"Original project brief: {_truncate(source_prompt, 2000)}")
    if mechanical:
        for field in ("cad_operations", "mechanism_benchmark"):
            value = getattr(mechanical, field, None)
            if value:
                prompt_parts.append(f"Saved {field} (geometry reference, never depict as text): " + json.dumps(
                    value, default=lambda item: item.model_dump(mode="json"), separators=(",", ":"),
                ))
        details = getattr(mechanical, "fabrication_details", None) or []
        if details:
            prompt_parts.append("Saved mechanical details: " + "; ".join(_limit_list(details, 12, item_limit=400)))
    if component_lines:
        prompt_parts.append("Main parts: " + "; ".join(component_lines))
    if constraints:
        prompt_parts.append("Design constraints: " + "; ".join(_limit_list(constraints, 12, item_limit=400)))
    if fabrication_notes:
        prompt_parts.append("Fabrication notes: " + "; ".join(_limit_list(fabrication_notes, 5)))
    if dimensions:
        prompt_parts.append(
            f"Private proportion reference only (never print or annotate this in the image): {dimensions}."
        )

    return "\n".join(prompt_parts)


def _attr_or_key(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _number_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _vector_dict(value: Any) -> Optional[Dict[str, float]]:
    if not value:
        return None
    x_mm = _number_or_none(_attr_or_key(value, "x_mm"))
    y_mm = _number_or_none(_attr_or_key(value, "y_mm"))
    z_mm = _number_or_none(_attr_or_key(value, "z_mm"))
    if x_mm is None or y_mm is None or z_mm is None:
        return None
    return {"x_mm": x_mm, "y_mm": y_mm, "z_mm": z_mm}


def _dimension_text(dimensions: Optional[Dict[str, float]], *, label: str) -> str:
    if not dimensions:
        return f"{label}: unspecified; do not invent numeric dimension callouts."
    return (
        f"{label}: {dimensions['x_mm']} mm W x "
        f"{dimensions['y_mm']} mm D x {dimensions['z_mm']} mm H"
    )


def _internal_dimensions(external: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not external:
        return None
    return {
        "x_mm": round(max(1.0, external["x_mm"] - 4.0), 2),
        "y_mm": round(max(1.0, external["y_mm"] - 4.0), 2),
        "z_mm": round(max(1.0, external["z_mm"] - 8.0), 2),
    }


def _component_records(ir: Any, limit: int = 18) -> List[Dict[str, Any]]:
    components = getattr(ir, "components", []) or []
    records = []
    for component in components[:limit]:
        records.append(
            {
                "ref_des": _attr_or_key(component, "ref_des", ""),
                "name": _attr_or_key(component, "name", ""),
                "part_number": _attr_or_key(component, "part_number", ""),
                "category": _attr_or_key(component, "category", ""),
                "quantity": _attr_or_key(component, "quantity", 1),
            }
        )
    return records


def _component_placement_records(ir: Any, limit: int = 24) -> List[Dict[str, Any]]:
    mechanical = getattr(ir, "mechanical", None)
    placements = getattr(mechanical, "component_placements", []) or []
    records = []
    for placement in placements[:limit]:
        records.append(
            {
                "ref_des": _attr_or_key(placement, "ref_des", ""),
                "label": _attr_or_key(placement, "label"),
                "category": _attr_or_key(placement, "category"),
                "layer": _attr_or_key(placement, "layer"),
                "position_mm": _vector_dict(_attr_or_key(placement, "position")),
                "size_mm": _vector_dict(_attr_or_key(placement, "size")),
                "mounting_face": _attr_or_key(placement, "mounting_face"),
                "notes": _truncate(_attr_or_key(placement, "notes", ""), 180),
            }
        )
    return records


def _component_record_text(component: Dict[str, Any]) -> str:
    return " ".join(
        str(component.get(key, ""))
        for key in ("ref_des", "name", "part_number", "category")
        if component.get(key)
    ).lower()


def _visual_role(component: Dict[str, Any]) -> str:
    text = _component_record_text(component)
    category = str(component.get("category") or "").lower()
    if any(token in text for token in ["oled", "display", "screen", "lcd", "tft"]):
        return "display"
    if any(token in text for token in ["knob", "potentiometer", "encoder", "button", "switch", "joystick"]):
        return "user_control"
    if any(token in text for token in ["usb", "dc jack", "barrel jack", "power input", "connector"]):
        return "external_port"
    if "sensor" in category or any(token in text for token in ["temperature", "temp", "humidity", "pressure", "sensor"]):
        return "sensor"
    if any(token in text for token in ["mosfet", "driver", "relay", "motor driver"]):
        return "driver_stage"
    if any(token in text for token in ["fan", "blower"]):
        return "fan_actuator"
    if "microcontroller" in category or any(token in text for token in ["raspberry pi", "pico", "esp32", "arduino", "mcu"]):
        return "controller_board"
    if any(token in text for token in ["pcb", "perf", "proto", "breadboard"]):
        return "carrier_board"
    if any(token in text for token in ["standoff", "insert", "screw", "fastener"]):
        return "mounting_hardware"
    return category or "electrical_component"


def _mounting_zone(component: Dict[str, Any], placement: Optional[Dict[str, Any]]) -> str:
    text = _component_record_text(component)
    role = _visual_role(component)
    explicit_face = ""
    if placement:
        explicit_face = str(placement.get("mounting_face") or "").strip().lower().replace("-", "_").replace(" ", "_")

    if role in {"display", "user_control"}:
        return "top_lid_operator_panel"
    if role == "fan_actuator":
        return "right_wall_exhaust"
    if role == "external_port" or any(token in text for token in ["usb", "5v dc", "dc in", "power input"]):
        return "front_wall_edge"
    if role == "sensor":
        return "air_inlet_zone_away_from_heat_sources"
    if explicit_face in {"lid", "top", "cover", "top_panel", "operator_panel"}:
        return "top_lid_operator_panel"
    if explicit_face in {"floor", "bottom", "base", "internal"}:
        return "bottom_shell_floor"
    if explicit_face in {"front", "front_wall"}:
        return "front_wall_edge"
    if explicit_face in {"back", "rear", "back_wall", "rear_wall"}:
        return "rear_wall"
    if explicit_face in {"left", "left_wall"}:
        return "left_wall"
    if explicit_face in {"right", "right_wall"}:
        return "right_wall"
    if role in {"controller_board", "driver_stage", "carrier_board"}:
        return "bottom_shell_floor"
    return "bottom_shell_floor"


def _component_subsystem(component: Dict[str, Any], role: Optional[str] = None) -> str:
    role = role or _visual_role(component)
    text = _component_record_text(component)
    category = str(component.get("category") or "").lower()
    if role in {"display", "user_control"}:
        return "ui"
    if role == "controller_board":
        return "control"
    if role in {"driver_stage", "external_port"} or category == "power" or any(
        token in text for token in ["regulator", "charger", "fuse", "polyfuse", "mosfet", "terminal", "power"]
    ):
        return "power_driver"
    if role == "sensor":
        return "sensing"
    if role == "fan_actuator" or any(token in text for token in ["fan", "blower", "air", "vent", "filter"]):
        return "airflow"
    if role in {"carrier_board", "mounting_hardware"} or category in {"mechanical", "3d print"}:
        return "mechanical"
    return "support"


def _orientation_metadata(component: Dict[str, Any], role: str, mounting_zone: str) -> Dict[str, str]:
    if mounting_zone == "top_lid_operator_panel":
        return {
            "mounted_on": "top_lid_operator_panel",
            "facing_normal": "+Z",
            "visible_side": "front/operator side",
            "service_side": "underside visible when lid is removed",
            "view_preference": "top_down_lid_window",
            "render_rule": "keep this component attached to the lid plane; do not rotate it to face the camera in bottom-shell views",
        }
    if mounting_zone == "front_wall_edge":
        return {
            "mounted_on": "front_wall",
            "facing_normal": "-Y",
            "visible_side": "front edge",
            "service_side": "inside wall wiring side",
            "view_preference": "top_down_edge_or_front_inset",
            "render_rule": "keep this component on the front wall edge; do not lay it flat on the PCB floor",
        }
    if mounting_zone == "right_wall_exhaust":
        return {
            "mounted_on": "right_wall",
            "facing_normal": "+X",
            "visible_side": "right exterior wall",
            "service_side": "inside wall wiring side",
            "view_preference": "top_down_edge_or_side_inset",
            "render_rule": "keep fan/airflow hardware on the right wall; do not rotate the fan to face upward unless shown as an inset",
        }
    if mounting_zone == "left_wall":
        return {
            "mounted_on": "left_wall",
            "facing_normal": "-X",
            "visible_side": "left exterior wall",
            "service_side": "inside wall wiring side",
            "view_preference": "top_down_edge_or_side_inset",
            "render_rule": "keep this component on the left wall edge; do not lay it flat on the PCB floor",
        }
    if mounting_zone in {"rear_wall", "back_wall"}:
        return {
            "mounted_on": "rear_wall",
            "facing_normal": "+Y",
            "visible_side": "rear exterior wall",
            "service_side": "inside wall wiring side",
            "view_preference": "top_down_edge_or_rear_inset",
            "render_rule": "keep this component on the rear wall edge; do not lay it flat on the PCB floor",
        }
    if mounting_zone == "air_inlet_zone_away_from_heat_sources":
        return {
            "mounted_on": "intake_or_sensor_zone",
            "facing_normal": "air_path",
            "visible_side": "airflow exposure side",
            "service_side": "sensor board wiring side",
            "view_preference": "top_down_transparent_air_path",
            "render_rule": "keep sensors in the airflow/intake zone and away from hot driver components",
        }
    if role == "mounting_hardware":
        return {
            "mounted_on": "enclosure_structure",
            "facing_normal": "+Z",
            "visible_side": "fastener head or insert opening",
            "service_side": "structural interface",
            "view_preference": "top_down_structural_reference",
            "render_rule": "show as structural reference hardware, not as an electrical module",
        }
    return {
        "mounted_on": "bottom_shell_floor",
        "facing_normal": "+Z",
        "visible_side": "component top side",
        "service_side": "solder or wiring underside",
        "view_preference": "top_down_floor_window",
        "render_rule": "prefer top-down visibility; keep this component on the bottom-shell floor plane and do not tilt it toward the camera",
    }


def _net_records(ir: Any, limit: int = 18) -> List[Dict[str, Any]]:
    nets = getattr(ir, "nets", []) or []
    records = []
    for net in nets[:limit]:
        pins = []
        for pin in (_attr_or_key(net, "pins", []) or [])[:8]:
            pins.append(
                {
                    "ref_des": _attr_or_key(pin, "ref_des", ""),
                    "pin_id": _attr_or_key(pin, "pin_id", ""),
                }
            )
        records.append(
            {
                "net_id": _attr_or_key(net, "net_id", ""),
                "name": _attr_or_key(net, "name", ""),
                "net_type": _attr_or_key(net, "net_type", ""),
                "voltage": _attr_or_key(net, "voltage", None),
                "pins": pins,
            }
        )
    return records


def _assembly_component_records(
    components: List[Dict[str, Any]],
    placements: List[Dict[str, Any]],
    limit: int = 24,
) -> List[Dict[str, Any]]:
    placements_by_ref = {
        str(placement.get("ref_des") or ""): placement
        for placement in placements
        if placement.get("ref_des")
    }
    records = []
    for component in components[:limit]:
        ref_des = str(component.get("ref_des") or "")
        placement = placements_by_ref.get(ref_des)
        role = _visual_role(component)
        mounting_zone = _mounting_zone(component, placement)
        orientation = _orientation_metadata(component, role, mounting_zone)
        records.append(
            {
                "ref_des": ref_des,
                "name": component.get("name", ""),
                "visual_role": role,
                "subsystem": _component_subsystem(component, role),
                "mounting_zone": mounting_zone,
                "mounted_on": orientation["mounted_on"],
                "facing_normal": orientation["facing_normal"],
                "visible_side": orientation["visible_side"],
                "service_side": orientation["service_side"],
                "view_preference": orientation["view_preference"],
                "render_rule": orientation["render_rule"],
                "position_mm": (placement or {}).get("position_mm"),
                "size_mm": (placement or {}).get("size_mm"),
                "mounting_face_source": (placement or {}).get("mounting_face"),
            }
        )
    return records


def _orientation_landmarks(assembly_components: List[Dict[str, Any]]) -> List[str]:
    roles = {str(component.get("visual_role") or "") for component in assembly_components}
    zones = {str(component.get("mounting_zone") or "") for component in assembly_components}
    landmarks = [
        "Coordinate frame is fixed for every view: X is left/right width, Y is front/back depth, Z is bottom/top height.",
        "In top-down diagrams, the front edge is the lower edge of the page and the rear edge is the upper edge.",
        "Do not mirror, rotate, or swap left/right/front/back between images.",
        "Every applicable shell, frame, panel, mount, port, and electronic module is a separate part with its own visibility state.",
    ]
    if "front_wall_edge" in zones:
        landmarks.append("Front wall landmark: external power/USB/input ports stay on the same front edge in all views.")
    if "right_wall_exhaust" in zones or "fan_actuator" in roles:
        landmarks.append("Right wall landmark: fan/exhaust stays on the same right-side wall and airflow points outward.")
    if "top_lid_operator_panel" in zones:
        landmarks.append("Operator-surface landmark: display, knob, buttons, and switches stay attached to their specified panel or mounting surface.")
    if "sensor" in roles:
        landmarks.append("Sensor landmark: air/ambient sensors stay near intake/venting and away from fan exhaust or hot driver stages.")
    return landmarks


def _control_loop_visual_requirements(user_prompt: str, ir: Any, nets: List[Dict[str, Any]]) -> List[str]:
    overview = getattr(ir, "overview", None)
    requirements = getattr(ir, "requirements", None)
    haystack_parts = [
        user_prompt,
        getattr(overview, "title", ""),
        getattr(overview, "description", ""),
        " ".join(getattr(requirements, "requirements", []) or []),
        " ".join(getattr(ir, "constraints", []) or []),
        " ".join(net.get("name", "") for net in nets),
        " ".join(net.get("net_type", "") for net in nets),
    ]
    haystack = " ".join(haystack_parts).lower()
    if not any(token in haystack for token in ["closed-loop", "closed loop", "feedback", "pid", "pwm", "setpoint"]):
        return []
    return [
        "Closed-loop visuals must separate the measured feedback path from the control output path.",
        "Show the measured variable sensor or feedback signal returning to the controller when present in the IR.",
        "Show the controller output path to the actuator or driver stage, such as PWM to a fan or motor driver, when present in the IR.",
        "Do not imply closed-loop speed control unless a feedback sensor, tach signal, or measured output path from the spec is physically visible.",
    ]


def _net_names_matching(nets: List[Dict[str, Any]], tokens: List[str], limit: int = 6) -> List[str]:
    matches: List[str] = []
    for net in nets:
        text = f"{net.get('net_id', '')} {net.get('name', '')} {net.get('net_type', '')}".lower()
        if any(token in text for token in tokens):
            label = " ".join(str(net.get(key) or "") for key in ("net_id", "name", "net_type")).strip()
            if label:
                matches.append(_truncate(label, 90))
        if len(matches) >= limit:
            break
    return matches


def _contract_for_subsystem(
    subsystem: str,
    components: List[Dict[str, Any]],
    nets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    component_refs = [str(component.get("ref_des") or "") for component in components if component.get("ref_des")]
    mounted_on = sorted({str(component.get("mounted_on") or "") for component in components if component.get("mounted_on")})
    facing_normals = sorted({str(component.get("facing_normal") or "") for component in components if component.get("facing_normal")})
    role_text = " ".join(str(component.get("visual_role") or "") for component in components).lower()

    contracts: Dict[str, Dict[str, Any]] = {
        "ui": {
            "purpose": "Expose human-readable status and user setpoint/control inputs without changing internal component placement.",
            "inputs": ["user action", "status data from controller"],
            "outputs": ["setpoint/control input to controller", "displayed status to user"],
            "physical_interfaces": ["operator-panel or surface cutouts", "serviceable wiring loop", "display/control mounting features"],
            "placement_constraints": [
                "UI components stay on their specified operator panel or mounting surface.",
                "Display apertures and knob/button shafts align to applicable cutouts.",
                "Leave a service loop when a panel must move or be removed without stressing wiring.",
            ],
            "failure_modes": ["misaligned panel cutouts", "service wiring blocks assembly", "controls duplicated on a carrier"],
            "verification_checks": [
                "UI appears on its specified physical surface, not flattened into an unrelated carrier plane.",
                "Controls keep facing_normal +Z unless an underside/service inset is explicitly shown.",
            ],
        },
        "control": {
            "purpose": "Run firmware/control logic and coordinate sensor readings, user inputs, display state, and actuator commands.",
            "inputs": ["sensor measurements", "user setpoint/control input", "feedback nets when present"],
            "outputs": ["display/status data", "driver enable/PWM/control nets"],
            "physical_interfaces": ["board standoffs or carrier", "programming/power access", "signal harness to UI, sensors, and drivers"],
            "placement_constraints": [
                "Controller stays on its specified carrier, frame, or mounting plane.",
                "Route signal wiring without crossing high-current driver paths unnecessarily.",
                "Keep access to USB/programming connector if the component has one.",
            ],
            "failure_modes": ["controller hidden by unrelated structure", "signal paths omitted", "board rotated to face camera instead of preserving its specified normal"],
            "verification_checks": [
                "Controller is visible in the assembly reference or control subsystem window.",
                "Control outputs and feedback inputs are represented when supported by nets.",
            ],
        },
        "power_driver": {
            "purpose": "Accept low-voltage input, protect/regulate power, and drive actuators from controller outputs.",
            "inputs": ["external low-voltage power", "controller command/PWM/enable nets"],
            "outputs": ["regulated rails", "switched actuator power", "driver status/feedback when present"],
            "physical_interfaces": ["edge power connector", "driver mounting area", "actuator terminal/wiring path"],
            "placement_constraints": [
                "Power ports stay on their wall/edge plane.",
                "Driver components stay clear of heat-sensitive sensors.",
                "High-current wiring should be short and mechanically restrained.",
            ],
            "failure_modes": ["power connector drawn on wrong side", "driver merged with fan", "hot driver placed near ambient sensor"],
            "verification_checks": [
                "Power/driver path is distinct from sensing and UI subsystems.",
                "Port facing normals and edge placement are preserved.",
            ],
        },
        "sensing": {
            "purpose": "Measure the controlled or observed variable without being corrupted by heat, airflow shortcuts, or enclosure geometry.",
            "inputs": ["ambient/process condition", "power and signal bus from controller"],
            "outputs": ["sensor measurement/feedback signal to controller"],
            "physical_interfaces": ["intake/vent exposure", "sensor board mount", "signal harness to controller"],
            "placement_constraints": [
                "Sensors stay exposed to the intended medium or airflow path.",
                "Keep ambient sensors upstream of fan exhaust and away from heat-generating drivers.",
                "Do not bury sensors under an opaque lid or PCB.",
            ],
            "failure_modes": ["sensor placed near exhaust/hot driver", "air path not visible", "feedback sensor missing in closed-loop design"],
            "verification_checks": [
                "Sensor has a visible exposure/air path when relevant.",
                "Feedback path to controller is represented when closed-loop behavior is claimed.",
            ],
        },
        "airflow": {
            "purpose": "Move air through the enclosure or process path in the intended direction.",
            "inputs": ["driver power/control", "intake air path"],
            "outputs": ["exhaust airflow", "tach/feedback signal when present"],
            "physical_interfaces": ["fan wall cutout", "intake/exhaust vents", "filter/duct/grille interfaces"],
            "placement_constraints": [
                "Fan remains on the specified wall plane and points outward for exhaust unless the spec says otherwise.",
                "Intake and exhaust paths must not be blocked by PCB, wiring, or lid features.",
                "Fan orientation should preserve facing_normal instead of being rotated upward for the camera.",
            ],
            "failure_modes": ["fan shown flat on PCB", "airflow direction ambiguous", "vents missing or blocked"],
            "verification_checks": [
                "Fan/air path appears in airflow or wall subsystem window.",
                "Airflow direction and relevant feedback/tach path are visible when supported.",
            ],
        },
        "mechanical": {
            "purpose": "Hold the product structure, mounts, panels, hardware, and service access relationships together.",
            "inputs": ["component envelopes", "mounting/clearance needs", "assembly/service requirements"],
            "outputs": ["stable physical form and mounting structure"],
            "physical_interfaces": ["shell or frame when present", "panels", "mounting surfaces", "standoffs", "fasteners", "cutouts"],
            "placement_constraints": [
                "Do not fuse panels, shells or frames, PCBs, and internals into one surface.",
                "Keep assembled, inspection, and service states distinct for the specified physical form.",
                "Show mounting hardware as structure rather than as electrical parts.",
            ],
            "failure_modes": ["product structure blended with electronics", "controls duplicated on another surface", "mounting hardware mistaken for circuit modules"],
            "verification_checks": [
                "Every applicable panel, shell or frame member, and PCB is visually distinct.",
                "Inspection views preserve product orientation and component mounting planes.",
            ],
        },
        "support": {
            "purpose": "Provide secondary electrical or mechanical support functions not captured by primary subsystems.",
            "inputs": ["power/signal connections as listed in the IR"],
            "outputs": ["support function to primary subsystems"],
            "physical_interfaces": ["local PCB/wire harness/mounting feature"],
            "placement_constraints": ["Keep support parts near the subsystem they support when possible."],
            "failure_modes": ["unexplained floating component", "support part duplicated across windows"],
            "verification_checks": ["Support components appear once and remain tied to a nearby parent subsystem."],
        },
    }

    contract = dict(contracts.get(subsystem, contracts["support"]))
    contract["component_refs"] = component_refs
    contract["mounted_on"] = mounted_on
    contract["facing_normals"] = facing_normals
    if subsystem in {"control", "power_driver", "airflow"} or "pwm" in role_text:
        contract["relevant_nets"] = _net_names_matching(nets, ["pwm", "enable", "control", "fan", "driver", "tach"])
    elif subsystem == "sensing":
        contract["relevant_nets"] = _net_names_matching(nets, ["sensor", "feedback", "tach", "i2c", "spi", "analog", "adc"])
    elif subsystem == "ui":
        contract["relevant_nets"] = _net_names_matching(nets, ["display", "button", "encoder", "set", "i2c", "spi", "analog"])
    else:
        contract["relevant_nets"] = _net_names_matching(nets, ["power", "ground", "gnd", "vcc", "5v", "3v3"])
    contract["assembly_states"] = [
        "assembled exterior",
        "top-down inspection with only occluding structure ghosted",
        "service access with the relevant removable part separated",
    ]
    return contract


def _subsystem_records(assembly_components: List[Dict[str, Any]], nets: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    nets = nets or []
    subsystem_labels = {
        "ui": "UI and human interface",
        "control": "Controller and logic",
        "power_driver": "Power input and driver stage",
        "sensing": "Sensors and feedback",
        "airflow": "Airflow and actuator path",
        "mechanical": "Mechanical structure",
        "support": "Support electronics",
    }
    order = ["ui", "control", "power_driver", "sensing", "airflow", "mechanical", "support"]
    grouped: Dict[str, List[Dict[str, Any]]] = {key: [] for key in order}
    for component in assembly_components:
        subsystem = str(component.get("subsystem") or "support")
        grouped.setdefault(subsystem, []).append(component)

    records: List[Dict[str, Any]] = []
    for subsystem in [*order, *sorted(key for key in grouped if key not in order)]:
        components = grouped.get(subsystem) or []
        if not components:
            continue
        mounting_zones = sorted({str(component.get("mounting_zone") or "") for component in components if component.get("mounting_zone")})
        facing_normals = sorted({str(component.get("facing_normal") or "") for component in components if component.get("facing_normal")})
        records.append(
            {
                "id": subsystem,
                "label": subsystem_labels.get(subsystem, subsystem.replace("_", " ").title()),
                "component_refs": [component.get("ref_des") for component in components if component.get("ref_des")],
                "mounting_zones": mounting_zones,
                "facing_normals": facing_normals,
                "contract": _contract_for_subsystem(subsystem, components, nets),
                "view_preference": "top_down_transparent_subsystem_window",
                "layout_rule": "draw this subsystem in its own physical layout window; preserve each component's mounted_on and facing_normal metadata",
            }
        )
    return records


def _refs_for_subsystem(assembly_components: List[Dict[str, Any]], subsystem: str) -> List[str]:
    return [
        str(component.get("ref_des") or "")
        for component in assembly_components
        if component.get("subsystem") == subsystem and component.get("ref_des")
    ]


def _physical_dependency_graph(assembly_components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    subsystem_edges = [
        ("ui", "control", "user setpoint/status interface", "UI harness must bridge its operator surface to the controller without forcing components onto the wrong plane."),
        ("sensing", "control", "measurement/feedback interface", "Sensor placement must expose the measured variable and route signal wiring back to controller."),
        ("control", "power_driver", "command/PWM/enable interface", "Driver command path must remain visually distinct from high-current wiring."),
        ("power_driver", "airflow", "actuator power interface", "Driver output should route to fan/actuator without blocking airflow or service access."),
        ("airflow", "sensing", "process/air path relationship", "Airflow path should not corrupt sensor readings unless that is the intended feedback measurement."),
        ("mechanical", "ui", "panel/cutout support", "Applicable panel cutouts and UI components must align."),
        ("mechanical", "control", "standoff/support", "PCB/controller must be mounted to its specified shell, frame, or carrier structure."),
        ("mechanical", "airflow", "cutout/support", "Fan, vents, and duct features must align to their specified openings."),
    ]
    present = {str(component.get("subsystem") or "") for component in assembly_components}
    for source, target, interface, check in subsystem_edges:
        if source in present and target in present:
            edges.append(
                {
                    "source_subsystem": source,
                    "target_subsystem": target,
                    "interface": interface,
                    "design_check": check,
                    "source_refs": _refs_for_subsystem(assembly_components, source),
                    "target_refs": _refs_for_subsystem(assembly_components, target),
                }
            )

    for component in assembly_components:
        ref_des = component.get("ref_des")
        if not ref_des:
            continue
        edges.append(
            {
                "source_subsystem": "mechanical",
                "target_ref_des": ref_des,
                "interface": f"mounts {ref_des} on {component.get('mounted_on') or component.get('mounting_zone')}",
                "design_check": component.get("render_rule") or "preserve component mounting plane and orientation",
            }
        )
    return edges[:28]


def _controlled_variables_from_text(text: str) -> List[str]:
    candidates = [
        ("temperature", ["temp", "thermal", "heater", "cooling"]),
        ("humidity", ["humidity", "moisture", "dry"]),
        ("airflow", ["airflow", "air flow", "fan", "blower", "wind"]),
        ("fan speed", ["rpm", "tach", "fan speed"]),
        ("pressure", ["pressure", "vacuum"]),
        ("light level", ["light", "lux"]),
        ("voltage/current", ["current", "battery", "discharge", "pack voltage"]),
        ("position", ["servo", "position", "linear actuator"]),
    ]
    found = [label for label, tokens in candidates if any(token in text for token in tokens)]
    return found or ["project state"]


def _behavior_control_model(
    user_prompt: str,
    assembly_components: List[Dict[str, Any]],
    nets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    overview_text = " ".join(
        [
            user_prompt,
            " ".join(str(component.get("name") or "") for component in assembly_components),
            " ".join(str(net.get("name") or "") for net in nets),
            " ".join(str(net.get("net_type") or "") for net in nets),
        ]
    ).lower()
    sensors = [component for component in assembly_components if component.get("subsystem") == "sensing"]
    ui = [component for component in assembly_components if component.get("subsystem") == "ui"]
    controllers = [component for component in assembly_components if component.get("subsystem") == "control"]
    drivers = [component for component in assembly_components if component.get("subsystem") == "power_driver"]
    actuators = [component for component in assembly_components if component.get("subsystem") == "airflow"]
    feedback_nets = _net_names_matching(nets, ["feedback", "tach", "sensor", "adc", "analog", "i2c", "spi"], limit=8)
    output_nets = _net_names_matching(nets, ["pwm", "enable", "control", "driver", "fan", "motor", "relay"], limit=8)
    is_closed_loop = any(token in overview_text for token in ["closed-loop", "closed loop", "feedback", "pid", "tach", "setpoint"])
    return {
        "control_model_type": "closed_loop" if is_closed_loop else "open_loop_or_monitoring",
        "controlled_variables": _controlled_variables_from_text(overview_text),
        "setpoint_sources": [component.get("ref_des") for component in ui if component.get("ref_des")],
        "measurement_sources": [component.get("ref_des") for component in sensors if component.get("ref_des")],
        "controller_refs": [component.get("ref_des") for component in controllers if component.get("ref_des")],
        "driver_refs": [component.get("ref_des") for component in drivers if component.get("ref_des")],
        "actuator_refs": [component.get("ref_des") for component in actuators if component.get("ref_des")],
        "feedback_nets": feedback_nets,
        "control_output_nets": output_nets,
        "visual_requirements": [
            "Show the measured variable source, controller, driver/output path, and actuator as separate roles when present.",
            "If closed-loop behavior is claimed, show a feedback path returning to the controller or mark feedback as missing.",
            "Keep behavior arrows separate from physical mounting planes so the image does not relocate components to simplify the loop.",
        ],
        "missing_feedback_warning": bool(is_closed_loop and not feedback_nets and not sensors),
    }


def _assembly_state_records() -> List[Dict[str, Any]]:
    return [
        {
            "id": "assembled_exterior",
            "purpose": "Product-like exterior validation.",
            "visible": ["specified shell or frame", "external controls", "ports", "vents", "fasteners", "intentionally exposed modules"],
            "hidden": ["only electronics hidden by the specified assembled structure"],
            "verification_checks": [
                "proportions match dimensions from spec without visible dimension annotations",
                "open products remain open and enclosed products do not reveal parts through opaque surfaces",
            ],
        },
        {
            "id": "transparent_top_down_inspection",
            "purpose": "Check physical placement without rotating components toward the camera.",
            "visible": ["ghosted occluding structure when present", "true component mounting planes", "subsystem grouping cues"],
            "hidden": ["opaque structure covering inspected parts", "camera-facing fake rotations"],
            "verification_checks": ["front/right orientation preserved", "mounted_on and facing_normal respected"],
        },
        {
            "id": "service_access",
            "purpose": "Confirm assembly and maintenance access.",
            "visible": ["removable part underside", "component carrier", "service wiring side", "fasteners/standoffs"],
            "hidden": ["fused structure/panel/PCB geometry"],
            "verification_checks": ["service wiring loop when needed", "ports/fans accessible from their specified surface"],
        },
    ]


def _design_assembly_model(
    user_prompt: str,
    components: List[Dict[str, Any]],
    placements: List[Dict[str, Any]],
    nets: List[Dict[str, Any]],
    external_dimensions: Optional[Dict[str, float]],
    internal_dimensions: Optional[Dict[str, float]],
    physical_form: str,
    enclosure_type: str,
) -> Dict[str, Any]:
    assembly_components = _assembly_component_records(components, placements)
    subsystem_decomposition = _subsystem_records(assembly_components, nets)
    dependency_graph = _physical_dependency_graph(assembly_components)
    behavior_control = _behavior_control_model(user_prompt, assembly_components, nets)
    return {
        "modeling_method": "single canonical assembly with design intent, subsystem contracts, and derived view states",
        "physical_form": physical_form,
        "enclosure_type": enclosure_type,
        "coordinate_frame": {
            "origin": "center of the overall product envelope",
            "x_axis": "width, left negative to right positive",
            "y_axis": "depth, front negative to rear positive",
            "z_axis": "height, bottom negative to top positive",
            "diagram_orientation": "top-down +Z view with the front edge at the bottom of the page",
        },
        "dimensions_mm": {
            "external": external_dimensions,
            "internal_usable": internal_dimensions,
        },
        "assembly_parts": [
            "specified shell, frame, folded structure, or body",
            "removable panel only when present",
            "PCB or component carrier",
            "surface-mounted controls and display",
            "ports, vents, and fan openings on their specified surfaces",
            "mounting hardware and wire harnesses",
        ],
        "component_mounting_zones": assembly_components,
        "subsystem_decomposition": subsystem_decomposition,
        "physical_dependency_graph": dependency_graph,
        "behavior_control_model": behavior_control,
        "assembly_states": _assembly_state_records(),
        "orientation_landmarks": _orientation_landmarks(assembly_components),
        "derived_view_states": {
            "case": {
                "camera": "three-quarter exterior product render",
                "visible": ["specified product body or frame", "external controls", "ports", "vents", "fasteners", "intentionally exposed modules"],
                "hidden": ["only electronics occluded by the specified assembled structure"],
                "rule": "show the assembled specified form; do not add a closed shell to an open-frame product.",
            },
            "inside": {
                "camera": "top-down or near top-down transparent assembly render",
                "visible": [
                    "transparent or ghosted occluding structure when present",
                    "true component mounting planes",
                    "electronics preserving their specified facing normals",
                    "panel-mounted UI on its panel plane",
                    "ports, fan, vents, and sensors on their specified surface planes",
                    "subsystem grouping cues",
                ],
                "hidden": ["opaque structure covering the inspected parts", "camera-facing rotated electronics"],
                "rule": "prefer top-down transparent views; keep every component on its mounted_on plane and facing_normal.",
            },
        },
        "continuity_checks": [
            "Every view is the same assembly, not a redesigned device.",
            "Only camera angle and part visibility may change between stages.",
            "Keep all ports, fan openings, vents, controls, display, mounting holes, and PCB positions on their assigned surfaces.",
            "Do not duplicate components across panels, shells, frames, or carriers unless the spec lists multiple instances.",
            "Do not draw internals underneath an opaque surface.",
            "Do not rotate components to face the camera; preserve facing_normal unless an inset explicitly shows the service side.",
        ],
    }


def _cad_source_records(ir: Any, limit: int = 8) -> List[Dict[str, Any]]:
    mechanical = getattr(ir, "mechanical", None)
    cad_sources = getattr(mechanical, "cad_sources", []) or []
    records = []
    for source in cad_sources[:limit]:
        records.append(
            {
                "name": _attr_or_key(source, "name", ""),
                "source_type": _attr_or_key(source, "source_type", ""),
                "url": _attr_or_key(source, "url", ""),
                "file_formats": _attr_or_key(source, "file_formats", []) or [],
                "license": _attr_or_key(source, "license"),
                "notes": _truncate(_attr_or_key(source, "notes", ""), 180),
            }
        )
    return records


def build_project_visual_spec(user_prompt: str, ir: Any) -> Dict[str, Any]:
    overview = getattr(ir, "overview", None)
    mechanical = getattr(ir, "mechanical", None)
    metadata = getattr(ir, "assembly_metadata", None) or {}
    physical_form = getattr(mechanical, "physical_form", "") if mechanical else ""
    external_dimensions = _vector_dict(getattr(mechanical, "render_dimensions", None))
    if not external_dimensions and isinstance(metadata, dict):
        external_dimensions = _vector_dict(metadata.get("render_dimensions"))
    internal_dimensions = _internal_dimensions(external_dimensions)
    components = _component_records(ir)
    placements = _component_placement_records(ir)
    nets = _net_records(ir)
    enclosure_type = getattr(mechanical, "enclosure_type", "not specified; a closed enclosure is not required")
    normalized_physical_form = physical_form or "Unspecified; infer from explicit project requirements without defaulting to a box"
    assembly_model = _design_assembly_model(
        user_prompt,
        components,
        placements,
        nets,
        external_dimensions,
        internal_dimensions,
        normalized_physical_form,
        enclosure_type,
    )
    allowed_labels = sorted(
        {
            label
            for component in components
            for label in (component.get("ref_des"), component.get("name"), component.get("part_number"))
            if label
        }.union(
            {
                label
                for net in nets
                for label in (net.get("net_id"), net.get("name"), net.get("net_type"))
                if label
            }
        )
    )
    return {
        "spec_version": "0.4",
        "title": getattr(overview, "title", "Hardware concept"),
        "description": getattr(overview, "description", user_prompt),
        "source_prompt": user_prompt,
        "physical_form": normalized_physical_form,
        "external_dimensions_mm": external_dimensions,
        "internal_usable_dimensions_mm": internal_dimensions,
        "external_dimensions_text": _dimension_text(external_dimensions, label="External dimensions"),
        "internal_dimensions_text": _dimension_text(internal_dimensions, label="Internal usable dimensions"),
        "enclosure_type": enclosure_type,
        "mounting_guidance": getattr(mechanical, "mounting_guidance", "internal standoffs and panel-mounted controls"),
        "components": components,
        "component_placements": placements,
        "connection_nets": nets,
        "design_assembly_model": assembly_model,
        "subsystems": assembly_model["subsystem_decomposition"],
        "physical_dependency_graph": assembly_model["physical_dependency_graph"],
        "behavior_control_model": assembly_model["behavior_control_model"],
        "assembly_states": assembly_model["assembly_states"],
        "control_loop_visual_requirements": _control_loop_visual_requirements(user_prompt, ir, nets),
        "cad_sources": _cad_source_records(ir),
        "fabrication_notes": _limit_list(getattr(ir, "fabrication_notes", []) or [], 8),
        "constraints": _limit_list(getattr(ir, "constraints", []) or [], 10),
        "allowed_visual_labels": allowed_labels,
        "truth_rules": [
            "Treat physical_form and explicit shape language in source_prompt as authoritative for the product silhouette.",
            "Do not default to a rectangular project box or invent a closed shell when the design is curved, cylindrical, radial, wearable, folded, structural, or open-frame.",
            "Use dimensions only to establish physical proportions; never render dimension lines, arrows, or values.",
            "Use only listed component refs, names, part numbers, CAD sources, and placements.",
            "Use the design_assembly_model as the source of truth for orientation, part visibility, and component mounting zones.",
            "Preserve every component's subsystem, mounted_on plane, and facing_normal metadata in generated physical layouts.",
            "Preserve subsystem contracts: purpose, inputs, outputs, physical interfaces, placement constraints, dependencies, and verification checks.",
            "Do not invent vendor part numbers, enclosure models, tolerances, wall thickness, mounting hardware, or dimensions.",
            "Render no text, labels, annotations, captions, legends, watermarks, or logos in the image.",
        ],
    }


def _spec_prompt_text(spec: Dict[str, Any]) -> str:
    assembly_model = spec["design_assembly_model"]
    compact_spec = {
        "title": spec["title"],
        "description": spec["description"],
        "source_prompt": spec["source_prompt"],
        "physical_form": spec["physical_form"],
        "external_dimensions_mm": spec["external_dimensions_mm"],
        "internal_usable_dimensions_mm": spec["internal_usable_dimensions_mm"],
        "enclosure_type": spec["enclosure_type"],
        "mounting_guidance": spec["mounting_guidance"],
        "components": spec["components"],
        "component_placements": spec["component_placements"],
        "connection_nets": spec["connection_nets"],
        "subsystems": spec["subsystems"],
        "physical_dependency_graph": spec["physical_dependency_graph"],
        "behavior_control_model": spec["behavior_control_model"],
        "assembly_states": spec["assembly_states"],
        "design_assembly_model": {
            "modeling_method": assembly_model["modeling_method"],
            "physical_form": assembly_model["physical_form"],
            "enclosure_type": assembly_model["enclosure_type"],
            "coordinate_frame": assembly_model["coordinate_frame"],
            "component_mounting_zones": assembly_model["component_mounting_zones"],
            "subsystem_decomposition": assembly_model["subsystem_decomposition"],
            "physical_dependency_graph": assembly_model["physical_dependency_graph"],
            "behavior_control_model": assembly_model["behavior_control_model"],
            "assembly_states": assembly_model["assembly_states"],
            "orientation_landmarks": assembly_model["orientation_landmarks"],
            "derived_view_states": assembly_model["derived_view_states"],
            "continuity_checks": assembly_model["continuity_checks"],
        },
        "control_loop_visual_requirements": spec["control_loop_visual_requirements"],
        "cad_sources": spec["cad_sources"],
        "fabrication_notes": spec["fabrication_notes"],
        "constraints": spec["constraints"],
        "truth_rules": spec["truth_rules"],
    }
    return json.dumps(compact_spec, separators=(",", ":"))


def build_project_image_sequence_prompts(user_prompt: str, ir: Any) -> List[Dict[str, Any]]:
    if not getattr(ir, "components", None) and not getattr(ir, "nets", None):
        # CAD-only projects have no electrical assembly to reveal. Do not seed
        # their views with the electronics layout defaults in the visual spec.
        base = build_project_image_prompt(user_prompt, ir)
        return [
            {"view_id": "case", "label": "Product exterior", "prompt": base + "\nRender the assembled exterior. Do not draw dimension lines or measurement arrows."},
            {"view_id": "inside", "label": "Assembly inspection", "prompt": base + "\nShow the exact same assembly in a top-down inspection view, without inventing a shell or hidden internals. Only the camera may change; retain all saved parts and relationships. Do not draw dimension lines or measurement arrows."},
        ]
    spec = build_project_visual_spec(user_prompt, ir)
    spec_text = _spec_prompt_text(spec)
    shared = [
        "Canonical Visual Design Spec, generated from the Hardware IR:",
        spec_text,
        f"Private geometry guidance for proportions only: {spec['external_dimensions_text']}; {spec['internal_dimensions_text']}. Never depict these measurements.",
        "Traditional design rule: this is one assembly model with derived view states, not three independent product concepts.",
        "Keep the same object identity, proportions, port positions, display/control layout, material, and scale across all images.",
        "Only the camera angle and part visibility state may change between stages.",
        "The visual sequence must not drift from the Canonical Visual Design Spec.",
        "Treat physical_form and explicit shape language in source_prompt as authoritative. Preserve that silhouette across every view.",
        "Do not default to a rectangular project box. Curved, cylindrical, radial, wearable, folded, structural, and open-frame products are first-class forms.",
        "Respect the fixed coordinate frame and orientation landmarks from design_assembly_model.",
        "Keep every applicable shell, frame, panel, PCB, control, display, fan, port, vent, standoff, and wire as a distinct physical part; do not invent absent enclosure pieces.",
        "Use subsystem_decomposition to organize physical layouts into UI, control, power/driver, sensing, airflow, mechanical, and support windows when applicable.",
        "Use subsystem contracts to preserve purpose, inputs, outputs, physical interfaces, placement constraints, failure modes, and verification checks.",
        "Use physical_dependency_graph and behavior_control_model to keep layout, wiring, airflow, and feedback relationships coherent.",
        "Preserve each component's mounted_on plane and facing_normal; do not rotate electronics to face the camera for aesthetics.",
        "Prefer top-down transparent or ghosted inspection views for enclosed products and unobstructed top-down views for open products rather than dramatic perspective views.",
        "When a lid or top surface exists, do not fuse it with internal electronics or place visible internals under an opaque closed surface.",
        "The rendered pixels must contain no text: no dimension lines or values, measurement arrows, labels, annotations, captions, legends, component names, part numbers, watermarks, or logos.",
        "Render only the saved parts and features. Do not add electronics, controls, displays, or wiring absent from the spec. No hands, people, watermarks, brand logos, weapons, medical equipment, or mains-voltage hazards.",
    ]

    shared_text = "\n".join(shared)
    return [
        {
            "view_id": "case",
            "label": "Product exterior",
            "prompt": "\n".join(
                [
                    "Stage 1 of 2: create the assembled product exterior render.",
                    shared_text,
                    "Use the case view state from design_assembly_model.",
                    "Render the specified physical form as an assembled product. It may be enclosed, partially enclosed, structural, wearable, folded, or open-frame; use a closed case only when the spec requires one.",
                    "Show only applicable exterior details such as screens, buttons, knobs, ports, seams, fillets, frame members, mounting features, vents, and panel cutouts.",
                    "If a lid or operator panel exists, show it installed and opaque except for explicitly visible windows, cutouts, ports, vents, or controls.",
                    "Use consistent orientation landmarks: front remains front, right remains right, and controls stay on their specified mounting surface.",
                    "Use external_dimensions_mm only to guide proportions. Do not draw dimension lines, measurement arrows, numeric values, or any other text.",
                    "Use a clean neutral studio background and a three-quarter view. For an enclosed product, do not show hidden electronics; for an open product, show only electronics that are naturally exposed.",
                ]
            ),
        },
        {
            "view_id": "inside",
            "label": "Transparent top-down assembly",
            "prompt": "\n".join(
                [
                    "Stage 2 of 2: image-to-image from the exterior render into a top-down assembly inspection view.",
                    shared_text,
                    "Use the inside view state from design_assembly_model.",
                    "Use the previous exterior product as the exact same physical assembly, now shown from a top-down or near top-down orthographic inspection angle.",
                    "If a shell exists, make it transparent or ghosted at roughly 15-25% opacity so its mounting planes are visible. If the product is open-frame, preserve the exposed frame without inventing a shell.",
                    "Do not roll, invert, mirror, or flip the product. Keep the same front, right, left, and rear orientation from the exterior render.",
                    "Keep the electronics on their specified mounting planes with a shared perspective; curved, radial, wall-mounted, and frame-mounted layouts do not need a flat tub floor.",
                    "The visible structure, mounting features, standoffs, and electronics must all agree on one coordinate frame.",
                    "Use each component's mounted_on, facing_normal, visible_side, view_preference, and render_rule metadata. Preserve every board, control, and surface-mounted component on its specified plane.",
                    "Do not rotate surface-mounted fans, ports, or displays to face the camera; use transparent structure, ghosting, or small true-plane insets instead.",
                    "Show subtle subsystem grouping through placement and color cues only, with no written labels.",
                    "Respect subsystem placement constraints: sensors stay exposed to the intended medium, hot drivers stay away from sensors, ports align with wall cutouts, airflow paths remain unblocked, and service loops remain plausible.",
                    "For behavior/control systems, preserve the measured variable source, controller, driver/output path, actuator, and feedback path from behavior_control_model.",
                    "Any opaque lid or top surface must not cover or blend into internal electronics that the inspection view is meant to reveal.",
                    "Keep display, knob, button, and switch parts attached to their specified operator surface or show their underside/cables clearly; do not relocate them to another carrier unless their mounting_zone says so.",
                    "Keep boards, drivers, power modules, terminals, standoffs, and wiring on their specified mounting surfaces or frame members.",
                    "Show only the electronics and placements listed in the spec: mounted boards, display module, buttons, encoder/knob, power board, connectors, wiring harnesses, standoffs, screws, cable routing, and clearance.",
                    "Use external_dimensions_mm and internal_usable_dimensions_mm only to guide proportions. Do not draw dimension lines, measurement arrows, numeric values, or any other text. Preserve exterior port, vent, fan, and control positions from the exterior render.",
                    "Use a realistic three-quarter product view with the internal electronics clearly visible.",
                ]
            ),
        },
    ]


def _svg_escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _svg_data_url(svg: str) -> str:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _fmt_mm(value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return "?"
    if float(number).is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _visual_label(component: Dict[str, Any], limit: int = 32) -> str:
    ref_des = str(component.get("ref_des") or "").strip()
    name = str(component.get("name") or "").strip()
    if ref_des and name:
        return _truncate(f"{ref_des} {name}", limit)
    return _truncate(ref_des or name or "Component", limit)


def _role_style(role: str) -> Dict[str, str]:
    styles = {
        "controller_board": {"fill": "#2f9e44", "stroke": "#14532d"},
        "carrier_board": {"fill": "#6aa84f", "stroke": "#31572c"},
        "display": {"fill": "#1e3a8a", "stroke": "#0f172a"},
        "user_control": {"fill": "#525252", "stroke": "#171717"},
        "external_port": {"fill": "#94a3b8", "stroke": "#334155"},
        "fan_actuator": {"fill": "#111827", "stroke": "#475569"},
        "sensor": {"fill": "#38bdf8", "stroke": "#0369a1"},
        "driver_stage": {"fill": "#f97316", "stroke": "#9a3412"},
        "mounting_hardware": {"fill": "#fbbf24", "stroke": "#92400e"},
        "power": {"fill": "#a78bfa", "stroke": "#5b21b6"},
    }
    return styles.get(role, {"fill": "#cbd5e1", "stroke": "#475569"})


def _component_position(
    component: Dict[str, Any],
    index: int,
    total: int,
    *,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    scale: float,
) -> Tuple[float, float]:
    zone = str(component.get("mounting_zone") or "")
    position = component.get("position_mm")
    margin = 24.0
    if isinstance(position, dict):
        x_mm = _number_or_none(position.get("x_mm"))
        y_mm = _number_or_none(position.get("y_mm"))
        if x_mm is not None and y_mm is not None:
            x = box_x + box_w / 2 + x_mm * scale
            y = box_y + box_h / 2 - y_mm * scale
            return (
                min(max(x, box_x + margin), box_x + box_w - margin),
                min(max(y, box_y + margin), box_y + box_h - margin),
            )

    slot = index / max(1, total - 1)
    if zone == "front_wall_edge":
        return box_x + box_w * (0.24 + slot * 0.52), box_y + box_h - 18
    if zone in {"rear_wall", "back_wall"}:
        return box_x + box_w * (0.24 + slot * 0.52), box_y + 18
    if zone == "right_wall_exhaust":
        return box_x + box_w - 24, box_y + box_h * (0.30 + slot * 0.40)
    if zone == "left_wall":
        return box_x + 24, box_y + box_h * (0.30 + slot * 0.40)
    if zone == "air_inlet_zone_away_from_heat_sources":
        return box_x + box_w * 0.18, box_y + box_h * (0.28 + slot * 0.34)

    columns = 3
    row = index // columns
    col = index % columns
    rows = max(1, (total + columns - 1) // columns)
    x = box_x + box_w * (0.25 + 0.25 * col)
    y = box_y + box_h * (0.32 + 0.38 * (row / max(1, rows - 1)))
    if total <= columns:
        y = box_y + box_h * 0.52
    return x, y


def _component_footprint(
    component: Dict[str, Any],
    *,
    scale: float,
    default_w: float,
    default_h: float,
    maximum: float = 110.0,
) -> Tuple[float, float]:
    size = component.get("size_mm")
    if isinstance(size, dict):
        x_mm = _number_or_none(size.get("x_mm"))
        y_mm = _number_or_none(size.get("y_mm"))
        z_mm = _number_or_none(size.get("z_mm"))
        if x_mm is not None and y_mm is not None:
            role = str(component.get("visual_role") or "")
            if role == "fan_actuator" and z_mm is not None:
                side = min(max(z_mm * scale, 42), maximum)
                return side, side
            return min(max(x_mm * scale, 34), maximum), min(max(y_mm * scale, 22), maximum)
    return default_w, default_h


def _draw_dimension_arrow(
    parts: List[str],
    *,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    label: str,
    text_x: float,
    text_y: float,
) -> None:
    parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#111827" stroke-width="1.5" marker-start="url(#arrow)" marker-end="url(#arrow)"/>')
    parts.append(f'<text x="{text_x:.1f}" y="{text_y:.1f}" class="dim">{_svg_escape(label)}</text>')


def _draw_component(
    parts: List[str],
    component: Dict[str, Any],
    *,
    x: float,
    y: float,
    w: float,
    h: float,
    label_inside: bool = False,
) -> None:
    role = str(component.get("visual_role") or "")
    style = _role_style(role)
    ref_des = _svg_escape(component.get("ref_des") or "")
    label = _svg_escape(_visual_label(component, 28))

    if role in {"fan_actuator", "user_control", "mounting_hardware"}:
        radius = max(8.0, min(w, h) / 2)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{style["fill"]}" stroke="{style["stroke"]}" stroke-width="2"/>')
        if role == "fan_actuator":
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{max(4.0, radius * 0.35):.1f}" fill="#334155" stroke="#64748b" stroke-width="1"/>')
            for angle in (0, 90, 180, 270):
                parts.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + radius * 0.72:.1f}" y2="{y:.1f}" stroke="#94a3b8" stroke-width="3" transform="rotate({angle} {x:.1f} {y:.1f})"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + radius + 15:.1f}" class="label" text-anchor="middle">{ref_des}</text>')
        return

    rx = min(8.0, max(2.0, min(w, h) * 0.12))
    parts.append(f'<rect x="{x - w / 2:.1f}" y="{y - h / 2:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx:.1f}" fill="{style["fill"]}" stroke="{style["stroke"]}" stroke-width="2"/>')
    if role in {"controller_board", "carrier_board"}:
        pin_count = 10
        for index in range(pin_count):
            px = x - w / 2 + 8 + index * max(3.0, (w - 16) / max(1, pin_count - 1))
            parts.append(f'<circle cx="{px:.1f}" cy="{y - h / 2 + 5:.1f}" r="2" fill="#fef3c7" stroke="#78350f" stroke-width="0.5"/>')
            parts.append(f'<circle cx="{px:.1f}" cy="{y + h / 2 - 5:.1f}" r="2" fill="#fef3c7" stroke="#78350f" stroke-width="0.5"/>')
    if label_inside:
        parts.append(f'<text x="{x:.1f}" y="{y - 3:.1f}" class="component-inverse" text-anchor="middle">{ref_des}</text>')
        parts.append(f'<text x="{x:.1f}" y="{y + 13:.1f}" class="component-inverse small" text-anchor="middle">{_svg_escape(_truncate(str(component.get("name") or ""), 16))}</text>')
    else:
        parts.append(f'<text x="{x:.1f}" y="{y + h / 2 + 15:.1f}" class="label" text-anchor="middle">{label}</text>')


def _group_components_by_mounting_zone(assembly_components: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    lid_components = [
        component
        for component in assembly_components
        if str(component.get("mounting_zone") or "") == "top_lid_operator_panel"
    ]
    lid_refs = {component.get("ref_des") for component in lid_components}
    bottom_components = [
        component
        for component in assembly_components
        if component.get("ref_des") not in lid_refs
    ]
    return bottom_components, lid_components


def _draw_layer_window(
    parts: List[str],
    *,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    subtitle: str,
    accent: str,
) -> Tuple[float, float, float, float]:
    parts.append(f'<rect x="{x + 4:.1f}" y="{y + 6:.1f}" width="{w:.1f}" height="{h:.1f}" rx="14" fill="#cbd5e1" opacity="0.45"/>')
    parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="14" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.5"/>')
    parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="42" rx="14" fill="#111827"/>')
    parts.append(f'<path d="M{x:.1f} {y + 28:.1f} H{x + w:.1f} V{y + 42:.1f} H{x:.1f} Z" fill="#111827"/>')
    parts.append(f'<rect x="{x:.1f}" y="{y + 40:.1f}" width="{w:.1f}" height="4" fill="{accent}"/>')
    parts.append(f'<text x="{x + 18:.1f}" y="{y + 25:.1f}" class="window-title">{_svg_escape(title)}</text>')
    parts.append(f'<text x="{x + w - 18:.1f}" y="{y + 25:.1f}" class="window-subtitle" text-anchor="end">{_svg_escape(subtitle)}</text>')
    return x + 18, y + 60, w - 36, h - 78


def _component_zone(component: Dict[str, Any]) -> str:
    return str(component.get("mounting_zone") or "bottom_shell_floor")


def build_project_layout_diagram_image(
    user_prompt: str,
    ir: Any,
    *,
    prompt: Optional[str] = None,
    reference_view_id: Optional[str] = None,
    prompt_compaction_result: Optional[PromptCompactionResult] = None,
) -> GeneratedImage:
    spec = build_project_visual_spec(user_prompt, ir)
    assembly_model = spec["design_assembly_model"]
    dimensions = spec.get("external_dimensions_mm") or {"x_mm": 120.0, "y_mm": 80.0, "z_mm": 40.0}
    width_mm = max(1.0, float(dimensions.get("x_mm") or 120.0))
    depth_mm = max(1.0, float(dimensions.get("y_mm") or 80.0))
    height_mm = max(1.0, float(dimensions.get("z_mm") or 40.0))
    internal = spec.get("internal_usable_dimensions_mm") or {}
    assembly_components = assembly_model.get("component_mounting_zones", [])
    bottom_components, lid_components = _group_components_by_mounting_zone(assembly_components)
    ui_components = [component for component in assembly_components if component.get("subsystem") == "ui"]
    sensing_components = [component for component in assembly_components if component.get("subsystem") == "sensing"]
    airflow_components = [component for component in assembly_components if component.get("subsystem") == "airflow"]
    wall_zones = {"front_wall_edge", "right_wall_exhaust", "left_wall", "rear_wall", "back_wall", "air_inlet_zone_away_from_heat_sources"}
    wall_components = [component for component in bottom_components if _component_zone(component) in wall_zones]
    floor_components = [component for component in bottom_components if _component_zone(component) not in wall_zones]
    if not floor_components:
        floor_components = [
            component
            for component in bottom_components
            if component.get("visual_role") not in {"external_port", "fan_actuator"}
        ]

    canvas_w = 1024
    canvas_h = 1024
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w}" height="{canvas_h}" viewBox="0 0 {canvas_w} {canvas_h}">',
        "<defs>",
        '<marker id="arrow" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L8,4 L0,8 Z" fill="#111827"/></marker>',
        "<style>",
        "text{font-family:Inter,Arial,Helvetica,sans-serif;fill:#111827}.title{font-size:28px;font-weight:800}.subtitle{font-size:13px;font-weight:700;letter-spacing:.08em}.dim{font-size:12px;font-weight:800;text-anchor:middle}.label{font-size:11px;font-weight:800}.small{font-size:10px}.note{font-size:12px}.component-inverse{font-size:11px;font-weight:800;fill:#f8fafc}.section{font-size:13px;font-weight:900;letter-spacing:.06em}.muted{fill:#475569}.net{font-size:11px}.tiny{font-size:9px}.window-title{fill:#f8fafc;font-size:13px;font-weight:900;letter-spacing:.08em}.window-subtitle{fill:#cbd5e1;font-size:10px;font-weight:800;letter-spacing:.08em}.chip{font-size:10px;font-weight:800}",
        "</style>",
        "</defs>",
        '<rect x="0" y="0" width="1024" height="1024" fill="#eef2f7"/>',
    ]

    title = _svg_escape(spec.get("title") or "Component Layout Diagram")
    parts.append(f'<text x="40" y="44" class="title">{title}</text>')
    parts.append('<text x="40" y="72" class="subtitle muted">SUBSYSTEM PHYSICAL LAYOUT SHEET - GENERATED FROM HARDWARE IR</text>')
    parts.append(
        f'<text x="40" y="98" class="note muted">External {_fmt_mm(width_mm)} mm W x {_fmt_mm(depth_mm)} mm D x {_fmt_mm(height_mm)} mm H'
        f' / internal usable {_fmt_mm(internal.get("x_mm"))} mm W x {_fmt_mm(internal.get("y_mm"))} mm D x {_fmt_mm(internal.get("z_mm"))} mm H'
        ' / transparent top-down reference / front edge is bottom</text>'
    )

    shell_cx, shell_cy, shell_cw, shell_ch = _draw_layer_window(
        parts,
        x=36,
        y=124,
        w=610,
        h=446,
        title="ASSEMBLY MAP / SUBSYSTEM POSITIONS",
        subtitle="TRANSPARENT CASE",
        accent="#22c55e",
    )
    lid_cx, lid_cy, lid_cw, lid_ch = _draw_layer_window(
        parts,
        x=672,
        y=124,
        w=316,
        h=210,
        title="UI SUBSYSTEM",
        subtitle="LID PLANE",
        accent="#38bdf8",
    )
    wall_cx, wall_cy, wall_cw, wall_ch = _draw_layer_window(
        parts,
        x=672,
        y=360,
        w=316,
        h=210,
        title="SENSING + AIRFLOW",
        subtitle="WALLS / PATH",
        accent="#f97316",
    )
    net_cx, net_cy, net_cw, net_ch = _draw_layer_window(
        parts,
        x=36,
        y=604,
        w=610,
        h=286,
        title="CONTROL + POWER/DRIVER",
        subtitle="NETS + LOOP",
        accent="#a855f7",
    )
    ref_cx, ref_cy, ref_cw, ref_ch = _draw_layer_window(
        parts,
        x=672,
        y=604,
        w=316,
        h=286,
        title="SUBSYSTEM SUMMARY",
        subtitle="RULES",
        accent="#facc15",
    )

    scale = min((shell_cw - 84) / width_mm, (shell_ch - 86) / depth_mm)
    shell_w = width_mm * scale
    shell_h = depth_mm * scale
    shell_x = shell_cx + (shell_cw - shell_w) / 2
    shell_y = shell_cy + 30 + (shell_ch - 70 - shell_h) / 2

    _draw_dimension_arrow(
        parts,
        x1=shell_x,
        y1=shell_y - 22,
        x2=shell_x + shell_w,
        y2=shell_y - 22,
        label=f"{_fmt_mm(width_mm)} mm W",
        text_x=shell_x + shell_w / 2,
        text_y=shell_y - 30,
    )
    _draw_dimension_arrow(
        parts,
        x1=shell_x - 24,
        y1=shell_y,
        x2=shell_x - 24,
        y2=shell_y + shell_h,
        label=f"{_fmt_mm(depth_mm)} mm D",
        text_x=shell_x - 42,
        text_y=shell_y + shell_h / 2,
    )
    parts.append(f'<rect x="{shell_x:.1f}" y="{shell_y:.1f}" width="{shell_w:.1f}" height="{shell_h:.1f}" rx="22" fill="#e2e8f0" opacity=".68" stroke="#0f172a" stroke-width="2.5"/>')
    parts.append(f'<rect x="{shell_x + 15:.1f}" y="{shell_y + 15:.1f}" width="{shell_w - 30:.1f}" height="{shell_h - 30:.1f}" rx="15" fill="#f8fafc" stroke="#94a3b8" stroke-width="1.25" stroke-dasharray="7 5"/>')
    parts.append(f'<rect x="{shell_x + 24:.1f}" y="{shell_y + shell_h - 30:.1f}" width="{shell_w - 48:.1f}" height="5" rx="2.5" fill="#0f172a" opacity=".22"/>')
    parts.append(f'<text x="{shell_x + shell_w / 2:.1f}" y="{shell_y + shell_h - 13:.1f}" class="tiny muted" text-anchor="middle">FRONT EDGE / GHOSTED ENCLOSURE / TRUE MOUNTING PLANES</text>')

    bottom_points: Dict[str, Tuple[float, float]] = {}
    zone_counts: Dict[str, int] = {}
    zone_indices: Dict[str, int] = {}
    for component in floor_components:
        zone = str(component.get("mounting_zone") or "bottom_shell_floor")
        zone_counts[zone] = zone_counts.get(zone, 0) + 1
    for index, component in enumerate(floor_components):
        zone = str(component.get("mounting_zone") or "bottom_shell_floor")
        zone_index = zone_indices.get(zone, 0)
        zone_indices[zone] = zone_index + 1
        x, y = _component_position(
            component,
            zone_index,
            zone_counts.get(zone, 1),
            box_x=shell_x + 18,
            box_y=shell_y + 18,
            box_w=shell_w - 36,
            box_h=shell_h - 36,
            scale=scale,
        )
        bottom_points[str(component.get("ref_des") or index)] = (x, y)
        w, h = _component_footprint(component, scale=scale, default_w=68, default_h=34, maximum=94)
        _draw_component(parts, component, x=x, y=y, w=w, h=h, label_inside=str(component.get("visual_role")) in {"controller_board", "carrier_board", "display"})

    for sx, sy in [
        (shell_x + 22, shell_y + 22),
        (shell_x + shell_w - 22, shell_y + 22),
        (shell_x + 22, shell_y + shell_h - 22),
        (shell_x + shell_w - 22, shell_y + shell_h - 22),
    ]:
        parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="10" fill="#fde68a" stroke="#92400e" stroke-width="2"/>')
        parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="4" fill="#92400e"/>')

    lid_shell_w = min(lid_cw - 36, width_mm * min((lid_cw - 44) / width_mm, (lid_ch - 56) / depth_mm))
    lid_shell_h = min(lid_ch - 54, depth_mm * min((lid_cw - 44) / width_mm, (lid_ch - 56) / depth_mm))
    lid_shell_x = lid_cx + (lid_cw - lid_shell_w) / 2
    lid_shell_y = lid_cy + 34
    parts.append(f'<rect x="{lid_shell_x:.1f}" y="{lid_shell_y:.1f}" width="{lid_shell_w:.1f}" height="{lid_shell_h:.1f}" rx="12" fill="#f8fafc" stroke="#0f172a" stroke-width="1.4" stroke-dasharray="6 4"/>')
    parts.append(f'<text x="{lid_cx:.1f}" y="{lid_cy + 16:.1f}" class="note muted">UI parts keep lid-plane facing metadata.</text>')
    ui_window_components = ui_components or lid_components
    if ui_window_components:
        lid_scale = min((lid_shell_w - 34) / width_mm, (lid_shell_h - 34) / depth_mm)
        for index, component in enumerate(ui_window_components):
            position = component.get("position_mm")
            if isinstance(position, dict) and _number_or_none(position.get("x_mm")) is not None and _number_or_none(position.get("y_mm")) is not None:
                x = lid_shell_x + lid_shell_w / 2 + float(position.get("x_mm")) * lid_scale
                y = lid_shell_y + lid_shell_h / 2 - float(position.get("y_mm")) * lid_scale
            else:
                x = lid_shell_x + 42 + index * min(74, (lid_shell_w - 84) / max(1, len(ui_window_components) - 1))
                y = lid_shell_y + lid_shell_h / 2
            x = min(max(x, lid_shell_x + 30), lid_shell_x + lid_shell_w - 30)
            y = min(max(y, lid_shell_y + 24), lid_shell_y + lid_shell_h - 24)
            w, h = _component_footprint(component, scale=lid_scale, default_w=50, default_h=28, maximum=64)
            _draw_component(parts, component, x=x, y=y, w=w, h=h, label_inside=str(component.get("visual_role")) == "display")
    else:
        parts.append(f'<text x="{lid_cx:.1f}" y="{lid_cy + 72:.1f}" class="note muted">No UI subsystem components in IR.</text>')

    wall_groups = [
        ("POWER / I/O", "front_wall_edge", "#64748b"),
        ("AIRFLOW", "right_wall_exhaust", "#f97316"),
        ("SENSING", "air_inlet_zone_away_from_heat_sources", "#38bdf8"),
        ("REAR / AUX", "rear_wall", "#94a3b8"),
    ]
    parts.append(f'<text x="{wall_cx:.1f}" y="{wall_cy + 16:.1f}" class="note muted">Subsystems keep wall/air-path facing normals.</text>')
    for index, (label, zone, color) in enumerate(wall_groups):
        row_y = wall_cy + 36 + index * 34
        parts.append(f'<rect x="{wall_cx:.1f}" y="{row_y:.1f}" width="{wall_cw:.1f}" height="26" rx="7" fill="#f8fafc" stroke="#cbd5e1" stroke-width="1"/>')
        parts.append(f'<rect x="{wall_cx:.1f}" y="{row_y:.1f}" width="74" height="26" rx="7" fill="{color}"/>')
        parts.append(f'<text x="{wall_cx + 37:.1f}" y="{row_y + 17:.1f}" class="chip" fill="#ffffff" text-anchor="middle">{_svg_escape(label)}</text>')
        zone_items = [component for component in wall_components if _component_zone(component) == zone or (zone == "rear_wall" and _component_zone(component) == "back_wall")]
        if zone == "right_wall_exhaust":
            zone_items.extend(component for component in airflow_components if component not in zone_items)
        if zone == "air_inlet_zone_away_from_heat_sources":
            zone_items.extend(component for component in sensing_components if component not in zone_items)
        if zone_items:
            text = " / ".join(f"{_visual_label(component, 14)} {component.get('facing_normal') or ''}" for component in zone_items[:3])
        else:
            text = "no assigned components"
        parts.append(f'<text x="{wall_cx + 86:.1f}" y="{row_y + 17:.1f}" class="note">{_svg_escape(text)}</text>')

    controller = next((component for component in floor_components if component.get("visual_role") == "controller_board"), None)
    driver = next((component for component in floor_components if component.get("visual_role") == "driver_stage"), None)
    fan = next((component for component in wall_components if component.get("visual_role") == "fan_actuator"), None)
    sensor = next((component for component in wall_components + floor_components if component.get("visual_role") == "sensor"), None)
    behavior = spec.get("behavior_control_model") or {}
    block_y = net_cy + 42
    block_specs = [
        ("SENSOR / FEEDBACK", sensor, "#38bdf8"),
        ("CONTROLLER", controller, "#22c55e"),
        ("DRIVER", driver, "#f97316"),
        ("ACTUATOR", fan, "#111827"),
    ]
    block_w = 122
    gap = 22
    block_points: List[Tuple[float, float]] = []
    for index, (label, component, color) in enumerate(block_specs):
        x = net_cx + index * (block_w + gap)
        parts.append(f'<rect x="{x:.1f}" y="{block_y:.1f}" width="{block_w:.1f}" height="58" rx="10" fill="#f8fafc" stroke="{color}" stroke-width="2"/>')
        parts.append(f'<text x="{x + block_w / 2:.1f}" y="{block_y + 22:.1f}" class="tiny" text-anchor="middle">{_svg_escape(label)}</text>')
        parts.append(f'<text x="{x + block_w / 2:.1f}" y="{block_y + 42:.1f}" class="label" text-anchor="middle">{_svg_escape(_visual_label(component or {}, 18) if component else "not specified")}</text>')
        block_points.append((x + block_w, block_y + 29))
        if index < len(block_specs) - 1:
            parts.append(f'<line x1="{x + block_w:.1f}" y1="{block_y + 29:.1f}" x2="{x + block_w + gap - 4:.1f}" y2="{block_y + 29:.1f}" stroke="#334155" stroke-width="2" marker-end="url(#arrow)"/>')
    parts.append(f'<path d="M{net_cx + block_w * 3 + gap * 3 + block_w / 2:.1f} {block_y + 68:.1f} C{net_cx + 420:.1f} {block_y + 116:.1f},{net_cx + 140:.1f} {block_y + 116:.1f},{net_cx + 62:.1f} {block_y + 68:.1f}" fill="none" stroke="#0284c7" stroke-width="2.4" stroke-dasharray="8 5" marker-end="url(#arrow)"/>')
    parts.append(f'<text x="{net_cx + 230:.1f}" y="{block_y + 120:.1f}" class="label" fill="#075985" text-anchor="middle">feedback path when supported by nets</text>')
    controlled_variables = ", ".join(str(item) for item in (behavior.get("controlled_variables") or [])[:3])
    control_type = str(behavior.get("control_model_type") or "control model")
    parts.append(f'<text x="{net_cx:.1f}" y="{block_y + 146:.1f}" class="note muted">{_svg_escape(_truncate(f"{control_type}: {controlled_variables}", 82))}</text>')

    nets = spec.get("connection_nets") or []
    control_nets = [
        net
        for net in nets
        if any(token in f'{net.get("name", "")} {net.get("net_type", "")}'.lower() for token in ["pwm", "tach", "feedback", "sensor", "i2c", "analog"])
    ][:7]
    if not control_nets:
        control_nets = nets[:7]
    if control_nets:
        for index, net in enumerate(control_nets):
            y = net_cy + 188 + index * 14
            pins = ", ".join(
                f'{pin.get("ref_des")}:{pin.get("pin_id")}'
                for pin in (net.get("pins") or [])[:4]
                if pin.get("ref_des") or pin.get("pin_id")
            )
            line = _truncate(f'{net.get("net_id") or ""}  {net.get("name") or ""}  [{net.get("net_type") or ""}]  {pins}', 88)
            parts.append(f'<text x="{net_cx:.1f}" y="{y:.1f}" class="net">{_svg_escape(line)}</text>')
    else:
        parts.append(f'<text x="{net_cx:.1f}" y="{net_cy + 188:.1f}" class="note muted">No connection nets present in IR.</text>')

    subsystem_colors = {
        "ui": "#38bdf8",
        "control": "#22c55e",
        "power_driver": "#a855f7",
        "sensing": "#06b6d4",
        "airflow": "#f97316",
        "mechanical": "#facc15",
        "support": "#94a3b8",
    }
    subsystem_rows = (spec.get("subsystems") or [])[:4]
    for index, subsystem in enumerate(subsystem_rows):
        y = ref_cy + 6 + index * 38
        label = subsystem.get("label") or subsystem.get("id") or "Subsystem"
        count = len(subsystem.get("component_refs") or [])
        color = subsystem_colors.get(str(subsystem.get("id") or ""), "#94a3b8")
        contract = subsystem.get("contract") if isinstance(subsystem.get("contract"), dict) else {}
        purpose = _truncate(contract.get("purpose", ""), 46)
        parts.append(f'<rect x="{ref_cx:.1f}" y="{y:.1f}" width="{ref_cw:.1f}" height="32" rx="7" fill="#f8fafc" stroke="#e2e8f0"/>')
        parts.append(f'<circle cx="{ref_cx + 13:.1f}" cy="{y + 12:.1f}" r="5" fill="{color}"/>')
        parts.append(f'<text x="{ref_cx + 28:.1f}" y="{y + 14:.1f}" class="note">{_svg_escape(label)}</text>')
        parts.append(f'<text x="{ref_cx + ref_cw - 12:.1f}" y="{y + 14:.1f}" class="label" text-anchor="end">{count}</text>')
        if purpose:
            parts.append(f'<text x="{ref_cx + 28:.1f}" y="{y + 27:.1f}" class="small muted">{_svg_escape(purpose)}</text>')
    rules = [
        "Contracts include purpose, interfaces, constraints, checks.",
        "Each component preserves mounted_on and facing_normal.",
        "Dependencies/feedback are relationships, not relocation excuses.",
        "Dimensions come only from the visual spec.",
    ]
    for index, rule in enumerate(rules):
        parts.append(f'<text x="{ref_cx:.1f}" y="{ref_cy + 170 + index * 18:.1f}" class="note muted">- {_svg_escape(rule)}</text>')

    parts.append("</svg>")
    svg = "\n".join(parts)
    return GeneratedImage(
        data_url=_svg_data_url(svg),
        provider="forma-layout",
        model="deterministic-subsystem-svg-v1",
        size=f"{canvas_w}x{canvas_h}",
        prompt=prompt or "Deterministic layout sheet generated from the canonical visual spec.",
        output_format="svg+xml",
        view_id="diagram",
        label="Layout diagram",
        reference_view_id=reference_view_id,
        prompt_original_length=prompt_compaction_result.original_length if prompt_compaction_result else None,
        prompt_final_length=prompt_compaction_result.final_length if prompt_compaction_result else None,
        prompt_compacted=prompt_compaction_result.was_compacted if prompt_compaction_result else False,
        prompt_compaction_strategy=prompt_compaction_result.strategy if prompt_compaction_result else "none",
    )


def build_image_provider(force_enabled: bool = False, settings: Optional[Settings] = None) -> ImageProvider:
    _env, _first_env, _first_env_bool, _first_env_float, _first_env_int, _first_env_optional_int = _scoped_image_env_helpers(settings)
    provider_name = (_env("IMAGE_PROVIDER") or "").strip().lower().replace("_", "-")
    enabled_default = bool(provider_name and provider_name not in {"none", "disabled", "off", "false", "simulation", "mock"})
    enabled = _first_env_bool(["IMAGE_OUTPUT_ENABLED", "OPENAI_IMAGE_OUTPUT_ENABLED"], default=enabled_default)

    if not enabled and not force_enabled:
        return NoImageProvider()

    if not provider_name:
        if _first_env(["OPENAI_IMAGE_API_KEY", "OPENAI_API_KEY"]):
            provider_name = "openai"
        elif _first_env(["GMI_IMAGE_API_KEY", "GMI_API_KEY", "GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY"]):
            provider_name = "gmi"
        elif _first_env(["TOGETHER_IMAGE_API_KEY", "TOGETHER_API_KEY"]):
            provider_name = "together"
        elif _first_env(["VERTEX_AI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID", "GCLOUD_PROJECT"]):
            provider_name = "vertex"
        elif _first_env(["HUGGINGFACE_IMAGE_API_KEY", "HF_IMAGE_TOKEN", "HF_TOKEN", "HUGGINGFACE_API_KEY"]) and _first_env(["HUGGINGFACE_IMAGE_MODEL", "HF_IMAGE_MODEL"]):
            provider_name = "huggingface"
        elif _first_env(["IMAGE_BASE_URL", "OPENAI_IMAGE_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL"]):
            provider_name = "openai-compatible"
        else:
            return NoImageProvider("Image output is enabled, but no image provider configuration was found.")

    if provider_name in {"none", "disabled", "off", "false", "simulation", "mock"}:
        return NoImageProvider()
    if provider_name in {"openai", "openai-compatible", "compatible"}:
        return OpenAIImageProvider(provider_name=provider_name, enabled=enabled, force_enabled=force_enabled, settings=settings)
    if provider_name in {"gmi", "gmi-cloud", "gmicloud", "gemicloud"}:
        return GMIImageProvider(enabled=enabled, force_enabled=force_enabled, settings=settings)
    if provider_name in {"together", "together-ai", "togetherai"}:
        return TogetherImageProvider(enabled=enabled, force_enabled=force_enabled, settings=settings)
    if provider_name in {"vertex", "vertex-ai", "google-vertex", "google-vertex-ai", "nano-banana"}:
        return VertexAIImageProvider(enabled=enabled, force_enabled=force_enabled, settings=settings)
    if provider_name in {"huggingface", "hugging-face", "hf"}:
        return HuggingFaceImageProvider(enabled=enabled, force_enabled=force_enabled, settings=settings)

    logger.warning("Unsupported IMAGE_PROVIDER %r; image output is disabled.", provider_name)
    return NoImageProvider(
        f"Unsupported IMAGE_PROVIDER '{provider_name}'. Supported providers are openai, openai-compatible, "
        "vertex, gmi, together, huggingface, and none."
    )


def get_image_output_debug_config(settings: Optional[Settings] = None) -> Dict[str, Any]:
    default_config = build_image_provider(settings=settings).get_debug_config()
    request_config = build_image_provider(force_enabled=True, settings=settings).get_debug_config()
    return {
        **default_config,
        "default_enabled": default_config.get("enabled", False),
        "request_capable": bool(request_config.get("configured")),
        "request_provider": request_config.get("provider"),
        "request_model_name": request_config.get("model_name"),
    }
