import json
import logging
from forma_core.config import config
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

logger = logging.getLogger(__name__)
Settings = Mapping[str, str]

DEFAULT_GMI_BASE_URL = "https://console.gmicloud.ai"
VIDEO_MODE_IMAGE_TO_VIDEO = "image-to-video"
VIDEO_MODE_VIDEO_TO_VIDEO = "video-to-video"
DEFAULT_GMI_IMAGE_TO_VIDEO_MODEL = "kling-v3-image-to-video"
DEFAULT_GMI_VIDEO_TO_VIDEO_MODEL = "wan2.7-videoedit"
GMI_GEMINI_OMNI_FLASH_PREVIEW_MODEL = "gemini-omni-flash-preview"
GMI_GEMINI_OMNI_FLASH_PREVIEW_MODEL_URL = "https://console.gmicloud.ai/user-console/ie/model-hub/video/gemini-omni-flash-preview"
DEFAULT_GMI_REQUESTS_PATH = "/api/v1/ie/requestqueue/apikey/requests"
DEFAULT_GMI_TIMEOUT_SECONDS = 120.0
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
DEFAULT_VIDEO_ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]


@dataclass(frozen=True)
class VideoModelDefinition:
    id: str
    label: str
    mode: str
    source_url: Optional[str] = None

    def response_metadata(self) -> Dict[str, str]:
        metadata = {
            "id": self.id,
            "label": self.label,
            "mode": self.mode,
            "type": self.mode,
        }
        if self.source_url:
            metadata["source_url"] = self.source_url
            metadata["sourceUrl"] = self.source_url
        return metadata


DEFAULT_GMI_IMAGE_TO_VIDEO_MODELS = [
    DEFAULT_GMI_IMAGE_TO_VIDEO_MODEL,
    "kling-o1-image-to-video",
    "Kling-Image2Video-V2.1-Master",
    "Kling-Image2Video-V2.1-Pro",
    "Kling-Image2Video-V2.1-Standard",
    "Kling-Image2Video-V2-Master",
    "Kling-Image2Video-V1.6-Pro",
    "Kling-Image2Video-V1.6-Standard",
    "skyreels-v4-image-to-video",
    "ltx-2-pro-image-to-video",
    "ltx-2-fast-image-to-video",
    "pixverse-v6-i2v",
    "pixverse-v5.6-i2v",
    "pixverse-v5.5-i2v",
    "seedance-1-0-pro-250528",
    "seedance-1-0-pro-fast-251015",
    "seedance-1-5-pro-251215",
    "seedance-2-0-260128",
    "seedance-2-0-fast-260128",
    "wan2.7-i2v",
    "wan2.6-i2v",
    "wan2.5-i2v-preview",
    "vidu-q3-pro-i2v",
    "vidu-q2-pro-i2v",
    GMI_GEMINI_OMNI_FLASH_PREVIEW_MODEL,
]

DEFAULT_GMI_VIDEO_TO_VIDEO_MODELS = [
    DEFAULT_GMI_VIDEO_TO_VIDEO_MODEL,
    "seedance-2-0-260128",
    "seedance-2-0-fast-260128",
    "pixverse-v6-extend",
    "pixverse-v5.6-extend",
    "kling-o1-edit-video",
    GMI_GEMINI_OMNI_FLASH_PREVIEW_MODEL,
]


