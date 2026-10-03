#!/usr/bin/env python3
"""Small MCP JSON-RPC client for the portable Forma Agent Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"


class FormaClientError(RuntimeError):
    pass


def _url(value: str | None) -> str:
    raw = (value or os.environ.get("FORMA_MCP_URL") or DEFAULT_MCP_URL).strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FormaClientError("FORMA_MCP_URL must be an http:// or https:// URL.")
    if parsed.path in {"", "/"}:
        parsed = parsed._replace(path="/mcp")
    return urlunparse(parsed)


def _timeout(value: float | None) -> float:
    raw: float | str = value if value is not None else os.environ.get("FORMA_TIMEOUT_SECONDS", "600")
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise FormaClientError("FORMA_TIMEOUT_SECONDS must be a number.") from exc
    if timeout <= 0:
        raise FormaClientError("Timeout must be greater than zero.")
    return timeout


def request(method: str, params: dict[str, Any] | None = None, *, url: str | None = None, timeout: float | None = None) -> Any:
    target_url = _url(url)
    payload = {
        "jsonrpc": "2.0",
        "id": f"forma-skill-{uuid.uuid4().hex}",
        "method": method,
        "params": params or {},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "Forma-Agent-Skill/1.0",
    }
    token = os.environ.get("FORMA_AUTH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    http_request = Request(target_url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urlopen(http_request, timeout=_timeout(timeout)) as response:
            result = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise FormaClientError(f"Forma returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FormaClientError(
            f"Could not reach Forma at {target_url}: {exc.reason}. "
            "Run ./scripts/development/dev.sh (or .\\scripts\\development\\dev.ps1 on Windows) "
            "from a Forma checkout, or set FORMA_MCP_URL to a hosted /api/mcp endpoint."
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormaClientError("Forma returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise FormaClientError("Forma returned an invalid JSON-RPC response.")
    if result.get("error"):
        error = result["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise FormaClientError(f"Forma JSON-RPC error: {message}")
    return result.get("result")


def call_tool(name: str, arguments: dict[str, Any], **options: Any) -> Any:
    result = request("tools/call", {"name": name, "arguments": arguments}, **options)
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    return result


def _load(path_value: str) -> dict[str, Any]:
    value = json.load(sys.stdin) if path_value == "-" else json.loads(Path(path_value).read_text())
    if not isinstance(value, dict):
        raise FormaClientError("Project JSON must contain an object.")
    for key in ("project_ir", "hardware_ir"):
        if isinstance(value.get(key), dict):
            return value[key]
    return value


def _write(value: Any, output: str | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if not output or output == "-":
        sys.stdout.write(rendered)
    else:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        print(path)


def _update_project_manifest(project_path: str, compiled: Any) -> None:
    """Replace a local draft manifest with the compiler's canonical IR."""
    if project_path == "-":
        raise FormaClientError("--update-project requires a file-backed project, not stdin.")

    compiled_ir = compiled.get("project_ir") if isinstance(compiled, dict) else None
    if not isinstance(compiled_ir, dict):
        raise FormaClientError("Forma did not return a compiled project IR.")

    path = Path(project_path)
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormaClientError(f"Could not read the local project manifest: {path}") from exc

    metadata = compiled_ir.get("assembly_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if isinstance(original, dict) and (
        "project_ir" in original or "hardware_ir" in original or original.get("format") == "forma-project"
    ):
        manifest = dict(original)
    else:
        manifest = {"format": "forma-project", "version": 1}

    manifest["format"] = str(manifest.get("format") or "forma-project")
    manifest["version"] = int(manifest.get("version") or 1)
    manifest["project_ir"] = compiled_ir
    manifest["project_id"] = str(
        compiled.get("project_id") or metadata.get("project_id") or manifest.get("project_id") or ""
    ).strip()
    if not manifest["project_id"]:
        raise FormaClientError("Forma did not return a project ID for the local manifest.")

    overview = compiled_ir.get("overview")
    overview = overview if isinstance(overview, dict) else {}
    if not str(manifest.get("title") or "").strip():
        manifest["title"] = str(overview.get("title") or "Untitled Forma Project")
    if not str(manifest.get("prompt") or "").strip():
        manifest["prompt"] = str(metadata.get("source_prompt") or metadata.get("project_prompt") or "")

    generated_artifacts = _write_compiled_artifacts(path, compiled)
    if generated_artifacts:
        existing_artifacts = manifest.get("artifacts")
        existing_artifacts = existing_artifacts if isinstance(existing_artifacts, list) else []
        generated_paths = {item["path"] for item in generated_artifacts}
        manifest["artifacts"] = [
            item
            for item in existing_artifacts
            if not isinstance(item, dict) or item.get("path") not in generated_paths
        ] + generated_artifacts

    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise FormaClientError(f"Could not write the compiled project manifest: {path}") from exc


