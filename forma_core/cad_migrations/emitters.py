"""Bounded target programs. Source content is data, never general-purpose code."""
from __future__ import annotations

import json

from .models import MigrationModel

COMMON = '''import hashlib
import json
from pathlib import Path
from uuid import uuid4

EXPECTED_HISTORY_SHA256 = "__DIGEST__"

def load_history():
    root = Path(__file__).resolve().parent
    data = (root / "rebuild.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED_HISTORY_SHA256:
        raise ValueError("Rebuild history changed. Regenerate and review the migration package.")
    return root, json.loads(data)

def expression(value):
    return "forma_" + value if isinstance(value, str) else str(value) + " mm"

def number(model, value):
    return model["parameters"][value] if isinstance(value, str) else value

def receipt(root, model, target, features, error=None):
    result = {"status": "failed" if error else "rebuilt_unverified", "target": target,
              "source_sha256": model["source"]["sha256"], "rebuild_sha256": EXPECTED_HISTORY_SHA256,
              "feature_map": features, "geometry_equivalence_verified": False, "error": error}
    with (root / ("execution-" + uuid4().hex + ".json")).open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)

'''

FUSION = '''def run(context):
    import adsk.core
    import adsk.fusion
    root, model = load_history()
    app = adsk.core.Application.get()
    document = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    features = []
    try:
        design = adsk.fusion.Design.cast(document.products.itemByProductType("DesignProductType"))
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        component = design.rootComponent
        component.name = model["source"]["name"]
        component.partNumber = model["metadata"]["part_number"]
        component.description = model["metadata"]["description"]
        for name, value in model["parameters"].items():
            design.userParameters.add("forma_" + name, adsk.core.ValueInput.createByString(str(value) + " mm"), "mm", "Forma source dimension")
        for item in model["features"]:
            x, y, z = item["origin"]
            plane = component.xYConstructionPlane
            if z != 0:
                plane_input = component.constructionPlanes.createInput()
                plane_input.setByOffset(plane, adsk.core.ValueInput.createByString(str(z) + " mm"))
                plane = component.constructionPlanes.add(plane_input)
            sketch = component.sketches.add(plane)
            sketch.name = item["name"] + " profile"
            point = adsk.core.Point3D.create
            if item["profile"] == "rectangle":
                width, height = number(model, item["width"]), number(model, item["height"])
                lines = sketch.sketchCurves.sketchLines.addTwoPointRectangle(point(x/10, y/10, 0), point((x+width)/10, (y+height)/10, 0))
                anchor = lines.item(0).startSketchPoint
                if not anchor.isFixed:
                    sketch.geometricConstraints.addFixed(anchor)
                # The first horizontal and vertical sides retain named dimension expressions.
                for index, key, orientation in ((0, "width", adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation),
                                                 (1, "height", adsk.fusion.DimensionOrientations.VerticalDimensionOrientation)):
                    line = lines.item(index)
                    dimension = sketch.sketchDimensions.addDistanceDimension(line.startSketchPoint, line.endSketchPoint,
                                                                             orientation, point((x+width)/10, (y+height)/10, 0))
                    dimension.parameter.expression = expression(item[key])
            else:
                radius = number(model, item["radius"])
                circle = sketch.sketchCurves.sketchCircles.addByCenterRadius(point(x/10, y/10, 0), radius/10)
                if not circle.centerSketchPoint.isFixed:
                    sketch.geometricConstraints.addFixed(circle.centerSketchPoint)
                dimension = sketch.sketchDimensions.addRadialDimension(circle, point((x+radius)/10, (y+radius)/10, 0))
                dimension.parameter.expression = expression(item["radius"])
            if sketch.profiles.count != 1:
                raise RuntimeError("Expected exactly one closed profile for " + item["id"])
            operations = {"new": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
                          "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
                          "cut": adsk.fusion.FeatureOperations.CutFeatureOperation}
            extrusions = component.features.extrudeFeatures
            inputs = extrusions.createInput(sketch.profiles.item(0), operations[item["operation"]])
            extent = adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByString(expression(item["depth"])))
            inputs.setOneSideExtent(extent, adsk.fusion.ExtentDirections.PositiveExtentDirection)
            if item["operation"] == "cut":
                inputs.participantBodies = [component.bRepBodies.item(0)]
            feature = extrusions.add(inputs)
            feature.name = item["name"]
            if feature.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
                raise RuntimeError("Fusion feature requires repair: " + item["id"])
            if component.bRepBodies.count != 1:
                raise RuntimeError("Rebuild no longer contains exactly one body")
            feature.attributes.add("Forma", "source_feature_id", item["id"])
            features.append({"source_id": item["id"], "target_name": feature.name, "timeline_index": feature.timelineObject.index})
        component.attributes.add("Forma", "migration", json.dumps({"source": model["source"], "metadata": model["metadata"]}, ensure_ascii=False))
        receipt(root, model, "fusion360", features)
        app.userInterface.messageBox("Forma rebuilt the supported feature history in a new unsaved design. "
                                    "Compare geometry, edit parameters, inspect metadata, then save. Material is reference text only.")
    except Exception as exc:
        receipt(root, model, "fusion360", features, str(exc))
        document.close(False)
        raise
'''

