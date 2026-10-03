from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, component_detail_payload


PROJECT_OBJECT_TYPE = "forma.project"
PROJECT_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$")


class ProjectNamespaceDescriptor(BaseModel):
    name: str = Field(..., description="Stable dotted namespace, for example product.mech or project.docs.")
    label: str
    description: str
    scope: str = Field(..., description="High-level owner scope, usually project or product.")


class ProjectNamespaceRegistry:
    def __init__(self, descriptors: Iterable[ProjectNamespaceDescriptor]) -> None:
        self.descriptors = tuple(descriptors)
        self.names = tuple(descriptor.name for descriptor in self.descriptors)

    def get(self, namespace: str) -> Optional[ProjectNamespaceDescriptor]:
        normalized = normalize_project_namespace(namespace)
        if normalized is None:
            return None
        return next((descriptor for descriptor in self.descriptors if descriptor.name == normalized), None)

    def contains(self, namespace: str) -> bool:
        return self.get(namespace) is not None


class ProjectAttributeMeta(BaseModel):
    namespace: str = Field(..., description="Namespace that owns this attribute, for example product.electrical.")
    attribute: str = Field(..., description="Stable attribute key inside the namespace payload.")
    label: str = Field(..., description="Human-readable attribute label.")
    source_path: str = Field(..., description="Dotted object path for trace/debug use.")
    value_type: str = Field(..., description="JSON-compatible value type for this attribute.")
    version: int = Field(1, ge=1)
    item_kind: Optional[str] = Field(None, description="Semantic kind for list items, for example component or net.")
    item_count: int = Field(0, ge=0)


class ProjectAttributeItemMeta(BaseModel):
    namespace: str = Field(..., description="Namespace that owns this item.")
    attribute: str = Field(..., description="Attribute that owns this item.")
    index: int = Field(..., ge=0)
    item_id: str = Field(..., description="Stable local identity for this item.")
    label: str = Field(..., description="Human-readable item label.")
    source_path: str = Field(..., description="Dotted object path for trace/debug use.")
    value_type: str = Field(..., description="JSON-compatible value type for this item.")
    item_kind: str = Field(..., description="Semantic kind, for example component, net, or assembly_step.")
    ref_des: Optional[str] = Field(None, description="Reference designator when the item represents a physical/electrical component.")
    category: Optional[str] = None
    part_number: Optional[str] = None


class ProjectAttributeItemObject(BaseModel):
    meta: ProjectAttributeItemMeta
    value: Any = None

    @property
    def item_id(self) -> str:
        return self.meta.item_id

    @property
    def label(self) -> str:
        return self.meta.label

    @property
    def item_kind(self) -> str:
        return self.meta.item_kind


class ProjectAttributeObject(BaseModel):
    meta: ProjectAttributeMeta
    value: Any = None
    items: list[ProjectAttributeItemObject] = Field(default_factory=list)

    @property
    def name(self) -> str:
        return self.meta.attribute

    @property
    def label(self) -> str:
        return self.meta.label

    def get_item(self, item_id: str) -> Optional[ProjectAttributeItemObject]:
        normalized = str(item_id).strip().lower()
        if not normalized:
            return None
        return next((item for item in self.items if item.item_id.lower() == normalized), None)


