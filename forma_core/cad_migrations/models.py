"""Versioned CAD history contract consumed by deterministic target emitters."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")]
Text = Annotated[str, StringConstraints(max_length=4096)]
Number = Annotated[float, Field(strict=True, ge=-10000, le=10000)]
Length = Number | Identifier
SCALE = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceDocument(Contract):
    """Source identity supplied by the extraction process, never an invented verification."""
    name: Text
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    system: Literal["solidworks", "creo", "inventor"]
    version: Text = "unspecified"
    units: Literal["mm", "cm", "m", "in"] = "mm"


class Provenance(Contract):
    method: Literal["source_api", "human", "ai_inferred"]
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    confidence: float = Field(ge=0, le=1)


class Feature(Contract):
    """One Z-axis extrusion; unsupported source features remain explicit inventory entries."""
    id: Identifier
    name: Text
    kind: Literal["extrude", "unsupported"]
    source_type: Text
    provenance: Provenance
    depends_on: list[Identifier] = Field(default_factory=list, max_length=200)
    suppressed: bool = False
    operation: Literal["new", "join", "cut"] = "new"
    profile: Literal["rectangle", "circle"] = "rectangle"
    # Placement is fixed in the source coordinate frame; parameterized placement is not v1.
    origin: tuple[Number, Number, Number] = (0.0, 0.0, 0.0)
    width: Length | None = None
    height: Length | None = None
    radius: Length | None = None
    depth: Length | None = None


class Metadata(Contract):
    part_number: Text = ""
    revision: Text = ""
    description: Text = ""
    material: Text = ""
    properties: dict[Identifier, Text] = Field(default_factory=dict, max_length=100)


class MigrationModel(Contract):
    """A source inventory plus reconstructed feature intent, not a parser for proprietary files."""
    format: Literal["forma-cad-history"] = "forma-cad-history"
    version: Literal[1] = 1
    source: SourceDocument
    metadata: Metadata = Field(default_factory=Metadata)
    parameters: dict[Identifier, Number] = Field(default_factory=dict, max_length=200)
    features: list[Feature] = Field(min_length=1, max_length=200)
    inventory_complete: bool = False
    inventory_notes: Text = ""

    @model_validator(mode="after")
    def consistent_history(self):
        ids = [feature.id for feature in self.features]
        if len(set(ids)) != len(ids):
            raise ValueError("Source feature IDs must be unique")
        known: set[str] = set()
        for feature in self.features:
            if len(feature.depends_on) != len(set(feature.depends_on)):
                raise ValueError(f"Duplicate dependency in {feature.id}")
            if any(item not in known for item in feature.depends_on):
                raise ValueError(f"Missing, forward or cyclic dependency in {feature.id}")
            known.add(feature.id)
            for field in ("width", "height", "radius", "depth"):
                value = getattr(feature, field)
                if isinstance(value, str) and value not in self.parameters:
                    raise ValueError(f"Unknown dimension parameter in {feature.id}.{field}")
            if feature.kind == "extrude":
                required = ("width", "height", "depth") if feature.profile == "rectangle" else ("radius", "depth")
                forbidden = ("radius",) if feature.profile == "rectangle" else ("width", "height")
                if any(getattr(feature, field) is not None for field in forbidden):
                    raise ValueError(f"Ambiguous profile dimensions in {feature.id}")
                for field in required:
                    value = getattr(feature, field)
                    if value is None or not 0 < self.resolve(value) <= 10000:
                        raise ValueError(f"Positive dimension up to 10000 mm required in {feature.id}.{field}")
            if any(abs(value * SCALE[self.source.units]) > 10000 for value in feature.origin):
                raise ValueError("Feature placement exceeds 10000 mm")
        if any(abs(value * SCALE[self.source.units]) > 10000 for value in self.parameters.values()):
            raise ValueError("Parameter exceeds 10000 mm")
        for key in self.metadata.properties:
            if any(marker in key.lower() for marker in ("secret", "token", "password", "apikey", "api_key", "authorization")):
                raise ValueError("Credential-like property names are not CAD metadata")
        return self

    def resolve(self, value: Length) -> float:
        """Resolve a named or literal source length to millimeters; no expression evaluation."""
        return (self.parameters[value] if isinstance(value, str) else value) * SCALE[self.source.units]

    def normalized(self) -> dict:
        """Return the same history with all literal lengths and named parameters in millimeters."""
        result = self.model_dump(mode="json")
        scale = SCALE[self.source.units]
        result["source"]["units"] = "mm"
        result["parameters"] = {key: value * scale for key, value in self.parameters.items()}
        for feature in result["features"]:
            feature["origin"] = [value * scale for value in feature["origin"]]
            for field in ("width", "height", "radius", "depth"):
                if isinstance(feature[field], (int, float)):
                    feature[field] *= scale
        return result