@dataclass
class VideoGenerationResult:
    request_id: str
    status: str
    video_urls: List[str]
    raw: Dict[str, Any]

    def response_metadata(self) -> Dict[str, Any]:
        return {
            "requestId": self.request_id,
            "status": self.status,
            "videoUrls": self.video_urls,
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


def _env_float(name: str, default: float, settings: Optional[Settings] = None) -> float:
    raw_value = _env(name, settings=settings)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Invalid %s value %r; using %.1fs.", name, raw_value, default)
        return default


def _parse_model_list(value: Optional[str]) -> List[str]:
    if not value:
        return []

    models: List[str] = []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            models = [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        models = [item.strip() for item in value.split(",") if item.strip()]

    return models


def _model_label(model: str) -> str:
    return model.replace("-", " ").replace("_", " ").strip().title()


def _model_definition(model: str, mode: str) -> VideoModelDefinition:
    source_url = GMI_GEMINI_OMNI_FLASH_PREVIEW_MODEL_URL if model == GMI_GEMINI_OMNI_FLASH_PREVIEW_MODEL else None
    return VideoModelDefinition(id=model, label=_model_label(model), mode=mode, source_url=source_url)


def _dedupe_definitions(definitions: List[VideoModelDefinition]) -> List[VideoModelDefinition]:
    deduped: List[VideoModelDefinition] = []
    seen = set()
    for definition in definitions:
        key = (definition.mode, definition.id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(definition)
    return deduped


def normalize_video_mode(mode: Optional[str]) -> str:
    normalized = (mode or VIDEO_MODE_IMAGE_TO_VIDEO).strip().lower()
    if normalized in {"image", "i2v", "image2video", "image_to_video"}:
        return VIDEO_MODE_IMAGE_TO_VIDEO
    if normalized in {"video", "v2v", "video2video", "video_to_video"}:
        return VIDEO_MODE_VIDEO_TO_VIDEO
    if normalized in {VIDEO_MODE_IMAGE_TO_VIDEO, VIDEO_MODE_VIDEO_TO_VIDEO}:
        return normalized
    return VIDEO_MODE_IMAGE_TO_VIDEO


def get_default_video_model(mode: str = VIDEO_MODE_IMAGE_TO_VIDEO, settings: Optional[Settings] = None) -> str:
    normalized_mode = normalize_video_mode(mode)
    if normalized_mode == VIDEO_MODE_VIDEO_TO_VIDEO:
        return _env("GMI_CLOUD_VIDEO_TO_VIDEO_MODEL", DEFAULT_GMI_VIDEO_TO_VIDEO_MODEL, settings) or DEFAULT_GMI_VIDEO_TO_VIDEO_MODEL
    return _env("GMI_CLOUD_IMAGE_TO_VIDEO_MODEL", DEFAULT_GMI_IMAGE_TO_VIDEO_MODEL, settings) or DEFAULT_GMI_IMAGE_TO_VIDEO_MODEL


def get_available_video_model_options(mode: Optional[str] = None, settings: Optional[Settings] = None) -> List[VideoModelDefinition]:
    image_models = [
        get_default_video_model(VIDEO_MODE_IMAGE_TO_VIDEO, settings),
        *DEFAULT_GMI_IMAGE_TO_VIDEO_MODELS,
        *_parse_model_list(_env("GMI_CLOUD_IMAGE_TO_VIDEO_MODELS", settings=settings)),
        *_parse_model_list(_env("GMI_CLOUD_VIDEO_MODELS", settings=settings)),
    ]
    video_models = [
        get_default_video_model(VIDEO_MODE_VIDEO_TO_VIDEO, settings),
        *DEFAULT_GMI_VIDEO_TO_VIDEO_MODELS,
        *_parse_model_list(_env("GMI_CLOUD_VIDEO_TO_VIDEO_MODELS", settings=settings)),
    ]

    definitions = [
        *[_model_definition(model, VIDEO_MODE_IMAGE_TO_VIDEO) for model in image_models if model],
        *[_model_definition(model, VIDEO_MODE_VIDEO_TO_VIDEO) for model in video_models if model],
    ]
    normalized_mode = normalize_video_mode(mode) if mode else None
    if normalized_mode:
        definitions = [definition for definition in definitions if definition.mode == normalized_mode]
    return _dedupe_definitions(definitions)


def get_available_video_models(mode: Optional[str] = None, settings: Optional[Settings] = None) -> List[str]:
    return [definition.id for definition in get_available_video_model_options(mode, settings)]


def get_available_video_aspect_ratios(settings: Optional[Settings] = None) -> List[str]:
    configured = _parse_model_list(_env("GMI_CLOUD_VIDEO_ASPECT_RATIOS", settings=settings))
    ratios = [*DEFAULT_VIDEO_ASPECT_RATIOS, *configured]
    return list(dict.fromkeys(ratio for ratio in ratios if ratio))


def normalize_video_aspect_ratio(value: Optional[str], settings: Optional[Settings] = None) -> str:
    raw_value = (value or DEFAULT_VIDEO_ASPECT_RATIO).strip().lower()
    aliases = {
        "landscape": "16:9",
        "wide": "16:9",
        "widescreen": "16:9",
        "portrait": "9:16",
        "vertical": "9:16",
        "square": "1:1",
    }
    normalized = aliases.get(raw_value, raw_value)
    allowed = get_available_video_aspect_ratios(settings)
    if normalized not in allowed:
        raise ValueError(f"Unsupported video aspect ratio '{value}'. Valid ratios: {', '.join(allowed)}.")
    return normalized


def _status_value(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "queued"


def _collect_video_urls(value: Any) -> List[str]:
    urls: List[str] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate.strip().startswith(("http://", "https://")):
            urls.append(candidate.strip())
        elif isinstance(candidate, dict):
            for key in ("video_url", "videoUrl", "url", "media_url", "mediaUrl", "download_url", "downloadUrl"):
                add(candidate.get(key))
        elif isinstance(candidate, list):
            for item in candidate:
                add(item)

    if isinstance(value, dict):
        containers = [
            value,
            value.get("outcome"),
            value.get("output"),
            value.get("result"),
            value.get("data"),
            value.get("response"),
        ]
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in (
                "video_url",
                "videoUrl",
                "video",
                "videos",
                "media_urls",
                "mediaUrls",
                "media",
                "output_urls",
                "outputUrls",
                "urls",
                "url",
            ):
                add(container.get(key))

            nested_outcome = container.get("outcome")
            if nested_outcome is not container:
                add(nested_outcome)
    else:
        add(value)

    return list(dict.fromkeys(urls))


class GMICloudProvider:
    provider_name = "gmi-cloud"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings
        self.api_key = _first_env(["GMI_CLOUD_API_KEY", "GMICLOUD_API_KEY", "GMI_API_KEY"], settings=settings)
        self.base_url = (_env("GMI_CLOUD_BASE_URL", DEFAULT_GMI_BASE_URL, settings) or DEFAULT_GMI_BASE_URL).rstrip("/")
        self.default_model = get_default_video_model(settings=settings)
        self.requests_path = _env("GMI_CLOUD_REQUESTS_PATH", DEFAULT_GMI_REQUESTS_PATH, settings) or DEFAULT_GMI_REQUESTS_PATH
        self.timeout_seconds = _env_float("GMI_CLOUD_TIMEOUT_SECONDS", DEFAULT_GMI_TIMEOUT_SECONDS, settings)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.api_key:
            reason = "GMI Cloud API key is missing."
        elif not self.base_url:
            reason = "GMI Cloud base URL is missing."
        return {
            "provider": self.provider_name,
            "configured": self.is_configured,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "default_video_to_video_model": get_default_video_model(VIDEO_MODE_VIDEO_TO_VIDEO, self.settings),
            "models": get_available_video_models(settings=self.settings),
            "model_options": [model.response_metadata() for model in get_available_video_model_options(settings=self.settings)],
            "aspect_ratios": get_available_video_aspect_ratios(self.settings),
            "default_aspect_ratio": DEFAULT_VIDEO_ASPECT_RATIO,
            "reason": reason,
        }

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise RuntimeError("GMI Cloud API key is missing. Set GMI_CLOUD_API_KEY, GMICLOUD_API_KEY, or GMI_API_KEY.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
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
            raise RuntimeError(f"GMI Cloud request failed with HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GMI Cloud request failed: {exc}") from exc

        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GMI Cloud returned non-JSON response.") from exc

    def _normalize_response(self, response: Dict[str, Any], fallback_request_id: Optional[str] = None) -> VideoGenerationResult:
        request_id = response.get("request_id") or response.get("requestId") or response.get("id")
        if not request_id and isinstance(response.get("data"), dict):
            data = response["data"]
            request_id = data.get("request_id") or data.get("requestId") or data.get("id")
        if not request_id and isinstance(response.get("result"), dict):
            result = response["result"]
            request_id = result.get("request_id") or result.get("requestId") or result.get("id")
        if not request_id:
            request_id = fallback_request_id
        if not request_id:
            raise RuntimeError("GMI Cloud response did not include request_id.")

        status = (
            response.get("status")
            or (response.get("data") if isinstance(response.get("data"), dict) else {}).get("status")
            or (response.get("result") if isinstance(response.get("result"), dict) else {}).get("status")
            or (response.get("outcome") if isinstance(response.get("outcome"), dict) else {}).get("status")
        )

        return VideoGenerationResult(
            request_id=str(request_id),
            status=_status_value(status),
            video_urls=_collect_video_urls(response),
            raw=response,
        )

    def create_image_to_video(
        self,
        *,
        image: str,
        prompt: str,
        model: str,
        duration: str,
        aspect_ratio: Optional[str] = None,
        sound: str = "off",
    ) -> VideoGenerationResult:
        model_key = model.strip().lower()
        image_key = "first_frame" if model_key.startswith("seedance-") else "image"
        payload = {
            "model": model,
            "payload": {
                image_key: image,
                "prompt": prompt,
                "duration": str(duration),
                "aspect_ratio": normalize_video_aspect_ratio(aspect_ratio, self.settings),
                "sound": sound or "off",
            },
        }
        response = self._request_json(self.requests_path, method="POST", payload=payload)
        return self._normalize_response(response)

    def create_video_to_video(
        self,
        *,
        video: str,
        prompt: str,
        model: str,
        duration: str,
        aspect_ratio: Optional[str] = None,
        sound: str = "off",
    ) -> VideoGenerationResult:
        model_key = model.strip().lower()
        if model_key.startswith("seedance-"):
            request_payload: Dict[str, Any] = {
                "reference_videos": [video],
                "prompt": prompt,
                "duration": str(duration),
                "aspect_ratio": normalize_video_aspect_ratio(aspect_ratio, self.settings),
                "sound": sound or "off",
            }
            payload = {
                "model": model,
                "payload": request_payload,
            }
            response = self._request_json(self.requests_path, method="POST", payload=payload)
            return self._normalize_response(response)

        source_key = "video_url" if model_key in {"kling-o1-edit-video"} else "video"
        request_payload: Dict[str, Any] = {
            source_key: video,
            "prompt": prompt,
            "duration": str(duration),
            "aspect_ratio": normalize_video_aspect_ratio(aspect_ratio, self.settings),
            "sound": sound or "off",
        }

        payload = {
            "model": model,
            "payload": request_payload,
        }
        response = self._request_json(self.requests_path, method="POST", payload=payload)
        return self._normalize_response(response)

    def get_request_status(self, request_id: str) -> VideoGenerationResult:
        response = self._request_json(f"{self.requests_path.rstrip('/')}/{request_id}", method="GET")
        return self._normalize_response(response, fallback_request_id=request_id)
