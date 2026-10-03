from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from typing import List, Optional, Dict, Any, Iterable, Mapping, Literal
from datetime import datetime
import re
from forma_core.workspaces.projects.mechanism_benchmarks import MechanismBenchmark
from forma_core.workspaces.projects.solid_cad import CadOperation

# ==========================================
# 1. Base / Seed Component Database Schemas
# ==========================================

class PinDefinition(BaseModel):
    pin_id: str = Field(..., description="Unique pin identifier, e.g., '1', 'GND', 'D13'")
    name: str = Field(..., description="Pin functional name, e.g., 'VCC', 'TX', 'GPIO4'")
    pin_type: str = Field(..., description="Type of pin: Power, Ground, Digital, Analog, I2C, SPI, UART, PWM, Passive")
    voltage: Optional[float] = Field(None, description="Operating voltage of the pin in Volts, e.g., 3.3 or 5.0")
    description: Optional[str] = Field(None, description="Detailed description of the pin function")
    direction: Optional[str] = Field(None, description="Signal direction such as input, output, or bidirectional")
    power_role: Optional[str] = Field(None, description="Power role such as input, source, regulated_output, or return")
    interface: Optional[str] = Field(None, description="Logical interface such as I2C, SPI, UART, PWM, or analog")

class ComponentTemplate(BaseModel):
    part_number: str = Field(..., description="Manufacturer or generic part number, e.g., 'ESP32-WROOM-32D', 'DHT11'")
    name: str = Field(..., description="Friendly name of the component, e.g., 'ESP32 Development Board'")
    category: str = Field(..., description="Category: Microcontroller, Sensor, Actuator, Display, Power, Passives, Communication")
    description: str = Field(..., description="Short explanation of what this part does")
    price: float = Field(0.0, description="Estimated unit price in USD")
    sourcing_url: Optional[str] = Field(None, description="Sourcing or datasheet link")
    pins: List[PinDefinition] = Field(default_factory=list, description="List of physical pins on the component")
    use_cases: List[str] = Field(default_factory=list, description="Common use cases or keywords")


class PartDefinition(BaseModel):
    """Source-agnostic identity and shared physical data for one selected part."""

    part_definition_id: str = Field(..., description="Stable project-local ID referenced by component instances and BOM rows")
    manufacturer: Optional[str] = Field(None, description="Part manufacturer when known")
    part_number: str = Field(..., description="Manufacturer or generic part number")
    name: str = Field(..., description="Human-readable part name")
    category: str = Field(..., description="Electrical or mechanical part category")
    description: str = Field("", description="Source-agnostic description of the selected part")
    electrical_specs: Dict[str, Any] = Field(default_factory=dict, description="Shared electrical limits and ratings")
    pins: List[PinDefinition] = Field(default_factory=list, description="Shared physical pin definitions")
    dimensions_mm: Dict[str, float] = Field(default_factory=dict, description="Known physical dimensions in millimeters")
    datasheet_url: Optional[str] = Field(None, description="Datasheet URL when known")
    sourcing_url: Optional[str] = Field(None, description="Selected sourcing URL when known")
    sourcing_offers: List[Dict[str, Any]] = Field(default_factory=list, description="Optional alternate sourcing offers")
    unit_price: float = Field(0.0, ge=0.0, description="Selected estimated unit price in USD")

# ==========================================
# 2. Project-Level Hardware Intermediate Representation (Shared State)
# ==========================================

class ProjectOverview(BaseModel):
    title: str = Field(..., description="Title of the hardware project")
    description: str = Field(..., description="Summary of the project")
    difficulty: str = Field(..., description="Difficulty level: Beginner, Intermediate, Advanced")
    estimated_cost: float = Field(0.0, description="Total estimated BOM cost in USD")
    category: str = Field(..., description="Primary project domain: IoT, Wearable, Automation, Robotics, Smart Home")

class FunctionalRequirements(BaseModel):
    requirements: List[str] = Field(default_factory=list, description="List of primary functional requirements")
    power_needs: str = Field(..., description="Power supply requirements, e.g., '5V USB', '3.7V LiPo Battery'")
    operating_voltage: float = Field(3.3, description="Main operating system voltage, typically 3.3 or 5.0")
    physical_constraints: List[str] = Field(default_factory=list, description="Size, weight, or environmental constraints")
    safety_notes: List[str] = Field(default_factory=list, description="Safety and handling advisories")
    missing_info: List[str] = Field(default_factory=list, description="Clarifying questions or unknown requirements")

