"""Slicer backends."""

from forma_core.workspaces.projects.fabrication.slicers.base import SlicerAdapter
from forma_core.workspaces.projects.fabrication.slicers.cura import CuraSlicerAdapter
from forma_core.workspaces.projects.fabrication.slicers.orca import OrcaSlicerAdapter

__all__ = ["CuraSlicerAdapter", "OrcaSlicerAdapter", "SlicerAdapter"]
