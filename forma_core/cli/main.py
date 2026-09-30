from __future__ import annotations

import argparse
import base64
import json
import mimetypes
from pathlib import Path
import sys
import tempfile
from typing import Any

from forma_core import __version__
from forma_core.config import config
from forma_core.selectors import split_llm_selector


CLI_LIVE_GENERATION_ENVIRONMENT = {
    "FORMA_DISABLE_GENERATION_FALLBACK": "true",
    "FORMA_STRICT_GENERATION": "true",
    "LLM_DISABLE_FALLBACK": "true",
    "STRICT_LLM": "true",
    "STRICT_ANTHROPIC": "true",
    "STRICT_CLAUDE": "true",
    "STRICT_BASETEN": "true",
    "STRICT_GEMINI": "true",
    "STRICT_GMI": "true",
    "STRICT_GMI_CLOUD": "true",
    "STRICT_GMICLOUD": "true",
    "STRICT_HUGGINGFACE": "true",
    "STRICT_HF": "true",
    "STRICT_CLOUDFLARE": "true",
    "STRICT_NVIDIA": "true",
    "STRICT_NVIDIA_NIM": "true",
    "STRICT_NIM": "true",
    "STRICT_OPENAI": "true",
    "STRICT_RUNPOD": "true",
}


def _cli_generation_environment(*, simulation: bool) -> dict[str, str]:
    if not simulation:
        return dict(CLI_LIVE_GENERATION_ENVIRONMENT)
    return {
        "FORMA_DISABLE_GENERATION_FALLBACK": "false",
        "FORMA_STRICT_GENERATION": "false",
        "LLM_DISABLE_FALLBACK": "false",
    }


def _provider_and_model(args: argparse.Namespace) -> tuple[str | None, str | None]:
    selected_provider, selected_model = split_llm_selector(args.llm)
    return args.provider or selected_provider, args.model or selected_model


def _read_json(path_value: str) -> Any:
    if path_value == "-":
        return json.load(sys.stdin)
    with Path(path_value).open(encoding="utf-8") as file:
        return json.load(file)


def _hardware_ir_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("project_ir", "hardware_ir"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    response = value.get("response")
    if isinstance(response, dict) and isinstance(response.get("project_ir"), dict):
        return response["project_ir"]
    return value


def _write_json(value: Any, output: str | None) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output in (None, "-"):
        sys.stdout.write(payload)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _image_payload(path_value: str | None) -> tuple[bytes | None, str | None]:
    if not path_value:
        return None, None
    path = Path(path_value)
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.read_bytes(), mime_type


def _write_data_url(data_url: str, output: Path) -> Path:
    if not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("Generated product image is not an inline data URL.")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Generated product image does not use base64 encoding.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(encoded))
    return output


def _reject_live_fallback_output(project: Any, *, simulation: bool) -> None:
    if simulation:
        return
    metadata = getattr(project, "assembly_metadata", None) or {}
    generation_error = metadata.get("generation_error")
    if metadata.get("status") == "failed" or generation_error:
        if isinstance(generation_error, dict):
            message = generation_error.get("message") or generation_error.get("type")
        else:
            message = generation_error
        raise RuntimeError(str(message or "Live generation failed."))
    if metadata.get("fallback_mode") or metadata.get("workflow_fallback"):
        raise RuntimeError(
            "Live generation returned fallback output; CLI fallback output is disabled. "
            "Retry with a working provider or pass --simulation explicitly."
        )


def cmd_workflows(args: argparse.Namespace) -> int:
    from forma_core.generation import list_workflows

    workflows = list_workflows()
    if args.json:
        _write_json(workflows, None)
    else:
        for workflow in workflows:
            print(f"{workflow['id']}: {workflow['label']} — {workflow['description']}")
    return 0


