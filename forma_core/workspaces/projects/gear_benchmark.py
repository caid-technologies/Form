"""Authoring parameters and OpenCAD source for the two-gear motion example."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpurGearPairBenchmark(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    kind: Literal["spur_gear_pair"] = "spur_gear_pair"
    driver_ref: str = Field("GEAR_DRIVER", min_length=1)
    driven_ref: str = Field("GEAR_DRIVEN", min_length=1)
    driver_teeth: int = Field(20, ge=18, le=120, strict=True)
    driven_teeth: int = Field(40, ge=18, le=120, strict=True)
    module_mm: float = Field(2.0, ge=0.5, le=5.0)
    pressure_angle_deg: float = Field(20.0, ge=20.0, le=25.0)
    face_width_mm: float = Field(8.0, gt=0, le=50)
    bore_diameter_mm: float = Field(6.0, ge=0)
    backlash_mm: float = Field(0.1, ge=0)
    cycle_seconds: float = Field(6.0, ge=1, le=60)

    @model_validator(mode="after")
    def valid_pair(self) -> "SpurGearPairBenchmark":
        if self.driver_ref.strip() == self.driven_ref.strip() or not self.driver_ref.strip() or not self.driven_ref.strip():
            raise ValueError("Gear component references must be distinct and nonempty.")
        if self.backlash_mm >= self.module_mm * 0.2:
            raise ValueError("Backlash must be smaller than 0.2 module.")
        root = self.module_mm * (min(self.driver_teeth, self.driven_teeth) / 2 - 1.25)
        if self.bore_diameter_mm / 2 >= root - self.module_mm:
            raise ValueError("Gear bore must leave at least one module of root wall.")
        return self


def gear_pair_source(spec: SpurGearPairBenchmark) -> str:
    return '''import math
from opencad import Part, get_default_context
from opencad.gears import SpurGearSpec, spur_gear
from opencad.kinematics import GearCoupling

PARAMS = %r
context = get_default_context()
distance = PARAMS["module_mm"] * (PARAMS["driver_teeth"] + PARAMS["driven_teeth"]) / 2
turns = PARAMS["driven_teeth"] // math.gcd(PARAMS["driver_teeth"], PARAMS["driven_teeth"])
driven_turns = PARAMS["driver_teeth"] // math.gcd(PARAMS["driver_teeth"], PARAMS["driven_teeth"])
shared = {key: PARAMS[key] for key in ("module_mm", "pressure_angle_deg", "face_width_mm", "bore_diameter_mm", "backlash_mm")}
# The static frame anchors both axes but is not part of the exported gear pair.
frame = Part(name="Gear reference frame").box(1, 1, 1)
driver_center = (-distance / 2, 0.0, 0.0)
driven_center = (distance / 2, 0.0, 0.0)
# Put a tooth space on the driven gear's inward-facing pitch point, for odd or even counts.
phase = math.pi - math.pi / PARAMS["driven_teeth"]
driver = spur_gear(SpurGearSpec(teeth=PARAMS["driver_teeth"], **shared), center_mm=driver_center, name="Driver gear")
driven = spur_gear(SpurGearSpec(teeth=PARAMS["driven_teeth"], **shared), center_mm=driven_center, phase_radians=phase, name="Driven gear")
for joint_id, part, ref, center, lower, upper in (
    ("gear-driver", driver, PARAMS["driver_ref"], driver_center, 0.0, math.tau * turns),
    ("gear-driven", driven, PARAMS["driven_ref"], driven_center, -math.tau * driven_turns, 0.0),
):
    result = context.registry.call("create_kinematic_joint", {
        "joint_id": joint_id, "type": "revolute", "parent_shape_id": frame.shape_id,
        "child_shape_id": part.shape_id, "origin_mm": center, "axis": (0, 0, 1),
        "lower_limit": lower, "upper_limit": upper, "label": ref,
        "metadata": {"forma_target_ref": ref},
    })
    if not result.ok:
        raise RuntimeError(result.message)

FORMA_EXPORT_SHAPE_IDS = [driver.shape_id, driven.shape_id]
FORMA_GEOMETRY_BY_REF = {
    PARAMS["driver_ref"]: driver.shape_id,
    PARAMS["driven_ref"]: driven.shape_id,
}
FORMA_PREVIEW_BODIES = [
    {"shape_id": driver.shape_id, "target_ref": PARAMS["driver_ref"], "name": "Driver gear", "color": "#3b82f6", "center_mm": driver_center, "marker_radius_mm": PARAMS["module_mm"] * PARAMS["driver_teeth"] * 0.3},
    {"shape_id": driven.shape_id, "target_ref": PARAMS["driven_ref"], "name": "Driven gear", "color": "#f59e0b", "center_mm": driven_center, "marker_radius_mm": PARAMS["module_mm"] * PARAMS["driven_teeth"] * 0.3},
]
FORMA_MOTION_PROGRAM = {
    "id": "spur-gear-pair", "label": "Meshing gears", "loop": True,
    "duration_seconds": PARAMS["cycle_seconds"], "driver_joint_id": "gear-driver",
    "sample_count": max(361, turns * 120 + 1, driven_turns * 120 + 1),
    "gear_couplings": [GearCoupling(driver_joint_id="gear-driver", driven_joint_id="gear-driven", driver_teeth=PARAMS["driver_teeth"], driven_teeth=PARAMS["driven_teeth"]).model_dump(mode="json")],
}
FORMA_MECHANISM_METADATA = {
    "family": "external spur gear pair", "benchmark": PARAMS["kind"], "body_count": 2,
    "parameters": PARAMS, "center_distance_mm": distance, "driven_geometry_phase_rad": phase,
    "limitations": "Prescribed rigid motion with sampled involute profiles; not contact dynamics or manufacturing qualification.",
}
model = driver
''' % spec.model_dump(mode="json")
