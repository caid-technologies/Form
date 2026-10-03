"""Persistent, non-secret user printer registry and profile resolution."""

from __future__ import annotations

import json
from pathlib import Path
import platform
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forma_core.config import config
from forma_core.workspaces.projects.fabrication.models import PrinterConfigurationError, SliceProfile


class PrinterProfileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    native_config: str = Field(min_length=1)
    material: str | None = None
    nozzle_diameter_mm: float | None = Field(default=None, gt=0)
    bed_size_mm: tuple[float, float] | None = None
    z_height_mm: float | None = Field(default=None, gt=0)
    require_temperatures: bool = False
    require_extrusion: bool = True


class UserPrinterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printer_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    default_profile: str = Field(min_length=1)
    profiles: dict[str, PrinterProfileReference] = Field(default_factory=dict)

    @field_validator("backend", mode="before")
    @classmethod
    def normalize_backend(cls, value: Any) -> str:
        return str(value or "").strip().lower()

    @model_validator(mode="after")
    def require_default_profile(self) -> "UserPrinterConfig":
        if self.default_profile not in self.profiles:
            raise ValueError(f"Default profile {self.default_profile!r} is not configured for {self.printer_id!r}.")
        return self


class PrinterRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_printer: str | None = None
    printers: dict[str, UserPrinterConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_default_printer(self) -> "PrinterRegistry":
        if self.default_printer is not None and self.default_printer not in self.printers:
            raise ValueError(f"Default printer {self.default_printer!r} is not configured.")
        return self


def default_printer_config_path() -> Path:
    configured = config.optional("FORMA_PRINTER_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    if platform.system() == "Windows":
        root = config.optional("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "Forma" / "printers.toml"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Forma" / "printers.toml"
    return Path(config.optional("XDG_CONFIG_HOME") or Path.home() / ".config") / "forma" / "printers.toml"


def _profile_payload(value: dict[str, Any]) -> PrinterProfileReference:
    return PrinterProfileReference.model_validate(value)


def load_printer_registry(path: str | Path | None = None) -> PrinterRegistry:
    target = Path(path).expanduser() if path else default_printer_config_path()
    if not target.exists():
        return PrinterRegistry()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PrinterConfigurationError(f"Could not read printer configuration: {target}") from exc
    if not isinstance(raw, dict):
        raise PrinterConfigurationError(f"Printer configuration must be a TOML table: {target}")
    printers: dict[str, UserPrinterConfig] = {}
    for printer_id, value in (raw.get("printers") or {}).items():
        if not isinstance(value, dict):
            raise PrinterConfigurationError(f"Printer {printer_id!r} must be a TOML table.")
        profiles = {
            str(profile_id): _profile_payload(profile)
            for profile_id, profile in (value.get("profiles") or {}).items()
            if isinstance(profile, dict)
        }
        printers[str(printer_id)] = UserPrinterConfig(
            printer_id=str(printer_id),
            display_name=str(value.get("display_name") or printer_id),
            backend=str(value.get("backend") or ""),
            default_profile=str(value.get("default_profile") or ""),
            profiles=profiles,
        )
    try:
        return PrinterRegistry(default_printer=raw.get("default_printer"), printers=printers)
    except ValueError as exc:
        raise PrinterConfigurationError(str(exc)) from exc


def _toml_string(value: str) -> str:
    return json.dumps(value)


def save_printer_registry(registry: PrinterRegistry, path: str | Path | None = None) -> Path:
    target = Path(path).expanduser() if path else default_printer_config_path()
    lines: list[str] = []
    if registry.default_printer:
        lines.append(f"default_printer = {_toml_string(registry.default_printer)}")
        lines.append("")
    for printer_id in sorted(registry.printers):
        printer = registry.printers[printer_id]
        lines.extend(
            (
                f"[printers.{_toml_string(printer_id)}]",
                f"display_name = {_toml_string(printer.display_name)}",
                f"backend = {_toml_string(printer.backend)}",
                f"default_profile = {_toml_string(printer.default_profile)}",
                "",
            )
        )
        for profile_id in sorted(printer.profiles):
            profile = printer.profiles[profile_id]
            lines.extend(
                (
                    f"[printers.{_toml_string(printer_id)}.profiles.{_toml_string(profile_id)}]",
                    f"native_config = {_toml_string(profile.native_config)}",
                )
            )
            for key, value in (
                ("material", profile.material),
                ("nozzle_diameter_mm", profile.nozzle_diameter_mm),
                ("bed_size_mm", profile.bed_size_mm),
                ("z_height_mm", profile.z_height_mm),
                ("require_temperatures", profile.require_temperatures),
                ("require_extrusion", profile.require_extrusion),
            ):
                if value is not None:
                    encoded = json.dumps(list(value) if isinstance(value, tuple) else value)
                    lines.append(f"{key} = {encoded}")
            lines.append("")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def resolve_slice_profile(
    registry: PrinterRegistry,
    *,
    printer_id: str | None = None,
    backend: str | None = None,
    native_config: str | None = None,
    profile_name: str | None = None,
) -> SliceProfile:
    """Resolve explicit settings, then named/default printer settings, deterministically."""
    selected_id = printer_id or (None if backend or native_config else registry.default_printer)
    if selected_id:
        printer = registry.printers.get(selected_id)
        if printer is None:
            raise PrinterConfigurationError(f"Printer configuration not found: {selected_id}")
        selected_profile_name = profile_name or printer.default_profile
        selected_profile = printer.profiles.get(selected_profile_name)
        if selected_profile is None:
            raise PrinterConfigurationError(
                f"Profile {selected_profile_name!r} is not configured for printer {selected_id!r}."
            )
        return SliceProfile(
            backend=printer.backend,
            printer_name=printer.display_name,
            profile_name=selected_profile_name,
            **selected_profile.model_dump(mode="python"),
        )
    if backend and native_config:
        return SliceProfile(
            backend=backend,
            printer_name=printer_id or "Explicit profile",
            profile_name=profile_name,
            native_config=native_config,
        )
    raise PrinterConfigurationError(
        "Printer configuration required: pass a configured printer or both backend and profile."
    )


__all__ = [
    "PrinterProfileReference",
    "PrinterRegistry",
    "UserPrinterConfig",
    "default_printer_config_path",
    "load_printer_registry",
    "resolve_slice_profile",
    "save_printer_registry",
]
