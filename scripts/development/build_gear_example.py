"""Regenerate the checked-in real-mesh gear example using the managed adapter.

Run from the repository root: python scripts/development/build_gear_example.py
Requires the OpenCAD gear/coupling runtime pinned by the adapter.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from forma_core.workspaces.projects.gear_benchmark import SpurGearPairBenchmark, gear_pair_source
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


ROOT = Path(__file__).resolve().parents[2]


def example_project() -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation.model_validate({
        "overview": {"title": "Two Meshing Gears", "description": "20-tooth driver and 40-tooth driven gear, with synchronized OpenCAD motion.", "difficulty": "Intermediate", "estimated_cost": 0, "category": "Mechanical"},
        "components": [
            {"ref_des": ref, "part_number": ref, "name": label, "category": "Mechanical", "rationale": "Separate gear body in a prescribed rigid mechanism."}
            for ref, label in [("GEAR_DRIVER", "20-tooth driver gear"), ("GEAR_DRIVEN", "40-tooth driven gear")]
        ],
        "mechanical": {
            "physical_form": "Two meshing external spur gears", "enclosure_type": "Open frame",
            "mounting_guidance": "Parallel axes, 60 mm pitch-center spacing.", "manufacturability_rating": "Moderate",
            "render_dimensions": {"x_mm": 130, "y_mm": 90, "z_mm": 12},
            "component_placements": [
                {"ref_des": ref, "label": label, "category": "Mechanical", "layer": "mechanism",
                 "position": {"x_mm": x, "y_mm": 0, "z_mm": 0}, "size": {"x_mm": diameter, "y_mm": diameter, "z_mm": 8}}
                for ref, label, x, diameter in [("GEAR_DRIVER", "20-tooth driver gear", -30, 44), ("GEAR_DRIVEN", "40-tooth driven gear", 30, 84)]
            ],
            "mechanism_benchmark": SpurGearPairBenchmark().model_dump(mode="json"),
            "fabrication_details": ["Module 2 mm, 20-degree pressure angle, 8 mm face width.", "Sampled involute profile with 0.1 mm total backlash; motion reference only."],
        },
        "assembly_metadata": {"status": "example", "image_features": ["Two meshing gears", "Synchronized motion", "2:1 reduction"]},
    })


def main() -> None:
    project = example_project()
    with tempfile.TemporaryDirectory() as directory:
        model = Path(directory) / "gears.py"
        model.write_text(gear_pair_source(project.mechanical.mechanism_benchmark), encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / ".agents/skills/forma-hardware/scripts/cad.py"), "build", str(model), str(Path(directory) / "gears.step")], check=True, capture_output=True, text=True)
    summary = json.loads(result.stdout)
    project.cad_model = {
        "adapter": "forma-opencad", "source": "OpenCAD-generated gear example; regenerate to create downloadable project artifacts",
        "units": "mm", "opencad_version": summary["opencad_version"],
        **{key: summary[key] for key in ("kinematics", "articulated_bodies", "mechanism")},
    }
    # Micrometer mesh precision is ample for the example and avoids duplicating
    # the body meshes in a second static-view payload.
    for body in project.cad_model["articulated_bodies"]:
        body["mesh"]["vertices"] = [round(value, 6) for value in body["mesh"]["vertices"]]
    # Compact the mesh/sample-heavy fixture. Both copies are intentionally identical.
    text = json.dumps(project.model_dump(mode="json", exclude_none=True), separators=(",", ":")) + "\n"
    for path in (ROOT / "examples/spur_gear_pair.json", ROOT / "apps/web/public/examples/spur_gear_pair.json"):
        path.write_text(text, encoding="utf-8")
    print(f"Generated two native gear meshes and {summary['kinematics']['sample_count']} synchronized poses per gear.")


if __name__ == "__main__":
    main()
