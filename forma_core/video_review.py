from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
from forma_core.config import config
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, Field, field_validator

from forma_core.workspaces.projects.iteration import compact_hardware_ir_for_iteration, coerce_hardware_ir
from forma_core.llm import LLMProviderConfigError, LLMProviderOutputError
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.objects import normalize_project_namespace


logger = logging.getLogger(__name__)

DEFAULT_FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL_SLUG = "qwen3-omni-30b-a3b-instruct"
DEFAULT_FIREWORKS_FRAME_REVIEW_MODEL_SLUG = "kimi-k2p6"
DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG = DEFAULT_FIREWORKS_FRAME_REVIEW_MODEL_SLUG
DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL = f"accounts/fireworks/models/{DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG}"
DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL = f"accounts/fireworks/models/{DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL_SLUG}"
FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS = {
    "qwen3-omni-30b-a3b-instruct": "video-audio-text",
    "molmo2-4b": "video-text",
    "molmo2-8b": "video-text",
}
FIREWORKS_VIDEO_REVIEW_FRAME_MODELS = {
    "kimi-k2p6": "image-text",
}
DEFAULT_FIREWORKS_TIMEOUT_SECONDS = 180.0
DEFAULT_VIDEO_REVIEW_MAX_FRAMES = 8
DEFAULT_VIDEO_REVIEW_MAX_SECONDS = 60
DEFAULT_VIDEO_REVIEW_NATIVE_FPS = 1.0
DEFAULT_VIDEO_REVIEW_NATIVE_HEIGHT = 360
DEFAULT_VIDEO_REVIEW_MAX_MEDIA_BYTES = 10_000_000
DEFAULT_VIDEO_REVIEW_INPUT_MODE = "auto"
VIDEO_REVIEW_ALLOWED_NAMESPACES = {
    "product.overview",
    "product.electrical",
    "product.mech",
    "product.validation",
    "product.assembly",
    "project.docs",
    "project.history",
}


class VideoCoherenceIssue(BaseModel):
    severity: str = Field("warning", description="Issue severity: critical, warning, or info.")
    category: str = Field("continuity", description="Issue type such as coherence, logic, continuity, rendering, or assembly.")
    frame_reference: Optional[str] = Field(None, description="Frame or time reference where the issue is visible.")
    description: str = Field(..., description="What is wrong or incoherent in the video.")
    evidence: str = Field("", description="Visual evidence from the sampled video frames.")
    suggested_correction: str = Field("", description="Concrete change to make in the project iteration.")

    @field_validator("severity", "category", mode="before")
    @classmethod
    def normalize_short_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower().replace("_", " ") or None
        return value


class VideoIterationReview(BaseModel):
    summary: str = Field(..., description="Short review of the video and project coherence.")
    coherence_score: float = Field(0.0, ge=0.0, le=1.0, description="0-1 assessment of video/project coherence.")
    needs_iteration: bool = Field(True, description="Whether the project should be revised.")
    target_namespace: str = Field("product.mech", description="Forma namespace best suited for the correction.")
    issues: List[VideoCoherenceIssue] = Field(default_factory=list)
    iteration_instruction: str = Field(..., description="Natural-language instruction for Forma's project iterator.")

    @field_validator("target_namespace", mode="before")
    @classmethod
    def normalize_target_namespace(cls, value: Any) -> str:
        normalized = normalize_project_namespace(str(value or "product.mech"))
        if normalized in VIDEO_REVIEW_ALLOWED_NAMESPACES:
            return normalized
        return "product.mech"

    @field_validator("iteration_instruction", "summary", mode="before")
    @classmethod
    def require_non_empty_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Video review text fields must not be empty.")
        return text


