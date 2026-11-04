"""Stale generated geometry cannot take the provided-CAD fast path."""

from unittest.mock import patch
import os
from pathlib import Path
import tempfile

import pytest

from forma_core.workspaces.projects.inspection import cad_intent_digest, compile_project
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


def test_editing_generic_dimensions_retires_previous_generated_geometry():
    project = HardwareIntermediateRepresentation.model_validate({"mechanical": {
        "enclosure_type": "Test", "mounting_guidance": "Unspecified",
        "manufacturability_rating": "Unassessed", "render_dimensions": {"x_mm": 40, "y_mm": 20, "z_mm": 4}}})
    project.cad_model = {"adapter": "forma-opencad", "generated": True, "path": "old.step",
                         "intent_sha256": cad_intent_digest(project)}
    project.mechanical.render_dimensions.x_mm = 60
    observed = []
    def compile_native(value, **_kwargs):
        observed.append(value.cad_model)
        return False
    with patch("forma_core.workspaces.projects.cad_generation.ensure_native_cad_model", side_effect=compile_native):
        result = compile_project(project)
    assert observed == [None]
    assert result.cad_model is None
    assert project.cad_model["path"] == "old.step"  # previous source stays intact


def test_explicit_imported_geometry_is_preserved_for_inspection():
    project = HardwareIntermediateRepresentation(cad_model={"source": "provided", "path": "supplied.step"})
    with patch("forma_core.workspaces.projects.cad_generation.ensure_native_cad_model", return_value=False):
        result = compile_project(project)
    assert result.cad_model == project.cad_model


@pytest.mark.skipif(os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") != "true", reason="native CAD gate")
def test_generic_native_recompile_changes_the_actual_exported_dimensions():
    import cadquery as cq
    from forma_core.config import config
    project = HardwareIntermediateRepresentation.model_validate({"mechanical": {
        "enclosure_type": "Test", "mounting_guidance": "Unspecified",
        "manufacturability_rating": "Unassessed", "render_dimensions": {"x_mm": 40, "y_mm": 20, "z_mm": 12}}})
    with tempfile.TemporaryDirectory() as directory, config.override({"FORMA_CAD_WORKSPACE": directory}):
        first = compile_project(project)
        original_path = Path(first.cad_model["path"])
        first.mechanical.render_dimensions.x_mm = 60
        revised = compile_project(first)
        original = cq.importers.importStep(str(original_path)).val()
        current = cq.importers.importStep(str(revised.cad_model["path"])).val()
        assert original.isValid() and current.isValid()
        assert abs(original.BoundingBox().xlen - 40) < 1e-6
        assert abs(current.BoundingBox().xlen - 60) < 1e-6
