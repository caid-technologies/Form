"""Translate Forma's semantic hardware hierarchy into an OpenCAD assembly spec.

OpenCAD owns the canonical AssemblyTree object.  Forma only prepares a
source-aware, JSON-safe specification that generated OpenCAD model scripts use
to construct that object after geometry IDs exist.
"""

from __future__ import annotations

from typing import Any

from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, SystemNode


OPENCAD_ASSEMBLY_SPEC_VERSION = 1


def _unique_id(preferred: str, used: set[str]) -> str:
    """Return a stable unique identifier without changing it unless necessary."""
    candidate = str(preferred or "").strip() or "component"
    if candidate not in used:
        return candidate
    index = 2
    while f"{candidate}:{index}" in used:
        index += 1
    return f"{candidate}:{index}"


def _placement_metadata(project: HardwareIntermediateRepresentation) -> dict[str, dict[str, Any]]:
    """Return mechanical placement metadata keyed by component reference."""
    mechanical = project.mechanical
    if mechanical is None:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for placement in mechanical.component_placements:
        result[placement.ref_des] = {
            "position_mm": [
                float(placement.position.x_mm),
                float(placement.position.y_mm),
                float(placement.position.z_mm),
            ],
            "size_mm": [
                float(placement.size.x_mm),
                float(placement.size.y_mm),
                float(placement.size.z_mm),
            ],
            "orientation_deg": [
                float(placement.orientation_deg.x_deg),
                float(placement.orientation_deg.y_deg),
                float(placement.orientation_deg.z_deg),
            ],
            "layer": placement.layer,
            "mounting_face": placement.mounting_face,
            "notes": placement.notes,
        }
    return result


def _append_system_node(
    node: SystemNode,
    *,
    components: dict[str, dict[str, Any]],
    used_ids: set[str],
) -> None:
    """Append one SystemNode subtree while preserving Forma system IDs."""
    node_id = str(node.system_id).strip()
    if not node_id:
        raise ValueError("Forma system architecture contains an empty system_id.")
    if node_id in used_ids:
        raise ValueError(f"Forma system architecture contains duplicate system_id '{node_id}'.")

    used_ids.add(node_id)
    child_ids = [str(child.system_id).strip() for child in node.children]
    components[node_id] = {
        "id": node_id,
        "name": node.name,
        "child_ids": child_ids,
        "geometry_binding": None,
        "metadata": {
            "source": "forma.system_architecture",
            "forma_system_id": node_id,
            "domain": node.domain,
            "purpose": node.purpose,
            "responsibilities": list(node.responsibilities),
            "constraints": list(node.constraints),
            "detail_owner": node.detail_owner,
        },
    }
    for child in node.children:
        _append_system_node(child, components=components, used_ids=used_ids)


def _benchmark_body_refs(project: HardwareIntermediateRepresentation) -> list[tuple[str, str]]:
    """Return stable semantic body IDs declared by a bounded mechanism benchmark."""
    mechanical = project.mechanical
    benchmark = mechanical.mechanism_benchmark if mechanical is not None else None
    if benchmark is None:
        return []

    kind = str(getattr(benchmark, "kind", ""))
    if kind == "spur_gear_pair":
        return [
            (str(getattr(benchmark, "driver_ref")), "Driver gear"),
            (str(getattr(benchmark, "driven_ref")), "Driven gear"),
        ]
    if kind == "print_in_place_hinge":
        return [
            (str(getattr(benchmark, "fixed_ref")), "Fixed hinge body"),
            (str(getattr(benchmark, "moving_ref")), "Moving hinge body"),
        ]
    if kind == "monolithic_flexure_hinge":
        return [
            (str(getattr(benchmark, "fixed_ref")), "Fixed flexure region"),
            (str(getattr(benchmark, "moving_ref")), "Moving flexure region"),
        ]
    return []


