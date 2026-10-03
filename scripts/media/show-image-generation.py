#!/usr/bin/env python3
"""Generate a product image from a Forma Hardware Intermediate Representation and show it in the terminal."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from forma_core.config import config
from forma_core.image_providers import GeneratedImage, build_image_provider
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.terminal.images import TerminalImageRenderConfig, render_images


DEFAULT_OUTPUT_DIR = ROOT_DIR / ".logs" / "image-generation"
DEFAULT_PROMPT = "Blue Sentinel USB-C desktop environmental monitor with ESP32-S3, OLED display, I2C air sensors, airflow module, and blue printable enclosure."


@dataclass(frozen=True)
class SavedGeneratedImage:
    path: Path
    metadata_path: Path
    generated: GeneratedImage
    elapsed_seconds: float


class ImageGenerationScriptError(RuntimeError):
    pass


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_project_ir_from_json(path: Path) -> HardwareIntermediateRepresentation:
    if not path.exists():
        raise ImageGenerationScriptError(f"Input JSON not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ImageGenerationScriptError("Input JSON must contain an object.")
    project_ir = payload.get("project_ir") if isinstance(payload.get("project_ir"), dict) else payload
    try:
        return HardwareIntermediateRepresentation.model_validate(project_ir)
    except Exception as exc:
        raise ImageGenerationScriptError(f"Input JSON did not contain a valid HardwareIntermediateRepresentation: {exc}") from exc


def load_example_ir(example: str) -> HardwareIntermediateRepresentation:
    filename = example if example.endswith(".json") else f"{example}.json"
    return load_project_ir_from_json(ROOT_DIR / "apps" / "web" / "public" / "examples" / filename)


def suffix_for_content_type(content_type: str, fallback_format: str) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized == "image/jpeg":
        return ".jpg"
    if normalized == "image/svg+xml":
        return ".svg"
    if normalized.startswith("image/"):
        suffix = normalized.removeprefix("image/").replace("+xml", "")
        return f".{suffix or fallback_format or 'png'}"
    return f".{(fallback_format or 'png').lstrip('.')}"


def image_bytes_from_data_url_or_url(value: str) -> tuple[bytes, str]:
    image_data = value.strip()
    if not image_data:
        raise ImageGenerationScriptError("Generated image data was empty.")
    if image_data.startswith(("http://", "https://")):
        request = urllib.request.Request(image_data, headers={"User-Agent": "Forma-OSS/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read(), response.headers.get_content_type() or "image/png"
    if "," in image_data:
        header, encoded = image_data.split(",", 1)
        content_type = "image/png"
        if header.startswith("data:") and ";base64" in header:
            content_type = header.removeprefix("data:").split(";", 1)[0] or content_type
        return base64.b64decode(encoded.strip()), content_type
    return base64.b64decode(image_data), "image/png"


def configure_image_env(args: argparse.Namespace) -> None:
    if args.provider:
        config.set("IMAGE_PROVIDER", args.provider)
    if args.model:
        config.set("OPENAI_IMAGE_MODEL", args.model)
        config.set("IMAGE_MODEL", args.model)
    if args.size:
        config.set("OPENAI_IMAGE_SIZE", args.size)
        config.set("IMAGE_SIZE", args.size)
    if args.output_format:
        config.set("OPENAI_IMAGE_OUTPUT_FORMAT", args.output_format)
        config.set("IMAGE_OUTPUT_FORMAT", args.output_format)
    if args.quality:
        config.set("OPENAI_IMAGE_QUALITY", args.quality)
        config.set("IMAGE_QUALITY", args.quality)
    if args.timeout_seconds:
        config.set("OPENAI_IMAGE_TIMEOUT_SECONDS", str(args.timeout_seconds))
        config.set("IMAGE_TIMEOUT_SECONDS", str(args.timeout_seconds))


def generate_images(prompt: str, ir: HardwareIntermediateRepresentation, *, sequence: bool) -> list[tuple[GeneratedImage, float]]:
    provider = build_image_provider(force_enabled=True)
    config = provider.get_debug_config()
    print(
        "[image-generation] "
        f"provider={config.get('provider')} model={config.get('model_name')} "
        f"configured={config.get('configured')} size={config.get('size')} format={config.get('output_format')}",
        flush=True,
    )
    if not config.get("configured"):
        raise ImageGenerationScriptError(config.get("reason") or "Image provider is not configured.")

    if sequence:
        started = time.perf_counter()
        generated = provider.generate_project_image_sequence(prompt, ir)
        elapsed = time.perf_counter() - started
        per_image = elapsed / max(1, len(generated))
        return [(image, per_image) for image in generated]

    started = time.perf_counter()
    image = provider.generate_project_image(prompt, ir)
    elapsed = time.perf_counter() - started
    if image is None:
        return []
    return [(image, elapsed)]


def save_generated_image(image: GeneratedImage, *, output_dir: Path, run_id: str, index: int, elapsed_seconds: float) -> SavedGeneratedImage:
    raw_bytes, content_type = image_bytes_from_data_url_or_url(image.data_url)
    suffix = suffix_for_content_type(content_type, image.output_format)
    view_id = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in (image.view_id or f"image-{index}"))
    image_path = output_dir / f"{run_id}-{index:02d}-{view_id}{suffix}"
    metadata_path = output_dir / f"{run_id}-{index:02d}-{view_id}.json"
    image_path.write_bytes(raw_bytes)
    metadata = {
        "provider": image.provider,
        "model": image.model,
        "size": image.size,
        "output_format": image.output_format,
        "content_type": content_type,
        "view_id": image.view_id,
        "label": image.label,
        "reference_view_id": image.reference_view_id,
        "elapsed_seconds": elapsed_seconds,
        "prompt_original_length": image.prompt_original_length,
        "prompt_final_length": image.prompt_final_length,
        "prompt_compacted": image.prompt_compacted,
        "prompt_compaction_strategy": image.prompt_compaction_strategy,
        "prompt": image.prompt,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SavedGeneratedImage(path=image_path, metadata_path=metadata_path, generated=image, elapsed_seconds=elapsed_seconds)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input-json", type=Path, default=None, help="Generated response JSON or raw HardwareIntermediateRepresentation JSON.")
    source.add_argument("--example", default="plant_watering", help="Frontend example name. Defaults to plant_watering.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default=None, help="Image model override, e.g. gpt-image-2.")
    parser.add_argument("--size", default=None, help="Image size override, e.g. 1024x1024.")
    parser.add_argument("--output-format", default=None, choices=("png", "jpeg", "jpg", "webp"))
    parser.add_argument("--quality", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--sequence", action="store_true", help="Generate the full product visual sequence instead of one image.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-terminal-image", action="store_true")
    parser.add_argument("--terminal-width", type=int, default=80)
    parser.add_argument("--terminal-max-height", type=int, default=32)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    configure_image_env(args)

    try:
        ir = load_project_ir_from_json(args.input_json.expanduser().resolve()) if args.input_json else load_example_ir(args.example)
        run_id = utc_run_id()
        prompt = args.prompt.strip() or DEFAULT_PROMPT
        print(f"[image-generation] title={ir.overview.title if ir.overview else 'Untitled'}", flush=True)
        print(f"[image-generation] prompt={prompt!r}", flush=True)
        generated = generate_images(prompt, ir, sequence=bool(args.sequence))
        if not generated:
            raise ImageGenerationScriptError("Image provider returned no images.")

        saved = [
            save_generated_image(image, output_dir=output_dir, run_id=run_id, index=index, elapsed_seconds=elapsed)
            for index, (image, elapsed) in enumerate(generated, start=1)
        ]
        for item in saved:
            print(
                "[image-generation] "
                f"saved={item.path} metadata={item.metadata_path} "
                f"label={item.generated.label!r} duration={item.elapsed_seconds:.1f}s",
                flush=True,
            )

        previewable = [item.path for item in saved if item.path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}]
        if previewable and not args.no_terminal_image:
            config = TerminalImageRenderConfig(width=args.terminal_width, max_height=args.terminal_max_height)
            print(render_images([previewable[0]], config), flush=True)
        print(f"[image-generation] done first_image={saved[0].path}", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\n[image-generation] interrupted", file=sys.stderr)
        return 130
    except (ImageGenerationScriptError, OSError, RuntimeError, ValueError) as exc:
        print(f"[image-generation] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