def _write_compiled_artifacts(project_path: Path, compiled: dict[str, Any]) -> list[dict[str, str]]:
    """Materialize compiler renderings next to the canonical project manifest."""
    outputs: list[tuple[str, str, str]] = []
    validation = compiled.get("validation")
    if validation is not None:
        outputs.append(
            (
                "validation.json",
                json.dumps(validation, indent=2, sort_keys=True) + "\n",
                "application/json",
            )
        )
    mermaid_code = compiled.get("mermaid_code")
    if isinstance(mermaid_code, str) and mermaid_code.strip():
        outputs.append(("wiring.mmd", mermaid_code.rstrip() + "\n", "text/vnd.mermaid"))
    svg_schematic = compiled.get("svg_schematic")
    if isinstance(svg_schematic, str) and svg_schematic.strip():
        outputs.append(("schematic.svg", svg_schematic.rstrip() + "\n", "image/svg+xml"))

    references: list[dict[str, str]] = []
    for filename, content, media_type in outputs:
        artifact_path = project_path.parent / filename
        data = content.encode("utf-8")
        temporary = artifact_path.with_suffix(f"{artifact_path.suffix}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(artifact_path)
        except OSError as exc:
            raise FormaClientError(f"Could not write compiled artifact: {artifact_path}") from exc
        references.append(
            {
                "path": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
                "media_type": media_type,
            }
        )
    return references


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call Forma from any Agent Skills host.")
    commands = parser.add_subparsers(dest="command", required=True)
    tools_parser = commands.add_parser("tools")
    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("project")
    compile_parser.add_argument(
        "--authoring-agent",
        required=True,
        choices=("openclaw", "opencode", "nemoclaw", "claude", "codex", "other"),
    )
    compile_parser.add_argument(
        "--update-project",
        action="store_true",
        help="Replace the input manifest's project_ir with the compiled response.",
    )
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("project")
    generate_parser = commands.add_parser("generate")
    generate_parser.add_argument("prompt")
    generate_parser.add_argument("--workflow", choices=("default", "web_research"), default="default")
    call_parser = commands.add_parser("call")
    call_parser.add_argument("tool")
    call_parser.add_argument("--arguments", default="{}")
    for command_parser in (tools_parser, compile_parser, validate_parser, generate_parser, call_parser):
        command_parser.add_argument("--url")
        command_parser.add_argument("--timeout", type=float)
        command_parser.add_argument("--output")
    return parser


def run(args: argparse.Namespace) -> Any:
    options = {"url": args.url, "timeout": args.timeout}
    if args.command == "tools":
        return request("tools/list", **options)
    if args.command == "compile":
        return call_tool("forma.compile_project", {"project_ir": _load(args.project), "authoring_agent": args.authoring_agent}, **options)
    if args.command == "validate":
        project = _load(args.project)
        return call_tool("forma.validate_circuit", {"components": project.get("components", []), "nets": project.get("nets", [])}, **options)
    if args.command == "generate":
        return call_tool("forma.generate_project", {"prompt": args.prompt, "workflow": args.workflow}, **options)
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise FormaClientError("--arguments must decode to an object.")
    return call_tool(args.tool, arguments, **options)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
        if args.command == "compile" and args.update_project:
            same_output = (
                args.output
                and args.output != "-"
                and Path(args.output).expanduser().resolve() == Path(args.project).expanduser().resolve()
            )
            if same_output:
                raise FormaClientError("--output must differ from the input when using --update-project.")
            _update_project_manifest(args.project, result)
        _write(result, args.output)
        return 0
    except (FormaClientError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"forma: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
