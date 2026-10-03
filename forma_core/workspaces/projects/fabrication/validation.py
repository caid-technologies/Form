"""Deterministic safety checks for generated FDM G-code."""

from __future__ import annotations

import math
from pathlib import Path
import re

from forma_core.workspaces.projects.fabrication.models import SliceProfile, SliceValidationResult


_COMMAND_RE = re.compile(r"^\s*([GMT]\d+(?:\.\d+)?)\b", re.IGNORECASE)
_VALUE_RE = re.compile(r"([XYZEF])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)
_KNOWN_COMMANDS = {"G0", "G1", "G2", "G3", "G28", "G90", "G91", "G92", "M82", "M83", "M104", "M109", "M140", "M190", "M106", "M107", "M84", "M117", "M220", "M221"}


def validate_gcode(path: str | Path, profile: SliceProfile | None = None) -> SliceValidationResult:
    """Validate a G-code file without executing it or trusting slicer output."""
    target = Path(path).expanduser()
    errors: list[str] = []
    warnings: list[str] = []
    if not target.is_file():
        return SliceValidationResult(valid=False, errors=[f"G-code output does not exist: {target}"])
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return SliceValidationResult(valid=False, errors=[f"G-code output could not be read: {exc}"])
    if not target.stat().st_size:
        return SliceValidationResult(valid=False, errors=["G-code output is empty."])

    movement_commands = 0
    extrusion_commands = 0
    temperature_commands = 0
    positions: list[tuple[float, float, float]] = []
    current = [0.0, 0.0, 0.0]
    relative_positioning = False
    has_e = False
    for raw_line in lines:
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        match = _COMMAND_RE.match(line)
        if not match:
            warnings.append(f"Unrecognized G-code line: {raw_line[:120]}")
            continue
        command = match.group(1).upper()
        if command not in _KNOWN_COMMANDS:
            warnings.append(f"Unsupported G-code command: {command}")
        values = {key.upper(): float(value) for key, value in _VALUE_RE.findall(line)}
        if command in {"G0", "G1"}:
            movement_commands += 1
            for index, axis in enumerate(("X", "Y", "Z")):
                if axis in values:
                    current[index] = current[index] + values[axis] if relative_positioning else values[axis]
            if "E" in values:
                has_e = True
                extrusion_commands += 1
            positions.append(tuple(current))
        elif command == "G90":
            relative_positioning = False
        elif command == "G91":
            relative_positioning = True
        elif command == "G92":
            for index, axis in enumerate(("X", "Y", "Z")):
                if axis in values:
                    current[index] = values[axis]
        if command in {"M104", "M109", "M140", "M190"}:
            temperature_commands += 1

    if movement_commands == 0:
        errors.append("G-code contains no movement commands.")
    if (profile is None or profile.require_extrusion) and not has_e:
        errors.append("G-code contains no extrusion commands.")
    if profile is not None and profile.require_temperatures and temperature_commands == 0:
        errors.append("G-code contains no temperature commands for the selected profile.")

    min_position = max_position = None
    if positions:
        min_position = tuple(min(position[index] for position in positions) for index in range(3))
        max_position = tuple(max(position[index] for position in positions) for index in range(3))
    if profile is not None and positions:
        if profile.bed_size_mm:
            bounds = (*profile.bed_size_mm, profile.z_height_mm or math.inf)
            for index, axis in enumerate(("X", "Y", "Z")):
                if min_position[index] < 0 or max_position[index] > bounds[index]:
                    errors.append(
                        f"{axis} moves exceed configured machine bounds: "
                        f"{min_position[index]:g}..{max_position[index]:g}."
                    )

    return SliceValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        movement_commands=movement_commands,
        extrusion_commands=extrusion_commands,
        temperature_commands=temperature_commands,
        min_position=min_position,
        max_position=max_position,
    )


__all__ = ["validate_gcode"]
