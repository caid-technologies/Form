"""Translate existing declarative Form operations without inventing a feature tree."""
import hashlib
import json

from .models import MigrationModel


def from_project(state) -> MigrationModel:
    operations = state.mechanical.cad_operations if state.mechanical else None
    if not operations:
        raise ValueError("This project has no saved declarative CAD operations; a STEP file alone has no recoverable feature history")
    source = json.dumps([item.model_dump(mode="json") for item in operations], sort_keys=True, allow_nan=False).encode()
    features, parameters = [], {}
    for index, item in enumerate(operations):
        ident = f"F{index+1}"
        x, y, z = item.center.x_mm, item.center.y_mm, item.center.z_mm
        sx, sy, sz = item.size.x_mm, item.size.y_mm, item.size.z_mm
        dims = {"depth": sz, **({"width": sx, "height": sy} if item.shape == "box" else {"radius": sx/2})}
        parameters.update({f"{ident}_{key}": value for key, value in dims.items()})
        features.append({"id": ident, "name": f"{item.operation} {item.shape} {index+1}",
            "kind": "unsupported" if item.axis_labels else "extrude", "source_type": "Form CadOperation",
            "depends_on": [features[-1]["id"]] if features else [],
            "operation": "cut" if item.operation == "cut" else "new" if index == 0 else "join",
            "profile": "rectangle" if item.shape == "box" else "circle",
            "origin": [x-sx/2, y-sy/2, z-sz/2] if item.shape == "box" else [x, y, z-sz/2],
            **{key: f"{ident}_{key}" for key in dims},
            "provenance": {"method": "source_api", "confidence": 1.0,
                           "evidence": f"Saved mechanical.cad_operations[{index}]; axis engraving requires an extended adapter"}})
    return MigrationModel.model_validate({"source": {"system": "form", "name": "Form declarative CAD operations",
        "sha256": hashlib.sha256(source).hexdigest(), "units": "mm"}, "features": features, "parameters": parameters,
        "inventory_complete": True, "inventory_notes": "The source hash identifies the saved operation list, not a native CAD file. Independent editable dimensions are created for the declared primitives. Source geometry measurements are not available from this conversion."})