DEFAULT_PROJECT_NAMESPACES = ProjectNamespaceRegistry(
    (
        ProjectNamespaceDescriptor(
            name="project.meta",
            label="Project Metadata",
            description="Object identity, runtime metadata, source usage, and workspace-level state.",
            scope="project",
        ),
        ProjectNamespaceDescriptor(
            name="project.docs",
            label="Project Documentation",
            description="Build docs, assembly text, validation report, fabrication notes, and exported guidance.",
            scope="project",
        ),
        ProjectNamespaceDescriptor(
            name="project.history",
            label="Project History",
            description="Version history, iteration decisions, and revision lineage.",
            scope="project",
        ),
        ProjectNamespaceDescriptor(
            name="product.overview",
            label="Product Overview",
            description="Product intent, requirements, constraints, and top-level description.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.architecture",
            label="Product Architecture",
            description="Purpose-driven hierarchy of product, electrical, mechanical, firmware, and nested systems.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.electrical",
            label="Product Electrical",
            description="Components, nets, buses, pin mappings, power rails, and electrical calculations.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.bom",
            label="Product BOM",
            description="Bill of materials and cost rollups.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.mech",
            label="Product Mechanical",
            description="Enclosure, CAD/fabrication sources, dimensions, placements, and mechanical constraints.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.firmware",
            label="Product Firmware",
            description="Firmware notes, pin behavior, control logic, and embedded software artifacts.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.visuals",
            label="Product Visuals",
            description="Generated product imagery, render metadata, visual sequences, and presentation assets.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.validation",
            label="Product Validation",
            description="Circuit validation, safety checks, and operation statuses.",
            scope="product",
        ),
        ProjectNamespaceDescriptor(
            name="product.assembly",
            label="Product Assembly",
            description="Step-by-step physical assembly and build workflow.",
            scope="product",
        ),
    )
)


