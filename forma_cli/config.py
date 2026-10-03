"""Non-secret CLI configuration and local project linkage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib
from typing import Any


# Cloud CLI commands target the hosted Forma deployment by default. Set
# FORMA_API_URL to a local API URL when developing against a local server.
DEFAULT_API_URL = "https://caid-technologies.us/api"
CONFIG_FILENAME = "config.json"
LINKAGE_FILENAME = "project.toml"


class CliConfigError(RuntimeError):
    """Raised when local CLI configuration is invalid or unavailable."""


def config_dir() -> Path:
    configured = os.environ.get("FORMA_CLI_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "Forma"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "forma"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {"api_url": os.environ.get("FORMA_API_URL", DEFAULT_API_URL).rstrip("/")}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliConfigError(f"Could not read CLI configuration: {path}") from exc
    if not isinstance(payload, dict):
        raise CliConfigError(f"CLI configuration must be a JSON object: {path}")
    return {
        "api_url": str(payload.get("api_url") or os.environ.get("FORMA_API_URL") or DEFAULT_API_URL).rstrip("/"),
        **{key: value for key, value in payload.items() if key != "api_url"},
    }


def save_config(values: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def api_url() -> str:
    return str(load_config().get("api_url") or DEFAULT_API_URL).rstrip("/")


def linkage_path(project_root: str | Path) -> Path:
    return Path(project_root) / ".forma" / LINKAGE_FILENAME


def load_linkage(project_root: str | Path) -> dict[str, Any]:
    path = linkage_path(project_root)
    if not path.exists():
        return {"version": 1}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliConfigError(f"Could not read project linkage: {path}") from exc
    if not isinstance(payload, dict):
        raise CliConfigError(f"Project linkage must be a TOML table: {path}")
    return payload


def _toml_value(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def save_linkage(project_root: str | Path, values: dict[str, Any]) -> None:
    path = linkage_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key} = {_toml_value(value)}" for key, value in values.items() if value is not None]
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


__all__ = [
    "CliConfigError",
    "DEFAULT_API_URL",
    "api_url",
    "config_dir",
    "config_path",
    "linkage_path",
    "load_config",
    "load_linkage",
    "save_config",
    "save_linkage",
]