NX = '''def nx_expression(value):
    # The new NX part and all named expressions use millimeters.
    return "forma_" + value if isinstance(value, str) else str(value)

def main():
    import NXOpen
    import NXOpen.Features
    import NXOpen.GeometricUtilities
    root, model = load_history()
    session = NXOpen.Session.GetSession()
    part = session.Parts.NewDisplay("FormaMigration_" + uuid4().hex[:12], NXOpen.Part.Units.Millimeters)
    mark = session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Forma migration")
    features = []
    try:
        unit = part.UnitCollection.FindObject("MilliMeter")
        for name, value in model["parameters"].items():
            part.Expressions.CreateWithUnits("forma_" + name + "=" + str(value), unit)
        for item in model["features"]:
            origin = NXOpen.Point3d(*item["origin"])
            if item["profile"] == "rectangle":
                builder = part.Features.CreateBlockFeatureBuilder(NXOpen.Features.Feature.Null)
                builder.Type = NXOpen.Features.BlockFeatureBuilder.Types.OriginAndEdgeLengths
            else:
                builder = part.Features.CreateCylinderBuilder(NXOpen.Features.Feature.Null)
                builder.Type = NXOpen.Features.CylinderBuilder.Types.AxisDiameterAndHeight
            try:
                if item["profile"] == "rectangle":
                    builder.SetOriginAndLengths(origin, nx_expression(item["width"]), nx_expression(item["height"]), nx_expression(item["depth"]))
                else:
                    builder.Axis.Point = part.Points.CreatePoint(origin)
                    builder.Axis.Direction = part.Directions.CreateDirection(origin, NXOpen.Vector3d(0.0, 0.0, 1.0), NXOpen.SmartObject.UpdateOption.WithinModeling)
                    builder.Diameter.RightHandSide = "2 * (" + nx_expression(item["radius"]) + ")"
                    builder.Height.RightHandSide = nx_expression(item["depth"])
                operation = {"new": NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Create,
                             "join": NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Unite,
                             "cut": NXOpen.GeometricUtilities.BooleanOperation.BooleanType.Subtract}[item["operation"]]
                builder.BooleanOption.Type = operation
                if item["operation"] != "new":
                    builder.BooleanOption.SetTargetBodies(list(part.Bodies))
                if not builder.Validate():
                    raise RuntimeError("NX builder rejected feature " + item["id"])
                feature = builder.CommitFeature()
                if feature is None or len(list(part.Bodies)) != 1:
                    raise RuntimeError("NX did not produce exactly one solid body")
                feature.SetName(item["name"])
                feature.SetUserAttribute("Forma.SourceFeatureId", -1, item["id"], NXOpen.Update.Option.Now)
                features.append({"source_id": item["id"], "target_name": item["name"], "journal_id": feature.JournalIdentifier})
            finally:
                builder.Destroy()
        part.SetUserAttribute("Forma.SourceSHA256", -1, model["source"]["sha256"], NXOpen.Update.Option.Now)
        for key, value in model["metadata"].items():
            if key == "properties":
                for name, content in value.items():
                    part.SetUserAttribute("Forma.Property." + name, -1, content, NXOpen.Update.Option.Now)
            else:
                part.SetUserAttribute("Forma." + key, -1, value, NXOpen.Update.Option.Now)
        receipt(root, model, "nx", features)
        session.ListingWindow.Open()
        session.ListingWindow.WriteLine("Forma rebuilt supported primitives in a new unsaved part. Validate geometry and edit expressions before saving.")
    except Exception as exc:
        session.UndoToMark(mark, None)
        receipt(root, model, "nx", features, str(exc))
        raise

if __name__ == "__main__":
    main()
'''


