from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.workspaces.projects.gear_benchmark import SpurGearPairBenchmark
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from scripts.development.build_gear_example import example_project
from tests.projects.test_mechanism_benchmarks import inspect_step


ROOT = Path(__file__).resolve().parents[2]


def test_gear_example_roundtrips_with_real_meshes_and_shared_tracks():
    text = (ROOT / "examples/spur_gear_pair.json").read_text()
    assert text == (ROOT / "apps/web/public/examples/spur_gear_pair.json").read_text()
    project = HardwareIntermediateRepresentation.model_validate_json(text)
    reloaded = HardwareIntermediateRepresentation.model_validate_json(project.model_dump_json())
    bodies = reloaded.cad_model["articulated_bodies"]
    tracks = reloaded.cad_model["kinematics"]["tracks"]
    assert len(bodies) == len(tracks) == 2
    assert {body["shape_id"] for body in bodies} == {track["joint"]["child_shape_id"] for track in tracks}
    assert all(len(body["mesh"]["faces"]) > 1000 for body in bodies)
    for a, b in zip(tracks[0]["samples"], tracks[1]["samples"]):
        assert a["progress"] == b["progress"]
        assert b["value"] == pytest.approx(-a["value"] / 2)
    assert tracks[0]["samples"][-1]["value"] == pytest.approx(4 * math.pi)


@pytest.mark.parametrize("bad", [{"driver_ref": "GEAR_DRIVEN"}, {"driver_teeth": 0}, {"backlash_mm": 1}, {"bore_diameter_mm": 50}, {"cycle_seconds": float("nan")}])
def test_invalid_gear_intent_fails_validation(bad):
    with pytest.raises(ValidationError):
        SpurGearPairBenchmark(**bad)


@pytest.mark.skipif(os.environ.get("FORMA_CAD_RUN_INTEGRATION_TESTS") != "true", reason="Native OCCT integration gate")
def test_gear_compile_exports_and_persists_two_bodies():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
        "FORMA_CAD_WORKSPACE": directory, "FORMA_CLI_ARTIFACT_STORAGE_BACKEND": "local",
        "FORMA_CLI_ARTIFACT_STORAGE_DIR": directory + "/stored",
    }):
        project = example_project()
        ensure_native_cad_model(project, project_id="52900000-0000-4529-8529-000000000001", required=True)
        native = inspect_step(project.cad_model["path"])
        assert native["valid"] and native["solids"] == 2
        assert set(project.cad_model["exports"]) == {"stl", "obj", "3mf"}
        reloaded = HardwareIntermediateRepresentation.model_validate_json(project.model_dump_json())
        assert len(reloaded.cad_model["articulated_bodies"]) == 2
        assert reloaded.cad_model["kinematics"]["mechanism"]["loop"] is True
        assert reloaded.cad_model["stored_sha256"]
