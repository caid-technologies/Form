"""Stable adapter contract shared by all slicer backends."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from forma_core.workspaces.projects.fabrication.models import (
    SliceRequest,
    SliceResult,
    SliceInspection,
    SliceValidationResult,
)


class SlicerAdapter(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def inspect(self, mesh_path: Path) -> SliceInspection: ...

    def slice(self, request: SliceRequest) -> SliceResult: ...

    def validate(self, result: SliceResult) -> SliceValidationResult: ...