def python_rebuild(target: str, digest: str) -> str:
    """Render a fixed native program bound to one normalized history digest."""
    return COMMON.replace("__DIGEST__", digest) + {"nx": NX, "fusion360": FUSION}[target]


def onshape_rebuild(model: MigrationModel) -> str:
    """Render inspectable FeatureScript with shared editable length parameters."""
    data = model.normalized()
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    def length(value):
        return "definition.p_" + value if isinstance(value, str) else f"({value} * millimeter)"
    lines = ['FeatureScript 2500;', 'import(path : "onshape/std/geometry.fs", version : "2500.0");', '',
             '// Bounded reconstruction; compare with source before engineering use.',
             'annotation { "Feature Type Name" : "Forma migration" }',
             'export const rebuildForma = defineFeature(function(context is Context, id is Id, definition is map)',
             'precondition', '{']
    for name, value in data["parameters"].items():
        lines.extend([f'    annotation {{ "Name" : {quote(name)} }}',
                      f'    isLength(definition.p_{name}, {{ (millimeter) : [-10000, {value}, 10000] }});'])
    lines.extend(['}', '{'])
    for index, feature in enumerate(data["features"]):
        x, y, z = feature["origin"]
        sketch = f's{index}'
        extrude = f'f{index}'
        lines.extend([f'    // Source feature: {feature["id"]}',
                      f'    var {sketch} = newSketchOnPlane(context, id + "{sketch}", {{ "sketchPlane" : plane(vector(0, 0, {z}) * millimeter, vector(0, 0, 1), vector(1, 0, 0)) }});'])
        if feature["profile"] == "rectangle":
            lines.append(f'    skRectangle({sketch}, "profile", {{ "firstCorner" : vector({x}, {y}) * millimeter, "secondCorner" : vector({x} * millimeter + {length(feature["width"])}, {y} * millimeter + {length(feature["height"])}) }});')
        else:
            lines.append(f'    skCircle({sketch}, "profile", {{ "center" : vector({x}, {y}) * millimeter, "radius" : {length(feature["radius"])} }});')
        lines.extend([f'    skSolve({sketch});',
                      f'    opExtrude(context, id + "{extrude}", {{ "entities" : qSketchRegion(id + "{sketch}"), "direction" : vector(0, 0, 1), "endBound" : BoundingType.BLIND, "endDepth" : {length(feature["depth"])} }});'])
        tool = f'qCreatedBy(id + "{extrude}", EntityType.BODY)'
        if index == 0:
            lines.append(f'    var body = {tool};')
        elif feature["operation"] == "cut":
            lines.append(f'    opBoolean(context, id + "b{index}", {{ "tools" : {tool}, "targets" : body, "operationType" : BooleanOperationType.SUBTRACTION }});')
        else:
            lines.append(f'    opBoolean(context, id + "b{index}", {{ "tools" : qUnion([body, {tool}]), "operationType" : BooleanOperationType.UNION }});')
        # Count only our bodies; unrelated Part Studio bodies are never boolean targets.
        lines.append(f'    if (size(evaluateQuery(context, qUnion([body, {tool}]))) != 1) throw regenError("Expected one connected solid after {feature["id"]}");')
        lines.append(f'    opDeleteBodies(context, id + "cleanup{index}", {{ "entities" : qCreatedBy(id + "{sketch}", EntityType.BODY) }});')
    lines.append(f'    setProperty(context, {{ "entities" : body, "propertyType" : PropertyType.NAME, "value" : {quote(data["source"]["name"])} }});')
    attribute = {"source": data["source"], "metadata": data["metadata"], "source_feature_ids": [feature.id for feature in model.features]}
    lines.append(f'    setAttribute(context, {{ "entities" : body, "name" : "FormaMigration", "attribute" : {quote(attribute)} }});')
    lines.extend(['});', ''])
    return "\n".join(lines)
