"""Explicit generation strategy selection for Forma projects.

Regular generation preserves the existing one-shot pipeline and is the default
for projects that do not opt into a mode. Progressive generation enables the
cost-aware hierarchical lifecycle introduced by issue #511 / PR #512.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


GENERATION_MODE_METADATA_KEY = "generation_mode"


class GenerationMode(str, Enum):
    """Top-level project generation strategy."""

    REGULAR = "regular"
    PROGRESSIVE = "progressive"


def get_generation_mode(project_or_metadata: Any) -> GenerationMode:
    """Return the persisted generation mode, defaulting safely to regular.

    The mode is intentionally explicit. Presence of design-brief, lifecycle,
    image, or approval metadata must never opt a project into progressive
    generation by itself.
    """

    if isinstance(project_or_metadata, Mapping):
        metadata = project_or_metadata
    else:
        metadata = getattr(project_or_metadata, "assembly_metadata", None) or {}

    raw = metadata.get(GENERATION_MODE_METADATA_KEY)
    if raw in (None, ""):
        return GenerationMode.REGULAR
    try:
        return raw if isinstance(raw, GenerationMode) else GenerationMode(str(raw).strip().lower())
    except ValueError:
        # Unknown persisted values should not silently enable expensive behavior.
        return GenerationMode.REGULAR


def is_progressive_generation(project_or_metadata: Any) -> bool:
    """Return True only for an explicit progressive-mode opt in."""

    return get_generation_mode(project_or_metadata) == GenerationMode.PROGRESSIVE


def set_generation_mode(project: Any, mode: GenerationMode | str) -> GenerationMode:
    """Persist a validated generation mode on a HardwareIntermediateRepresentation-compatible object."""

    normalized = mode if isinstance(mode, GenerationMode) else GenerationMode(str(mode).strip().lower())
    project.assembly_metadata = {
        **(getattr(project, "assembly_metadata", None) or {}),
        GENERATION_MODE_METADATA_KEY: normalized.value,
    }
    return normalized


__all__ = [
    "GENERATION_MODE_METADATA_KEY",
    "GenerationMode",
    "get_generation_mode",
    "is_progressive_generation",
    "set_generation_mode",
]
