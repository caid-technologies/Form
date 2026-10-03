#!/usr/bin/env python3
"""Create Forma project objects with Ollama, Runpod, Baseten, GMI, and Hugging Face, synchronously."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from forma_core.config import config  # noqa: E402

DEFAULT_PROMPT = (
    "Design a compact blue desktop environmental monitor with an OLED display, "
    "temperature and humidity sensing, USB-C power, and optional battery operation."
)
DEFAULT_OUTPUT_DIR = "examples/results"
DEFAULT_ENV_FILE = ".env"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_OLLAMA_MODEL = "qwen3:0.6b"
DEFAULT_RUNPOD_MODEL = "caid-technologies/parti-base"
DEFAULT_BASETEN_BASE_URL = "https://inference.baseten.co/v1"
DEFAULT_BASETEN_GLM_MODEL = "zai-org/GLM-5.2"
DEFAULT_GMI_BASE_URL = "https://api.gmi-serving.com/v1"
DEFAULT_GMI_FABLE_MODEL = "anthropic/claude-fable-5"
DEFAULT_HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"
DEFAULT_HUGGINGFACE_QWEN_MODEL = "Qwen/Qwen2.5-Coder-3B-Instruct:nscale"
SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|rpa_[A-Za-z0-9_-]{8,}|nvapi-[A-Za-z0-9_-]{8,}|"
    r"[A-Za-z0-9]{8}\.[A-Za-z0-9._-]{16,})"
)


@dataclass(frozen=True)
class EnvSetting:
    name: str
    value: str

    def apply(self) -> None:
        config.set(self.name, self.value)


@dataclass(frozen=True)
class ProviderJob:
    label: str
    provider: str
    model: str
    settings: tuple[EnvSetting, ...]

    @property
    def selector(self) -> str:
        return f"{self.label}/{self.model}"


@dataclass
class ProjectObjectRunResult:
    job: ProviderJob
    status: str
    duration_seconds: float
    generate_image: bool = False
    object_path: Optional[Path] = None
    hardware_ir_path: Optional[Path] = None
    object_id: Optional[str] = None
    version: Optional[int] = None
    title: Optional[str] = None
    is_valid: Optional[bool] = None
    pipeline: Optional[str] = None
    runtime_provider: Optional[str] = None
    runtime_model: Optional[str] = None
    namespace_names: tuple[str, ...] = ()
    image_output_requested: Optional[bool] = None
    image_output_enabled: Optional[bool] = None
    image_output_configured: Optional[bool] = None
    image_output_status: Optional[str] = None
    image_output_error_type: Optional[str] = None
    image_output_error: Optional[str] = None
    image_output_provider: Optional[str] = None
    image_output_model: Optional[str] = None
    image_output_generated_count: Optional[int] = None
    has_product_image: bool = False
    product_image_url: Optional[str] = None
    operation_summary: Optional[dict[str, Any]] = None
    operation_statuses: tuple[dict[str, Any], ...] = ()
    error_type: Optional[str] = None
    error: Optional[str] = None
    traceback_text: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_json_object(self) -> dict[str, Any]:
        return {
            "provider": self.job.label,
            "runtime_provider": self.runtime_provider,
            "model": self.job.model,
            "runtime_model": self.runtime_model,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "generate_image": self.generate_image,
            "object_id": self.object_id,
            "version": self.version,
            "title": self.title,
            "is_valid": self.is_valid,
            "pipeline": self.pipeline,
            "namespaces": list(self.namespace_names),
            "image_output_requested": self.image_output_requested,
            "image_output_enabled": self.image_output_enabled,
            "image_output_configured": self.image_output_configured,
            "image_output_status": self.image_output_status,
            "image_output_error_type": self.image_output_error_type,
            "image_output_error": self.image_output_error,
            "image_output_provider": self.image_output_provider,
            "image_output_model": self.image_output_model,
            "image_output_generated_count": self.image_output_generated_count,
            "has_product_image": self.has_product_image,
            "product_image_url": self.product_image_url,
            "operation_summary": self.operation_summary,
            "operation_statuses": list(self.operation_statuses),
            "object_path": str(self.object_path) if self.object_path else None,
            "hardware_ir_path": str(self.hardware_ir_path) if self.hardware_ir_path else None,
            "error_type": self.error_type,
            "error": self.error,
            "traceback": self.traceback_text,
        }


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def load_env_file(path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("python-dotenv is required. Install the repo requirements first.") from exc

    if not path.exists():
        raise RuntimeError(f"Env file not found: {path}")
    load_dotenv(path, override=True)


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        return SECRET_PATTERN.sub(lambda match: match.group(0)[:5] + "...redacted", value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items()}
    return value


def compact_error(error: BaseException, max_chars: int = 1600) -> str:
    message = str(error).strip() or error.__class__.__name__
    if len(message) <= max_chars:
        return message
    return message[:max_chars] + "...<truncated>"


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned or "project"


def common_settings(timeout_seconds: float, *, generate_image: bool = False) -> tuple[EnvSetting, ...]:
    timeout_value = str(timeout_seconds)
    return (
        EnvSetting("FORMA_DEV_MODE", "true"),
        EnvSetting("FORMA_DEPLOYMENT", "false"),
        EnvSetting("FORMA_DEPLOYMENT_MODE", "local"),
        EnvSetting("DEPLOYMENT", "false"),
        EnvSetting("DEPLOYMENT_MODE", "false"),
        EnvSetting("NEXT_PUBLIC_FORMA_DEPLOYMENT", "false"),
        EnvSetting("FORMA_DISABLE_GENERATION_FALLBACK", "true"),
        EnvSetting("FORMA_STRICT_GENERATION", "true"),
        EnvSetting("LLM_DISABLE_FALLBACK", "true"),
        EnvSetting("IMAGE_OUTPUT_ENABLED", "true" if generate_image else "false"),
        EnvSetting("LLM_TIMEOUT_SECONDS", timeout_value),
        EnvSetting("LLM_FALLBACK_MODEL", ""),
    )


def ollama_job(model: str, base_url: str, timeout_seconds: float, *, generate_image: bool = False) -> ProviderJob:
    allowed_models = ",".join(
        dict.fromkeys(
            [
                model,
                config.get("OLLAMA_FORMA_MODEL", ""),
                "qwen3:0.6b",
                "qwen3:8b",
                "qwen3-vl:8b",
                "gemma3:12b",
            ]
        )
    ).strip(",")
    return ProviderJob(
        label="ollama",
        provider="openai-compatible",
        model=model,
        settings=(
            *common_settings(timeout_seconds, generate_image=generate_image),
            EnvSetting("LLM_PROVIDER", "openai-compatible"),
            EnvSetting("LLM_ALLOWED_PROVIDERS", "openai-compatible"),
            EnvSetting("LLM_BASE_URL", base_url),
            EnvSetting("LLM_MODEL", model),
            EnvSetting("OPENAI_COMPATIBLE_ALLOWED_MODELS", allowed_models),
            EnvSetting("LLM_ALLOW_NO_API_KEY", "true"),
            EnvSetting("LLM_RESPONSE_FORMAT", "json_object"),
            EnvSetting("OPENAI_FALLBACK_MODEL", ""),
        ),
    )


def runpod_job(model: str, timeout_seconds: float, base_url: Optional[str], *, generate_image: bool = False) -> ProviderJob:
    extra_settings: list[EnvSetting] = []
    if base_url:
        extra_settings.append(EnvSetting("RUNPOD_OPENAI_BASE_URL", base_url))

    return ProviderJob(
        label="runpod",
        provider="runpod",
        model=model,
        settings=(
            *common_settings(timeout_seconds, generate_image=generate_image),
            EnvSetting("LLM_PROVIDER", "runpod"),
            EnvSetting("LLM_ALLOWED_PROVIDERS", "runpod"),
            EnvSetting("RUNPOD_ALLOWED_MODELS", f"{model},runpod-default"),
            EnvSetting("RUNPOD_OPENAI_MODEL", model),
            EnvSetting("RUNPOD_MODEL", model),
            EnvSetting("RUNPOD_RESPONSE_FORMAT", "json_object"),
            EnvSetting("RUNPOD_TIMEOUT_SECONDS", str(timeout_seconds)),
            EnvSetting("RUNPOD_PARTI_SEED_TIMEOUT_SECONDS", str(timeout_seconds)),
            EnvSetting("RUNPOD_FALLBACK_MODEL", ""),
            EnvSetting("RUNPOD_OPENAI_FALLBACK_MODEL", ""),
            *extra_settings,
        ),
    )


def baseten_job(
    model: str,
    timeout_seconds: float,
    base_url: str = DEFAULT_BASETEN_BASE_URL,
    *,
    generate_image: bool = False,
) -> ProviderJob:
    return ProviderJob(
        label="baseten",
        provider="baseten",
        model=model,
        settings=(
            *common_settings(timeout_seconds, generate_image=generate_image),
            EnvSetting("LLM_PROVIDER", "baseten"),
            EnvSetting("LLM_ALLOWED_PROVIDERS", "baseten"),
            EnvSetting("BASETEN_BASE_URL", base_url),
            EnvSetting("BASETEN_MODEL", model),
            EnvSetting("BASETEN_ALLOWED_MODELS", f"{model},deepseek-ai/DeepSeek-V4-Pro"),
            EnvSetting("BASETEN_RESPONSE_FORMAT", "json_object"),
            EnvSetting("BASETEN_TIMEOUT_SECONDS", str(timeout_seconds)),
            EnvSetting("BASETEN_FALLBACK_MODEL", ""),
        ),
    )


def gmi_job(
    model: str,
    timeout_seconds: float,
    base_url: str = DEFAULT_GMI_BASE_URL,
    *,
    generate_image: bool = False,
) -> ProviderJob:
    return ProviderJob(
        label="gmi",
        provider="gmi",
        model=model,
        settings=(
            *common_settings(timeout_seconds, generate_image=generate_image),
            EnvSetting("LLM_PROVIDER", "gmi"),
            EnvSetting("LLM_ALLOWED_PROVIDERS", "gmi"),
            EnvSetting("GMI_BASE_URL", base_url),
            EnvSetting("GMI_MODEL", model),
            EnvSetting("GMI_ALLOWED_MODELS", f"{model},anthropic/claude-fable-5"),
            EnvSetting("GMI_RESPONSE_FORMAT", "json_object"),
            EnvSetting("GMI_TIMEOUT_SECONDS", str(timeout_seconds)),
            EnvSetting("GMI_FALLBACK_MODEL", ""),
        ),
    )


def huggingface_job(
    model: str,
    timeout_seconds: float,
    base_url: str = DEFAULT_HUGGINGFACE_BASE_URL,
    *,
    generate_image: bool = False,
) -> ProviderJob:
    return ProviderJob(
        label="huggingface",
        provider="huggingface",
        model=model,
        settings=(
            *common_settings(timeout_seconds, generate_image=generate_image),
            EnvSetting("LLM_PROVIDER", "huggingface"),
            EnvSetting("LLM_ALLOWED_PROVIDERS", "huggingface"),
            EnvSetting("HUGGINGFACE_BASE_URL", base_url),
            EnvSetting("HUGGINGFACE_MODEL", model),
            EnvSetting("HUGGINGFACE_ALLOWED_MODELS", model),
            EnvSetting("HUGGINGFACE_RESPONSE_FORMAT", "json_object"),
            EnvSetting("HUGGINGFACE_TIMEOUT_SECONDS", str(timeout_seconds)),
            EnvSetting("HUGGINGFACE_FALLBACK_MODEL", ""),
            EnvSetting("HF_FALLBACK_MODEL", ""),
        ),
    )


def apply_job_environment(base_environment: dict[str, str], job: ProviderJob) -> None:
    config.replace(base_environment)
    for setting in job.settings:
        setting.apply()


def namespace_key_preview(project_object: Any) -> str:
    interesting = ("product.overview", "product.electrical", "product.mech", "project.docs")
    pieces: list[str] = []
    for namespace_name in interesting:
        namespace = project_object.get_namespace(namespace_name)
        if namespace is None:
            continue
        payload_keys = ", ".join(sorted(namespace.payload.keys())[:5])
        pieces.append(f"{namespace.name}@v{namespace.version} [{payload_keys}]")
    return "; ".join(pieces)


def generation_failure_reason(job: ProviderJob, metadata: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    pipeline = str(metadata.get("pipeline") or "")
    runtime_provider = metadata.get("runtime_provider") or metadata.get("llm_provider")
    runtime_model = metadata.get("runtime_model") or metadata.get("model_name")
    operation_summary = metadata.get("operation_summary")

    if "deterministic simulation" in pipeline.lower():
        return (
            "ProviderFallback",
            (
                "Generation fell back to deterministic simulation. "
                f"Requested {job.selector}, runtime={runtime_provider}/{runtime_model}, pipeline={pipeline!r}."
            ),
        )

    if metadata.get("fallback_mode") is True:
        return (
            "ProviderFallback",
            (
                "Generation used provider fallback mode. "
                f"Requested {job.selector}, runtime={runtime_provider}/{runtime_model}."
            ),
        )

    if job.provider == "runpod" and metadata.get("parti_seed_error"):
        return (
            "RunpodPartiSeedError",
            f"Runpod Parti seed request failed before deterministic repair: {metadata.get('parti_seed_error')}",
        )

    if isinstance(operation_summary, dict) and operation_summary.get("failed"):
        return (
            "OperationFailure",
            f"One or more generation operations failed: {operation_summary}",
        )

    if isinstance(runtime_provider, str) and runtime_provider and runtime_provider != job.provider:
        return (
            "ProviderMismatch",
            f"Requested provider {job.provider!r}, but runtime provider was {runtime_provider!r}.",
        )

    if isinstance(runtime_model, str) and runtime_model and runtime_model != job.model:
        return (
            "ModelMismatch",
            f"Requested model {job.model!r}, but runtime model was {runtime_model!r}.",
        )

    return None, None


def run_project_object_job(
    job: ProviderJob,
    *,
    prompt: str,
    output_dir: Path,
    run_id: str,
    base_environment: dict[str, str],
    print_object_json: bool,
    generate_image: bool = False,
) -> ProjectObjectRunResult:
    from apps.api.a2a import build_generation_response
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
    from forma_core.workspaces.projects.objects import attach_project_object_metadata, build_project_object

    apply_job_environment(base_environment, job)
    started = time.monotonic()
    print(f"[project-objects] starting {job.selector}", flush=True)

    try:
        response = build_generation_response(
            prompt,
            generate_image=generate_image,
            workflow="default",
            provider=job.provider,
            model=job.model,
        )
        hardware_ir = HardwareIntermediateRepresentation.model_validate(response["project_ir"])
        hardware_ir = attach_project_object_metadata(hardware_ir)
        project_object = build_project_object(hardware_ir)
        metadata = hardware_ir.assembly_metadata or {}
        overview = hardware_ir.overview
        operation_statuses = tuple(
            item for item in metadata.get("operation_statuses", [])
            if isinstance(item, dict)
        )
        image_output_generated_count = metadata.get("image_output_generated_count")
        if image_output_generated_count is not None:
            try:
                image_output_generated_count = int(image_output_generated_count)
            except (TypeError, ValueError):
                image_output_generated_count = None

        object_path = output_dir / f"{run_id}-{slug(job.selector)}.project-object.json"
        hardware_ir_path = output_dir / f"{run_id}-{slug(job.selector)}.hardware-ir.json"
        object_path.write_text(project_object.model_dump_json(indent=2), encoding="utf-8")
        hardware_ir_path.write_text(hardware_ir.model_dump_json(indent=2), encoding="utf-8")
        error_type, error_message = generation_failure_reason(job, metadata)
        status = "fail" if error_message else "pass"

        result = ProjectObjectRunResult(
            job=job,
            status=status,
            duration_seconds=round(time.monotonic() - started, 3),
            generate_image=generate_image,
            object_path=object_path,
            hardware_ir_path=hardware_ir_path,
            object_id=project_object.object_id,
            version=project_object.version,
            title=overview.title if overview else None,
            is_valid=hardware_ir.is_valid,
            pipeline=metadata.get("pipeline"),
            runtime_provider=metadata.get("runtime_provider") or metadata.get("llm_provider"),
            runtime_model=metadata.get("runtime_model") or metadata.get("model_name"),
            namespace_names=tuple(namespace.name for namespace in project_object.namespaces),
            image_output_requested=metadata.get("image_output_requested"),
            image_output_enabled=metadata.get("image_output_enabled"),
            image_output_configured=metadata.get("image_output_configured"),
            image_output_status=metadata.get("image_output_status"),
            image_output_error_type=metadata.get("image_output_error_type"),
            image_output_error=metadata.get("image_output_error") or metadata.get("product_image_error"),
            image_output_provider=metadata.get("image_output_provider") or metadata.get("product_image_provider"),
            image_output_model=metadata.get("image_output_model") or metadata.get("product_image_model"),
            image_output_generated_count=image_output_generated_count,
            has_product_image=bool(metadata.get("product_image_data") or metadata.get("product_image_url")),
            product_image_url=metadata.get("product_image_url"),
            operation_summary=metadata.get("operation_summary"),
            operation_statuses=operation_statuses,
            error_type=error_type,
            error=error_message,
        )

        status_label = "PASS" if result.ok else "FAIL"
        print(
            f"[project-objects] {status_label} {job.selector} "
            f"duration={result.duration_seconds:.1f}s object_id={result.object_id} "
            f"version={result.version} title={result.title!r}",
            flush=True,
        )
        print(f"[project-objects]      pipeline={result.pipeline}", flush=True)
        if result.image_output_requested is not None:
            print(
                f"[project-objects]      image status={result.image_output_status} "
                f"requested={result.image_output_requested} provider={result.image_output_provider} "
                f"model={result.image_output_model} generated={result.image_output_generated_count}",
                flush=True,
            )
        if result.image_output_error:
            print(f"[project-objects]      image_error={result.image_output_error}", flush=True)
        if result.error:
            print(f"[project-objects]      error={result.error}", flush=True)
        print(f"[project-objects]      namespaces={namespace_key_preview(project_object)}", flush=True)
        print(f"[project-objects]      saved={object_path}", flush=True)
        if print_object_json:
            print(project_object.model_dump_json(indent=2), flush=True)
        return result
    except Exception as exc:
        result = ProjectObjectRunResult(
            job=job,
            status="fail",
            duration_seconds=round(time.monotonic() - started, 3),
            generate_image=generate_image,
            error_type=exc.__class__.__name__,
            error=compact_error(exc),
            traceback_text=traceback.format_exc(limit=12),
        )
        print(
            f"[project-objects] FAIL {job.selector} "
            f"duration={result.duration_seconds:.1f}s error={result.error}",
            flush=True,
        )
        return result


def save_summary(results: list[ProjectObjectRunResult], *, output_dir: Path, run_id: str) -> Path:
    summary_path = output_dir / f"{run_id}-summary.json"
    latest_path = output_dir / "latest-project-objects-summary.json"
    payload = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": {
            "total": len(results),
            "passed": sum(1 for result in results if result.ok),
            "failed": sum(1 for result in results if not result.ok),
            "ok": all(result.ok for result in results),
        },
        "results": [result.to_json_object() for result in results],
    }
    safe_payload = scrub(payload)
    summary_path.write_text(json.dumps(safe_payload, indent=2) + "\n", encoding="utf-8")
    latest_path.write_text(json.dumps(safe_payload, indent=2) + "\n", encoding="utf-8")
    print(f"[project-objects] summary={summary_path}", flush=True)
    print(f"[project-objects] latest={latest_path}", flush=True)
    return summary_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create Forma project objects with Ollama, Runpod, Baseten, GMI, and Hugging Face.")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT, help="Project prompt to generate.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help=f"Dotenv file to load. Defaults to {DEFAULT_ENV_FILE}.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Directory for JSON output. Defaults to {DEFAULT_OUTPUT_DIR}.")
    parser.add_argument("--run-id", default=None, help="Stable run id for output filenames.")
    parser.add_argument("--only", action="append", choices=("ollama", "runpod", "baseten", "gmi", "huggingface"), help="Run only one provider. Can be repeated.")
    parser.add_argument("--ollama-model", default=config.get("OLLAMA_FORMA_MODEL", DEFAULT_OLLAMA_MODEL))
    parser.add_argument("--ollama-base-url", default=config.get("OLLAMA_OPENAI_BASE_URL", DEFAULT_OLLAMA_BASE_URL))
    parser.add_argument("--runpod-model", default=config.get("RUNPOD_FORMA_MODEL", DEFAULT_RUNPOD_MODEL))
    parser.add_argument("--runpod-base-url", default=config.get("RUNPOD_OPENAI_BASE_URL"), help="Optional Runpod OpenAI-compatible base URL override.")
    parser.add_argument("--baseten-model", default=config.get("BASETEN_FORMA_MODEL", DEFAULT_BASETEN_GLM_MODEL))
    parser.add_argument("--baseten-base-url", default=config.get("BASETEN_BASE_URL", DEFAULT_BASETEN_BASE_URL))
    parser.add_argument("--gmi-model", default=config.get("GMI_FORMA_MODEL", config.get("GMI_MODEL", DEFAULT_GMI_FABLE_MODEL)))
    parser.add_argument("--gmi-base-url", default=config.get("GMI_BASE_URL", DEFAULT_GMI_BASE_URL))
    parser.add_argument("--huggingface-model", default=config.get("HUGGINGFACE_FORMA_MODEL", DEFAULT_HUGGINGFACE_QWEN_MODEL))
    parser.add_argument("--huggingface-base-url", default=config.get("HUGGINGFACE_BASE_URL", DEFAULT_HUGGINGFACE_BASE_URL))
    parser.add_argument("--timeout-seconds", type=float, default=1200.0, help="Provider timeout for slow jobs.")
    parser.add_argument("--generate-image", action="store_true", help="Request product image generation for each project object.")
    parser.add_argument("--print-object-json", action="store_true", help="Also print full project objects to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_id = args.run_id or utc_run_id()
    output_dir = repo_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        load_env_file(repo_path(args.env_file))
    except Exception as exc:
        print(f"[project-objects] config error: {exc}", file=sys.stderr)
        return 2

    base_environment = config.snapshot()
    requested = set(args.only or ("ollama", "runpod", "baseten"))
    jobs: list[ProviderJob] = []
    if "ollama" in requested:
        jobs.append(ollama_job(args.ollama_model, args.ollama_base_url, args.timeout_seconds, generate_image=args.generate_image))
    if "runpod" in requested:
        jobs.append(runpod_job(args.runpod_model, args.timeout_seconds, args.runpod_base_url, generate_image=args.generate_image))
    if "baseten" in requested:
        jobs.append(baseten_job(args.baseten_model, args.timeout_seconds, args.baseten_base_url, generate_image=args.generate_image))
    if "gmi" in requested:
        jobs.append(gmi_job(args.gmi_model, args.timeout_seconds, args.gmi_base_url, generate_image=args.generate_image))
    if "huggingface" in requested:
        jobs.append(huggingface_job(args.huggingface_model, args.timeout_seconds, args.huggingface_base_url, generate_image=args.generate_image))

    results: list[ProjectObjectRunResult] = []
    for job in jobs:
        results.append(
            run_project_object_job(
                job,
                prompt=args.prompt,
                output_dir=output_dir,
                run_id=run_id,
                base_environment=base_environment,
                print_object_json=args.print_object_json,
                generate_image=args.generate_image,
            )
        )

    save_summary(results, output_dir=output_dir, run_id=run_id)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