def build_opencad_assembly_spec(project: HardwareIntermediateRepresentation) -> dict[str, Any]:
    """Build the source-side specification used to create OpenCAD AssemblyTree.

    The specification deliberately does not define an alternative assembly
    model.  Generated OpenCAD code resolves geometry bindings and instantiates
    OpenCAD's own AssemblyComponent and AssemblyTree classes.

    Forma system IDs and component reference designators remain the semantic
    identities whenever they do not collide.  Geometry IDs are resolved later
    in the OpenCAD process and may change independently.
    """
    components: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()

    architecture = project.system_architecture
    if architecture is not None:
        root_id = str(architecture.root.system_id).strip()
        _append_system_node(
            architecture.root,
            components=components,
            used_ids=used_ids,
        )
        tree_name = (
            project.overview.title
            if project.overview is not None and project.overview.title.strip()
            else architecture.root.name
        )
    else:
        root_id = "project"
        used_ids.add(root_id)
        tree_name = (
            project.overview.title
            if project.overview is not None and project.overview.title.strip()
            else "Forma project"
        )
        components[root_id] = {
            "id": root_id,
            "name": tree_name,
            "child_ids": [],
            "geometry_binding": None,
            "metadata": {
                "source": "forma.hardware_ir",
                "synthetic": True,
            },
        }

    # The whole-system root selects every shape intentionally exported by the
    # generated OpenCAD model.  Child components can still select narrower
    # geometry through ref-des bindings.
    components[root_id]["geometry_binding"] = {"kind": "exported"}

    placement_by_ref = _placement_metadata(project)
    architecture_ids = set(components)
    unassigned_component_ids: list[str] = []

    for component in project.components:
        component_id = _unique_id(component.ref_des, used_ids)
        used_ids.add(component_id)
        configuration = dict(component.configuration or {})
        explicit_system_id = configuration.get("system_id")
        parent_id = (
            str(explicit_system_id).strip()
            if isinstance(explicit_system_id, str)
            and str(explicit_system_id).strip() in architecture_ids
            else None
        )

        metadata: dict[str, Any] = {
            "source": "forma.component",
            "forma_component_id": component.ref_des,
            "ref_des": component.ref_des,
            "part_definition_id": component.part_definition_id,
            "part_number": component.part_number,
            "category": component.category,
        }
        if component.ref_des in placement_by_ref:
            metadata["placement"] = placement_by_ref[component.ref_des]

        components[component_id] = {
            "id": component_id,
            "name": component.name or component.part_number or component.ref_des,
            "child_ids": [],
            "geometry_binding": (
                {"kind": "ref_des", "ref_des": component.ref_des}
                if component.ref_des in placement_by_ref
                else None
            ),
            "metadata": metadata,
        }

        if parent_id is not None:
            components[parent_id]["child_ids"].append(component_id)
        else:
            unassigned_component_ids.append(component_id)

    existing_source_refs = {
        str(item["metadata"].get("ref_des"))
        for item in components.values()
        if isinstance(item.get("metadata"), dict) and item["metadata"].get("ref_des")
    }
    for ref_des, label in _benchmark_body_refs(project):
        if not ref_des or ref_des in existing_source_refs:
            continue
        body_id = _unique_id(ref_des, used_ids)
        used_ids.add(body_id)
        components[body_id] = {
            "id": body_id,
            "name": label,
            "child_ids": [],
            "geometry_binding": {"kind": "ref_des", "ref_des": ref_des},
            "metadata": {
                "source": "forma.mechanism_benchmark",
                "forma_component_id": ref_des,
                "ref_des": ref_des,
            },
        }
        unassigned_component_ids.append(body_id)
        existing_source_refs.add(ref_des)

    if unassigned_component_ids:
        group_id = _unique_id(f"{root_id}.components", used_ids)
        used_ids.add(group_id)
        components[group_id] = {
            "id": group_id,
            "name": "Physical Components",
            "child_ids": unassigned_component_ids,
            "geometry_binding": None,
            "metadata": {
                "source": "forma.hardware_ir",
                "synthetic": True,
            },
        }
        components[root_id]["child_ids"].append(group_id)

    return {
        "schema_version": OPENCAD_ASSEMBLY_SPEC_VERSION,
        "id": root_id,
        "name": tree_name,
        "root_ids": [root_id],
        "components": components,
        "metadata": {
            "source": "forma.hardware_ir",
            "hardware_ir_version": project.hardware_ir_version,
        },
    }


__all__ = [
    "OPENCAD_ASSEMBLY_SPEC_VERSION",
    "build_opencad_assembly_spec",
]
