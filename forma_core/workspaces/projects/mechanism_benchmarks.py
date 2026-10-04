"""Parameterized additive-mechanism benchmark geometry for OpenCAD.

These sources are deterministic and bounded. They deliberately keep rigid
print-in-place joints separate from compliant deformation previews.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from forma_core.workspaces.projects.gear_benchmark import SpurGearPairBenchmark, gear_pair_source


class PrintInPlaceHingeBenchmark(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["print_in_place_hinge"] = "print_in_place_hinge"
    fixed_ref: str = "PIP_FIXED"
    moving_ref: str = "PIP_MOVING"
    hinge_length_mm: float = Field(56.0, gt=20.0, le=300.0)
    barrel_diameter_mm: float = Field(10.0, gt=4.0, le=80.0)
    pin_diameter_mm: float = Field(4.0, gt=1.0, le=40.0)
    radial_clearance_mm: float = Field(0.35, gt=0.0, le=3.0)
    axial_clearance_mm: float = Field(0.40, gt=0.0, le=5.0)
    wall_thickness_mm: float = Field(1.8, gt=0.6, le=10.0)
    leaf_width_mm: float = Field(24.0, gt=5.0, le=150.0)
    leaf_thickness_mm: float = Field(3.0, gt=0.8, le=20.0)
    travel_deg: float = Field(110.0, gt=0.0, le=180.0)
    stop_thickness_mm: float = Field(2.0, gt=0.5, le=12.0)
    process: str = "FDM"
    material_note: str = "Calibrate clearance for the actual printer, material, layer height, and orientation."

    @model_validator(mode="after")
    def validate_clearances(self) -> "PrintInPlaceHingeBenchmark":
        barrel_radius = self.barrel_diameter_mm / 2.0
        hole_radius = self.pin_diameter_mm / 2.0 + self.radial_clearance_mm
        if barrel_radius - hole_radius < self.wall_thickness_mm:
            raise ValueError(
                "barrel_diameter_mm must leave at least wall_thickness_mm around the pin clearance."
            )
        if self.hinge_length_mm <= 4.0 * self.axial_clearance_mm + 8.0:
            raise ValueError("hinge_length_mm is too short for the requested axial clearance.")
        return self


class MonolithicFlexureBenchmark(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["monolithic_flexure_hinge"] = "monolithic_flexure_hinge"
    fixed_ref: str = "FLEX_FIXED"
    moving_ref: str = "FLEX_MOVING"
    flexure_thickness_mm: float = Field(0.8, gt=0.2, le=5.0)
    flexure_width_mm: float = Field(14.0, gt=2.0, le=100.0)
    flexure_length_mm: float = Field(10.0, gt=2.0, le=80.0)
    rigid_body_length_mm: float = Field(24.0, gt=5.0, le=150.0)
    rigid_body_width_mm: float = Field(24.0, gt=5.0, le=150.0)
    rigid_body_thickness_mm: float = Field(4.0, gt=1.0, le=30.0)
    bend_direction: Literal["positive_z", "negative_z"] = "positive_z"
    nominal_travel_deg: float = Field(22.0, gt=0.0, le=60.0)
    preview_samples: int = Field(41, ge=3, le=201)
    material_note: str = (
        "Flexure life depends strongly on material, print orientation, layer adhesion, thickness, and strain."
    )

    @model_validator(mode="after")
    def validate_flexure(self) -> "MonolithicFlexureBenchmark":
        if self.flexure_thickness_mm >= self.rigid_body_thickness_mm:
            raise ValueError("flexure_thickness_mm must be thinner than the rigid-body thickness.")
        if self.flexure_width_mm > self.rigid_body_width_mm:
            raise ValueError("flexure_width_mm cannot exceed rigid_body_width_mm.")
        return self


MechanismBenchmark = Annotated[
    Union[PrintInPlaceHingeBenchmark, MonolithicFlexureBenchmark, SpurGearPairBenchmark],
    Field(discriminator="kind"),
]


def mechanism_cad_source(benchmark: MechanismBenchmark) -> str:
    if isinstance(benchmark, SpurGearPairBenchmark):
        return gear_pair_source(benchmark)
    if isinstance(benchmark, PrintInPlaceHingeBenchmark):
        return _print_in_place_hinge_source(benchmark)
    if isinstance(benchmark, MonolithicFlexureBenchmark):
        return _monolithic_flexure_source(benchmark)
    raise TypeError(f"Unsupported mechanism benchmark: {type(benchmark)!r}")


def _print_in_place_hinge_source(spec: PrintInPlaceHingeBenchmark) -> str:
    payload = repr(spec.model_dump(mode="json"))
    return '''from opencad import Part, Sketch, get_default_context
import math

PARAMS = %s

def box(length, width, height, center, name):
    return Part(name=name).box(length, width, height, name=name).translate(center, name=name + " positioned")

def x_cylinder(x_start, length, radius, name):
    profile = Sketch(plane="YZ", origin=(x_start, 0.0, 0.0), name=name + " profile")
    profile.circle(radius, center=(0.0, 0.0))
    return Part(name=name).extrude(profile, depth=length, name=name)

def x_tube(x_start, length, outer_radius, inner_radius, name):
    outer = x_cylinder(x_start, length, outer_radius, name + " outer")
    inner = x_cylinder(x_start - 0.05, length + 0.10, inner_radius, name + " clearance")
    return outer.cut(inner, name=name)

length = PARAMS["hinge_length_mm"]
barrel_radius = PARAMS["barrel_diameter_mm"] / 2.0
pin_radius = PARAMS["pin_diameter_mm"] / 2.0
radial_clearance = PARAMS["radial_clearance_mm"]
axial_clearance = PARAMS["axial_clearance_mm"]
leaf_width = PARAMS["leaf_width_mm"]
leaf_thickness = PARAMS["leaf_thickness_mm"]
stop_thickness = PARAMS["stop_thickness_mm"]
wall_thickness = PARAMS["wall_thickness_mm"]
hole_radius = pin_radius + radial_clearance
outer_knuckle_radius = hole_radius + wall_thickness
leaf_gap = max(radial_clearance, 0.20)

usable = length - 2.0 * axial_clearance
outer_knuckle_length = usable * 0.25
center_knuckle_length = usable * 0.50
left_outer_x = -length / 2.0
center_x = left_outer_x + outer_knuckle_length + axial_clearance
right_outer_x = center_x + center_knuckle_length + axial_clearance

fixed_leaf_width = leaf_width - leaf_gap / 2.0
moving_leaf_width = leaf_width - leaf_gap / 2.0
fixed = box(
    length,
    fixed_leaf_width,
    leaf_thickness,
    (0.0, -(leaf_gap / 2.0 + fixed_leaf_width / 2.0), 0.0),
    "Fixed leaf",
)
moving = box(
    length,
    moving_leaf_width,
    leaf_thickness,
    (0.0, leaf_gap / 2.0 + moving_leaf_width / 2.0, 0.0),
    "Moving leaf",
)

for x_start, label in (
    (left_outer_x, "Left outer knuckle"),
    (right_outer_x, "Right outer knuckle"),
):
    knuckle = x_tube(
        x_start,
        outer_knuckle_length,
        outer_knuckle_radius,
        hole_radius,
        label,
    )
    fixed = fixed.union(knuckle, name=label + " joined")

center_knuckle = x_cylinder(
    center_x,
    center_knuckle_length,
    barrel_radius,
    "Moving center knuckle",
)
moving = moving.union(center_knuckle, name="Center knuckle joined")

cap_thickness = max(wall_thickness, 1.2)
cap_radius = min(
    barrel_radius - radial_clearance,
    max(pin_radius + radial_clearance * 2.0, pin_radius + 0.8),
)
pin_start = -length / 2.0 - axial_clearance - cap_thickness
pin_length = length + 2.0 * axial_clearance + 2.0 * cap_thickness
pin = x_cylinder(pin_start, pin_length, pin_radius, "Captive pin")
moving = moving.union(pin, name="Captive pin joined")

left_cap = x_cylinder(pin_start, cap_thickness, cap_radius, "Left captive cap")
right_cap = x_cylinder(length / 2.0 + axial_clearance, cap_thickness, cap_radius, "Right captive cap")
moving = moving.union(left_cap, name="Left cap joined").union(right_cap, name="Right cap joined")

# Printable contact lands are included as tunable stop material. The requested
# travel angle remains a kinematic preview limit until collision validation
# becomes available.
land_length = min(10.0, length * 0.30)
fixed_stop = box(
    land_length,
    stop_thickness,
    leaf_thickness + stop_thickness,
    (0.0, -(leaf_gap / 2.0 + stop_thickness), stop_thickness / 2.0),
    "Fixed stop land",
)
moving_stop = box(
    land_length,
    stop_thickness,
    leaf_thickness + stop_thickness,
    (0.0, leaf_gap / 2.0 + stop_thickness, stop_thickness / 2.0),
    "Moving stop land",
)
fixed = fixed.union(fixed_stop, name="Fixed stop land joined")
moving = moving.union(moving_stop, name="Moving stop land joined")

context = get_default_context()
joint_result = context.registry.call(
    "create_kinematic_joint",
    {
        "joint_id": "print-in-place-hinge",
        "type": "revolute",
        "parent_shape_id": fixed.shape_id,
        "child_shape_id": moving.shape_id,
        "axis": (1.0, 0.0, 0.0),
        "origin_mm": (0.0, 0.0, 0.0),
        "lower_limit": 0.0,
        "upper_limit": math.radians(PARAMS["travel_deg"]),
        "label": "Print-in-place hinge travel",
        "metadata": {
            "forma_target_ref": PARAMS["moving_ref"],
            "forma_parent_ref": PARAMS["fixed_ref"],
            "notes": "Rigid preview from OpenCAD; physical stop contact is not collision-validated.",
        },
    },
)
if not joint_result.ok:
    raise RuntimeError("OpenCAD print-in-place joint creation failed: " + joint_result.message)

FORMA_EXPORT_SHAPE_IDS = [fixed.shape_id, moving.shape_id]
FORMA_GEOMETRY_BY_REF = {
    PARAMS["fixed_ref"]: fixed.shape_id,
    PARAMS["moving_ref"]: moving.shape_id,
}
FORMA_MECHANISM_METADATA = {
    "family": "print-in-place mechanism",
    "benchmark": PARAMS["kind"],
    "body_count": 2,
    "clearance": {
        "radial_mm": radial_clearance,
        "axial_mm": axial_clearance,
        "knuckle_wall_mm": wall_thickness,
    },
    "travel_deg": PARAMS["travel_deg"],
    "stop_thickness_mm": stop_thickness,
    "process": PARAMS["process"],
    "post_print": (
        "Allow the assembly to cool fully, remove stringing/support debris, then free the hinge gradually. "
        "Do not force a fused joint; recalibrate clearance instead."
    ),
    "limitations": (
        "Clearance is printer/material/orientation dependent. The stop lands are printable contact features; "
        "their exact contact angle is not yet interference-validated."
    ),
}

model = fixed
''' % payload


def _monolithic_flexure_source(spec: MonolithicFlexureBenchmark) -> str:
    payload = repr(spec.model_dump(mode="json"))
    return '''from opencad import Part
import math

PARAMS = %s

def box(length, width, height, center, name):
    return Part(name=name).box(length, width, height, name=name).translate(center, name=name + " positioned")

body_length = PARAMS["rigid_body_length_mm"]
body_width = PARAMS["rigid_body_width_mm"]
body_thickness = PARAMS["rigid_body_thickness_mm"]
flexure_length = PARAMS["flexure_length_mm"]
flexure_width = PARAMS["flexure_width_mm"]
flexure_thickness = PARAMS["flexure_thickness_mm"]
overlap = max(0.5, min(1.5, flexure_length * 0.12))

left_center_x = -(flexure_length / 2.0 + body_length / 2.0 - overlap)
right_center_x = -left_center_x

left = box(body_length, body_width, body_thickness, (left_center_x, 0.0, 0.0), "Fixed rigid region")
right = box(body_length, body_width, body_thickness, (right_center_x, 0.0, 0.0), "Moving rigid region")
flexure = box(
    flexure_length + 2.0 * overlap,
    flexure_width,
    flexure_thickness,
    (0.0, 0.0, 0.0),
    "Flexure web",
)

model = left.union(flexure, name="Fixed region to flexure").union(right, name="Monolithic flexure body")
FORMA_EXPORT_SHAPE_IDS = [model.shape_id]
# A monolithic flexure is one solid. Both semantic regions intentionally map
# to that same geometry until subshape/region ownership is available.
FORMA_GEOMETRY_BY_REF = {
    PARAMS["fixed_ref"]: model.shape_id,
    PARAMS["moving_ref"]: model.shape_id,
}

axis_y = -1.0 if PARAMS["bend_direction"] == "positive_z" else 1.0
limit = math.radians(PARAMS["nominal_travel_deg"])
samples = []
for sample_index in range(PARAMS["preview_samples"]):
    progress = sample_index / float(PARAMS["preview_samples"] - 1)
    angle = limit * progress
    samples.append({
        "progress": progress,
        "value": angle,
        "unit": "radian",
        "transform": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_quaternion_xyzw": [0.0, axis_y * math.sin(angle / 2.0), 0.0, math.cos(angle / 2.0)],
        },
    })

FORMA_COMPLIANT_PREVIEW = {
    "source": "forma-compliant-approximation",
    "method": "rigid-tip-proxy",
    "structural_validation": False,
    "warning": (
        "Approximate visualization only. This does not compute flexure strain, stress, fatigue life, "
        "or a physically deformed solid."
    ),
    "tracks": [{
        "id": "flexure-bend-preview",
        "label": "Approximate flexure bend",
        "type": "compliant",
        "target_ref": PARAMS["moving_ref"],
        "parent_ref": PARAMS["fixed_ref"],
        "lower_limit": 0.0,
        "upper_limit": limit,
        "unit": "radian",
        "axis": [0.0, axis_y, 0.0],
        "notes": "Rigid-tip proxy around the flexure center; not structural validation.",
        "samples": samples,
    }],
}
FORMA_MECHANISM_METADATA = {
    "family": "monolithic compliant mechanism",
    "benchmark": PARAMS["kind"],
    "body_count": 1,
    "flexure": {
        "thickness_mm": flexure_thickness,
        "width_mm": flexure_width,
        "length_mm": flexure_length,
        "bend_direction": PARAMS["bend_direction"],
        "nominal_travel_deg": PARAMS["nominal_travel_deg"],
    },
    "process": "additive-manufacturing benchmark",
    "limitations": (
        "The CAD is one connected solid. Preview motion is approximate only; material strain, stress, "
        "fatigue life, and print anisotropy are not validated."
    ),
}

model
''' % payload