class VideoReviewClient(Protocol):
    model: str

    def review_video(
        self,
        current_ir: HardwareIntermediateRepresentation,
        *,
        video_url: str,
        original_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> VideoIterationReview:
        ...


@dataclass(frozen=True)
class FireworksPreparedVideo:
    video_data_url: str
    audio_data_url: Optional[str] = None


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = config.get(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped if stripped else default


def _env_float(name: str, default: float) -> float:
    raw_value = _env(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw_value = _env(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = _env(name)
    if raw_value is None:
        return default
    try:
        return max(minimum, min(maximum, int(raw_value)))
    except ValueError:
        return default


def _validate_http_video_url(video_url: str) -> str:
    url = str(video_url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("video_url must be an http(s) URL.")
    return url


def _fireworks_video_model_slug(model: str) -> str:
    base_model = str(model or "").split("#", 1)[0].strip().strip("/")
    if not base_model:
        return DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG
    return base_model.rsplit("/", 1)[-1]


def _fireworks_model_path_for_slug(slug_or_model: str) -> str:
    normalized = str(slug_or_model or "").strip()
    if not normalized:
        normalized = DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG
    if normalized.startswith("accounts/"):
        return normalized
    return f"accounts/fireworks/models/{normalized}"


def _resolve_fireworks_video_review_model(
    model: Optional[str],
    *,
    account_id: Optional[str],
    deployment_id: Optional[str],
) -> str:
    raw_model = str(model or DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL).strip()
    if "#" in raw_model:
        return raw_model
    if account_id and deployment_id:
        slug = _fireworks_video_model_slug(raw_model)
        return f"accounts/{account_id}/models/{slug}#accounts/{account_id}/deployments/{deployment_id}"
    return _fireworks_model_path_for_slug(raw_model)


def _fireworks_video_model_requires_deployment(model: str) -> bool:
    return _fireworks_video_model_slug(model) in FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS


def _fireworks_video_model_supports_audio(model: str) -> bool:
    return FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS.get(_fireworks_video_model_slug(model)) == "video-audio-text"


def _extract_json_document(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed = None
        for index, char in enumerate(stripped):
            if char not in {"{", "["}:
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[index:])
                break
            except json.JSONDecodeError:
                continue
        if parsed is None:
            raise
    if not isinstance(parsed, dict):
        raise ValueError("Video review response must be a JSON object.")
    return parsed


def _response_preview(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if not text:
        return "<empty>"
    single_line = " ".join(text.split())
    return single_line[:limit] + ("..." if len(single_line) > limit else "")


def _message_content_text(message: Dict[str, Any]) -> str:
    for key in ("content", "reasoning_content", "reasoning", "analysis"):
        value = message.get(key)
        if not value:
            continue
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or item))
                else:
                    parts.append(str(item))
            text = "\n".join(parts).strip()
        else:
            text = str(value).strip()
        if text:
            return text
    return ""


def _review_finding_sentences(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    meta_prefixes = (
        "the user wants me",
        "i need to",
        "i should",
        "first, let me",
        "let me",
        "we need to",
        "the task is",
    )
    finding_sentences = []
    for sentence in sentences:
        cleaned = sentence.strip(" -\t\n")
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if any(lowered.startswith(prefix) for prefix in meta_prefixes):
            continue
        finding_sentences.append(cleaned)
    return finding_sentences


def _distill_unstructured_review_text(text: str, *, limit: int = 1400) -> str:
    sentences = _review_finding_sentences(text)
    if not sentences:
        return ""
    issue_keywords = (
        "mismatch",
        "inconsistent",
        "inconsistency",
        "does not match",
        "doesn't match",
        "do not match",
        "not match",
        "issue",
        "problem",
        "concern",
        "however",
        "but",
        "instead",
        "wrong",
        "not visible",
        "not shown",
        "missing",
        "video shows",
        "frames show",
        "looks like",
        "appears to",
    )
    selected = [sentence for sentence in sentences if any(keyword in sentence.lower() for keyword in issue_keywords)]
    if not selected:
        selected = sentences
    distilled = " ".join(selected[:6]).strip()
    return _response_preview(distilled, limit=limit)


def _unstructured_review_target_namespace(text: str) -> str:
    lowered = str(text or "").lower()

    visual_keywords = (
        "frame",
        "video",
        "enclosure",
        "placement",
        "mechanical",
        "display",
        "lcd",
        "oled",
        "screen",
        "motor",
        "servo",
        "printer",
        "gantry",
        "nozzle",
        "bed",
        "gear",
        "impeller",
        "visual",
        "looks like",
        "appears",
        "shown",
        "visible",
    )
    electrical_keywords = (
        "wire",
        "wiring",
        "net",
        "netlist",
        "pin",
        "gpio",
        "voltage",
        "short",
        "ground",
        "circuit",
        "electrical connection",
        "power rail",
    )
    if any(keyword in lowered for keyword in visual_keywords):
        return "product.mech"
    if any(keyword in lowered for keyword in electrical_keywords):
        return "product.electrical"
    return "project.docs"


def _review_from_unstructured_text(text: str, *, model: str) -> Optional[VideoIterationReview]:
    preview = _distill_unstructured_review_text(text, limit=1400)
    if not preview or preview == "<empty>":
        return None
    namespace = _unstructured_review_target_namespace(preview)
    score = 0.3 if any(keyword in preview.lower() for keyword in ("mismatch", "does not match", "not match", "inconsistent")) else 0.5
    return VideoIterationReview(
        summary=f"Video review findings from Fireworks {model}: {preview}",
        coherence_score=score,
        needs_iteration=True,
        target_namespace=namespace,
        issues=[
            VideoCoherenceIssue(
                severity="warning",
                category="coherence",
                frame_reference="sampled video review",
                description="The video review identified coherence or continuity findings in unstructured text.",
                evidence=preview,
                suggested_correction="Revise the targeted project namespace so the generated video and HardwareIntermediateRepresentation describe the same physical build.",
            )
        ],
        iteration_instruction=(
            "Apply the Fireworks video review findings to make the HardwareIntermediateRepresentation and generated video coherent. "
            f"Target {namespace}. Findings: {preview}"
        ),
    )


def _fireworks_http_error_message(*, status_code: int, body: str, model: str) -> str:
    raw_body = (body or "").strip()
    provider_message = raw_body[:800] if raw_body else "No response body returned."
    provider_code = ""
    request_id = ""
    try:
        parsed = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            provider_message = str(error.get("message") or provider_message)
            provider_code = str(error.get("code") or "")
        elif parsed.get("message"):
            provider_message = str(parsed.get("message"))
        request_id = str(parsed.get("request_id") or "")

    message = f"forma_core.video_review Fireworks video review failed for model {model} with HTTP {status_code}"
    if provider_code:
        message += f" ({provider_code})"
    message += f": {provider_message}"
    if status_code == 404 or provider_code == "NOT_FOUND":
        message += (
            " The model may be unavailable to this Fireworks API key or not deployed; deploy it in Fireworks "
            "or set FIREWORKS_VIDEO_REVIEW_MODEL to a deployed model path."
        )
    if request_id:
        message += f" Fireworks request_id={request_id}."
    return message


def _ffprobe_duration_seconds(video_url: str) -> Optional[float]:
    if not shutil.which("ffprobe"):
        return None
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_url,
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        return max(0.0, float(completed.stdout.strip()))
    except Exception:
        return None


def sample_video_frames(video_url: str, *, max_frames: Optional[int] = None) -> List[bytes]:
    """Sample chronological JPEG frames from a video URL for VLM review."""
    url = _validate_http_video_url(video_url)
    frame_count = max_frames or _env_int("FIREWORKS_VIDEO_REVIEW_MAX_FRAMES", DEFAULT_VIDEO_REVIEW_MAX_FRAMES, 1, 16)
    if not shutil.which("ffmpeg"):
        logger.warning("Video self-correction frame sampling is unavailable because ffmpeg is not installed.")
        raise LLMProviderConfigError("ffmpeg is required for video self-correction frame sampling.")

    duration = _ffprobe_duration_seconds(url)
    fps = 1.0
    if duration and duration > 0:
        fps = min(2.0, max(0.05, frame_count / duration))
    logger.info(
        "Sampling video frames for self-correction: max_frames=%s fps=%.4f duration_seconds=%s",
        frame_count,
        fps,
        f"{duration:.2f}" if duration is not None else "unknown",
    )

    with tempfile.TemporaryDirectory(prefix="forma-video-review-") as temp_dir:
        output_pattern = str(Path(temp_dir) / "frame-%03d.jpg")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            url,
            "-vf",
            f"fps={fps:.4f},scale=768:-2:force_original_aspect_ratio=decrease",
            "-frames:v",
            str(frame_count),
            "-q:v",
            "4",
            output_pattern,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
        except subprocess.CalledProcessError as exc:
            logger.warning("Video self-correction frame sampling failed: %s", exc.stderr.strip() or exc)
            raise LLMProviderOutputError(f"Could not sample video frames: {exc.stderr.strip() or exc}") from exc
        except subprocess.TimeoutExpired as exc:
            logger.warning("Video self-correction frame sampling timed out.")
            raise LLMProviderOutputError("Timed out while sampling video frames.") from exc

        frames = [path.read_bytes() for path in sorted(Path(temp_dir).glob("frame-*.jpg")) if path.stat().st_size > 0]
    if not frames:
        logger.warning("Video self-correction frame sampling returned no frames.")
        raise LLMProviderOutputError("Video self-correction could not sample any frames from the video.")
    logger.info("Sampled %s video frames for self-correction.", len(frames[:frame_count]))
    return frames[:frame_count]


def _bounded_float(value: Optional[float], default: float, minimum: float, maximum: float) -> float:
    if value is None:
        return default
    return max(minimum, min(maximum, float(value)))


def _bounded_int(value: Optional[int], default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    return max(minimum, min(maximum, int(value)))


def _run_ffmpeg(command: List[str], *, error_message: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as exc:
        raise LLMProviderOutputError(f"{error_message}: {exc.stderr.strip() or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMProviderOutputError(f"{error_message}: timed out.") from exc


def prepare_video_for_fireworks_native_review(
    video_url: str,
    *,
    include_audio: bool = True,
    max_seconds: Optional[int] = None,
    fps: Optional[float] = None,
    height: Optional[int] = None,
    max_media_bytes: Optional[int] = None,
) -> FireworksPreparedVideo:
    """Transcode a remote video URL into Fireworks native video/audio data URLs."""
    url = _validate_http_video_url(video_url)
    if not shutil.which("ffmpeg"):
        logger.warning("Fireworks native video review is unavailable because ffmpeg is not installed.")
        raise LLMProviderConfigError("ffmpeg is required for Fireworks native video review preprocessing.")

    duration_seconds = _bounded_int(max_seconds, DEFAULT_VIDEO_REVIEW_MAX_SECONDS, 1, 300)
    video_fps = _bounded_float(fps, DEFAULT_VIDEO_REVIEW_NATIVE_FPS, 0.05, 5.0)
    video_height = _bounded_int(height, DEFAULT_VIDEO_REVIEW_NATIVE_HEIGHT, 144, 1080)
    media_cap = _bounded_int(max_media_bytes, DEFAULT_VIDEO_REVIEW_MAX_MEDIA_BYTES, 100_000, 50_000_000)

    logger.info(
        "Preparing Fireworks native video input: max_seconds=%s fps=%.3f height=%s include_audio=%s",
        duration_seconds,
        video_fps,
        video_height,
        include_audio,
    )

    with tempfile.TemporaryDirectory(prefix="forma-fireworks-video-") as temp_dir:
        output_video = Path(temp_dir) / "review-video.mp4"
        video_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            url,
            "-t",
            str(duration_seconds),
            "-vf",
            f"fps={video_fps:.4f},scale=-2:{video_height}:force_original_aspect_ratio=decrease",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-movflags",
            "+faststart",
            str(output_video),
        ]
        _run_ffmpeg(video_command, error_message="Could not prepare video for Fireworks review")

        video_bytes = output_video.read_bytes()
        if len(video_bytes) > media_cap:
            raise LLMProviderOutputError(
                f"Prepared Fireworks review video is {len(video_bytes)} bytes, above the {media_cap} byte limit. "
                "Lower FIREWORKS_VIDEO_REVIEW_MAX_SECONDS, FIREWORKS_VIDEO_REVIEW_NATIVE_FPS, or FIREWORKS_VIDEO_REVIEW_NATIVE_HEIGHT."
            )

        audio_data_url: Optional[str] = None
        if include_audio:
            output_audio = Path(temp_dir) / "review-audio.ogg"
            audio_command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                url,
                "-t",
                str(duration_seconds),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libopus",
                "-b:a",
                "24k",
                str(output_audio),
            ]
            try:
                _run_ffmpeg(audio_command, error_message="Could not prepare audio for Fireworks review")
                audio_bytes = output_audio.read_bytes()
                if len(audio_bytes) <= media_cap:
                    audio_data_url = f"data:audio/ogg;base64,{base64.b64encode(audio_bytes).decode('ascii')}"
                else:
                    logger.warning(
                        "Skipping Fireworks review audio because prepared audio is %s bytes, above the %s byte limit.",
                        len(audio_bytes),
                        media_cap,
                    )
            except LLMProviderOutputError as exc:
                logger.info("Skipping Fireworks review audio: %s", exc)

    logger.info(
        "Prepared Fireworks native video input: video_bytes=%s audio=%s",
        len(video_bytes),
        "yes" if audio_data_url else "no",
    )
    return FireworksPreparedVideo(
        video_data_url=f"data:video/mp4;base64,{base64.b64encode(video_bytes).decode('ascii')}",
        audio_data_url=audio_data_url,
    )


def _image_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _video_review_system_prompt() -> str:
    return (
        "You are Forma's video self-correction reviewer. You inspect generated hardware video evidence "
        "against the current HardwareIntermediateRepresentation. Find visual coherence, physical logic, assembly continuity, camera continuity, "
        "component placement, wiring, enclosure, and documentation issues. Return one valid JSON object only. "
        "Do not include prose, markdown, or hidden reasoning."
    )


def _video_review_user_content(
    current_ir: HardwareIntermediateRepresentation,
    *,
    video_url: str,
    frames: List[bytes],
    original_prompt: Optional[str],
    project_id: Optional[str],
) -> List[Dict[str, Any]]:
    compact_ir = compact_hardware_ir_for_iteration(current_ir, max_string_chars=1600)
    instructions = {
        "required_json_shape": {
            "summary": "string",
            "coherence_score": "number between 0 and 1",
            "needs_iteration": "boolean",
            "target_namespace": "one of product.overview, product.electrical, product.mech, product.validation, product.assembly, project.docs, project.history",
            "issues": [
                {
                    "severity": "critical|warning|info",
                    "category": "coherence|logic|continuity|rendering|assembly|mechanical|electrical",
                    "frame_reference": "frame/time reference",
                    "description": "specific issue",
                    "evidence": "what the frames show",
                    "suggested_correction": "project change",
                }
            ],
            "iteration_instruction": "single concise instruction for Forma's project iterator to revise the HardwareIntermediateRepresentation",
        },
        "review_rules": [
            "Do not invent invisible parts. Ground findings in the frames and HardwareIntermediateRepresentation.",
            "Prefer the smallest coherent project iteration.",
            "If the video is mostly correct, write an iteration that records the review and fixes minor documentation/continuity issues.",
            "Mention video continuity evidence in the iteration_instruction.",
        ],
    }
    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Project id: {project_id or (current_ir.assembly_metadata or {}).get('project_id') or 'unknown'}\n"
                f"Original prompt: {original_prompt or 'unknown'}\n"
                f"Video URL: {video_url}\n"
                f"Current HardwareIntermediateRepresentation JSON:\n{json.dumps(compact_ir, indent=2, sort_keys=True)}\n\n"
                f"Instructions:\n{json.dumps(instructions, indent=2, sort_keys=True)}"
            ),
        }
    ]
    for index, frame in enumerate(frames, start=1):
        content.append({"type": "text", "text": f"Chronological sampled frame {index} of {len(frames)}."})
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(frame)}})
    return content