class ComponentInstance(BaseModel):
    """One independently addressable physical occurrence in a project.

    The excluded identity fields keep Python callers and legacy records readable
    during the 0.1 -> 0.2 transition. New serialized IR stores those values once
    in ``part_definitions`` and emits only the definition reference here.
    """

    ref_des: str = Field(..., description="Reference designator, e.g., 'U1', 'R1', 'SEN1'")
    part_definition_id: Optional[str] = Field(None, description="ID of the shared PartDefinition")
    rationale: str = Field(..., description="Why this component was selected for this project")
    configuration: Dict[str, Any] = Field(default_factory=dict, description="Instance-specific configuration only")

    # Transitional runtime fields. They are deliberately excluded from serialized
    # Hardware Intermediate Representation; HardwareIntermediateRepresentation hydrates them from the referenced PartDefinition.
    part_number: str = Field("", exclude=True, repr=False)
    name: str = Field("", exclude=True, repr=False)
    category: str = Field("", exclude=True, repr=False)
    unit_price: float = Field(0.0, exclude=True, repr=False)
    sourcing_url: Optional[str] = Field(None, exclude=True, repr=False)
    pins: List[PinDefinition] = Field(default_factory=list, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_quantity(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "quantity" not in value:
            return value
        payload = dict(value)
        try:
            quantity = max(1, int(payload.pop("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        configuration = dict(payload.get("configuration") or {})
        if quantity > 1:
            configuration["_legacy_aggregate_quantity"] = quantity
        payload["configuration"] = configuration
        return payload

    @model_validator(mode="after")
    def assign_part_definition_id(self) -> "ComponentInstance":
        if not self.part_definition_id:
            self.part_definition_id = _part_definition_id(self.part_number or self.name or self.ref_des)
        return self

    @property
    def quantity(self) -> int:
        """Every instance represents exactly one physical occurrence."""

        return 1


class BOMLineItem(BaseModel):
    """Deterministic procurement aggregation over physical component instances."""

    line_id: str = Field(..., description="Stable project-local BOM row ID")
    part_definition_id: str = Field(..., description="Referenced shared part definition")
    instance_refs: List[str] = Field(default_factory=list, description="Physical instances aggregated into this row")
    quantity: int = Field(..., ge=1, description="Number of referenced physical instances")
    manufacturer: Optional[str] = None
    part_number: str
    name: str
    category: str
    unit_price: float = Field(0.0, ge=0.0)
    sourcing_url: Optional[str] = None
    extended_price: float = Field(0.0, ge=0.0)
    rationale: str = Field("", description="Combined project roles represented by this BOM row")

    @model_validator(mode="after")
    def quantity_matches_instances(self) -> "BOMLineItem":
        if self.quantity != len(self.instance_refs):
            raise ValueError(
                f"BOM line '{self.line_id}' quantity {self.quantity} does not match "
                f"its {len(self.instance_refs)} referenced component instances."
            )
        return self

class PinReference(BaseModel):
    ref_des: str = Field(..., description="Component reference designator, e.g., 'U1'")
    pin_id: str = Field(..., description="Pin ID on the target component, e.g., 'GND' or '12'")

class ConnectionNet(BaseModel):
    net_id: str = Field(..., description="Unique ID for the electrical net, e.g., 'NET_VCC', 'NET_I2C_SDA'")
    name: str = Field(..., description="Friendly net name, e.g., '3.3V Power Rail', 'I2C Data'")
    net_type: str = Field(..., description="Net type: Power, Ground, Analog, Digital, I2C, SPI, UART, PWM")
    voltage: Optional[float] = Field(None, description="Expected voltage of this net, e.g., 3.3")
    pins: List[PinReference] = Field(default_factory=list, description="All component pins tied to this net")

class AssemblyStep(BaseModel):
    step_num: int = Field(..., description="Index order of this assembly instruction")
    title: str = Field(..., description="Short title of the step")
    description: str = Field(..., description="Step-by-step assembly description")
    danger_flag: bool = Field(False, description="True if step carries electric, thermal, or physical risk")
    danger_message: Optional[str] = Field(None, description="Warning warning note for this step")
    affected_components: List[str] = Field(default_factory=list, description="Reference designators of components handled in this step")

class MechanicalSource(BaseModel):
    name: str = Field(..., description="Display name of the CAD, enclosure, or fabrication source")
    source_type: str = Field(..., description="Source class: Open STL, Paid STL, Vendor CAD, Reference CAD, or Fabrication Estimate")
    url: str = Field(..., description="Resolvable source URL for the CAD model, enclosure datasheet, or fabrication reference")
    file_formats: List[str] = Field(default_factory=list, description="Known CAD/download formats such as STL, STEP, DXF, or Fusion 360")
    license: Optional[str] = Field(None, description="Source license or commercial availability note")
    estimated_unit_price_usd: float = Field(0.0, description="Estimated CAD download, fabrication, or enclosure unit cost in USD")
    notes: Optional[str] = Field(None, description="How this source should be adapted for the generated design")

class MechanicalVector3(BaseModel):
    x_mm: float = Field(..., description="X-axis measurement in millimeters, where X is project width")
    y_mm: float = Field(..., description="Y-axis measurement in millimeters, where Y is project depth")
    z_mm: float = Field(..., description="Z-axis measurement in millimeters, where Z is project height")

class MechanicalRotation3(BaseModel):
    x_deg: float = Field(0.0, description="Rotation around the X axis in degrees")
    y_deg: float = Field(0.0, description="Rotation around the Y axis in degrees")
    z_deg: float = Field(0.0, description="Rotation around the Z axis in degrees")

class MechanicalPlacement(BaseModel):
    ref_des: str = Field(..., description="Reference designator of the component this placement represents")
    label: Optional[str] = Field(None, description="Display label for the placed component")
    category: Optional[str] = Field(None, description="Component class or placement layer such as Microcontroller, Display, 3D Print, or Mechanical")
    layer: str = Field("electrical", description="Visibility layer: electrical, mechanism, print, enclosure, structural, or misc")
    position: MechanicalVector3 = Field(..., description="Component center position in millimeters relative to the enclosure center")
    size: MechanicalVector3 = Field(..., description="Approximate component envelope size in millimeters")
    orientation_deg: MechanicalRotation3 = Field(default_factory=MechanicalRotation3, description="Euler orientation in degrees around X, Y, and Z")
    mounting_face: Optional[str] = Field(None, description="Face or surface used for mounting, such as front, back, floor, lid, left, or right")
    notes: Optional[str] = Field(None, description="Clearance, fastener, cable routing, or assembly notes for this placement")

class MechanicalSpatialRelationship(BaseModel):
    source_ref_des: str = Field(..., description="Reference designator of the source component")
    target_ref_des: str = Field(..., description="Reference designator of the target component")
    relation: str = Field(..., description="Physical relationship such as centered-above, adjacent-to, mounted-on, aligned-with, or clearance-from")
    axis: Optional[str] = Field(None, description="Dominant axis for the relationship: X, Y, or Z")
    offset_mm: Optional[float] = Field(None, description="Signed offset between components along the dominant axis")
    notes: Optional[str] = Field(None, description="Additional placement or clearance rationale")

class MechanicalMotionIntent(BaseModel):
    motion_id: Optional[str] = Field(None, description="Stable project-local identifier for this motion")
    label: Optional[str] = Field(None, description="Human-readable name for the motion preview")
    type: Literal["revolute", "prismatic", "compliant"] = Field(..., description="Authoring intent. Rigid revolute/prismatic intents are resolved and evaluated by OpenCAD; compliant intent is reserved for future deformation preview.")
    target_ref: str = Field(..., description="Reference designator of the moving placement")
    parent_ref: Optional[str] = Field(None, description="Optional reference designator of the stationary parent")
    axis: Literal["X", "Y", "Z"] = Field("Z", description="Project-space motion axis")
    pivot_mm: List[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0], min_length=3, max_length=3, description="Project-space pivot [x, y, z] in millimeters")
    min_deg: Optional[float] = Field(None, description="Minimum angular travel for revolute/compliant motion")
    max_deg: Optional[float] = Field(None, description="Maximum angular travel for revolute/compliant motion")
    min_mm: Optional[float] = Field(None, description="Minimum linear travel for prismatic motion")
    max_mm: Optional[float] = Field(None, description="Maximum linear travel for prismatic motion")
    notes: Optional[str] = Field(None, description="Motion intent, stops, clearances, or preview limitations")

    @field_validator("axis", mode="before")
    @classmethod
    def normalize_axis(cls, value: Any) -> str:
        axis = str(value or "Z").strip().upper()
        if axis not in {"X", "Y", "Z"}:
            raise ValueError("Mechanical motion axis must be X, Y, or Z")
        return axis

    @field_validator("pivot_mm", mode="before")
    @classmethod
    def normalize_pivot(cls, value: Any) -> Any:
        if value is None:
            return [0.0, 0.0, 0.0]
        if isinstance(value, Mapping):
            return [
                value.get("x_mm", value.get("x", 0.0)),
                value.get("y_mm", value.get("y", 0.0)),
                value.get("z_mm", value.get("z", 0.0)),
            ]
        return value


class MechanicalNotes(BaseModel):
    cad_operations: List[CadOperation] = Field(default_factory=list, max_length=32, description="Explicit solid CAD in millimeters, applied in order; first operation must add. Use for standalone solids (components/nets may be empty). An empty list retains the legacy enclosure generator.")

    @field_validator("cad_operations")
    @classmethod
    def first_cad_operation_adds(cls, operations):
        if operations and operations[0].operation != "add":
            raise ValueError("The first CAD operation must add a solid.")
        return operations

    physical_form: str = Field(
        "Unspecified",
        description=(
            "Overall product shape and silhouette, such as curved handheld, cylindrical, wearable, folded, "
            "open-frame, or another explicitly requested form; do not assume a rectangular box"
        ),
    )
    enclosure_type: str = Field(..., description="Type of housing: 3D Printed, Off-the-shelf, Custom Acrylic, Waterproof, Acrylic laser cut")
    mounting_guidance: str = Field(..., description="Mounting and standoffs instructions")
    fabrication_details: List[str] = Field(default_factory=list, description="Enclosure dimensions, material recommendations, or printing instructions")
    fabrication_cost_estimate_usd: float = Field(0.0, description="Estimated mechanical fabrication cost in USD, excluding electrical BOM")
    cad_sources: List[MechanicalSource] = Field(default_factory=list, description="CAD, enclosure, and fabrication source records")
    manufacturability_rating: str = Field(..., description="Ease of manufacturing: Easy, Moderate, Challenging")
    render_dimensions: Optional[MechanicalVector3] = Field(None, description="Overall live-render envelope dimensions in millimeters")
    component_placements: List[MechanicalPlacement] = Field(default_factory=list, description="Per-component 3D placements for live Three.js rendering")
    spatial_relationships: List[MechanicalSpatialRelationship] = Field(default_factory=list, description="Physical offsets and alignment relationships between placed components")
    motion_intents: List[MechanicalMotionIntent] = Field(default_factory=list, description="Agent-authored motion intent resolved into OpenCAD kinematic joints during CAD generation")
    mechanism_benchmark: Optional[MechanismBenchmark] = Field(None, description="Bounded mechanism: print_in_place_hinge, monolithic_flexure_hinge, or spur_gear_pair. For two meshing gears use spur_gear_pair (defaults: 20/40 teeth, module 2 mm). CAD generation creates real gear bodies and synchronized OpenCAD poses.")

class PinMappingEntry(BaseModel):
    mcu_pin: str = Field(..., description="MCU pin identifier, e.g., 'GPIO23'")
    connected_to: str = Field(..., description="Name of the sensor pin/function connected, e.g., 'DHT22 Data'")
    net_name: str = Field(..., description="Electrical net name, e.g., 'DHT_SDA_NET'")

class ValidationIssue(BaseModel):
    severity: str = Field(..., description="Severity level: CRITICAL, WARNING, or INFO")
    category: str = Field(..., description="Short circuit, Voltage Mismatch, Unpowered IC, Pin Conflict, Overcurrent, Safety Block")
    description: str = Field(..., description="Detailed description of the validation issue")
    troubleshooting: str = Field(..., description="Suggested remediation action for self-healing or user override")

class ValidationSummary(BaseModel):
    critical: List[ValidationIssue] = Field(default_factory=list, description="Critical blocking issues or errors")
    warning: List[ValidationIssue] = Field(default_factory=list, description="Warning level issues")
    info: List[ValidationIssue] = Field(default_factory=list, description="Informational recommendations")

class BusConnection(BaseModel):
    bus_id: str = Field(..., description="Unique ID for the digital communication bus, e.g., 'BUS_I2C_1'")
    bus_type: str = Field(..., description="Bus type: I2C, SPI, UART, CAN")
    clock_frequency_hz: Optional[float] = Field(None, description="Operating bus speed if applicable")
    nets: List[str] = Field(default_factory=list, description="Electrical net IDs associated with this bus")

class PowerRail(BaseModel):
    rail_id: str = Field(..., description="Unique ID for power rail, e.g., 'RAIL_3V3'")
    voltage: float = Field(..., description="Nominal operating voltage in Volts")
    max_current_capacity_ma: float = Field(..., description="Maximum continuous current capacity in mA")
    source_component: str = Field(..., description="Reference designator of the power source component")

class SystemInterface(BaseModel):
    name: str = Field(..., description="Human-readable boundary between two systems")
    connects_to: str = Field(..., description="System ID on the other side of the boundary")
    purpose: str = Field(..., description="What crosses this boundary and why the connection is needed")

class SystemNode(BaseModel):
    system_id: str = Field(..., description="Stable dotted ID such as electrical.power or mechanical.enclosure")
    name: str = Field(..., description="Human-readable system name")
    domain: str = Field(..., description="Broad discipline such as product, electrical, mechanical, or firmware")
    purpose: str = Field(..., description="Why this system is needed in the complete product")
    responsibilities: List[str] = Field(default_factory=list, description="Outcomes owned by this system")
    constraints: List[str] = Field(default_factory=list, description="Important system-level limits without implementation minutiae")
    expected_component_roles: List[str] = Field(default_factory=list, description="Abstract roles this system needs, not exact parts or pins")
    interfaces: List[SystemInterface] = Field(default_factory=list, description="Boundaries with sibling or parent systems")
    detail_owner: str = Field("system architect", description="Specialist agent responsible for expanding this node")
    children: List["SystemNode"] = Field(default_factory=list, description="More specific systems nested below this node")

    @field_validator("interfaces", mode="before")
    @classmethod
    def normalize_interface_shorthand(cls, value: Any) -> Any:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        normalized: List[Any] = []
        for item in items:
            if item is None:
                continue
            if not isinstance(item, str):
                normalized.append(item)
                continue
            connects_to = item.strip()
            if not re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+", connects_to):
                continue
            label = connects_to.replace(".", " ").replace("_", " ").replace("-", " ").title()
            normalized.append({
                "name": f"{label} interface",
                "connects_to": connects_to,
                "purpose": f"Coordinates this system with {label.lower()} responsibilities.",
            })
        return normalized

    @field_validator("children", mode="before")
    @classmethod
    def normalize_child_shorthand(cls, value: Any) -> Any:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        normalized: List[Any] = []
        for item in items:
            if item is None:
                continue
            if not isinstance(item, str):
                normalized.append(item)
                continue
            system_id = item.strip()
            if not re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+", system_id):
                continue
            label = system_id.rsplit(".", 1)[-1].replace("_", " ").replace("-", " ").title()
            domain = system_id.split(".", 1)[0].strip() or "system"
            normalized.append({
                "system_id": system_id,
                "name": label,
                "domain": domain,
                "purpose": f"Defines the {label.lower()} responsibilities referenced by the architecture.",
            })
        return normalized

class SystemArchitecture(BaseModel):
    summary: str = Field(..., description="Concise explanation of the complete system decomposition")
    root: SystemNode = Field(..., description="Root of the hierarchical system tree")


def _part_definition_id(value: str) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "_", str(value or "PART").upper()).strip("_") or "PART"
    return f"PART_{stem}"


def _instance_payload(value: ComponentInstance | Mapping[str, Any]) -> Dict[str, Any]:
    if isinstance(value, ComponentInstance):
        return {
            "ref_des": value.ref_des,
            "part_definition_id": value.part_definition_id,
            "rationale": value.rationale,
            "configuration": dict(value.configuration),
            "part_number": value.part_number,
            "name": value.name,
            "category": value.category,
            "unit_price": value.unit_price,
            "sourcing_url": value.sourcing_url,
            "pins": [pin.model_dump(mode="json") for pin in value.pins],
        }
    return dict(value)


def _expanded_ref_des(base_ref: str, offset: int, used_refs: set[str]) -> str:
    match = re.fullmatch(r"(.*?)(\d+)", base_ref.strip())
    prefix = (match.group(1) if match else base_ref.strip()) or "X"
    start = int(match.group(2)) if match else 1
    candidate_index = start + offset
    candidate = f"{prefix}{candidate_index}"
    while candidate in used_refs:
        candidate_index += 1
        candidate = f"{prefix}{candidate_index}"
    return candidate


def expand_component_instances(
    components: Iterable[ComponentInstance | Mapping[str, Any]],
) -> List[ComponentInstance]:
    """Expand legacy aggregate quantities into unique physical instances."""

    expanded: List[ComponentInstance] = []
    used_refs: set[str] = set()
    for index, value in enumerate(components):
        payload = _instance_payload(value)
        configuration = dict(payload.get("configuration") or {})
        legacy_quantity = configuration.pop("_legacy_aggregate_quantity", None)
        if legacy_quantity is None:
            legacy_quantity = payload.pop("quantity", 1)
        try:
            quantity = max(1, int(legacy_quantity or 1))
        except (TypeError, ValueError):
            quantity = 1
        payload["configuration"] = configuration
        base_ref = str(payload.get("ref_des") or "X1").strip() or "X1"
        for offset in range(quantity):
            if offset == 0:
                ref_des = base_ref
            else:
                ref_des = _expanded_ref_des(base_ref, offset, used_refs)
            if ref_des in used_refs:
                raise ValueError(f"Duplicate component reference designator '{ref_des}'.")
            used_refs.add(ref_des)
            try:
                expanded.append(ComponentInstance.model_validate({**payload, "ref_des": ref_des}))
            except ValidationError as exc:
                raise ValidationError.from_exception_data("ComponentInstance", [
                    {**error, "loc": ("components", index, *error["loc"])} for error in exc.errors()
                ]) from exc
    return expanded


def part_definition_from_instance(component: ComponentInstance) -> PartDefinition:
    return PartDefinition(
        part_definition_id=str(component.part_definition_id),
        part_number=component.part_number or str(component.part_definition_id),
        name=component.name or component.part_number or str(component.part_definition_id),
        category=component.category or "Component",
        pins=component.pins,
        sourcing_url=component.sourcing_url,
        unit_price=max(0.0, component.unit_price),
    )


def derive_part_definitions(
    components: Iterable[ComponentInstance],
    existing: Iterable[PartDefinition] = (),
) -> List[PartDefinition]:
    definitions = {item.part_definition_id: item for item in existing}
    for component in components:
        part_id = str(component.part_definition_id)
        candidate = part_definition_from_instance(component)
        current = definitions.get(part_id)
        if current is None:
            definitions[part_id] = candidate
            continue
        has_legacy_identity = bool(
            component.part_number or component.name or component.category or component.pins
        )
        if not has_legacy_identity:
            continue
        if current.part_number != candidate.part_number:
            raise ValueError(f"Part definition '{part_id}' is used for conflicting part numbers.")
        if current.pins and candidate.pins and current.pins != candidate.pins:
            raise ValueError(f"Part definition '{part_id}' has conflicting shared pin data.")
    return list(definitions.values())


def derive_bom_line_items(
    part_definitions: Iterable[PartDefinition],
    components: Iterable[ComponentInstance],
) -> List[BOMLineItem]:
    definitions = {item.part_definition_id: item for item in part_definitions}
    grouped: Dict[str, List[ComponentInstance]] = {}
    for component in components:
        grouped.setdefault(str(component.part_definition_id), []).append(component)

    rows: List[BOMLineItem] = []
    for part_id, instances in grouped.items():
        definition = definitions.get(part_id)
        if definition is None:
            raise ValueError(f"Component instance '{instances[0].ref_des}' references unknown part definition '{part_id}'.")
        refs = [instance.ref_des for instance in instances]
        rationales = list(dict.fromkeys(instance.rationale for instance in instances if instance.rationale.strip()))
        quantity = len(refs)
        rows.append(BOMLineItem(
            line_id=f"BOM_{part_id}",
            part_definition_id=part_id,
            instance_refs=refs,
            quantity=quantity,
            manufacturer=definition.manufacturer,
            part_number=definition.part_number,
            name=definition.name,
            category=definition.category,
            unit_price=definition.unit_price,
            sourcing_url=definition.sourcing_url,
            extended_price=round(definition.unit_price * quantity, 2),
            rationale="; ".join(rationales),
        ))
    return rows


def component_instance_count(component: ComponentInstance) -> int:
    """Return one for normalized instances or a pending legacy expansion count."""

    try:
        return max(1, int(component.configuration.get("_legacy_aggregate_quantity") or 1))
    except (TypeError, ValueError):
        return 1


def component_detail_payload(component: ComponentInstance) -> Dict[str, Any]:
    """Resolve the shared part fields needed by specialist-agent prompts."""

    return {
        "ref_des": component.ref_des,
        "part_definition_id": component.part_definition_id,
        "part_number": component.part_number,
        "name": component.name,
        "category": component.category,
        "unit_price": component.unit_price,
        "sourcing_url": component.sourcing_url,
        "rationale": component.rationale,
        "configuration": component.configuration,
        "pins": [pin.model_dump(mode="json") for pin in component.pins],
    }

class HardwareIntermediateRepresentation(BaseModel):
    """The master typed document capturing the entire generated hardware design."""
    hardware_ir_version: str = Field("0.2", description="Structured schema version")
    overview: Optional[ProjectOverview] = Field(None, description="Project overview metadata")
    requirements: Optional[FunctionalRequirements] = Field(None, description="Extracted constraints & requirements")
    system_architecture: Optional[SystemArchitecture] = Field(None, description="Hierarchical, purpose-driven decomposition of the complete product")
    part_definitions: List[PartDefinition] = Field(default_factory=list, description="Shared source-agnostic part definitions")
    components: List[ComponentInstance] = Field(default_factory=list, description="Independently addressable physical component instances")
    bom: List[BOMLineItem] = Field(default_factory=list, description="Deterministically aggregated procurement rows")
    nets: List[ConnectionNet] = Field(default_factory=list, description="Electrical netlist connections")
    buses: List[BusConnection] = Field(default_factory=list, description="Digital communication buses")
    pin_mappings: List[PinMappingEntry] = Field(default_factory=list, description="MCU functional pin map")
    assembly: List[AssemblyStep] = Field(default_factory=list, description="Step-by-step physical build instruction package")
    mechanical: Optional[MechanicalNotes] = Field(None, description="Enclosure and fabrications specifications")
    cad_model: Optional[Any] = Field(
        None,
        description=(
            "Optional canonical CAD model source for the project. This may be a serialized Forma/OpenCAD adapter "
            "payload, a server-accessible file path or URL, an S3 location, or renderable mesh data."
        ),
    )
    
    # Extra requested fields
    constraints: List[str] = Field(default_factory=list, description="Project architectural and electrical constraints")
    power_rails: List[PowerRail] = Field(default_factory=list, description="Active power delivery rails")
    estimated_current_draw_ma: float = Field(0.0, description="Total calculated peak current consumption in mA")
    fabrication_notes: List[str] = Field(default_factory=list, description="Printed circuit/manufacturability and casing guidelines")
    assembly_metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional builder metadata and configurations")
    project_version_history: List[Dict[str, Any]] = Field(default_factory=list, description="Revision and modification history of this project package")
    
    validation: ValidationSummary = Field(default_factory=ValidationSummary, description="Categorized safety and electrical checks")
    is_valid: bool = Field(True, description="True if project passes critical validation checks")

    @model_validator(mode="before")
    @classmethod
    def migrate_component_model(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        components = expand_component_instances(payload.get("components") or [])
        existing_definitions = []
        for index, item in enumerate(payload.get("part_definitions") or []):
            try:
                existing_definitions.append(item if isinstance(item, PartDefinition) else PartDefinition.model_validate(item))
            except ValidationError as exc:
                raise ValidationError.from_exception_data("PartDefinition", [
                    {**error, "loc": ("part_definitions", index, *error["loc"])} for error in exc.errors()
                ]) from exc
        definitions = derive_part_definitions(components, existing_definitions)
        payload["hardware_ir_version"] = "0.2"
        payload["components"] = components
        payload["part_definitions"] = definitions
        if not payload.get("bom"):
            payload["bom"] = derive_bom_line_items(definitions, components)
        return payload

    @model_validator(mode="after")
    def validate_component_model(self) -> "HardwareIntermediateRepresentation":
        definitions = {item.part_definition_id: item for item in self.part_definitions}
        component_refs: Dict[str, ComponentInstance] = {}
        expected_bom_refs: Dict[str, List[str]] = {}
        for component in self.components:
            if component.ref_des in component_refs:
                raise ValueError(f"Duplicate component reference designator '{component.ref_des}'.")
            definition = definitions.get(str(component.part_definition_id))
            if definition is None:
                raise ValueError(
                    f"Component instance '{component.ref_des}' references unknown part definition "
                    f"'{component.part_definition_id}'."
                )
            component.part_number = definition.part_number
            component.name = definition.name
            component.category = definition.category
            component.unit_price = definition.unit_price
            component.sourcing_url = definition.sourcing_url
            component.pins = list(definition.pins)
            component_refs[component.ref_des] = component
            expected_bom_refs.setdefault(str(component.part_definition_id), []).append(component.ref_des)

        bom_refs: List[str] = []
        bom_part_ids: set[str] = set()
        bom_line_ids: set[str] = set()
        for row in self.bom:
            if row.line_id in bom_line_ids:
                raise ValueError(f"Duplicate BOM line ID '{row.line_id}'.")
            if row.part_definition_id in bom_part_ids:
                raise ValueError(
                    f"Part definition '{row.part_definition_id}' must aggregate into exactly one BOM line."
                )
            bom_line_ids.add(row.line_id)
            bom_part_ids.add(row.part_definition_id)
            definition = definitions.get(row.part_definition_id)
            if definition is None:
                raise ValueError(f"BOM line '{row.line_id}' references unknown part definition '{row.part_definition_id}'.")
            if sorted(row.instance_refs) != sorted(expected_bom_refs.get(row.part_definition_id, [])):
                raise ValueError(
                    f"BOM line '{row.line_id}' must contain every physical instance of its part definition."
                )
            if (
                row.part_number != definition.part_number
                or row.name != definition.name
                or row.category != definition.category
                or row.manufacturer != definition.manufacturer
                or round(row.unit_price, 2) != round(definition.unit_price, 2)
                or row.sourcing_url != definition.sourcing_url
            ):
                raise ValueError(f"BOM line '{row.line_id}' does not match its shared part definition.")
            for ref_des in row.instance_refs:
                component = component_refs.get(ref_des)
                if component is None:
                    raise ValueError(f"BOM line '{row.line_id}' references unknown component instance '{ref_des}'.")
                if component.part_definition_id != row.part_definition_id:
                    raise ValueError(
                        f"BOM line '{row.line_id}' includes instance '{ref_des}' from a different part definition."
                    )
                bom_refs.append(ref_des)
            expected_extended = round(row.unit_price * row.quantity, 2)
            if round(row.extended_price, 2) != expected_extended:
                raise ValueError(f"BOM line '{row.line_id}' extended price is not deterministic.")
        if sorted(bom_refs) != sorted(component_refs):
            raise ValueError("Every component instance must appear exactly once in the BOM.")

        for net in self.nets:
            for pin_ref in net.pins:
                component = component_refs.get(pin_ref.ref_des)
                if component is None:
                    raise ValueError(
                        f"Net '{net.net_id}' references unknown component instance '{pin_ref.ref_des}'."
                    )
                valid_pins = {pin.pin_id for pin in component.pins}
                if valid_pins and pin_ref.pin_id not in valid_pins:
                    raise ValueError(
                        f"Net '{net.net_id}' references unknown pin '{pin_ref.pin_id}' on instance '{pin_ref.ref_des}'."
                    )

        if self.mechanical:
            for placement in self.mechanical.component_placements:
                if placement.ref_des not in component_refs:
                    raise ValueError(
                        f"Mechanical placement references unknown component instance '{placement.ref_des}'."
                    )
        return self


class Project(BaseModel):
    """Durable design artifact contained by a workspace."""

    project_id: str
    owner_user_id: str | None = None
    chat_id: str | None = None
    title: str
    prompt: str
    hardware_ir: HardwareIntermediateRepresentation | Dict[str, Any]
    created_at: str
    updated_at: str | None = None
    creation_channel: str = "hosted"
    visibility: str = "public"


class ProjectSummary(BaseModel):
    """Common gallery contract returned for every project creation channel."""

    project_id: str
    creation_channel: str
    chat_id: str | None = None
    title: str
    prompt: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    visibility: str = "public"
    can_chat: bool = False
    creator_display: str = "unknown"
    creator_username: str | None = None
    creator_image_url: str | None = None
    parts_count: int = 0
    save_count: int = 0
    remix_count: int = 0
    saved: bool = False
    has_product_image: bool = False
    product_image_url: str | None = None
    product_image_data: str | None = None
    product_image_content_type: str | None = None
    product_image_model: str | None = None
    product_visual_sequence: list[dict[str, Any]] = Field(default_factory=list)
    image_output_status: str | None = None
    generation_status: str = "succeeded"
    project_readiness: str = "complete"

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, key: str) -> Any:
        """Allow existing response assertions to inspect typed contracts by key."""
        return getattr(self, key)


class ProjectIdentityResponse(BaseModel):
    """Stable identity projection shared by hosted and CLI project listings."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    project_id: str
    owner_user_id: str | None = None
    creation_channel: str = "hosted"
    title: str = ""
    prompt: str = ""
    chat_id: str | None = None
    workspace_id: str | None = None
    visibility: str = "public"
    status: str = "active"
    current_revision: int = 0
    current_revision_id: str | None = None
    created_at: str | datetime | None = None
    updated_at: str | datetime | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class ProjectDetail(ProjectSummary):
    """Extended project contract used by authenticated detail views."""

    project_ir: dict[str, Any] | HardwareIntermediateRepresentation | None = None
    project_object: dict[str, Any] | None = None
    mermaid_code: str | None = None
    svg_schematic: str | None = None
    generation_stages: dict[str, Any] = Field(default_factory=dict)

# ==========================================
# 3. API Requests & Response Models
# ==========================================

class GenerateProjectRequest(BaseModel):
    prompt: str = Field(..., max_length=12000, description="User's natural language project description")
    project_id: Optional[str] = Field(
        None,
        description="Optional context project id whose workflow authorizes this generation.",
    )
    retry_stage: Optional[str] = Field(
        None,
        description="Retry one failed generation stage using persisted upstream artifacts.",
    )
    workflow: str = Field(
        "default",
        description="Generation workflow id: default or web_research"
    )
    image_data: Optional[str] = Field(
        None,
        max_length=8 * 1024 * 1024,
        description="Optional data URL or base64-encoded reference image for multimodal project extraction"
    )
    generate_image: bool = Field(
        False,
        description="When true, generate a product concept image with the configured image provider"
    )
    generation_mode: str = Field(
        "regular",
        description="Generation strategy: regular one-shot or progressive hierarchical generation.",
    )
    provider: Optional[str] = Field(
        None,
        description="Optional runtime LLM provider override, for example vertex, openai, anthropic, baseten, gmi, huggingface, cloudflare, nvidia, openai-compatible, gemini, runpod, runpod-serverless, or simulation"
    )
    model: Optional[str] = Field(
        None,
        description="Optional runtime model override. Must be allowed for the selected provider."
    )
    chat_id: Optional[str] = Field(
        None,
        description="Optional chat/thread id that owns the generated project."
    )
    source_project_id: Optional[str] = Field(
        None,
        description="Optional project id that this generation was requested from."
    )
    client_job_id: Optional[str] = Field(
        None,
        description="Optional frontend-generated job id for progress polling."
    )
    external_source_provider: Optional[str] = Field(
        None,
        description="Optional web research provider override. Firecrawl is the only active provider."
    )
    data_sources: List[str] = Field(
        default_factory=list,
        description="Optional lightweight context sources. Currently supports past_jobs.",
    )
    past_jobs_limit: int = Field(
        3,
        ge=1,
        le=8,
        description="Maximum number of relevant completed jobs to include when data_sources contains past_jobs.",
    )

    @field_validator("generation_mode", mode="before")
    @classmethod
    def normalize_generation_mode(cls, value: Any) -> str:
        normalized = str(value or "regular").strip().lower()
        if normalized not in {"regular", "progressive"}:
            raise ValueError("generation_mode must be regular or progressive.")
        return normalized

    @field_validator("provider", "model", "project_id", "retry_stage", "chat_id", "source_project_id", "client_job_id", "external_source_provider", mode="before")
    @classmethod
    def strip_optional_generation_selector(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("client_job_id")
    @classmethod
    def validate_client_job_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
            raise ValueError("client_job_id may only contain letters, numbers, dot, dash, underscore, or colon.")
        return value

    @field_validator("external_source_provider")
    @classmethod
    def validate_external_source_provider(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"auto", "firecrawl"}:
            return "firecrawl"
        if normalized == "tavily":
            return "tavily"
        raise ValueError("external_source_provider must be firecrawl or tavily.")

    @field_validator("data_sources", mode="before")
    @classmethod
    def validate_data_sources(cls, value: Any) -> List[str]:
        from forma_core.jobs.context import normalize_generation_data_sources

        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("data_sources must be a list of source ids.")
        return normalize_generation_data_sources(value)


class ProjectUpdateRequest(BaseModel):
    """Owner-managed project metadata updates."""

    title: str | None = None
    prompt: str | None = None
    visibility: str | None = None


class ProjectContributionConsentRequest(BaseModel):
    """Explicit, versioned consent for a sanitized project contribution."""

    granted: bool = Field(..., description="Must be true; withdrawal uses DELETE.")
    consent_version: str = Field(..., min_length=1, max_length=80)
    permitted_purposes: List[str] = Field(..., min_length=1, max_length=5)

    @field_validator("granted")
    @classmethod
    def require_grant(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("granted must be true; use DELETE to withdraw consent.")
        return value

    @field_validator("consent_version")
    @classmethod
    def normalize_consent_version(cls, value: str) -> str:
        return value.strip()

    @field_validator("permitted_purposes")
    @classmethod
    def normalize_purposes(cls, value: List[str]) -> List[str]:
        normalized = sorted({str(item).strip().lower() for item in value if str(item).strip()})
        if not normalized:
            raise ValueError("At least one permitted purpose is required.")
        return normalized


class ClarifyingQuestion(BaseModel):
    id: str = Field(..., min_length=1, description="Stable question id.")
    label: str = Field(..., min_length=1, description="Short UI label for the question.")
    question: str = Field(..., min_length=1, description="Question to ask the user.")
    placeholder: str = Field("", description="Optional example answer or placeholder.")
    suggestions: List[str] = Field(default_factory=list, description="Short suggested answer chips.")


class ClarifyingQuestionsRequest(BaseModel):
    prompt: str = Field("", description="User's natural language project description.")
    workflow: str = Field("default", description="Generation workflow id.")
    has_image: bool = Field(False, description="Whether the user supplied a reference image.")
    max_questions: int = Field(3, ge=0, le=5, description="Maximum number of clarifying questions to return.")
    force: bool = Field(True, description="When true, ask useful context questions unless the user explicitly skips them.")

    @field_validator("prompt", "workflow", mode="before")
    @classmethod
    def strip_clarifier_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class ClarifyingQuestionsResponse(BaseModel):
    agent: str = Field("Context Clarifier Agent", description="Agent that produced the questions.")
    should_ask: bool = Field(False, description="Whether the UI should pause and ask these questions.")
    reason: str = Field("", description="Short reason for the clarification decision.")
    questions: List[ClarifyingQuestion] = Field(default_factory=list)
    workflow: str = Field("default")


class IterateProjectRequest(BaseModel):
    instruction: str = Field(..., min_length=1, description="Natural language change request to apply to an existing project")
    namespace: Optional[str] = Field(
        None,
        description="Optional dotted project object namespace to target, for example product.mech or project.docs.",
    )
    provider: Optional[str] = Field(
        None,
        description="Optional runtime LLM provider override for the iteration.",
    )
    model: Optional[str] = Field(
        None,
        description="Optional runtime model override for the iteration.",
    )
    save: bool = Field(True, description="When true, persist the revised HardwareIntermediateRepresentation over the existing project record.")
    idempotency_key: Optional[str] = Field(
        None,
        description="Optional stable key used to replay a saved iteration without creating another revision.",
    )

    @field_validator("instruction", mode="before")
    @classmethod
    def strip_iteration_instruction(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("namespace", "provider", "model", "idempotency_key", mode="before")
    @classmethod
    def strip_optional_iteration_selector(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class VideoSelfCorrectRequest(BaseModel):
    video_url: str = Field(..., min_length=1, description="HTTP(S) URL for the generated video to review.")
    video_key: Optional[str] = Field(
        None,
        description="Optional saved video object key. Used to verify the review target belongs to this project.",
    )
    namespace: Optional[str] = Field(
        None,
        description="Optional project namespace to target for the corrective iteration.",
    )
    provider: Optional[str] = Field(
        None,
        description="Optional runtime LLM provider override for applying the iteration.",
    )
    model: Optional[str] = Field(
        None,
        description="Optional runtime LLM model override for applying the iteration.",
    )
    review_model: Optional[str] = Field(
        None,
        description="Optional Fireworks review model override. Defaults to kimi-k2p6 frame review unless native video deployment routing is configured.",
    )
    save: bool = Field(True, description="When true, persist the revised HardwareIntermediateRepresentation over the existing project record.")
    idempotency_key: Optional[str] = Field(
        None,
        description="Optional stable key used to replay a saved correction without creating another revision.",
    )

    @field_validator("video_url", mode="before")
    @classmethod
    def strip_video_url(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("video_key", "namespace", "provider", "model", "review_model", "idempotency_key", mode="before")
    @classmethod
    def strip_optional_video_review_selector(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("video_url")
    @classmethod
    def validate_video_url(cls, value: str) -> str:
        if not re.match(r"^https?://", value):
            raise ValueError("video_url must be an http(s) URL.")
        return value

class ValidationReport(BaseModel):
    is_valid: bool
    issues: List[ValidationIssue]
