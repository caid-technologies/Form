"""Conservative capability planning: no feature is silently omitted."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import MigrationModel

ROUTES = (("solidworks", "onshape"), ("solidworks", "nx"), ("creo", "nx"), ("inventor", "fusion360"),
          ("form", "fusion360"), ("form", "onshape"), ("form", "nx"))


def history_bytes(model: MigrationModel) -> bytes:
    """Canonical serialization used to bind a plan to the exact source history."""
    return (json.dumps(model.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def plan_migration(model: MigrationModel, target: str, *, approve_inferred: bool = False) -> dict[str, Any]:
    """Return blockers, feature mappings and losses without invoking an AI or CAD application."""
    blockers = []
    if (model.source.system, target) not in ROUTES:
        blockers.append({"code": "unsupported_route", "feature_id": None})
    if not model.inventory_complete:
        blockers.append({"code": "incomplete_source_inventory", "feature_id": None})
    if model.source_geometry is not None and model.source_geometry.body_count != 1:
        blockers.append({"code": "single_solid_source_required", "feature_id": None})
    mappings = []
    previous = None
    envelope = None
    normalized_features = model.normalized()["features"]
    for feature in model.features:
        issues = []
        if feature.kind != "extrude":
            issues.append("unsupported_feature")
        if feature.suppressed:
            issues.append("suppression_not_supported")
        if feature.provenance.method == "ai_inferred" and not approve_inferred:
            issues.append("inferred_feature_requires_review")
        if feature.provenance.confidence < 0.8:
            issues.append("insufficient_evidence")
        if previous is None and (feature.operation != "new" or feature.depends_on):
            issues.append("first_feature_must_create_body")
        if previous is not None and (feature.operation == "new" or feature.depends_on != [previous]):
            issues.append("single_body_linear_history_required")
        if feature.kind == "extrude":
            # A necessary overlap check only. A bounding box is not a solid kernel.
            item = normalized_features[len(mappings)]
            x, y, z = item["origin"]
            value = lambda key: model.resolve(getattr(feature, key))
            if feature.profile == "circle":
                r = value("radius")
                bounds = ([x-r, y-r, z], [x+r, y+r, z+value("depth")])
            else:
                bounds = ([x, y, z], [x+value("width"), y+value("height"), z+value("depth")])
            if envelope is not None:
                overlaps = [min(envelope[1][i], bounds[1][i]) - max(envelope[0][i], bounds[0][i]) for i in range(3)]
                if feature.operation == "cut" and any(v <= 0 for v in overlaps):
                    issues.append("cut_outside_body_bounds")
                elif feature.operation == "join" and (any(v < 0 for v in overlaps) or sum(v == 0 for v in overlaps) > 1):
                    issues.append("disconnected_join")
            if feature.operation in {"new", "join"}:
                envelope = bounds if envelope is None else (
                    [min(a,b) for a,b in zip(envelope[0], bounds[0])],
                    [max(a,b) for a,b in zip(envelope[1], bounds[1])])
        blockers.extend({"code": issue, "feature_id": feature.id} for issue in issues)
        representation = {"onshape": "FeatureScript operation in a custom feature",
                          "nx": "parametric block/cylinder and boolean feature",
                          "fusion360": "dimensioned sketch and timeline extrusion"}.get(target, "unsupported")
        mappings.append({"source_id": feature.id, "source_type": feature.source_type, "target": representation,
                         "status": "blocked" if issues else "mapped", "provenance": feature.provenance.model_dump(),
                         "source_name": feature.name,
                         "source_dimensions_mm": {key: model.resolve(getattr(feature, key)) for key in ("width", "height", "radius", "depth") if getattr(feature, key) is not None},
                         "review_guidance": ("Keep this source feature unchanged until a supported adapter or a separately reviewed source-history replacement is supplied." if issues else
                                             "Rebuild as " + representation + "; compare named dimensions and native behavior before accepting.")})
        previous = feature.id
    return {
        "format": "forma-cad-migration-plan", "version": 1,
        "source_system": model.source.system, "target_system": target,
        "source_sha256": model.source.sha256,
        "history_sha256": hashlib.sha256(history_bytes(model)).hexdigest(),
        "status": "blocked" if blockers else "ready_for_rebuild",
        "approved_inferred_features": approve_inferred,
        "source_identity_verified": False,
        "native_execution_verified": False, "geometry_equivalence_verified": False,
        "blockers": blockers, "features": mappings,
        "limitations": [
            "Source inventory and source hash are supplied by the extraction process, not verified by a native file parser.",
            "Only one linear solid history of XY rectangles/circles extruded along +Z is supported.",
            "Placement is fixed; named length parameters are retained. Arbitrary equations and native sketch constraints are not reconstructed.",
            "Onshape operations are inside one editable custom feature, not separate original feature-tree nodes.",
            "NX uses equivalent parametric primitives rather than original source sketches.",
            "Metadata is retained in JSON and mapped to supported native attributes; material is reference text only.",
            "Assemblies, mates, drawings, PMI, configurations, patterns, fillets and suppressed features require an extended adapter.",
            "Native target execution, solid validity and comparison with source geometry remain required.",
            "Bounds preflight rejects obvious disjoint operations; it cannot prove intersection with the remaining solid.",
        ] + (["Source extraction note: " + model.inventory_notes] if model.inventory_notes else []),
    }