def _video_review_instructions() -> Dict[str, Any]:
    return {
        "required_json_shape": {
            "summary": "string",
            "coherence_score": "number between 0 and 1",
            "needs_iteration": "boolean",
            "target_namespace": "one of product.overview, product.electrical, product.mech, product.validation, product.assembly, project.docs, project.history",
            "issues": [
                {
                    "severity": "critical|warning|info",
                    "category": "coherence|logic|continuity|rendering|assembly|mechanical|electrical",
                    "frame_reference": "frame/time reference",
                    "description": "specific issue",
                    "evidence": "what the video shows",
                    "suggested_correction": "project change",
                }
            ],
            "iteration_instruction": "single concise instruction for Forma's project iterator to revise the HardwareIntermediateRepresentation",
        },
        "review_rules": [
            "Do not invent invisible parts. Ground findings in the video/audio and HardwareIntermediateRepresentation.",
            "Prefer the smallest coherent project iteration.",
            "If the video is mostly correct, write an iteration that records the review and fixes minor documentation/continuity issues.",
            "Mention video continuity evidence in the iteration_instruction.",
        ],
    }


def _video_review_text_prompt(
    current_ir: HardwareIntermediateRepresentation,
    *,
    video_url: str,
    original_prompt: Optional[str],
    project_id: Optional[str],
) -> str:
    compact_ir = compact_hardware_ir_for_iteration(current_ir, max_string_chars=1600)
    return (
        f"Project id: {project_id or (current_ir.assembly_metadata or {}).get('project_id') or 'unknown'}\n"
        f"Original prompt: {original_prompt or 'unknown'}\n"
        f"Video URL: {video_url}\n"
        f"Current HardwareIntermediateRepresentation JSON:\n{json.dumps(compact_ir, indent=2, sort_keys=True)}\n\n"
        f"Instructions:\n{json.dumps(_video_review_instructions(), indent=2, sort_keys=True)}"
    )


