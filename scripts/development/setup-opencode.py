#!/usr/bin/env python3
"""Prepare a local Forma workspace for OpenCode.

This helper deliberately uses only the Python standard library so the bootstrap
scripts can run before the backend environment has been installed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


LOCAL_MCP_URL = "http://127.0.0.1:8000/mcp"
OPENCODE_SCHEMA = "https://opencode.ai/config.json"
SKILL_RELATIVE_PATH = Path(".agents") / "skills" / "forma-hardware"


class SetupError(RuntimeError):
    """Raised when local OpenCode setup cannot be completed safely."""


def _command_version(command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        raise SetupError(f"{command} was not found on PATH.")
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(f"Could not read the installed {command} version.") from exc
    return (result.stdout or result.stderr).strip()


def _major_version(value: str) -> int | None:
    match = re.search(r"(?:^|\s|v)(\d+)(?:\.\d+)?", value)
    return int(match.group(1)) if match else None


def _require_runtime_prerequisites() -> dict[str, str]:
    if sys.version_info < (3, 11):
        raise SetupError(
            f"Python 3.11 or newer is required; found {sys.version_info.major}.{sys.version_info.minor}."
        )

    versions = {"python": ".".join(str(part) for part in sys.version_info[:3])}
    node_version = _command_version("node")
    node_major = _major_version(node_version)
    if node_major is None or node_major < 18:
        raise SetupError(f"Node.js 18 or newer is required; found {node_version or 'an unknown version'}.")
    versions["node"] = node_version
    versions["npm"] = _command_version("npm")

    try:
        versions["opencode"] = _command_version("opencode")
    except SetupError as exc:
        raise SetupError(
            "OpenCode was not found on PATH. Install OpenCode from https://opencode.ai/docs/cli/ "
            "and run this setup again."
        ) from exc
    return versions


def _repo_version(root: Path) -> str:
    version_file = root / "forma_core" / "_version.py"
    try:
        content = version_file.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)', content)
    return match.group(1) if match else "unknown"


def _git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _install_cli(root: Path) -> None:
    command = [sys.executable, "-m", "pip", "install"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        command.append("--user")
    command.extend(("--editable", str(root)))
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError("Could not install the forma-oss CLI from the local checkout.") from exc


def _copy_skill(root: Path, workspace: Path) -> Path:
    source = root / SKILL_RELATIVE_PATH
    if not source.is_dir():
        raise SetupError(f"The Forma hardware skill is missing from the checkout: {source}")
    destination = workspace / SKILL_RELATIVE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _configure_opencode(workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "opencode.json"
    payload: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupError(
                f"{config_path} is not valid JSON. No configuration was changed; repair it and rerun setup."
            ) from exc
        if not isinstance(loaded, dict):
            raise SetupError(f"{config_path} must contain a JSON object. No configuration was changed.")
        payload = loaded

    payload.setdefault("$schema", OPENCODE_SCHEMA)
    mcp = payload.get("mcp")
    if mcp is None:
        mcp = {}
    if not isinstance(mcp, dict):
        raise SetupError(f"The mcp setting in {config_path} must be an object. No configuration was changed.")

    expected = {
        "type": "remote",
        "url": LOCAL_MCP_URL,
        "enabled": True,
        "oauth": False,
    }
    existing = mcp.get("forma")
    if existing is None:
        mcp["forma"] = expected
    elif not isinstance(existing, dict):
        raise SetupError(
            f"The existing forma MCP entry in {config_path} is not an object. "
            "No configuration was changed."
        )
    elif existing.get("url") not in {None, LOCAL_MCP_URL}:
        raise SetupError(
            f"The existing forma MCP entry points to {existing.get('url')!r}, not the local endpoint. "
            "No configuration was changed."
        )
    else:
        mcp["forma"] = {**expected, **existing, "url": LOCAL_MCP_URL}
    payload["mcp"] = mcp
    _write_json(config_path, payload)
    return config_path


def configure(root: Path, workspace: Path, *, install_cli: bool) -> dict[str, str]:
    root = root.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    if not root.is_dir():
        raise SetupError(f"Forma checkout does not exist: {root}")

    versions = _require_runtime_prerequisites()
    if install_cli:
        _install_cli(root)
    skill_path = _copy_skill(root, workspace)
    config_path = _configure_opencode(workspace)
    return {
        "forma_version": _repo_version(root),
        "git_revision": _git_revision(root),
        "workspace": str(workspace),
        "skill": str(skill_path),
        "opencode_config": str(config_path),
        **versions,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare a local Forma workspace for OpenCode.")
    parser.add_argument("--root", required=True, help="Forma OSS checkout to use for the local runtime.")
    parser.add_argument(
        "--workspace",
        default=str(Path.home() / "forma-workspace"),
        help="OpenCode workspace that will contain the local skill and MCP configuration.",
    )
    parser.add_argument(
        "--install-cli",
        action="store_true",
        help="Install the checkout's forma-oss command into the current Python environment.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        details = configure(Path(args.root), Path(args.workspace), install_cli=args.install_cli)
    except SetupError as exc:
        print(f"forma setup: {exc}", file=sys.stderr)
        return 2

    print("Forma local OpenCode setup is ready.")
    print(f"  Forma version: {details['forma_version']} ({details['git_revision']})")
    print(f"  OpenCode: {details['opencode']}")
    print(f"  Workspace: {details['workspace']}")
    print(f"  MCP: {LOCAL_MCP_URL}")
    print("  No simulation or server-side model was configured.")
    print("")
    print("Open a second terminal and run:")
    print(f"  opencode {details['workspace']}")
    print("  forma-oss login  # only when you choose to upload a project")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