def cmd_namespaces(args: argparse.Namespace) -> int:
    from forma_core.generation import list_project_namespaces

    namespaces = [descriptor.__dict__ for descriptor in list_project_namespaces()]
    if args.json:
        _write_json(namespaces, None)
    else:
        for namespace in namespaces:
            print(f"{namespace['name']}: {namespace['label']} — {namespace['description']}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from forma_core.generation import generate_project_with_workflow
    from forma_core.terminal.images import TerminalImageRenderConfig, render_images
    from forma_core.workspaces.projects.output import (
        attach_product_image,
        attach_assembly_step,
        persist_project_output,
        primary_product_image_data,
    )

    provider, model = _provider_and_model(args)
    if args.simulation:
        provider, model = "simulation", None
    image_bytes, image_mime_type = _image_payload(args.image_file)
    with config.override({
        **_cli_generation_environment(simulation=args.simulation),
        "IMAGE_PROVIDER": args.image_provider,
        "IMAGE_MODEL": args.image_model,
        "GMI_IMAGE_MODEL": args.image_model,
        "OPENAI_IMAGE_MODEL": args.image_model,
        "TOGETHER_IMAGE_MODEL": args.image_model,
        "HUGGINGFACE_IMAGE_MODEL": args.image_model,
    }):
        project = generate_project_with_workflow(
            args.workflow,
            args.prompt,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            provider_name=provider,
            model_name=model,
            external_source_provider=args.external_source_provider,
        )
        _reject_live_fallback_output(project, simulation=args.simulation)
        if args.assembly_step:
            attach_assembly_step(project, args.assembly_step)
        attach_product_image(args.prompt, project, generate_image=args.generate_image)
        persist_project_output(project, prompt_text=args.prompt)

    if args.generate_image and (project.assembly_metadata or {}).get("image_output_status") != "succeeded":
        error = (project.assembly_metadata or {}).get("image_output_error") or "Image generation failed."
        raise RuntimeError(str(error))

    image_data = primary_product_image_data(project)
    image_output = Path(args.image_output).expanduser() if args.image_output else None
    temporary_image: tempfile.NamedTemporaryFile | None = None
    if (image_output or args.show_image) and not image_data:
        raise RuntimeError("The generated image was stored remotely and cannot be rendered as inline terminal data.")
    if image_data and image_output:
        _write_data_url(image_data, image_output)
    if image_data and args.show_image:
        render_path = image_output
        if render_path is None:
            temporary_image = tempfile.NamedTemporaryFile(suffix=".png")
            render_path = _write_data_url(image_data, Path(temporary_image.name))
        print(
            render_images(
                [render_path],
                TerminalImageRenderConfig(width=args.terminal_width, max_height=args.terminal_height),
            ),
            file=sys.stderr,
        )
        if temporary_image is not None:
            temporary_image.close()
    _write_json(project, args.output)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from forma_core.validation import build_validation_summary, validate_circuit
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

    project = HardwareIntermediateRepresentation.model_validate(_hardware_ir_payload(_read_json(args.project)))
    summary = build_validation_summary(validate_circuit(project.components, project.nets, project.requirements))
    result = {
        "is_valid": not summary.critical,
        "validation": summary.model_dump(mode="json"),
    }
    _write_json(result, args.output)
    return 0 if result["is_valid"] else 1


def cmd_iterate(args: argparse.Namespace) -> int:
    from forma_core.workspaces.projects.iteration import iterate_project
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

    provider, model = _provider_and_model(args)
    current_project = HardwareIntermediateRepresentation.model_validate(_hardware_ir_payload(_read_json(args.project)))
    with config.override(_cli_generation_environment(simulation=args.simulation)):
        revised_project = iterate_project(
            current_project,
            args.instruction,
            original_prompt=args.original_prompt,
            project_id=args.project_id,
            target_namespace=args.namespace,
            provider_name=provider,
            model_name=model,
            use_simulation=args.simulation,
            require_live_generation=not args.simulation,
        )
    _write_json(revised_project, args.output)
    return 0


def _add_runtime_selector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--llm", help="LLM selector in provider/model form, for example openai/gpt-5.6-sol.")
    parser.add_argument("--provider", help="LLM provider override.")
    parser.add_argument("--model", help="LLM model override.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forma-core",
        description="Run Forma Core operations directly, without a backend server.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    workflows = subparsers.add_parser("workflows", help="List available generation workflows.")
    workflows.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    workflows.set_defaults(func=cmd_workflows)

    namespaces = subparsers.add_parser("namespaces", help="List project object namespaces.")
    namespaces.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    namespaces.set_defaults(func=cmd_namespaces)

    generate = subparsers.add_parser("generate", help="Generate a HardwareIntermediateRepresentation directly through Forma Core.")
    generate.add_argument("prompt", help="Hardware idea to generate.")
    generate.add_argument("--workflow", default="default", choices=("default", "web_research"))
    generate.add_argument("--external-source-provider", choices=("firecrawl",))
    generate.add_argument("--image-file", help="Optional reference image.")
    generate.add_argument("--generate-image", action="store_true", help="Generate and persist product imagery in the same run.")
    generate.add_argument("--image-provider", help="Image provider override, for example gmi.")
    generate.add_argument("--image-model", help="Image model override.")
    generate.add_argument("--image-output", help="Also export the primary generated image to this path.")
    generate.add_argument("--show-image", action="store_true", help="Render the generated product image in the terminal.")
    generate.add_argument("--terminal-width", type=int, help="Maximum terminal columns for --show-image.")
    generate.add_argument("--terminal-height", type=int, default=40, help="Maximum terminal rows for --show-image.")
    generate.add_argument("--assembly-step", help="Attach a validated assembly.step or assembled.step artifact before persistence.")
    generate.add_argument("--simulation", action="store_true", help="Use the deterministic built-in generator.")
    generate.add_argument("--output", help="Write HardwareIntermediateRepresentation JSON to this path; defaults to stdout.")
    _add_runtime_selector_arguments(generate)
    generate.set_defaults(func=cmd_generate)

    validate = subparsers.add_parser("validate", help="Validate a HardwareIntermediateRepresentation JSON document.")
    validate.add_argument("project", help="HardwareIntermediateRepresentation JSON path, or - for stdin.")
    validate.add_argument("--output", help="Write validation JSON to this path; defaults to stdout.")
    validate.set_defaults(func=cmd_validate)

    iterate = subparsers.add_parser("iterate", help="Revise a HardwareIntermediateRepresentation directly through Forma Core.")
    iterate.add_argument("project", help="HardwareIntermediateRepresentation JSON path, or - for stdin.")
    iterate.add_argument("instruction", help="Natural-language revision instruction.")
    iterate.add_argument("--namespace", help="Target namespace, for example product.mech.")
    iterate.add_argument("--original-prompt")
    iterate.add_argument("--project-id")
    iterate.add_argument("--simulation", action="store_true", help="Apply a metadata-only simulated iteration.")
    iterate.add_argument("--output", help="Write revised HardwareIntermediateRepresentation JSON to this path; defaults to stdout.")
    _add_runtime_selector_arguments(iterate)
    iterate.set_defaults(func=cmd_iterate)

    from forma_core.cad_migrations.cli import register_parser as register_cad_migrations
    register_cad_migrations(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