class ProjectNamespaceObject(BaseModel):
    name: str = Field(..., description="Stable dotted namespace, for example product.mech or project.docs.")
    label: str
    description: str
    scope: str = Field(..., description="High-level owner scope, usually project or product.")
    version: int = Field(1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    attributes: list[ProjectAttributeObject] = Field(default_factory=list)

    def get_attribute(self, attribute: str) -> Optional[ProjectAttributeObject]:
        normalized = str(attribute).strip()
        if not normalized:
            return None
        return next((item for item in self.attributes if item.name == normalized), None)


class FormaProjectObject(BaseModel):
    object_type: str = Field(PROJECT_OBJECT_TYPE, description="Stable object kind for Forma project objects.")
    object_id: str
    version: int = Field(1, ge=1)
    namespaces: list[ProjectNamespaceObject] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def get_namespace(self, namespace: str) -> Optional[ProjectNamespaceObject]:
        normalized = normalize_project_namespace(namespace)
        if normalized is None:
            return None
        return next((item for item in self.namespaces if item.name == normalized), None)

    def get_attribute(self, namespace: str, attribute: str) -> Optional[ProjectAttributeObject]:
        namespace_object = self.get_namespace(namespace)
        if namespace_object is None:
            return None
        return namespace_object.get_attribute(attribute)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_project_namespace(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    namespace = value.strip().lower()
    if not namespace:
        return None
    if not PROJECT_NAMESPACE_PATTERN.match(namespace):
        raise ValueError("Project namespace must be dotted, for example product.mech or project.docs.")
    return namespace


def is_known_project_namespace(value: str) -> bool:
    return DEFAULT_PROJECT_NAMESPACES.contains(value)


def project_namespace_descriptor(namespace: str) -> ProjectNamespaceDescriptor:
    normalized = normalize_project_namespace(namespace)
    if normalized is None:
        raise ValueError("Project namespace is required.")
    descriptor = DEFAULT_PROJECT_NAMESPACES.get(normalized)
    if descriptor:
        return descriptor
    scope = normalized.split(".", 1)[0]
    label = " ".join(part.replace("-", " ").replace("_", " ").title() for part in normalized.split("."))
    return ProjectNamespaceDescriptor(
        name=normalized,
        label=label,
        description=f"Custom project object namespace {normalized}.",
        scope=scope,
    )


def list_project_namespaces() -> list[ProjectNamespaceDescriptor]:
    return list(DEFAULT_PROJECT_NAMESPACES.descriptors)


def coerce_hardware_ir(value: HardwareIntermediateRepresentation | dict[str, Any]) -> HardwareIntermediateRepresentation:
    if isinstance(value, HardwareIntermediateRepresentation):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        return HardwareIntermediateRepresentation.model_validate(value)
    raise TypeError("project object source must be a HardwareIntermediateRepresentation or HardwareIntermediateRepresentation dictionary.")


def project_object_version(ir: HardwareIntermediateRepresentation | dict[str, Any]) -> int:
    hardware_ir = coerce_hardware_ir(ir)
    metadata = hardware_ir.assembly_metadata or {}
    raw_value = metadata.get("revision")
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return max(1, len(hardware_ir.project_version_history or []))


def _canonical_object_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return str(uuid.uuid4())


def _project_object_metadata(ir: HardwareIntermediateRepresentation) -> dict[str, Any]:
    metadata = ir.assembly_metadata or {}
    raw_object = metadata.get("project_object")
    return dict(raw_object) if isinstance(raw_object, dict) else {}


def _namespace_versions(ir: HardwareIntermediateRepresentation, namespaces: Iterable[str]) -> dict[str, int]:
    object_metadata = _project_object_metadata(ir)
    raw_versions = object_metadata.get("namespace_versions")
    previous_versions = raw_versions if isinstance(raw_versions, dict) else {}
    project_version = project_object_version(ir)
    versions: dict[str, int] = {}
    for namespace in namespaces:
        raw_value = previous_versions.get(namespace)
        try:
            versions[namespace] = max(1, int(raw_value))
        except (TypeError, ValueError):
            versions[namespace] = project_version
    return versions


def _redact_payload_value(value: Any, *, key: str = "", max_string_chars: int = 4000) -> Any:
    lowered_key = key.lower()
    if isinstance(value, dict):
        return {
            item_key: _redact_payload_value(item_value, key=item_key, max_string_chars=max_string_chars)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload_value(item, max_string_chars=max_string_chars) for item in value]
    if isinstance(value, str):
        if ("image" in lowered_key or "data" in lowered_key or "visual" in lowered_key) and value.startswith("data:"):
            return f"<redacted data url: {len(value)} chars>"
        if len(value) > max_string_chars:
            return value[:max_string_chars] + f"...<truncated {len(value) - max_string_chars} chars>"
    return value


def _ir_payload(ir: HardwareIntermediateRepresentation) -> dict[str, Any]:
    return ir.model_dump(mode="json", exclude_none=True)


def _json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return value.__class__.__name__


def _title_from_key(value: str) -> str:
    return " ".join(piece for piece in value.replace("-", "_").split("_") if piece).title()


def _attribute_item_kind(attribute: str, item: Any) -> str:
    singular_by_attribute = {
        "assembly": "assembly_step",
        "buses": "bus",
        "cad_sources": "cad_source",
        "components": "component",
        "component_placements": "component_placement",
        "constraints": "constraint",
        "critical": "validation_issue",
        "fabrication_notes": "fabrication_note",
        "info": "validation_issue",
        "line_items": "bom_line_item",
        "nets": "net",
        "pin_mappings": "pin_mapping",
        "power_rails": "power_rail",
        "product_visual_sequence": "visual",
        "project_version_history": "history_entry",
        "requirements": "requirement",
        "spatial_relationships": "spatial_relationship",
        "warning": "validation_issue",
    }
    if attribute in singular_by_attribute:
        return singular_by_attribute[attribute]
    if isinstance(item, Mapping) and item.get("ref_des"):
        return "component"
    if attribute.endswith("s"):
        return attribute[:-1]
    return "item"


def _item_identity(attribute: str, index: int, item: Any) -> str:
    if isinstance(item, Mapping):
        for key in (
            "ref_des",
            "net_id",
            "bus_id",
            "rail_id",
            "mcu_pin",
            "step_num",
            "part_number",
            "view_id",
            "version",
            "name",
            "title",
            "category",
        ):
            value = item.get(key)
            if value not in (None, ""):
                return str(value)
    return f"{attribute}_{index + 1}"


def _item_label(attribute: str, item_id: str, item: Any) -> str:
    if isinstance(item, Mapping):
        for key in ("label", "name", "title", "description", "part_number", "net_id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if item.get("ref_des"):
            return str(item.get("ref_des"))
    if isinstance(item, str) and item.strip():
        return item.strip()[:120]
    return item_id


def _item_meta(namespace: str, attribute: str, item: Any, index: int) -> ProjectAttributeItemMeta:
    item_id = _item_identity(attribute, index, item)
    return ProjectAttributeItemMeta(
        namespace=namespace,
        attribute=attribute,
        index=index,
        item_id=item_id,
        label=_item_label(attribute, item_id, item),
        source_path=f"{namespace}.{attribute}[{index}]",
        value_type=_json_value_type(item),
        item_kind=_attribute_item_kind(attribute, item),
        ref_des=str(item.get("ref_des")) if isinstance(item, Mapping) and item.get("ref_des") else None,
        category=str(item.get("category")) if isinstance(item, Mapping) and item.get("category") else None,
        part_number=str(item.get("part_number")) if isinstance(item, Mapping) and item.get("part_number") else None,
    )


def _attribute_items(namespace: str, attribute: str, value: Any) -> list[ProjectAttributeItemObject]:
    if not isinstance(value, list):
        return []
    return [
        ProjectAttributeItemObject(
            meta=_item_meta(namespace, attribute, item, index),
            value=item,
        )
        for index, item in enumerate(value)
    ]


def build_project_attribute_objects(
    namespace: str,
    payload: Mapping[str, Any],
    *,
    version: int = 1,
) -> list[ProjectAttributeObject]:
    attributes: list[ProjectAttributeObject] = []
    for attribute, value in payload.items():
        items = _attribute_items(namespace, attribute, value)
        item_kind = items[0].item_kind if items else None
        attributes.append(
            ProjectAttributeObject(
                meta=ProjectAttributeMeta(
                    namespace=namespace,
                    attribute=attribute,
                    label=_title_from_key(attribute),
                    source_path=f"{namespace}.{attribute}",
                    value_type=_json_value_type(value),
                    version=version,
                    item_kind=item_kind,
                    item_count=len(items),
                ),
                value=value,
                items=items,
            )
        )
    return attributes


def _component_bom_payload(ir_payload: dict[str, Any]) -> dict[str, Any]:
    bom = ir_payload.get("bom") or []
    if bom:
        line_items = [dict(item) for item in bom if isinstance(item, dict)]
        electrical_total = round(sum(float(item.get("extended_price") or 0.0) for item in line_items), 2)
        return {
            "line_items": line_items,
            "component_count": sum(int(item.get("quantity") or 0) for item in line_items),
            "line_item_count": len(line_items),
            "estimated_electrical_cost": electrical_total,
            "estimated_total_cost": (ir_payload.get("overview") or {}).get("estimated_cost", electrical_total),
        }

    components = ir_payload.get("components") or []
    total = 0.0
    line_items = []
    for component in components:
        if not isinstance(component, dict):
            continue
        quantity = int(component.get("quantity") or 1)
        unit_price = float(component.get("unit_price") or 0.0)
        total += quantity * unit_price
        line_items.append(
            {
                "ref_des": component.get("ref_des"),
                "part_number": component.get("part_number"),
                "name": component.get("name"),
                "category": component.get("category"),
                "quantity": quantity,
                "unit_price": unit_price,
                "extended_price": round(quantity * unit_price, 2),
                "sourcing_url": component.get("sourcing_url"),
            }
        )
    return {
        "line_items": line_items,
        "component_count": len(line_items),
        "estimated_electrical_cost": round(total, 2),
        "estimated_total_cost": (ir_payload.get("overview") or {}).get("estimated_cost", round(total, 2)),
    }


def _visual_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    visual_keys = (
        "image",
        "visual",
        "render",
        "product_case",
        "product_front",
        "product_back",
        "product_side",
        "product_top",
    )
    return {
        key: value
        for key, value in metadata.items()
        if any(fragment in key.lower() for fragment in visual_keys)
    }


def _firmware_payload(ir_payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "pin_mappings": ir_payload.get("pin_mappings") or [],
        "buses": ir_payload.get("buses") or [],
        "firmware_notes": metadata.get("firmware_notes") or metadata.get("firmware") or [],
        "control_logic": metadata.get("control_logic") or [],
    }


def namespace_payload(ir: HardwareIntermediateRepresentation | dict[str, Any], namespace: str) -> dict[str, Any]:
    normalized = normalize_project_namespace(namespace)
    if normalized is None:
        raise ValueError("Project namespace is required.")

    hardware_ir = coerce_hardware_ir(ir)
    payload = _ir_payload(hardware_ir)
    metadata = payload.get("assembly_metadata") or {}
    cad_model = payload.get("cad_model")
    custom_payloads = metadata.get("namespace_payloads") if isinstance(metadata, dict) else None
    if isinstance(custom_payloads, dict) and isinstance(custom_payloads.get(normalized), dict):
        return _redact_payload_value(custom_payloads[normalized])

    if normalized == "project.meta":
        selected_payload = {
            "hardware_ir_version": payload.get("hardware_ir_version"),
            "assembly_metadata": metadata,
            "project_id": metadata.get("project_id"),
            "revision": metadata.get("revision"),
            "workflow": metadata.get("workflow"),
            "source_usage": metadata.get("source_usage"),
        }
    elif normalized == "project.docs":
        selected_payload = {
            "assembly": payload.get("assembly") or [],
            "fabrication_notes": payload.get("fabrication_notes") or [],
            "constraints": payload.get("constraints") or [],
            "validation": payload.get("validation") or {},
        }
    elif normalized == "project.history":
        selected_payload = {
            "project_version_history": payload.get("project_version_history") or [],
        }
    elif normalized == "product.overview":
        selected_payload = {
            "overview": payload.get("overview"),
            "requirements": payload.get("requirements"),
            "constraints": payload.get("constraints") or [],
        }
    elif normalized == "product.architecture":
        selected_payload = {
            "system_architecture": payload.get("system_architecture"),
        }
    elif normalized == "product.electrical":
        selected_payload = {
            "part_definitions": payload.get("part_definitions") or [],
            "components": [component_detail_payload(component) for component in hardware_ir.components],
            "bom": payload.get("bom") or [],
            "nets": payload.get("nets") or [],
            "buses": payload.get("buses") or [],
            "pin_mappings": payload.get("pin_mappings") or [],
            "power_rails": payload.get("power_rails") or [],
            "estimated_current_draw_ma": payload.get("estimated_current_draw_ma"),
        }
    elif normalized == "product.bom":
        selected_payload = _component_bom_payload(payload)
    elif normalized == "product.mech":
        selected_payload = {
            "mechanical": payload.get("mechanical"),
            "cad_model": cad_model,
            "fabrication_notes": payload.get("fabrication_notes") or [],
            "render_dimensions": metadata.get("render_dimensions"),
            "component_placement_count": metadata.get("component_placement_count"),
            "spatial_relationship_count": metadata.get("spatial_relationship_count"),
        }
    elif normalized == "product.firmware":
        selected_payload = _firmware_payload(payload, metadata)
    elif normalized == "product.visuals":
        selected_payload = _visual_payload(metadata)
    elif normalized == "product.validation":
        selected_payload = {
            "validation": payload.get("validation") or {},
            "is_valid": payload.get("is_valid"),
            "operation_statuses": metadata.get("operation_statuses") or [],
            "operation_summary": metadata.get("operation_summary"),
        }
    elif normalized == "product.assembly":
        selected_payload = {
            "assembly": payload.get("assembly") or [],
        }
    else:
        selected_payload = {}
    return _redact_payload_value(selected_payload)


def _namespace_names_for_ir(ir: HardwareIntermediateRepresentation, target_namespace: Optional[str] = None) -> list[str]:
    object_metadata = _project_object_metadata(ir)
    raw_versions = object_metadata.get("namespace_versions")
    previous_names = list(raw_versions.keys()) if isinstance(raw_versions, dict) else []
    names = [*DEFAULT_PROJECT_NAMESPACES.names, *previous_names]
    normalized_target = normalize_project_namespace(target_namespace)
    if normalized_target:
        names.append(normalized_target)
    return sorted(dict.fromkeys(names))


def build_project_object(ir: HardwareIntermediateRepresentation | dict[str, Any], *, target_namespace: Optional[str] = None) -> FormaProjectObject:
    from forma_core.workspaces.projects.context_governance import DEFAULT_CONTEXT_GOVERNANCE_POLICY

    hardware_ir = coerce_hardware_ir(ir)
    namespace_names = _namespace_names_for_ir(hardware_ir, target_namespace=target_namespace)
    versions = _namespace_versions(hardware_ir, namespace_names)
    metadata = hardware_ir.assembly_metadata or {}
    object_id = _canonical_object_id(metadata.get("project_id"))
    namespaces: list[ProjectNamespaceObject] = []
    for namespace in namespace_names:
        descriptor = project_namespace_descriptor(namespace)
        version = versions[namespace]
        payload = namespace_payload(hardware_ir, namespace)
        namespaces.append(ProjectNamespaceObject(
            name=namespace,
            label=descriptor.label,
            description=descriptor.description,
            scope=descriptor.scope,
            version=version,
            payload=payload,
            attributes=build_project_attribute_objects(namespace, payload, version=version),
        ))

    return FormaProjectObject(
        object_id=object_id,
        version=project_object_version(hardware_ir),
        namespaces=namespaces,
        metadata={
            "project_id": object_id,
            "revision": project_object_version(hardware_ir),
            "updated_at": metadata.get("iterated_at") or metadata.get("generated_at") or utc_now(),
            "namespace_versions": versions,
            "context_governance": DEFAULT_CONTEXT_GOVERNANCE_POLICY.model_dump(mode="json"),
        },
    )


def attach_project_object_metadata(
    ir: HardwareIntermediateRepresentation | dict[str, Any],
    *,
    target_namespace: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> HardwareIntermediateRepresentation:
    hardware_ir = coerce_hardware_ir(ir)
    normalized_target = normalize_project_namespace(target_namespace)
    namespace_names = _namespace_names_for_ir(hardware_ir, target_namespace=normalized_target)
    project_version = project_object_version(hardware_ir)
    versions = _namespace_versions(hardware_ir, namespace_names)
    if normalized_target:
        versions[normalized_target] = project_version
    else:
        versions = {namespace: project_version for namespace in namespace_names}

    metadata = dict(hardware_ir.assembly_metadata or {})
    object_id = _canonical_object_id(metadata.get("project_id"))
    metadata["project_id"] = object_id
    metadata["project_object"] = {
        "object_type": PROJECT_OBJECT_TYPE,
        "object_id": object_id,
        "version": project_version,
        "namespaces": namespace_names,
        "namespace_versions": versions,
        "target_namespace": normalized_target,
        "updated_at": updated_at or utc_now(),
    }
    hardware_ir.assembly_metadata = metadata
    return hardware_ir


def attach_project_object_metadata_to_dict(
    hardware_ir: dict[str, Any],
    *,
    target_namespace: Optional[str] = None,
) -> dict[str, Any]:
    ir = attach_project_object_metadata(hardware_ir, target_namespace=target_namespace)
    return ir.model_dump(mode="json")


__all__ = [
    "FormaProjectObject",
    "DEFAULT_PROJECT_NAMESPACES",
    "PROJECT_OBJECT_TYPE",
    "ProjectAttributeItemMeta",
    "ProjectAttributeItemObject",
    "ProjectAttributeMeta",
    "ProjectAttributeObject",
    "ProjectNamespaceDescriptor",
    "ProjectNamespaceObject",
    "ProjectNamespaceRegistry",
    "attach_project_object_metadata",
    "attach_project_object_metadata_to_dict",
    "build_project_attribute_objects",
    "build_project_object",
    "is_known_project_namespace",
    "list_project_namespaces",
    "namespace_payload",
    "normalize_project_namespace",
    "project_namespace_descriptor",
    "project_object_version",
]
