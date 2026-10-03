"""Bounded, declarative solid modelling. No agent-authored Python is executed."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CadDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x_mm: float = Field(gt=0, le=10_000)
    y_mm: float = Field(gt=0, le=10_000)
    z_mm: float = Field(gt=0, le=10_000)


class CadPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x_mm: float = Field(default=0, ge=-10_000, le=10_000)
    y_mm: float = Field(default=0, ge=-10_000, le=10_000)
    z_mm: float = Field(default=0, ge=-10_000, le=10_000)


class CadOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shape: Literal["box", "cylinder"]
    operation: Literal["add", "cut"] = "add"
    size: CadDimensions = Field(description="Exact solid size in mm. For a Z-axis cylinder x and y are equal diameters; z is height.")
    center: CadPosition = Field(default_factory=CadPosition)
    axis_labels: bool = Field(default=False, description="Engrave X, Y, Z into the positive faces of an added box, preserving its outside dimensions.")

    @model_validator(mode="after")
    def valid_primitive(self):
        if self.shape == "cylinder" and self.size.x_mm != self.size.y_mm:
            raise ValueError("Cylinder x and y diameters must match.")
        if self.axis_labels and (self.shape != "box" or self.operation != "add"):
            raise ValueError("Axis labels require an added box.")
        return self


def solid_cad_source(operations: list[CadOperation]) -> str:
    # Only validated numeric/enum/bool values enter this generated program.
    payload = repr([item.model_dump(mode="json") for item in operations])
    return '''from opencad import Part, Sketch

OPERATIONS = %s
# Closed outlines in a unit square; no system fonts are needed.
GLYPHS = {
    "X": [(0,0),(.2,0),(.5,.35),(.8,0),(1,0),(.62,.5),(1,1),(.8,1),(.5,.65),(.2,1),(0,1),(.38,.5)],
    "Y": [(.4,0),(.6,0),(.6,.42),(1,1),(.78,1),(.5,.62),(.22,1),(0,1),(.4,.42)],
    "Z": [(0,0),(1,0),(1,.18),(.28,.18),(1,.82),(1,1),(0,1),(0,.82),(.72,.82),(0,.18)],
}

def engrave(part, letter, plane, origin, u, v, size, depth):
    points = [(u + (x-.5)*size, v + (y-.5)*size) for x,y in GLYPHS[letter]]
    sketch = Sketch(plane=plane, origin=origin, name=letter + " label outline")
    for start, end in zip(points, points[1:] + points[:1]):
        sketch.line(start, end)
    cutter = Part(name=letter + " engraving").extrude(sketch, depth=depth + .01)
    return part.cut(cutter, name=letter + " engraved face")

model = None
for index, item in enumerate(OPERATIONS):
    x,y,z = (item["center"][key] for key in ("x_mm","y_mm","z_mm"))
    sx,sy,sz = (item["size"][key] for key in ("x_mm","y_mm","z_mm"))
    profile = Sketch(plane="XY", origin=(0,0,z-sz/2), name="Solid profile " + str(index))
    if item["shape"] == "box":
        profile.rect(sx, sy, origin=(x-sx/2,y-sy/2))
    else:
        profile.circle(sx/2, center=(x,y))
    part = Part(name=item["shape"]).extrude(profile, depth=sz)
    if item["axis_labels"]:
        depth = min(.5, min(sx,sy,sz)/20)
        part = engrave(part, "X", "YZ", (x+sx/2-depth,0,0), y,z,min(sy,sz)*.45,depth)
        part = engrave(part, "Y", "XZ", (0,y+sy/2-depth,0), x,z,min(sx,sz)*.45,depth)
        part = engrave(part, "Z", "XY", (0,0,z+sz/2-depth), x,y,min(sx,sy)*.45,depth)
    if model is None:
        model = part
    elif item["operation"] == "cut":
        model = model.cut(part, name="Cut " + str(index))
    else:
        model = model.union(part, name="Union " + str(index))

model
''' % payload
