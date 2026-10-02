"""Read a saved active Inventor part through COM; never open or modify source CAD.

Only constant, untapered XY rectangle/circle extrusions are reconstructed. Every
other feature remains in the inventory as unsupported. Sketch constraints and
equations are not claimed as preserved by this first extraction adapter.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .models import MigrationModel


def _items(collection):
    return [collection.Item(index) for index in range(1, collection.Count + 1)]


def _point(point):
    return [float(point.X) * 10, float(point.Y) * 10, float(point.Z) * 10]


def _literal(parameter):
    # Equations and links require an explicit mapping, even when currently numeric.
    if not re.fullmatch(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*(?:mm|cm|m|in|deg|rad)?\s*", str(parameter.Expression)):
        raise ValueError("Equations or linked dimensions require a reviewed adapter")
    return float(parameter.Value)


def _extrusion(feature, constants, ident, parameters, first):
    definition = feature.Definition
    if definition.IsTwoDirectional or definition.ExtentType != constants.kDistanceExtent:
        raise ValueError("Only one-sided distance extents are supported")
    if definition.Extent.Direction != constants.kPositiveExtentDirection or abs(_literal(definition.TaperAngle)) > 1e-10:
        raise ValueError("Only positive untapered extrusions are supported")
    profile = definition.Profile
    sketch = profile.Parent
    normal = sketch.PlanarEntityGeometry.Normal
    if abs(normal.X) > 1e-8 or abs(normal.Y) > 1e-8 or abs(normal.Z - 1) > 1e-8:
        raise ValueError("Sketch plane must face global +Z")
    if profile.Count != 1:
        raise ValueError("Nested or multiple profile regions are unsupported")
    path = profile.Item(1)
    if path.TextBoxPath or not path.Closed or not path.AddsMaterial:
        raise ValueError("A closed additive profile is required")
    for constraint in _items(sketch.DimensionConstraints):
        _literal(constraint.Parameter)
    operations = {constants.kNewBodyOperation: "new", constants.kJoinOperation: "new" if first else "join",
                  constants.kCutOperation: "cut"}
    if definition.Operation not in operations:
        raise ValueError("Unsupported solid operation")
    result = {"operation": operations[definition.Operation]}
    entities = _items(path)
    if len(entities) == 1 and entities[0].SketchEntity.Type == constants.kSketchCircleObject:
        circle = entities[0].SketchEntity.Geometry
        result.update(profile="circle", origin=_point(sketch.SketchToModelSpace(circle.Center)), radius=float(circle.Radius) * 10)
    elif len(entities) == 4 and all(entity.SketchEntity.Type == constants.kSketchLineObject for entity in entities):
        points = [_point(sketch.SketchToModelSpace(entity.StartSketchPoint.Geometry)) for entity in entities]
        xs, ys, zs = ([round(p[axis], 7) for p in points] for axis in range(3))
        if len(set(xs)) != 2 or len(set(ys)) != 2 or len(set(zs)) != 1 or len(set(zip(xs, ys))) != 4:
            raise ValueError("Profile must be an axis-aligned rectangle")
        for a, b in zip(points, points[1:] + points[:1]):
            if abs(a[0]-b[0]) > 1e-7 and abs(a[1]-b[1]) > 1e-7:
                raise ValueError("Diagonal rectangle edges are unsupported")
        result.update(profile="rectangle", origin=[min(xs), min(ys), zs[0]], width=max(xs)-min(xs), height=max(ys)-min(ys))
    else:
        raise ValueError("Only one complete circle or four straight rectangle edges are supported")
    result["depth"] = _literal(definition.Extent.Distance) * 10
    for dimension in ("width", "height", "radius", "depth"):
        if dimension in result:
            key = f"{ident}_{dimension}"
            parameters[key] = result[dimension]
            result[dimension] = key
    return result


def extract_document(document, constants, application_version: str) -> MigrationModel:
    """Testable COM boundary. Exceptions never turn missing features into success."""
    path = Path(str(document.FullFileName))
    if document.DocumentType != constants.kPartDocumentObject or path.suffix.lower() != ".ipt" or not path.is_file():
        raise ValueError("Activate a saved Inventor .ipt part")
    if document.Dirty:
        raise ValueError("Save the active part before extraction")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    component = document.ComponentDefinition
    features, parameters = [], {}
    for index, feature in enumerate(_items(component.Features)):
        ident = f"F{index+1}"
        entry = {"id": ident, "name": str(feature.Name), "kind": "unsupported", "source_type": str(feature.Type),
                 "suppressed": bool(feature.Suppressed), "depends_on": [features[-1]["id"]] if features else [],
                 "provenance": {"method": "source_api", "confidence": 1.0, "evidence": f"Inventor PartFeatures.Item({index+1})"}}
        try:
            if feature.Type != constants.kExtrudeFeatureObject or feature.Suppressed:
                raise ValueError("Unsupported or suppressed source feature")
            extracted = {}
            entry.update(_extrusion(feature, constants, ident, extracted, index == 0))
            entry["kind"] = "extrude"
            parameters.update(extracted)
            entry["provenance"]["evidence"] += "; API profile geometry sampled; independent named dimensions reconstructed"
        except (AttributeError, ValueError) as exc:
            entry["provenance"]["evidence"] += "; " + str(exc)
        features.append(entry)
    # Built-in property set identifiers do not depend on the Inventor UI language.
    design = document.PropertySets.Item("{32853F0F-3444-11D1-9E93-0060B03C1CA6}")
    summary = document.PropertySets.Item("{F29F85E0-4FF9-1068-AB91-08002B27B3D9}")
    metadata = {"part_number": str(design.ItemByPropId(5).Value), "description": str(design.ItemByPropId(29).Value),
                "material": str(design.ItemByPropId(20).Value), "revision": str(summary.ItemByPropId(9).Value)}
    custom = document.PropertySets.Item("{D5CDD505-2E9C-101B-9397-08002B2CF9AE}")
    properties = {}
    for prop in _items(custom):
        name = str(prop.Name)
        compact = "".join(c for c in name.lower() if c.isalnum())
        if any(marker in compact for marker in ("password", "secret", "token", "apikey", "authorization")):
            raise ValueError("Credential-like custom CAD properties must be removed before extraction")
        # Keep original labels, including spaces/unicode, within the strict
        # Identifier -> Text contract; stable keys avoid normalization collisions.
        key = "custom_" + hashlib.sha256(name.encode()).hexdigest()[:32]
        properties[key] = json.dumps({"name": name, "value": str(prop.Value)}, ensure_ascii=False)
    metadata["properties"] = properties
    bodies = _items(component.SurfaceBodies)
    if not bodies or any(not body.IsSolid for body in bodies):
        raise ValueError("Source must contain solid bodies only")
    boxes = [body.RangeBox for body in bodies]
    geometry = {"body_count": len(bodies), "volume_mm3": float(component.MassProperties.Volume) * 1000,
                "minimum_mm": [min(_point(box.MinPoint)[i] for box in boxes) for i in range(3)],
                "maximum_mm": [max(_point(box.MaxPoint)[i] for box in boxes) for i in range(3)]}
    with path.open("rb") as stream:
        if document.Dirty or hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("Source changed during extraction; save and retry")
    return MigrationModel.model_validate({"source": {"name": path.name, "sha256": digest, "system": "inventor",
        "version": application_version, "units": "mm"}, "features": features, "parameters": parameters,
        "metadata": metadata, "source_geometry": geometry, "inventory_complete": True,
        "inventory_notes": "Read saved active Inventor part via COM. Original sketch constraints, parameter names and dependencies are not retained; constant dimensions become independent F<n>_<dimension> parameters. Custom property values are text; custom_<hash> entries contain JSON with the original name and value. Unsupported features block reconstruction."})


def extract_active() -> MigrationModel:
    """Requires Windows, licensed running Inventor and the optional pywin32 package."""
    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError("Inventor extraction requires Windows with pywin32 and licensed Inventor") from exc
    try:
        app = win32com.client.gencache.EnsureDispatch(win32com.client.GetActiveObject("Inventor.Application"))
        return extract_document(app.ActiveDocument, win32com.client.constants, str(app.SoftwareVersion.DisplayVersion))
    except Exception as exc:
        raise RuntimeError("Inventor extraction failed. Activate a saved part and review its supported feature inventory.") from exc