def _video_review_native_user_content(
    current_ir: HardwareIntermediateRepresentation,
    *,
    video_url: str,
    prepared_video: FireworksPreparedVideo,
    original_prompt: Optional[str],
    project_id: Optional[str],
) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [
        {"type": "video_url", "video_url": {"url": prepared_video.video_data_url}},
    ]
    if prepared_video.audio_data_url:
        content.append({"type": "audio_url", "audio_url": {"url": prepared_video.audio_data_url}})
    content.append(
        {
            "type": "text",
            "text": _video_review_text_prompt(
                current_ir,
                video_url=video_url,
                original_prompt=original_prompt,
                project_id=project_id,
            ),
        }
    )
    return content


class FireworksVideoReviewClient:
    """Fireworks-hosted VLM reviewer for generated project videos."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        account_id: Optional[str] = None,
        deployment_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_frames: Optional[int] = None,
        input_mode: Optional[str] = None,
        include_audio: Optional[bool] = None,
        max_seconds: Optional[int] = None,
        native_fps: Optional[float] = None,
        native_height: Optional[int] = None,
        max_media_bytes: Optional[int] = None,
    ) -> None:
        self.api_key = api_key or _env("FIREWORKS_API_KEY") or _env("FIREWORKS_AI_API_KEY")
        self.base_url = (base_url or _env("FIREWORKS_BASE_URL", DEFAULT_FIREWORKS_BASE_URL) or DEFAULT_FIREWORKS_BASE_URL).rstrip("/")
        self.account_id = account_id or _env("FIREWORKS_ACCOUNT_ID") or _env("FIREWORKS_VIDEO_REVIEW_ACCOUNT_ID")
        self.deployment_id = deployment_id or _env("FIREWORKS_VIDEO_REVIEW_DEPLOYMENT_ID") or _env("FIREWORKS_DEPLOYMENT_ID")
        self.timeout_seconds = timeout_seconds or _env_float("FIREWORKS_TIMEOUT_SECONDS", DEFAULT_FIREWORKS_TIMEOUT_SECONDS)
        self.max_frames = max_frames or _env_int("FIREWORKS_VIDEO_REVIEW_MAX_FRAMES", DEFAULT_VIDEO_REVIEW_MAX_FRAMES, 1, 16)
        self.input_mode = (input_mode or _env("FIREWORKS_VIDEO_REVIEW_INPUT_MODE", DEFAULT_VIDEO_REVIEW_INPUT_MODE) or DEFAULT_VIDEO_REVIEW_INPUT_MODE).strip().lower().replace("-", "_")
        if self.input_mode not in {"auto", "native_video", "frames"}:
            self.input_mode = DEFAULT_VIDEO_REVIEW_INPUT_MODE
        requested_model = model or _env("FIREWORKS_VIDEO_REVIEW_MODEL")
        explicit_model_is_native = bool(requested_model) and (
            "#" in str(requested_model) or _fireworks_video_model_requires_deployment(str(requested_model))
        )
        has_native_deployment = bool(self.account_id and self.deployment_id)
        if self.input_mode == "auto":
            if explicit_model_is_native or (has_native_deployment and not requested_model):
                self.input_mode = "native_video"
                requested_model = requested_model or DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL
            else:
                self.input_mode = "frames"
                requested_model = requested_model or DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL
        elif self.input_mode == "native_video":
            requested_model = requested_model or DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL
        else:
            requested_model = requested_model or DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL
        self.model = _resolve_fireworks_video_review_model(
            requested_model,
            account_id=self.account_id if self.input_mode == "native_video" else None,
            deployment_id=self.deployment_id if self.input_mode == "native_video" else None,
        )
        self.include_audio = _fireworks_video_model_supports_audio(self.model) if include_audio is None else include_audio
        if _env("FIREWORKS_VIDEO_REVIEW_INCLUDE_AUDIO") is not None:
            self.include_audio = _env_bool("FIREWORKS_VIDEO_REVIEW_INCLUDE_AUDIO", self.include_audio)
        self.max_seconds = max_seconds or _env_int("FIREWORKS_VIDEO_REVIEW_MAX_SECONDS", DEFAULT_VIDEO_REVIEW_MAX_SECONDS, 1, 300)
        self.native_fps = native_fps or _env_float("FIREWORKS_VIDEO_REVIEW_NATIVE_FPS", DEFAULT_VIDEO_REVIEW_NATIVE_FPS)
        self.native_height = native_height or _env_int("FIREWORKS_VIDEO_REVIEW_NATIVE_HEIGHT", DEFAULT_VIDEO_REVIEW_NATIVE_HEIGHT, 144, 1080)
        self.max_media_bytes = max_media_bytes or _env_int("FIREWORKS_VIDEO_REVIEW_MAX_MEDIA_BYTES", DEFAULT_VIDEO_REVIEW_MAX_MEDIA_BYTES, 100_000, 50_000_000)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def deployment_configured(self) -> bool:
        return "#" in self.model or not _fireworks_video_model_requires_deployment(self.model)

    def get_debug_config(self) -> Dict[str, Any]:
        reason = None
        if not self.configured:
            reason = "Set FIREWORKS_API_KEY to enable video self-correction."
        elif self.input_mode == "native_video" and not self.deployment_configured:
            reason = "Fireworks native video models require a dedicated deployment. Set FIREWORKS_ACCOUNT_ID and FIREWORKS_VIDEO_REVIEW_DEPLOYMENT_ID."
        return {
            "provider": "fireworks",
            "model": self.model,
            "configured": self.configured,
            "reason": reason,
            "base_url": self.base_url,
            "max_frames": self.max_frames,
            "input_mode": self.input_mode,
            "include_audio": self.include_audio,
            "deployment_configured": self.deployment_configured,
            "available_native_models": list(FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS),
            "available_frame_models": list(FIREWORKS_VIDEO_REVIEW_FRAME_MODELS),
        }

    def review_video(
        self,
        current_ir: HardwareIntermediateRepresentation,
        *,
        video_url: str,
        original_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> VideoIterationReview:
        if not self.api_key:
            logger.warning("Fireworks video review is not configured; set FIREWORKS_API_KEY.")
            raise LLMProviderConfigError("Set FIREWORKS_API_KEY to enable video self-correction.")
        url = _validate_http_video_url(video_url)
        logger.info(
            "Starting Fireworks video review: project_id=%s model=%s input_mode=%s max_frames=%s",
            project_id or (current_ir.assembly_metadata or {}).get("project_id") or "unknown",
            self.model,
            self.input_mode,
            self.max_frames,
        )
        if self.input_mode == "native_video" and not self.deployment_configured:
            logger.warning(
                "Fireworks native video review is using a base model path without a deployment suffix: model=%s. "
                "Set FIREWORKS_ACCOUNT_ID and FIREWORKS_VIDEO_REVIEW_DEPLOYMENT_ID or provide a full FIREWORKS_VIDEO_REVIEW_MODEL path.",
                self.model,
            )
        if self.input_mode == "native_video":
            prepared_video = prepare_video_for_fireworks_native_review(
                url,
                include_audio=self.include_audio,
                max_seconds=self.max_seconds,
                fps=self.native_fps,
                height=self.native_height,
                max_media_bytes=self.max_media_bytes,
            )
            user_content = _video_review_native_user_content(
                current_ir,
                video_url=url,
                prepared_video=prepared_video,
                original_prompt=original_prompt,
                project_id=project_id,
            )
            input_count = 1 + int(bool(prepared_video.audio_data_url))
        else:
            frames = sample_video_frames(url, max_frames=self.max_frames)
            user_content = _video_review_user_content(
                current_ir,
                video_url=url,
                frames=frames,
                original_prompt=original_prompt,
                project_id=project_id,
            )
            input_count = len(frames)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _video_review_system_prompt()},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "max_tokens": 1800,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Forma-OSS/0.1",
            },
            method="POST",
        )
        try:
            logger.info(
                "Submitting Fireworks video review: project_id=%s model=%s input_mode=%s inputs=%s",
                project_id or (current_ir.assembly_metadata or {}).get("project_id") or "unknown",
                self.model,
                self.input_mode,
                input_count,
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = _fireworks_http_error_message(status_code=exc.code, body=body, model=self.model)
            logger.error(message)
            raise LLMProviderOutputError(
                message
            ) from exc
        except Exception as exc:
            message = f"forma_core.video_review Fireworks video review failed for model {self.model}: {exc}"
            logger.exception(message)
            raise LLMProviderOutputError(message) from exc

        try:
            message_payload = response_payload["choices"][0]["message"]
            content = _message_content_text(message_payload)
            parsed = _extract_json_document(content)
            review = VideoIterationReview.model_validate(parsed)
            logger.info(
                "Completed Fireworks video review: project_id=%s model=%s coherence_score=%.3f issue_count=%s target_namespace=%s",
                project_id or (current_ir.assembly_metadata or {}).get("project_id") or "unknown",
                self.model,
                review.coherence_score,
                len(review.issues),
                review.target_namespace,
            )
            return review
        except Exception as exc:
            try:
                message_payload = response_payload["choices"][0].get("message") or {}
            except Exception:
                message_payload = {}
            content = _message_content_text(message_payload) if isinstance(message_payload, dict) else ""
            fallback_review = _review_from_unstructured_text(content, model=self.model)
            if fallback_review:
                logger.warning(
                    "forma_core.video_review Fireworks video review returned unstructured text; using fallback review. "
                    "message_keys=%s finding_preview=%s parse_error=%s",
                    sorted(message_payload.keys()) if isinstance(message_payload, dict) else [],
                    _response_preview(fallback_review.issues[0].evidence if fallback_review.issues else fallback_review.summary),
                    exc,
                )
                return fallback_review
            message = (
                f"forma_core.video_review Fireworks video review returned unusable JSON: {exc}; "
                f"message_keys={sorted(message_payload.keys()) if isinstance(message_payload, dict) else []}; "
                f"content_preview={_response_preview(content)}"
            )
            logger.error(message)
            raise LLMProviderOutputError(message) from exc


__all__ = [
    "DEFAULT_FIREWORKS_FRAME_REVIEW_MODEL_SLUG",
    "DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL",
    "DEFAULT_FIREWORKS_NATIVE_VIDEO_REVIEW_MODEL_SLUG",
    "DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL",
    "DEFAULT_FIREWORKS_VIDEO_REVIEW_MODEL_SLUG",
    "FIREWORKS_VIDEO_REVIEW_FRAME_MODELS",
    "FIREWORKS_VIDEO_REVIEW_NATIVE_MODELS",
    "FireworksVideoReviewClient",
    "FireworksPreparedVideo",
    "VideoCoherenceIssue",
    "VideoIterationReview",
    "prepare_video_for_fireworks_native_review",
    "sample_video_frames",
]
