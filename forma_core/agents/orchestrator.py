import base64
import json
import logging
from forma_core.config import config
from forma_core.config.parti import resolve_parti_model
import re
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field, field_serializer
from dotenv import load_dotenv

load_dotenv()

from forma_core.database import (
    get_component_template_by_part_number,
    list_component_templates,
    persist_chat_project_revision,
    persist_legacy_project_projection,
    update_project_deletion_state,
)
from forma_core.llm import (
    LLMProviderConfigError,
    LLMProviderInputError,
    LLMProviderOutputError,
    LLMProviderPreflightError,
    LLMProviderValidation,
    LLMRuntimeConfig,
    build_llm_provider,
    enforce_production_llm_preflight,
    resolve_llm_runtime_config,
)
from forma_core.debug import redact_debug_text
from forma_core.observability import serialize_for_langfuse, start_observation, update_observation
from forma_core.agents.pipeline import (
    GenerationStageRecord,
    GenerationStageRun,
    GenerationStageSpec,
    PipelineCancelledError,
    agent_pipeline_step,
    emit_agent_pipeline_event,
    ensure_agent_pipeline_active,
)
from forma_core.agents.component_reconciliation import reconcile_explicit_catalog_components
from forma_core.agents.system_architecture import (
    architecture_tree_is_usable,
    build_default_system_architecture,
    compact_component_catalog,
    compact_component_context,
    compact_net_context,
    ensure_system_architecture,
    hydrate_catalog_components,
    system_context,
)
from forma_core.runtime import (
    AlphaGenerationUnavailableError,
    deployment_mode_enabled,
    generation_unavailable_message,
)
from forma_core.user_integrations import ResolvedIntegrationSettings
from forma_core.workspaces.projects.models import (
    HardwareIntermediateRepresentation, ProjectOverview, FunctionalRequirements, 
    ComponentInstance, ConnectionNet, PinReference, AssemblyStep, 
    MechanicalNotes, MechanicalSource, MechanicalVector3, MechanicalRotation3,
    MechanicalPlacement, MechanicalSpatialRelationship, PinMappingEntry,
    ValidationIssue, PinDefinition, ValidationSummary, BusConnection, PowerRail,
    SystemArchitecture, component_detail_payload, component_instance_count,
    expand_component_instances,
)
from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.validation import validate_circuit, check_safety_violations, build_validation_summary
from forma_core.wiring import (
    WiringIntent,
    build_endpoint_catalog,
    compile_wiring_intent,
    derive_pin_mappings,
    endpoint_catalog_prompt,
    wiring_failure_category,
)

logger = logging.getLogger(__name__)


class DefaultComponentSelection(BaseModel):
    components: List[ComponentInstance]
    reconciled_component_parts: List[str] = Field(default_factory=list)

    @field_serializer("components")
    def serialize_components(self, components: List[ComponentInstance]) -> list[dict[str, object]]:
        # Stage snapshots have no shared part_definitions to hydrate from.
        return [component_detail_payload(component) for component in components]


class DefaultWiringOutput(BaseModel):
    nets: List[ConnectionNet]
    pin_mappings: List[PinMappingEntry]
    intent_issues: List[ValidationIssue] = Field(default_factory=list)


class DefaultValidationOutput(BaseModel):
    nets: List[ConnectionNet]
    pin_mappings: List[PinMappingEntry]
    issues: List[ValidationIssue]
    is_valid: bool


class DefaultBomOutput(BaseModel):
    estimated_cost: float


class DefaultAssemblyOutput(BaseModel):
    steps: List[AssemblyStep]


DEFAULT_GENERATION_STAGE_SPECS = [
    GenerationStageSpec(stage_id="intent_parser"),
    GenerationStageSpec(stage_id="requirements", dependencies=["intent_parser"]),
    GenerationStageSpec(stage_id="system_architecture", dependencies=["intent_parser", "requirements"]),
    GenerationStageSpec(
        stage_id="component_selection",
        dependencies=["requirements", "system_architecture"],
    ),
    GenerationStageSpec(stage_id="wiring_netlist", dependencies=["requirements", "component_selection"]),
    GenerationStageSpec(
        stage_id="validation_repair",
        dependencies=["requirements", "component_selection", "wiring_netlist"],
    ),
    GenerationStageSpec(stage_id="bom", dependencies=["intent_parser", "component_selection"]),
    GenerationStageSpec(
        stage_id="mechanical_fabrication",
        dependencies=["intent_parser", "system_architecture", "component_selection"],
    ),
    GenerationStageSpec(
        stage_id="assembly",
        dependencies=[
            "intent_parser",
            "system_architecture",
            "component_selection",
            "wiring_netlist",
            "mechanical_fabrication",
        ],
    ),
    GenerationStageSpec(
        stage_id="cad_generation",
        dependencies=["mechanical_fabrication", "assembly"],
    ),
    GenerationStageSpec(stage_id="package_project"),
]

PLACEHOLDER_TEXT_VALUES = {
    "",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "new",
    "new__rewrite_1",
}
PARTI_COMPONENT_TITLE_VALUES = {
    "main mcu",
    "mcu",
    "main controller",
    "microcontroller",
    "controller",
    "sensor",
    "display",
    "battery",
    "resistor",
    "led",
    "component",
    "module",
    "power module",
}
PARTI_COMPONENT_TITLE_TOKENS = {
    "mcu",
    "microcontroller",
    "sensor",
    "display",
    "battery",
    "resistor",
    "led",
    "module",
    "component",
}


def _is_placeholder_text(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in PLACEHOLDER_TEXT_VALUES


def _clean_parti_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("_", " ")
    cleaned = re.sub(r"\s*rewrite\s*\d+\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if _is_placeholder_text(cleaned):
        return None
    return cleaned or None


def _normalized_parti_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def _is_component_like_parti_title(value: Optional[str]) -> bool:
    normalized = _normalized_parti_text(value or "")
    if not normalized:
        return True
    if normalized in PARTI_COMPONENT_TITLE_VALUES:
        return True
    compact = normalized.replace(" ", "")
    if re.fullmatch(r"(u|r|c|sen|disp|bat|led|mcu)\d*", compact):
        return True
    tokens = normalized.split()
    return len(tokens) <= 3 and any(token in PARTI_COMPONENT_TITLE_TOKENS for token in tokens)


def _parti_seed_summary(seed: Dict[str, Any]) -> Optional[str]:
    for key in ("summary", "description", "project_summary", "new_prompt"):
        text = _clean_parti_text(seed.get(key))
        if text:
            return text
    return None


def _parti_seed_quality_issue(seed: Optional[Dict[str, Any]], seed_title: Optional[str]) -> Optional[str]:
    if not seed:
        return "seed response did not include a usable JSON object."
    if seed.get("synthetic") is True or seed.get("seed_true") is True:
        return "seed response was marked synthetic instead of a real model-derived project seed."
    if _is_component_like_parti_title(seed_title):
        return f"seed project title is component-like, not project-like: {seed_title!r}."
    if not _parti_seed_summary(seed):
        return "seed response did not include a usable non-placeholder summary."
    return None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _env_float(name: str, default: float) -> float:
    raw_value = config.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.1fs.", name, raw_value, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw_value = config.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _generation_fallback_disabled() -> bool:
    return any(
        _env_bool(name)
        for name in (
            "FORMA_DISABLE_GENERATION_FALLBACK",
            "FORMA_STRICT_GENERATION",
            "LLM_DISABLE_FALLBACK",
        )
    )


def canonical_project_uuid(value: Optional[str] = None) -> str:
    """Return a canonical UUID string, ignoring legacy/non-UUID project ids."""
    if value:
        try:
            return str(uuid.UUID(str(value).strip()))
        except (TypeError, ValueError, AttributeError):
            pass
    return str(uuid.uuid4())

# Tool to query database templates
def get_db_component_templates() -> List[Dict[str, Any]]:
    """Helper tool that returns all available hardware templates in the seed database."""
    templates = []
    for t in list_component_templates():
        templates.append({
            "part_number": t.part_number,
            "name": t.name,
            "category": t.category,
            "description": t.description,
            "price": t.price,
            "sourcing_url": t.sourcing_url,
            "pins": t.pins,
            "use_cases": t.use_cases
        })
    return templates

# Helper utilities to enrich HardwareIntermediateRepresentation schemas dynamically
def extract_power_rails(components: List[ComponentInstance], nets: List[ConnectionNet]) -> List[PowerRail]:
    rails = []
    component_lookup = {component.ref_des: component for component in components}
    for net in nets:
        if net.net_type.lower() == "power" and net.voltage:
            source = None
            for pin_ref in net.pins:
                component = component_lookup.get(pin_ref.ref_des)
                if component and component.category.lower() == "power":
                    source = pin_ref.ref_des
                    break
                if pin_ref.ref_des == "BAT1":
                    source = "BAT1"
                elif pin_ref.ref_des == "USB-Power" or "power" in pin_ref.ref_des.lower():
                    source = pin_ref.ref_des
            if not source:
                source = "U1"
            
            rails.append(PowerRail(
                # Separate nets can intentionally carry the same nominal voltage
                # (for example, always-on logic power and switched actuator power).
                # The net ID identifies the electrical domain; voltage alone does
                # not and can produce duplicate ProjectSystem IDs downstream.
                rail_id=f"RAIL_{net.net_id}",
                voltage=net.voltage,
                max_current_capacity_ma=500.0 if net.voltage == 3.3 else 1000.0,
                source_component=source
            ))
    return rails

def extract_buses(nets: List[ConnectionNet]) -> List[BusConnection]:
    buses = []
    i2c_nets = [net.net_id for net in nets if net.net_type.lower() == "i2c"]
    if i2c_nets:
        buses.append(BusConnection(
            bus_id="BUS_I2C_1",
            bus_type="I2C",
            clock_frequency_hz=100000.0,
            nets=i2c_nets
        ))
    spi_nets = [net.net_id for net in nets if net.net_type.lower() == "spi"]
    if spi_nets:
        buses.append(BusConnection(
            bus_id="BUS_SPI_1",
            bus_type="SPI",
            clock_frequency_hz=1000000.0,
            nets=spi_nets
        ))
    return buses

def estimate_current_draw(components: List[ComponentInstance]) -> float:
    draw = 0.0
    for comp in components:
        cat = comp.category.lower()
        if cat == "microcontroller":
            draw += 80.0
        elif cat == "display":
            draw += 25.0
        elif cat == "actuator":
            if comp.part_number == "SG90-Servo":
                draw += 250.0
            else:
                draw += 70.0 # relay coil
        elif cat == "sensor":
            draw += 5.0
        elif comp.part_number == "LED-Red-Generic":
            draw += 15.0
    return draw

def _mechanical_vector(x_mm: float, y_mm: float, z_mm: float) -> MechanicalVector3:
    return MechanicalVector3(
        x_mm=round(float(x_mm), 2),
        y_mm=round(float(y_mm), 2),
        z_mm=round(float(z_mm), 2)
    )

def _component_text(component: ComponentInstance) -> str:
    return f"{component.ref_des} {component.name} {component.part_number} {component.category}".lower()

def _category_key(component: ComponentInstance) -> str:
    return component.category.strip().lower()

def _is_enclosure_component(component: ComponentInstance) -> bool:
    text = _component_text(component)
    if any(token in text for token in ["screw", "insert", "standoff", "button cap", "fastener"]):
        return False
    return any(token in text for token in ["main enclosure", "enclosure shell", "project box", "shell", "housing", "case"])

def _infer_render_dimensions(ir: HardwareIntermediateRepresentation) -> MechanicalVector3:
    if ir.mechanical and ir.mechanical.render_dimensions:
        return ir.mechanical.render_dimensions

    haystack = " ".join([
        ir.overview.title if ir.overview else "",
        ir.overview.description if ir.overview else "",
        " ".join(ir.constraints or []),
        " ".join(ir.fabrication_notes or []),
    ]).lower()

    if any(token in haystack for token in ["mp3", "audio", "pocket", "portable"]):
        return _mechanical_vector(100, 21, 54)
    if any(token in haystack for token in ["plant", "water", "soil", "garden"]):
        return _mechanical_vector(116, 82, 55)
    if any(token in haystack for token in ["thermostat", "nest", "hvac"]):
        return _mechanical_vector(86, 24, 86)
    if any(token in haystack for token in ["deadbolt", "lock", "servo"]):
        return _mechanical_vector(92, 64, 38)

    electrical_count = len([component for component in ir.components if _category_key(component) not in {"mechanical", "3d print"}])
    width = max(92, min(150, 70 + electrical_count * 7))
    depth = max(48, min(92, 36 + electrical_count * 4))
    height = max(30, min(70, 24 + electrical_count * 3))
    return _mechanical_vector(width, depth, height)

def _placement_layer(component: ComponentInstance) -> str:
    key = _category_key(component)
    text = _component_text(component)

    if _is_enclosure_component(component):
        return "enclosure"
    if key == "3d print":
        return "print"
    if key == "mechanical":
        if any(token in text for token in ["screw", "insert", "standoff", "boss"]):
            return "structural"
        return "mechanism"
    return "electrical"

def _placement_size(component: ComponentInstance, dimensions: MechanicalVector3) -> MechanicalVector3:
    key = _category_key(component)
    text = _component_text(component)

    if _is_enclosure_component(component):
        return dimensions
    if any(token in text for token in ["front bezel", "faceplate", "acrylic", "window", "trim"]):
        return _mechanical_vector(dimensions.x_mm * 0.82, max(2.0, dimensions.y_mm * 0.12), dimensions.z_mm * 0.72)
    if any(token in text for token in ["back cover", "rear cover", "cover"]):
        return _mechanical_vector(dimensions.x_mm * 0.88, max(2.0, dimensions.y_mm * 0.12), dimensions.z_mm * 0.86)
    if "battery" in text:
        return _mechanical_vector(min(48, dimensions.x_mm * 0.45), min(26, dimensions.y_mm * 0.65), min(9, dimensions.z_mm * 0.22))
    if "speaker" in text:
        return _mechanical_vector(24, min(12, dimensions.y_mm * 0.45), 24)
    if "relay" in text:
        return _mechanical_vector(38, 26, 16)
    if "servo" in text:
        return _mechanical_vector(23, 12, 29)
    if any(token in text for token in ["oled", "display"]):
        return _mechanical_vector(34, 3, 18)
    if any(token in text for token in ["button", "switch", "cap"]):
        return _mechanical_vector(10, 7, 10)
    if any(token in text for token in ["usb-c", "usb"]):
        return _mechanical_vector(18, 8, 7)
    if any(token in text for token in ["screw", "insert", "standoff"]):
        return _mechanical_vector(5, 5, 8)
    if any(token in text for token in ["mount", "bracket", "plate"]):
        return _mechanical_vector(34, 4, 18)

    sizes = {
        "microcontroller": (38, 28, 5),
        "sensor": (20, 12, 14),
        "actuator": (30, 22, 14),
        "display": (34, 3, 18),
        "power": (42, 22, 8),
        "passives": (15, 12, 8),
        "communication": (28, 18, 5),
        "mechanical": (14, 10, 8),
        "3d print": (30, 5, 18),
    }
    x_mm, y_mm, z_mm = sizes.get(key, (22, 16, 6))
    return _mechanical_vector(x_mm, y_mm, z_mm)

def _row_position(index: int, count: int, span: float) -> float:
    if count <= 1:
        return 0.0
    return -span / 2 + span * (index / (count - 1))

def _placement_position(component: ComponentInstance, components: List[ComponentInstance], dimensions: MechanicalVector3) -> MechanicalVector3:
    key = _category_key(component)
    text = _component_text(component)
    width = dimensions.x_mm
    depth = dimensions.y_mm
    height = dimensions.z_mm

    if _is_enclosure_component(component):
        return _mechanical_vector(0, 0, 0)
    if any(token in text for token in ["front bezel", "faceplate", "trim plate", "acrylic cover", "window"]):
        return _mechanical_vector(0, -depth * 0.46, height * 0.04)
    if any(token in text for token in ["back cover", "rear cover", "cover"]):
        return _mechanical_vector(0, depth * 0.46, 0)
    if any(token in text for token in ["oled mount", "display bezel"]):
        return _mechanical_vector(0, -depth * 0.36, height * 0.22)
    if any(token in text for token in ["controller mount", "esp32 mount", "board mount"]):
        return _mechanical_vector(0, -depth * 0.05, -height * 0.1)

    button_like = [item for item in components if any(token in _component_text(item) for token in ["button", "switch", "cap"])]
    button_index = next((index for index, item in enumerate(button_like) if item.ref_des == component.ref_des), -1)
    if button_index >= 0:
        return _mechanical_vector(_row_position(button_index, len(button_like), width * 0.42), -depth * 0.43, -height * 0.12)

    structural = [item for item in components if _placement_layer(item) == "structural"]
    structural_index = next((index for index, item in enumerate(structural) if item.ref_des == component.ref_des), -1)
    if structural_index >= 0:
        corner_x = -width * 0.42 if structural_index % 2 == 0 else width * 0.42
        corner_z = -height * 0.36 if structural_index < 2 else height * 0.36
        return _mechanical_vector(corner_x, depth * 0.28, corner_z)

    if "display" in key or "oled" in text:
        return _mechanical_vector(0, -depth * 0.43, height * 0.24)
    if key == "microcontroller":
        return _mechanical_vector(0, 0, -height * 0.04)
    if "battery" in text:
        return _mechanical_vector(-width * 0.27, depth * 0.24, -height * 0.26)
    if any(token in text for token in ["charger", "usb-c", "usb"]):
        return _mechanical_vector(width * 0.28, -depth * 0.36, -height * 0.28)
    if "speaker" in text:
        return _mechanical_vector(width * 0.32, depth * 0.3, height * 0.2)
    if any(token in text for token in ["sd", "storage"]):
        return _mechanical_vector(-width * 0.3, -depth * 0.04, height * 0.04)
    if any(token in text for token in ["dac", "audio"]):
        return _mechanical_vector(width * 0.22, depth * 0.02, 0)
    if key == "sensor":
        sensors = [item for item in components if _category_key(item) == "sensor"]
        sensor_index = max(0, next((index for index, item in enumerate(sensors) if item.ref_des == component.ref_des), 0))
        return _mechanical_vector(_row_position(sensor_index, len(sensors), width * 0.44), -depth * 0.42, height * 0.16)
    if key == "actuator":
        actuators = [item for item in components if _category_key(item) == "actuator"]
        actuator_index = max(0, next((index for index, item in enumerate(actuators) if item.ref_des == component.ref_des), 0))
        return _mechanical_vector(width * 0.3, depth * (0.12 - actuator_index * 0.18), -height * 0.04 + actuator_index * height * 0.18)
    if key == "power":
        power_parts = [item for item in components if _category_key(item) == "power"]
        power_index = max(0, next((index for index, item in enumerate(power_parts) if item.ref_des == component.ref_des), 0))
        return _mechanical_vector(-width * 0.28 + power_index * width * 0.22, depth * 0.22, -height * 0.25)

    remaining = [
        item for item in components
        if _category_key(item) not in {"mechanical", "3d print"}
        and _category_key(item) not in {"microcontroller", "display", "sensor", "actuator", "power"}
    ]
    remaining_index = max(0, next((index for index, item in enumerate(remaining) if item.ref_des == component.ref_des), 0))
    return _mechanical_vector(_row_position(remaining_index, len(remaining), width * 0.64), -depth * 0.16, -height * 0.03)

def _dominant_axis(source: MechanicalPlacement, target: MechanicalPlacement) -> str:
    deltas = {
        "X": abs(target.position.x_mm - source.position.x_mm),
        "Y": abs(target.position.y_mm - source.position.y_mm),
        "Z": abs(target.position.z_mm - source.position.z_mm),
    }
    return max(deltas, key=deltas.get)

def _offset_for_axis(source: MechanicalPlacement, target: MechanicalPlacement, axis: str) -> float:
    if axis == "X":
        return target.position.x_mm - source.position.x_mm
    if axis == "Y":
        return target.position.y_mm - source.position.y_mm
    return target.position.z_mm - source.position.z_mm

def build_mechanical_render_data(ir: HardwareIntermediateRepresentation) -> HardwareIntermediateRepresentation:
    """Populate the live Three.js/R3F render contract when the agent output is sparse."""
    ensure_system_architecture(ir)
    if not ir.mechanical or not ir.components:
        return ir

    dimensions = _infer_render_dimensions(ir)
    ir.mechanical.render_dimensions = dimensions

    existing_placement_refs = {placement.ref_des for placement in ir.mechanical.component_placements}
    generated_placements: List[MechanicalPlacement] = []
    for component in ir.components:
        if component.ref_des in existing_placement_refs:
            continue

        position = _placement_position(component, ir.components, dimensions)
        generated_placements.append(
            MechanicalPlacement(
                ref_des=component.ref_des,
                label=component.name,
                category=component.category,
                layer=_placement_layer(component),
                position=position,
                size=_placement_size(component, dimensions),
                orientation_deg=MechanicalRotation3(),
                mounting_face="front" if position.y_mm < -dimensions.y_mm * 0.32 else "internal",
                notes=component.rationale
            )
        )

    if generated_placements:
        ir.mechanical.component_placements = [
            *ir.mechanical.component_placements,
            *generated_placements,
        ]

    if not ir.mechanical.spatial_relationships:
        placements_by_ref = {placement.ref_des: placement for placement in ir.mechanical.component_placements}
        controller = next(
            (placement for placement in ir.mechanical.component_placements if (placement.category or "").lower() == "microcontroller"),
            None
        )
        relationships: List[MechanicalSpatialRelationship] = []

        if controller:
            for placement in ir.mechanical.component_placements:
                if placement.ref_des == controller.ref_des or placement.layer == "enclosure":
                    continue
                axis = _dominant_axis(controller, placement)
                relationships.append(MechanicalSpatialRelationship(
                    source_ref_des=controller.ref_des,
                    target_ref_des=placement.ref_des,
                    relation="spatial offset from controller",
                    axis=axis,
                    offset_mm=round(_offset_for_axis(controller, placement, axis), 2),
                    notes=f"{placement.ref_des} is placed along the {axis} axis relative to {controller.ref_des}."
                ))
                if len(relationships) >= 9:
                    break

        for placement in ir.mechanical.component_placements:
            text = f"{placement.ref_des} {placement.label or ''}".lower()
            if "display" in text or "oled" in text:
                bezel = next((candidate for candidate in ir.mechanical.component_placements if "bezel" in f"{candidate.label or ''}".lower()), None)
                if bezel and placement.ref_des != bezel.ref_des:
                    relationships.append(MechanicalSpatialRelationship(
                        source_ref_des=placement.ref_des,
                        target_ref_des=bezel.ref_des,
                        relation="aligned with display opening",
                        axis="Y",
                        offset_mm=round(bezel.position.y_mm - placement.position.y_mm, 2),
                        notes="Display centerline is aligned to the front bezel/window cutout."
                    ))
                    break

        ir.mechanical.spatial_relationships = [
            relationship
            for relationship in relationships
            if relationship.source_ref_des in placements_by_ref and relationship.target_ref_des in placements_by_ref
        ]

    metadata = ir.assembly_metadata or {}
    ir.assembly_metadata = {
        **metadata,
        "render_dimensions": dimensions.model_dump(),
        "component_placement_count": len(ir.mechanical.component_placements),
        "spatial_relationship_count": len(ir.mechanical.spatial_relationships),
        "render_pipeline": "Three.js + React Three Fiber",
    }
    return ir

# Define the ADK-style Multi-Agent Orchestrator
class HardwarePipelineOrchestrator:
    def __init__(
        self,
        use_simulation: bool = False,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        runtime_config: Optional[LLMRuntimeConfig] = None,
        settings: Optional[ResolvedIntegrationSettings] = None,
        persist_project: bool = True,
    ):
        self.runtime_config = runtime_config or resolve_llm_runtime_config(
            provider_name=provider_name,
            model_name=model_name,
            settings=settings,
        )
        self.llm_provider = build_llm_provider(runtime_config=self.runtime_config, settings=settings)
        self.use_simulation = use_simulation or not self.llm_provider.is_configured
        self.model_name = self.llm_provider.model_name
        self.persist_project = persist_project
        self._active_generation_metadata: Dict[str, Any] = {}

    def get_debug_config(self, *, raise_on_preflight: bool = True) -> Dict[str, Any]:
        """Return LLM provider resolution details without exposing credentials."""
        validation = self.llm_provider.validate_configured_model(raise_on_strict=False)
        debug = validation.as_debug_dict()
        try:
            enforce_production_llm_preflight(validation)
        except LLMProviderPreflightError as exc:
            if raise_on_preflight:
                raise
            # Configuration reads must still describe unavailable providers so
            # independent OpenCode authoring can initialize. Execution retains
            # the strict preflight in validate_configured_model().
            debug["live_generation_enabled"] = False
            debug["validation_error"] = redact_debug_text(str(exc))
        else:
            self.model_name = validation.actual_model or self.llm_provider.model_name
        return {
            **debug,
            "runtime": self.runtime_config.as_debug_dict(),
        }

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        """Resolve and validate the configured LLM provider/model."""
        validation = self.llm_provider.validate_configured_model(raise_on_strict=raise_on_strict)
        enforce_production_llm_preflight(validation)
        self.model_name = validation.actual_model or self.llm_provider.model_name
        return validation

    def _call_llm_structured(self, prompt: str, schema_class: Any, image_bytes: Optional[bytes] = None, image_mime_type: Optional[str] = None) -> Any:
        """Invoke the configured LLM provider for structured JSON output."""
        if self.use_simulation:
            raise RuntimeError("Simulation mode is active; should use simulated generator instead.")

        schema_name = getattr(schema_class, "__name__", "StructuredResponse")
        metadata = {
            "workflow": "default",
            "llm_provider": self.llm_provider.provider_name,
            "runtime_provider": self.runtime_config.provider,
            "runtime_model": self.runtime_config.model,
            "requested_provider": self.runtime_config.requested_provider,
            "requested_model": self.runtime_config.requested_model,
            "response_schema": schema_name,
            "has_reference_image": bool(image_bytes),
            "image_mime_type": image_mime_type,
        }
        with start_observation(
            name=f"forma.default.{schema_name}",
            as_type="generation",
            model=self.llm_provider.model_name,
            input={
                "prompt": prompt,
                "schema": schema_name,
                "has_reference_image": bool(image_bytes),
                "image_mime_type": image_mime_type,
            },
            metadata=metadata,
        ) as observation:
            try:
                result = self.llm_provider.generate_structured(prompt, schema_class, image_bytes, image_mime_type)
                self.model_name = self.llm_provider.model_name
                update_observation(
                    observation,
                    output=serialize_for_langfuse(result),
                    metadata={**metadata, "actual_model": self.model_name},
                )
                return result
            except Exception as e:
                update_observation(
                    observation,
                    metadata={**metadata, "error_type": e.__class__.__name__, "error": str(e)[:1000]},
                )
                logger.error(f"LLM structured call failed via {self.llm_provider.provider_name}: {e}")
                raise

    def generate_project(
        self,
        user_prompt: str,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
        generation_metadata: Optional[Dict[str, Any]] = None,
    ) -> HardwareIntermediateRepresentation:
        """Orchestrates the 7-agent hardware compilation pipeline with verification loop."""
        self.validate_configured_model()
        self._active_generation_metadata = {
            key: value
            for key, value in (generation_metadata or {}).items()
            if value is not None and value != ""
        }
        self._active_generation_metadata.setdefault(
            "cad_required",
            False,
        )
        # 0. Safety Guardrail Pre-check
        emit_agent_pipeline_event("default", "safety_guardrail", "started")
        safety_prompt = str(self._active_generation_metadata.get("project_prompt") or user_prompt)
        safety_error = check_safety_violations(safety_prompt)
        if safety_error:
            emit_agent_pipeline_event("default", "safety_guardrail", "failed", details={"reason": safety_error})
            logger.warning(f"Safety block triggered for prompt: '{safety_prompt}'")
            # Compile a default safety-blocked IR package
            overview = ProjectOverview(
                title="PROJECT BLOCKED - Safe Scope Enforced",
                description="Your design compilation was blocked because it falls outside of the low-voltage, educational hardware MVP scope.",
                difficulty="N/A",
                estimated_cost=0.0,
                category="Safety Blocked"
            )
            issue = ValidationIssue(
                severity="CRITICAL",
                category="Safety Block",
                description=safety_error,
                troubleshooting="Please modify your design request to focus exclusively on safe, low-voltage educational electronics (e.g. Arduino, ESP32, low-voltage sensors, displays, standard 3V-5V DC relays or simple hobbyist motors)."
            )
            validation_summary = ValidationSummary(critical=[issue])
            
            project_ir = HardwareIntermediateRepresentation(
                hardware_ir_version="0.1",
                overview=overview,
                requirements=FunctionalRequirements(
                    requirements=["Compile blocked due to high-voltage, weapons, automotive, or clinical risk."],
                    power_needs="Blocked",
                    operating_voltage=0.0,
                    missing_info=["Blocked"]
                ),
                components=[],
                nets=[],
                buses=[],
                pin_mappings=[],
                assembly=[],
                mechanical=None,
                constraints=["Safety envelope enforcement"],
                power_rails=[],
                estimated_current_draw_ma=0.0,
                fabrication_notes=["Compilation blocked"],
                assembly_metadata={
                    "status": "blocked",
                    "llm_provider": self.llm_provider.provider_name,
                    "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                    "runtime_provider": self.runtime_config.provider,
                    "requested_model": self.runtime_config.requested_model or self.runtime_config.model,
                    "runtime_model": self.runtime_config.model,
                    "pipeline": "safety guardrail",
                },
                project_version_history=[{"version": "0.1", "description": "Blocked design generation due to safety violations"}],
                validation=validation_summary,
                is_valid=False
            )
            # Save blocked run as record in database
            self.save_project_to_db(user_prompt, project_ir)
            return project_ir
        emit_agent_pipeline_event("default", "safety_guardrail", "completed")

        logger.info("Invoking Context Clarifier Agent...")
        with agent_pipeline_step("default", "context_clarifier", details={
            "has_human_context": "HUMAN-IN-THE-LOOP CONTEXT:" in user_prompt,
        }):
            pass

        if self.use_simulation:
            if deployment_mode_enabled():
                raise AlphaGenerationUnavailableError(generation_unavailable_message(self.get_debug_config()))
            if _generation_fallback_disabled():
                raise LLMProviderConfigError(generation_unavailable_message(self.get_debug_config()))
            return self._generate_simulated_project(user_prompt, has_image=bool(image_bytes))

        try:
            model_validation = self.validate_configured_model()
            if image_bytes:
                self.llm_provider.validate_image_input()
        except LLMProviderConfigError as e:
            if deployment_mode_enabled():
                logger.error("LLM provider validation failed in deployment mode: %s", e)
                raise AlphaGenerationUnavailableError(generation_unavailable_message(self.get_debug_config())) from e
            raise

        if resolve_parti_model(model_validation.provider, model_validation.actual_model or self.llm_provider.model_name):
            return self._generate_parti_project(
                user_prompt,
                model_validation=model_validation,
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
            )

        if self._active_generation_metadata.get("legacy_atomic_generation") is not True:
            return self._generate_staged_project(
                user_prompt,
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
                model_validation=model_validation,
            )

        try:
            logger.info("Starting Forma Agent Pipeline Execution...")
            
            # 1. Intent Parser Agent
            logger.info("Invoking Intent Parser Agent...")
            with agent_pipeline_step("default", "intent_parser"):
                intent_prompt = f"""
                You are an Intent Parser Agent. Convert the user's idea and visual reference (if provided) into a structured hardware project overview.
                User Idea: "{user_prompt}"
                Generate the ProjectOverview schema containing title, description, difficulty, estimated cost (set to 0 for now), and category.
                """
                overview: ProjectOverview = self._call_llm_structured(intent_prompt, ProjectOverview, image_bytes, image_mime_type)
            if _is_placeholder_text(overview.title) or _is_placeholder_text(overview.description):
                raise LLMProviderOutputError(
                    "LLM output was unusable: the selected model returned placeholder project overview fields "
                    f"(title={overview.title!r}, description={overview.description!r}) for "
                    f"{self.llm_provider.provider_name}/{self.llm_provider.model_name}. "
                    "Choose a model compatible with the requested structured-output schema."
                )

            # 2. Requirements Agent
            logger.info("Invoking Requirements Agent...")
            with agent_pipeline_step("default", "requirements"):
                req_prompt = f"""
                You are a Requirements Agent. Extract the functional requirements, power needs, physical constraints, operating voltage, safety notes, and missing information for this hardware project.
                User Idea: "{user_prompt}"
                Project Title: "{overview.title}"
                Project Description: "{overview.description}"
                Generate the FunctionalRequirements schema. Make sure to identify appropriate operating voltage (usually 3.3V or 5V depending on common microcontrollers like ESP32 or Arduino).
                """
                requirements: FunctionalRequirements = self._call_llm_structured(req_prompt, FunctionalRequirements, image_bytes, image_mime_type)

            # 3. System Architecture Agent
            logger.info("Invoking System Architecture Agent...")
            with agent_pipeline_step("default", "system_architecture"):
                architecture_prompt = f"""
                You are a System Architecture Agent. Decompose the complete product into a concise hierarchical tree before any specialist expands implementation details.

                Project: {overview.model_dump_json()}
                Requirements: {requirements.model_dump_json()}

                Return SystemArchitecture with one product root and only the applicable discipline branches, such as:
                - electrical: power, control/compute, sensing/input, communication, and output/actuation systems
                - mechanical: enclosure/protection, mounting/structure, motion, thermal, sealing, and service systems
                - firmware: hardware abstraction, control logic, communications, and diagnostics

                Every node must explain why it is needed, what outcomes it owns, its system-level constraints, abstract component roles, interfaces, and the specialist detail_owner.
                Keep this at architecture level. Do not choose exact parts, nets, packages, connectors, or pin numbers.
                Use stable dotted system_id values and nest specific systems under their discipline.
                Every children item must be a complete nested SystemNode object, never a dotted-ID string.
                Every interfaces item must be a SystemInterface object with name, connects_to, and purpose;
                use connects_to for dotted system-ID references rather than putting references in children.
                """
                system_architecture: SystemArchitecture = self._call_llm_structured(
                    architecture_prompt,
                    SystemArchitecture,
                    image_bytes,
                    image_mime_type,
                )
                if not architecture_tree_is_usable(system_architecture):
                    logger.warning(
                        "System architecture provider output contained flattened or duplicate child IDs; "
                        "using the deterministic architecture tree instead."
                    )
                    system_architecture = build_default_system_architecture(overview, requirements)

            # 4. Component Selection Agent
            logger.info("Invoking Component Selection Agent...")
            with agent_pipeline_step("default", "component_selection"):
                db_components = get_db_component_templates()
                compact_catalog_json = json.dumps(compact_component_catalog(db_components), indent=2)
                architecture_json = json.dumps(system_context(system_architecture), indent=2)
                
                comp_prompt = f"""
                You are a Component Selection Agent.
                Your job is to select compatible components from our inventory database to fulfill the project's requirements.
                
                Requirements: {requirements.model_dump_json()}

                System architecture (purpose and roles, intentionally no pins):
                {architecture_json}
                
                Here is the compact component catalog. Pin counts and interface types are included, but physical pin definitions are intentionally omitted:
                {compact_catalog_json}
                
                Select a suitable list of components. You MUST include a microcontroller (e.g., ESP32-WROOM-32D or Arduino-Nano-V3) and any sensors, actuators, displays, or passive/power parts needed.
                For each selected component, instantiate it as a ComponentInstance with:
                - ref_des: Unique ID like 'U1' (for MCUs), 'SEN1', 'ACT1', 'DISP1', 'R1', 'LED1', 'BAT1'
                - part_number: MUST match exactly one of the available part_numbers in the database list above.
                - name, category, unit_price, sourcing_url: Match the selected DB template.
                - rationale: Explain why this component is selected and how it fits.
                - pins: Return an empty list. Exact catalog pins are hydrated deterministically after selection.
                Every ComponentInstance is one physical occurrence. Repeated parts must be emitted as separate
                records with unique reference designators (for example M1, M2, M3, and M4), never as one record
                with an aggregate quantity.
                
                Output a JSON representation conforming to a List[ComponentInstance].
                """
                # Helper class to wrap list output
                class ComponentListWrapper(BaseModel):
                    components: List[ComponentInstance]
                    
                comp_wrapper: ComponentListWrapper = self._call_llm_structured(comp_prompt, ComponentListWrapper, image_bytes, image_mime_type)
                components = hydrate_catalog_components(comp_wrapper.components, db_components)
                components, reconciled_component_parts = reconcile_explicit_catalog_components(
                    user_prompt,
                    components,
                    db_components,
                )
                if reconciled_component_parts:
                    logger.info(
                        "Restored explicitly requested catalog parts omitted by component selection: %s",
                        ", ".join(reconciled_component_parts),
                    )
                components = expand_component_instances(components)

            # Compile intermediate IR for wiring
            components_json = json.dumps([component_detail_payload(c) for c in components], indent=2)

            # 5. Wiring/Netlist Agent (With Auto-Correction Loop)
            logger.info("Invoking Wiring/Netlist Agent...")
            with agent_pipeline_step("default", "wiring_netlist"):
                endpoint_catalog_json = json.dumps(
                    endpoint_catalog_prompt(build_endpoint_catalog(components)),
                    indent=2,
                )
                wiring_prompt = f"""
            You are a Wiring Intent Agent. Describe the connections needed for the selected components.
            Do not generate canonical net IDs or nested ref_des/pin_id objects.
            
            Endpoint catalog. Use only these exact endpoint IDs:
            {endpoint_catalog_json}
            
            Requirements: {requirements.model_dump_json()}
            
            Rules for connecting:
            1. Establish a Ground rail (GND) net and connect all compatible ground endpoints to it.
            2. Establish a Power rail (VCC/3.3V/5V) net only when the catalog identifies a source or regulated output.
               Make sure operating voltages match. Do not short 5V to 3.3V or power to ground.
            3. Wire signal endpoints: Connect communication pins together:
               - I2C SCL connects to the MCU's SCL (e.g., ESP32 pin 'D22' or Arduino pin 'A5')
               - I2C SDA connects to the MCU's SDA (e.g., ESP32 pin 'D21' or Arduino pin 'A4')
               - Digital sensor data pins connect to any Digital/GPIO pin on the MCU.
               - PWM actuators connect to a PWM-capable pin on the MCU.
            4. Every non-power/non-ground endpoint should appear in only one net.
            5. Passive parts bridge nets by using one passive endpoint per net.
            6. Do NOT leave critical pins unconnected.
            
            Return WiringIntent with nets containing name, net_type, voltage, and endpoint_ids.
            """
                wiring_intent: WiringIntent = self._call_llm_structured(
                    wiring_prompt,
                    WiringIntent,
                    image_bytes,
                    image_mime_type,
                )
                wiring_compilation = compile_wiring_intent(components, wiring_intent)
                nets = list(wiring_compilation.nets)
                pin_mappings = derive_pin_mappings(components, nets)

            # Self-healing loop: Run validation checks on wiring
            logger.info("Running circuit validation checks on generated netlist...")
            with agent_pipeline_step("default", "validation_repair"):
                validation_issues = [
                    *wiring_compilation.issues,
                    *validate_circuit(components, nets, requirements, prompt=user_prompt),
                ]
                if validation_issues:
                    emit_agent_pipeline_event(
                        "default",
                        "validation_repair",
                        "diagnostics",
                        details={
                            "failure_categories": sorted({
                                wiring_failure_category(issue) for issue in validation_issues
                            }),
                        },
                    )
                is_valid = not any(issue.severity == "CRITICAL" for issue in validation_issues)

                if not is_valid:
                    logger.warning("Critical circuit validation errors found! Triggering self-healing validation loop...")
                    issues_json = json.dumps([issue.model_dump() for issue in validation_issues], indent=2)
                    
                    healing_prompt = f"""
                You are a Wiring Intent Auto-Correction Agent. Correct only the rejected or invalid nets.
                Return replacement intents for existing canonical nets using replace_net_id. Omit valid nets.
                
                Endpoint catalog:
                {endpoint_catalog_json}
                
                Previous Wiring Nets:
                {json.dumps([n.model_dump() for n in nets], indent=2)}
                
                Critical Validation Errors:
                {issues_json}
                
                Fix these connections!
                - If there's a Short Circuit (VCC connected to GND), separate them.
                - If there's a Voltage Mismatch (e.g. 5V logic connected to 3.3V), either suggest level conversion or use a compatible operating voltage / net.
                - If an IC is unpowered, connect its VCC and GND pins to the corresponding power/ground nets.
                - If a pin is reused in multiple signal nets, fix the mapping to separate GPIO pins.
                - A physical pin must appear in only one net. For pull-up or pull-down resistors, put one resistor pin on the signal net and the other resistor pin on the power/ground rail; never put the sensor/MCU signal pin in a separate resistor-only signal net.
                
                Return WiringIntent. Use endpoint_ids from the catalog only. Keep valid existing nets unchanged.
                """
                    corrected_intent: WiringIntent = self._call_llm_structured(
                        healing_prompt,
                        WiringIntent,
                        image_bytes,
                        image_mime_type,
                    )
                    repaired = compile_wiring_intent(
                        components,
                        corrected_intent,
                        existing_nets=nets,
                    )
                    nets = repaired.all_nets
                    pin_mappings = derive_pin_mappings(components, nets)
                    
                    # Re-validate
                    validation_issues = [
                        *repaired.issues,
                        *validate_circuit(components, nets, requirements, prompt=user_prompt),
                    ]
                    is_valid = not any(issue.severity == "CRITICAL" for issue in validation_issues)
                    logger.info(f"Self-healing completed. Is valid: {is_valid}")

            # 5. BOM Agent
            logger.info("Invoking BOM Agent...")
            with agent_pipeline_step("default", "bom"):
                total_cost = sum(c.unit_price * component_instance_count(c) for c in components)
                overview.estimated_cost = round(total_cost, 2)

            # 6. Mechanical/Fabrication Agent
            logger.info("Invoking Mechanical/Fabrication Agent...")
            with agent_pipeline_step("default", "mechanical_fabrication"):
                mechanical_components_json = json.dumps(compact_component_context(components), indent=2)
                mechanical_architecture_json = json.dumps(system_context(system_architecture, "mechanical"), indent=2)
                mech_prompt = f"""
            You are a Mechanical/Fabrication and CAD Sourcing Agent. Provide enclosure, mounting, material, and 3D printing/laser cutting details for this project.
            Project: "{overview.title}" - Description: "{overview.description}"
            Original user request (including explicit shape/form-factor context):
            {user_prompt}
            Mechanical system branch:
            {mechanical_architecture_json}
            Components Selected (identity and envelope purpose only; electrical pins are intentionally omitted):
            {mechanical_components_json}
            Populate the MechanicalNotes schema, including:
            - physical_form: the requested overall shape, silhouette, and form factor. Treat explicit human shape context as authoritative. Do not default to a rectangular project box; enclosed, curved, cylindrical, wearable, folded, structural, and open-frame designs are all valid.
            - enclosure_type: the housing approach when the design needs a housing. For an exposed or open-frame design, say so instead of inventing a closed case.
            - fabrication_cost_estimate_usd: realistic mechanical-only print/cut/enclosure cost, excluding electrical components.
            - cad_sources: CAD/enclosure/fabrication records with name, source_type, url, file_formats, license, estimated_unit_price_usd, and adaptation notes.
            - render_dimensions: overall X/Y/Z envelope dimensions in millimeters.
            - component_placements: live 3D render placements for relevant electrical and mechanical components. Use an enclosure-centered coordinate system: X is width left/right, Y is depth front/back, Z is height bottom/top. Include center position, approximate component size, visibility layer, mounting face, and notes.
            - spatial_relationships: important physical offsets and alignment relationships between placed components, with dominant X/Y/Z axis and offset_mm when known.
            Use source URLs only when they are known from supplied source data or a browsing/sourcing agent result. Do not invent URLs.
            Generate the MechanicalNotes schema.
            """
                mechanical: MechanicalNotes = self._call_llm_structured(mech_prompt, MechanicalNotes, image_bytes, image_mime_type)

            # 7. Assembly Instruction Agent
            logger.info("Invoking Assembly Instruction Agent...")
            with agent_pipeline_step("default", "assembly"):
                assembly_components_json = json.dumps(compact_component_context(components), indent=2)
                assembly_nets_json = json.dumps(compact_net_context(nets), indent=2)
                assembly_architecture_json = json.dumps(system_context(system_architecture), indent=2)
                assembly_prompt = f"""
            You are an Assembly Instruction Agent. Produce step-by-step physical and electronic build instructions for the user.
            Project: "{overview.title}"
            System hierarchy: {assembly_architecture_json}
            Components (pin-free context): {assembly_components_json}
            Wiring Nets (system connectivity without individual pin IDs): {assembly_nets_json}
            Mechanical Guide: {mechanical.model_dump_json()}
            
            Provide structured sequential steps in the AssemblyStep schema (list of steps). Specify warnings/dangers where necessary.
            """
                class AssemblyWrapper(BaseModel):
                    steps: List[AssemblyStep]
                
                assembly_wrapper: AssemblyWrapper = self._call_llm_structured(assembly_prompt, AssemblyWrapper, image_bytes, image_mime_type)
                assembly = assembly_wrapper.steps

            # Dynamic field extractions
            with agent_pipeline_step("default", "package_project"):
                power_rails = extract_power_rails(components, nets)
                buses = extract_buses(nets)
                current_draw = estimate_current_draw(components)
                constraints = requirements.physical_constraints + [f"Operating Voltage: {requirements.operating_voltage}V"]
                fab_notes = mechanical.fabrication_details if mechanical else []
                
                validation_summary = build_validation_summary(validation_issues)

                # Compile into final HardwareIntermediateRepresentation
                project_ir = HardwareIntermediateRepresentation(
                    hardware_ir_version="0.1",
                    overview=overview,
                    requirements=requirements,
                    system_architecture=system_architecture,
                    components=components,
                    nets=nets,
                    buses=buses,
                    pin_mappings=pin_mappings,
                    assembly=assembly,
                    mechanical=mechanical,
                    constraints=constraints,
                    power_rails=power_rails,
                    estimated_current_draw_ma=current_draw,
                    fabrication_notes=fab_notes,
                    assembly_metadata={
                    "generated_at": datetime.utcnow().isoformat(),
                    "revision": 1,
                    "model_name": self.model_name,
                    "fallback_mode": model_validation.fallback_active,
                    "requested_model": model_validation.requested_model,
                    "actual_model": model_validation.actual_model,
                    "llm_provider": model_validation.provider,
                    "component_reconciliation_added": reconciled_component_parts,
                    "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                    "runtime_provider": self.runtime_config.provider,
                    "runtime_model": self.runtime_config.model,
                    "provider_overridden": self.runtime_config.provider_overridden,
                    "model_overridden": self.runtime_config.model_overridden,
                    "pipeline": "provider-agnostic structured LLM + ADK-style hardware agents",
                    },
                    project_version_history=[{"version": "0.1", "description": "Initial design compilation via 7-agent ADK pipeline"}],
                    validation=validation_summary,
                    is_valid=is_valid
                )
                
                # Save generated project to DB
                project_ir = build_mechanical_render_data(project_ir)
                self.save_project_to_db(user_prompt, project_ir)
            return project_ir

        except PipelineCancelledError:
            raise
        except LLMProviderConfigError:
            raise
        except LLMProviderInputError:
            raise
        except LLMProviderOutputError:
            raise
        except Exception as e:
            if deployment_mode_enabled():
                logger.error("Pipeline execution encountered an error in deployment mode: %s", e)
                debug_config = self.get_debug_config()
                if not debug_config.get("live_generation_enabled"):
                    raise AlphaGenerationUnavailableError(generation_unavailable_message(debug_config)) from e
                raise
            if _generation_fallback_disabled():
                logger.error("Pipeline execution failed and fallback is disabled: %s", e)
                raise
            logger.error("Pipeline execution encountered an error: %s", e)
            return self._generate_failed_project(e)

    def _request_parti_seed(
        self,
        user_prompt: str,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        selected_model = f"{self.llm_provider.provider_name}/{self.llm_provider.model_name}"
        parti_model = resolve_parti_model(self.llm_provider.provider_name, self.llm_provider.model_name)
        if image_bytes and (parti_model is None or not parti_model.supports_images):
            raise LLMProviderInputError(f"The selected Parti model '{selected_model}' cannot read reference images.")
        request_json = getattr(self.llm_provider, "_request_json", None)
        if not callable(request_json):
            return None, f"Parti model {selected_model} does not expose an OpenAI-compatible request method."

        prompt = (
            "You are Parti, a hardware project seed generator. Return only one concise JSON object. "
            'Use keys "project_title", "summary", and "hardware_roles": a concrete project name, '
            "a one sentence summary, and up to eight hardware role hints. "
            "Use the reference image when supplied. "
            "Do not use unknown, new__, synthetic, seed_ref, visual_ref, or placeholder values.\n"
            f"User request: {user_prompt}"
        )
        content: Any = prompt
        if image_bytes:
            mime_type = image_mime_type or "image/jpeg"
            if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise LLMProviderInputError(f"Parti model {selected_model} received an unsupported image MIME type.")
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                }},
            ]
        payload = {
            "model": self.llm_provider.model_name,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 500,
            "temperature": 0.2,
            "repetition_penalty": 1.1,
        }

        original_timeout = getattr(self.llm_provider, "timeout_seconds", None)
        seed_timeout = _env_float(
            "RUNPOD_PARTI_SEED_TIMEOUT_SECONDS",
            float(original_timeout) if original_timeout is not None else 1200.0,
        )
        try:
            if original_timeout is not None:
                self.llm_provider.timeout_seconds = max(1.0, seed_timeout)
            response = request_json("chat/completions", method="POST", payload=payload)
            choices = response.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                return None, f"Parti model {selected_model} seed response did not include text content."
            seed = _extract_json_object(content)
            if seed is None:
                return None, f"Parti model {selected_model} seed response did not include a usable JSON object."
            return seed, None
        except Exception as exc:
            return None, f"Parti model {selected_model} seed request failed: {exc}"
        finally:
            if original_timeout is not None:
                self.llm_provider.timeout_seconds = original_timeout

    def _component_from_template(
        self,
        *,
        ref_des: str,
        part_number: str,
        rationale: str,
        name: Optional[str] = None,
        category: Optional[str] = None,
    ) -> ComponentInstance:
        template = get_component_template_by_part_number(part_number)
        return ComponentInstance(
            ref_des=ref_des,
            part_number=part_number,
            name=name or (template.name if template else part_number),
            category=category or (template.category if template else "Passives"),
            quantity=1,
            unit_price=float(template.price if template else 0.0),
            sourcing_url=template.sourcing_url if template else None,
            rationale=rationale,
            pins=self._get_pins_for_part(part_number),
        )

    def _generate_parti_project(
        self,
        user_prompt: str,
        *,
        model_validation: LLMProviderValidation,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> HardwareIntermediateRepresentation:
        selected_model = model_validation.actual_model or self.llm_provider.model_name
        parti_model = resolve_parti_model(model_validation.provider, selected_model)
        if parti_model is None:
            raise LLMProviderConfigError(f"No Parti adapter configured for {model_validation.provider}/{selected_model}.")
        adapter_details = {"adapter": parti_model.adapter_version, "model": selected_model}
        emit_agent_pipeline_event("default", "intent_parser", "started", details=adapter_details)
        seed, seed_error = self._request_parti_seed(user_prompt, image_bytes, image_mime_type)
        if _generation_fallback_disabled() and (seed_error or not seed):
            emit_agent_pipeline_event("default", "intent_parser", "failed", details={"error": seed_error})
            raise LLMProviderOutputError(
                f"Runpod Parti model {selected_model} seed generation failed and generation fallback is disabled: "
                f"{seed_error or 'seed response did not include a usable JSON object.'}"
            )

        seed_title = None
        if seed:
            for key in ("project_title", "title", "display_name", "project_name", "seed_project_name"):
                seed_title = _clean_parti_text(seed.get(key))
                if seed_title:
                    break
        if _generation_fallback_disabled() and not seed_title:
            seed_preview = json.dumps(seed, ensure_ascii=True)[:1000] if seed is not None else "null"
            raise LLMProviderOutputError(
                f"Runpod Parti model {selected_model} seed response did not include a usable project title and generation fallback is disabled. "
                f"seed_preview={seed_preview}"
            )
        if _generation_fallback_disabled():
            seed_quality_issue = _parti_seed_quality_issue(seed, seed_title)
            if seed_quality_issue:
                seed_preview = json.dumps(seed, ensure_ascii=True)[:1000] if seed is not None else "null"
                raise LLMProviderOutputError(
                    f"Runpod Parti model {selected_model} seed response failed strict quality checks and generation fallback is disabled: "
                    f"{seed_quality_issue} seed_preview={seed_preview}"
                )
        emit_agent_pipeline_event("default", "intent_parser", "completed", details=adapter_details)

        emit_agent_pipeline_event("default", "requirements", "started", details=adapter_details)
        prompt_lower = user_prompt.lower()
        title = "Battery Environmental Sensor Dashboard"
        category = "IoT"
        if "gas" in prompt_lower:
            title = "Low-Voltage Gas Alert Monitor"
        elif "plant" in prompt_lower or "water" in prompt_lower or "soil" in prompt_lower:
            title = "Plant Environment Monitor"
        elif seed_title and "environmental" not in seed_title.lower():
            title = seed_title

        overview = ProjectOverview(
            title=title,
            description=f"Catalog-repaired Runpod Parti project for: '{user_prompt}'",
            difficulty="Intermediate",
            estimated_cost=0.0,
            category=category,
        )
        requirements = FunctionalRequirements(
            requirements=[
                "Read environmental temperature, humidity, and pressure sensor data.",
                "Show live readings and status feedback on a small OLED display.",
                "Run from a portable low-voltage battery source with shared ground.",
                "Expose a simple status LED for alerts, boot state, or threshold warnings.",
            ],
            power_needs="3.7V LiPo battery feeding the ESP32 VIN path; add a protected charger/boost module for production hardware.",
            operating_voltage=3.3,
            physical_constraints=[
                "Handheld or desktop enclosure under 120mm x 80mm x 40mm.",
                "Vent slots near the environmental sensors.",
                "OLED visible on the front face.",
                "Battery isolated from sensor airflow and mounting screws.",
            ],
            safety_notes=[
                "Do not connect bare LiPo cells without protection and charge management.",
                "Keep sensor openings away from liquid ingress and conductive debris.",
            ],
            missing_info=[],
        )
        emit_agent_pipeline_event("default", "requirements", "completed", details=adapter_details)

        emit_agent_pipeline_event("default", "system_architecture", "started", details=adapter_details)
        system_architecture = build_default_system_architecture(overview, requirements)
        emit_agent_pipeline_event("default", "system_architecture", "completed", details=adapter_details)

        emit_agent_pipeline_event("default", "component_selection", "started", details=adapter_details)
        components = [
            self._component_from_template(
                ref_des="U1",
                part_number="ESP32-WROOM-32D",
                rationale="Main controller with WiFi/Bluetooth, ADC, GPIO, and I2C support for a compact monitor.",
            ),
            self._component_from_template(
                ref_des="SEN1",
                part_number="DHT22",
                rationale="Measures ambient temperature and humidity for the environmental dashboard.",
            ),
            self._component_from_template(
                ref_des="SEN2",
                part_number="BMP280",
                rationale="Adds pressure and secondary temperature readings over the shared I2C bus.",
            ),
            self._component_from_template(
                ref_des="DISP1",
                part_number="SSD1306-I2C",
                rationale="Provides a low-power local display for readings, warning state, and battery status.",
            ),
            self._component_from_template(
                ref_des="BAT1",
                part_number="Battery-LiPo-3.7V",
                rationale="Portable rechargeable power source for a handheld or desk-mounted monitor.",
            ),
            self._component_from_template(
                ref_des="LED1",
                part_number="LED-Red-Generic",
                rationale="Visual threshold or boot-status indicator.",
            ),
            self._component_from_template(
                ref_des="R1",
                part_number="Resistor-220R",
                rationale="Limits current through the status LED.",
            ),
            self._component_from_template(
                ref_des="R2",
                part_number="Resistor-10k",
                rationale="Pull-up resistor for the DHT22 single-wire data line.",
            ),
        ]
        overview.estimated_cost = round(
            sum(component.unit_price * component_instance_count(component) for component in components),
            2,
        )
        emit_agent_pipeline_event("default", "component_selection", "completed", details={"component_count": len(components)})

        emit_agent_pipeline_event("default", "wiring_netlist", "started", details=adapter_details)
        nets = [
            ConnectionNet(
                net_id="NET_GND",
                name="System Ground",
                net_type="Ground",
                voltage=0.0,
                pins=[
                    PinReference(ref_des="U1", pin_id="GND"),
                    PinReference(ref_des="SEN1", pin_id="GND"),
                    PinReference(ref_des="SEN2", pin_id="GND"),
                    PinReference(ref_des="DISP1", pin_id="GND"),
                    PinReference(ref_des="BAT1", pin_id="NEG"),
                    PinReference(ref_des="LED1", pin_id="CATHODE"),
                ],
            ),
            ConnectionNet(
                net_id="NET_BAT_RAW",
                name="Protected Battery Input",
                net_type="Power",
                voltage=3.7,
                pins=[
                    PinReference(ref_des="BAT1", pin_id="POS"),
                    PinReference(ref_des="U1", pin_id="VIN"),
                ],
            ),
            ConnectionNet(
                net_id="NET_3V3",
                name="3.3V Logic Rail",
                net_type="Power",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="3V3"),
                    PinReference(ref_des="SEN1", pin_id="VCC"),
                    PinReference(ref_des="SEN2", pin_id="VCC"),
                    PinReference(ref_des="DISP1", pin_id="VCC"),
                    PinReference(ref_des="R2", pin_id="2"),
                ],
            ),
            ConnectionNet(
                net_id="NET_I2C_SDA",
                name="I2C Serial Data",
                net_type="I2C",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D21"),
                    PinReference(ref_des="SEN2", pin_id="SDA"),
                    PinReference(ref_des="DISP1", pin_id="SDA"),
                ],
            ),
            ConnectionNet(
                net_id="NET_I2C_SCL",
                name="I2C Serial Clock",
                net_type="I2C",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D22"),
                    PinReference(ref_des="SEN2", pin_id="SCL"),
                    PinReference(ref_des="DISP1", pin_id="SCL"),
                ],
            ),
            ConnectionNet(
                net_id="NET_DHT_DATA",
                name="DHT22 Data with Pull-up",
                net_type="Digital",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D27"),
                    PinReference(ref_des="SEN1", pin_id="DATA"),
                    PinReference(ref_des="R2", pin_id="1"),
                ],
            ),
            ConnectionNet(
                net_id="NET_LED_DRIVE",
                name="Status LED Drive",
                net_type="Digital",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D25"),
                    PinReference(ref_des="R1", pin_id="1"),
                ],
            ),
            ConnectionNet(
                net_id="NET_LED_ANODE",
                name="Current Limited LED Anode",
                net_type="Digital",
                voltage=2.0,
                pins=[
                    PinReference(ref_des="R1", pin_id="2"),
                    PinReference(ref_des="LED1", pin_id="ANODE"),
                ],
            ),
        ]
        pin_mappings = [
            PinMappingEntry(mcu_pin="D21", connected_to="BMP280 and OLED SDA", net_name="NET_I2C_SDA"),
            PinMappingEntry(mcu_pin="D22", connected_to="BMP280 and OLED SCL", net_name="NET_I2C_SCL"),
            PinMappingEntry(mcu_pin="D27", connected_to="DHT22 DATA with 10k pull-up", net_name="NET_DHT_DATA"),
            PinMappingEntry(mcu_pin="D25", connected_to="Status LED current-limit resistor", net_name="NET_LED_DRIVE"),
        ]
        emit_agent_pipeline_event("default", "wiring_netlist", "completed", details={"net_count": len(nets)})

        emit_agent_pipeline_event("default", "validation_repair", "started", details=adapter_details)
        validation_issues = validate_circuit(components, nets, requirements)
        validation_issues.append(ValidationIssue(
            severity="INFO",
            category="Power Path",
            description="Battery operation is modeled as a low-voltage LiPo source, but the seed catalog does not include a dedicated charger/boost regulator module.",
            troubleshooting="Add a protected LiPo charger and boost or buck/boost power-path board before building the battery-powered version.",
        ))
        emit_agent_pipeline_event(
            "default",
            "validation_repair",
            "completed",
            details={"issue_count": len(validation_issues)},
        )

        emit_agent_pipeline_event("default", "bom", "started", details=adapter_details)
        emit_agent_pipeline_event("default", "bom", "completed", details={"estimated_cost": overview.estimated_cost})

        assembly = [
            AssemblyStep(
                step_num=1,
                title="Mount controller, sensors, and display",
                description="Install the ESP32, DHT22, BMP280, and OLED on standoffs with sensor vent holes clear of the battery bay.",
                affected_components=["U1", "SEN1", "SEN2", "DISP1"],
            ),
            AssemblyStep(
                step_num=2,
                title="Wire power and common ground",
                description="Tie all GND pins to NET_GND. Route the LiPo positive lead through a protected charger/boost or power-path module before feeding ESP32 VIN.",
                danger_flag=True,
                danger_message="Do not charge or discharge a bare LiPo without a protection and charge-management board.",
                affected_components=["BAT1", "U1"],
            ),
            AssemblyStep(
                step_num=3,
                title="Build sensor and display buses",
                description="Wire BMP280 and OLED to the shared ESP32 I2C bus on D21/D22, then connect DHT22 DATA to D27 with a 10k pull-up to 3.3V.",
                affected_components=["U1", "SEN1", "SEN2", "DISP1", "R2"],
            ),
            AssemblyStep(
                step_num=4,
                title="Add status LED and firmware threshold checks",
                description="Drive LED1 through R1 from ESP32 D25. Blink or latch this indicator when readings cross configured temperature, humidity, or pressure thresholds.",
                affected_components=["U1", "LED1", "R1"],
            ),
        ]

        emit_agent_pipeline_event("default", "mechanical_fabrication", "started", details=adapter_details)
        mechanical = MechanicalNotes(
            enclosure_type="3D Printed handheld sensor enclosure",
            mounting_guidance="Use M2.5 standoffs for the ESP32 and display, isolate the LiPo in a rear pocket, and keep sensor vent slots on the side wall.",
            fabrication_details=[
                "Target envelope: 112mm x 72mm x 34mm.",
                "Use 2mm walls with side ventilation near DHT22 and BMP280.",
                "Front OLED window should sit flush with the display bezel.",
                "Provide a separate battery access hatch and strain relief for the cell leads.",
            ],
            fabrication_cost_estimate_usd=8.0,
            cad_sources=[
                MechanicalSource(
                    name="ESP32 Project Enclosure with OLED SSD1306",
                    source_type="Open STL",
                    url="https://3dgo.app/models/printables/94864",
                    file_formats=["STL"],
                    license="Verify original Printables license before fabrication",
                    estimated_unit_price_usd=0.0,
                    notes="Use as a reference for ESP32/OLED standoff placement and display bezel geometry.",
                )
            ],
            manufacturability_rating="Easy",
        )
        emit_agent_pipeline_event("default", "mechanical_fabrication", "completed", details=adapter_details)

        emit_agent_pipeline_event("default", "assembly", "started", details=adapter_details)
        emit_agent_pipeline_event("default", "assembly", "completed", details={"step_count": len(assembly)})

        validation_summary = build_validation_summary(validation_issues)

        emit_agent_pipeline_event("default", "package_project", "started", details=adapter_details)
        project_ir = HardwareIntermediateRepresentation(
            hardware_ir_version="0.1",
            overview=overview,
            requirements=requirements,
            system_architecture=system_architecture,
            components=components,
            nets=nets,
            buses=extract_buses(nets),
            pin_mappings=pin_mappings,
            assembly=assembly,
            mechanical=mechanical,
            constraints=requirements.physical_constraints,
            power_rails=extract_power_rails(components, nets),
            estimated_current_draw_ma=estimate_current_draw(components),
            fabrication_notes=mechanical.fabrication_details,
            assembly_metadata={
                "generated_at": datetime.utcnow().isoformat(),
                "revision": 1,
                "model_name": self.model_name,
                "fallback_mode": model_validation.fallback_active,
                "requested_model": model_validation.requested_model,
                "actual_model": model_validation.actual_model,
                "llm_provider": model_validation.provider,
                "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                "runtime_provider": self.runtime_config.provider,
                "runtime_model": self.runtime_config.model,
                "provider_overridden": self.runtime_config.provider_overridden,
                "model_overridden": self.runtime_config.model_overridden,
                "pipeline": "runpod parti seed + deterministic catalog repair",
                "parti_seed": seed,
                "parti_seed_error": seed_error,
                "parti_adapter": parti_model.adapter_version,
                "parti_model": selected_model,
                "schematic": {
                    "canvas": {"width": 1180, "height": 760},
                    "placements": {
                        "BAT1": {"x": 80, "y": 90},
                        "U1": {"x": 450, "y": 300},
                        "SEN1": {"x": 250, "y": 520},
                        "SEN2": {"x": 720, "y": 120},
                        "DISP1": {"x": 900, "y": 360},
                        "LED1": {"x": 900, "y": 560},
                        "R1": {"x": 700, "y": 560},
                        "R2": {"x": 250, "y": 350},
                    },
                },
            },
            project_version_history=[{"version": "0.1", "description": "Initial Runpod Parti catalog-repaired design generation"}],
            validation=validation_summary,
            is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues),
        )

        project_ir = build_mechanical_render_data(project_ir)
        self.save_project_to_db(user_prompt, project_ir)
        emit_agent_pipeline_event("default", "package_project", "completed", details=adapter_details)
        return project_ir

    def _generate_staged_project(
        self,
        user_prompt: str,
        *,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
        model_validation: LLMProviderValidation,
    ) -> HardwareIntermediateRepresentation:
        """Run the default workflow as durable, dependency-aware artifact stages."""

        metadata = self._active_generation_metadata
        metadata["project_id"] = canonical_project_uuid(metadata.get("project_id"))
        prior_run = metadata.get("prior_generation_run")
        prior_run = prior_run if isinstance(prior_run, dict) else {}
        external_checkpoint = metadata.get("stage_checkpoint")

        def persist_stage_run(
            stage_run: GenerationStageRun,
            record: Optional[GenerationStageRecord],
        ) -> None:
            if callable(external_checkpoint):
                external_checkpoint({
                    "generation_run_id": stage_run.run_id,
                    "workflow": stage_run.workflow,
                    "retry_stage": stage_run.retry_stage,
                    "status": stage_run.overall_status,
                    "record": record.model_dump(mode="json") if record is not None else None,
                })
            if getattr(self, "persist_project", False):
                snapshot = self._project_ir_from_stage_run(
                    stage_run,
                    model_validation=model_validation,
                )
                if not self.save_project_to_db(user_prompt, snapshot):
                    raise RuntimeError("Generation stage checkpoint could not be persisted.")

        stage_run = GenerationStageRun(
            "default",
            DEFAULT_GENERATION_STAGE_SPECS,
            run_id=prior_run.get("generation_run_id") or metadata.get("frontend_job_id"),
            prior_records=prior_run.get("records") if isinstance(prior_run.get("records"), dict) else None,
            retry_stage=metadata.get("retry_stage"),
            replay_retry=bool(metadata.get("retry_stage_replay")),
            persist=persist_stage_run,
        )
        stage_run.checkpoint()

        overview = stage_run.run(
            "intent_parser",
            lambda: self._generate_default_overview(user_prompt, image_bytes, image_mime_type),
            schema=ProjectOverview,
        )
        requirements = stage_run.run(
            "requirements",
            lambda: self._generate_default_requirements(
                user_prompt,
                overview,
                image_bytes,
                image_mime_type,
            ),
            schema=FunctionalRequirements,
        )
        architecture = stage_run.run(
            "system_architecture",
            lambda: self._generate_default_architecture(
                overview,
                requirements,
                image_bytes,
                image_mime_type,
            ),
            schema=SystemArchitecture,
        )
        selection = stage_run.run(
            "component_selection",
            lambda: self._generate_default_components(
                user_prompt,
                requirements,
                architecture,
                image_bytes,
                image_mime_type,
            ),
            schema=DefaultComponentSelection,
        )
        components = list(selection.components) if selection is not None else []
        wiring = stage_run.run(
            "wiring_netlist",
            lambda: self._generate_default_wiring(components, requirements, image_bytes, image_mime_type),
            schema=DefaultWiringOutput,
        )
        validation = stage_run.run(
            "validation_repair",
            lambda: self._generate_default_validation(
                user_prompt,
                components,
                requirements,
                wiring,
                image_bytes,
                image_mime_type,
            ),
            schema=DefaultValidationOutput,
        )
        stage_run.run(
            "bom",
            lambda: DefaultBomOutput(estimated_cost=round(sum(
                component.unit_price * component_instance_count(component)
                for component in components
            ), 2)),
            schema=DefaultBomOutput,
        )
        mechanical = stage_run.run(
            "mechanical_fabrication",
            lambda: self._generate_default_mechanical(
                user_prompt,
                overview,
                architecture,
                components,
                image_bytes,
                image_mime_type,
            ),
            schema=MechanicalNotes,
        )
        stage_run.run(
            "assembly",
            lambda: self._generate_default_assembly(
                overview,
                architecture,
                components,
                list(validation.nets if validation is not None else wiring.nets),
                mechanical,
                image_bytes,
                image_mime_type,
            ),
            schema=DefaultAssemblyOutput,
        )
        cad_snapshot = self._project_ir_from_stage_run(stage_run, model_validation=model_validation)
        stage_run.run(
            "cad_generation",
            lambda: self._generate_cad_stage(cad_snapshot, metadata),
        )
        stage_run.run(
            "package_project",
            lambda: {
                "project_id": metadata["project_id"],
                "project_readiness": self._default_project_readiness(stage_run),
            },
        )
        return self._project_ir_from_stage_run(stage_run, model_validation=model_validation)

    def _generate_cad_stage(
        self,
        project: HardwareIntermediateRepresentation,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        ensure_native_cad_model(
            project,
            project_id=metadata.get("project_id"),
            required=bool(
                metadata.get(
                    "cad_required",
                    False,
                )
            ),
        )
        return {
            "cad_model": project.cad_model,
            "cad_generation": (project.assembly_metadata or {}).get("cad_generation"),
        }

    def _generate_default_overview(
        self,
        user_prompt: str,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> ProjectOverview:
        overview = self._call_llm_structured(
            f"""
            You are an Intent Parser Agent. Convert the user's idea and visual reference into a structured
            hardware project overview. User idea: {user_prompt!r}. Return ProjectOverview and set estimated
            cost to zero until component selection is complete.
            """,
            ProjectOverview,
            image_bytes,
            image_mime_type,
        )
        if _is_placeholder_text(overview.title) or _is_placeholder_text(overview.description):
            raise LLMProviderOutputError("The selected model returned placeholder project overview fields.")
        return overview

    def _generate_default_requirements(
        self,
        user_prompt: str,
        overview: ProjectOverview,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> FunctionalRequirements:
        return self._call_llm_structured(
            f"""
            You are a Requirements Agent. Extract functional requirements, power needs, physical constraints,
            operating voltage, safety notes, and missing information.
            User idea: {user_prompt!r}
            Project: {overview.model_dump_json()}
            Return FunctionalRequirements.
            """,
            FunctionalRequirements,
            image_bytes,
            image_mime_type,
        )

    def _generate_default_architecture(
        self,
        overview: ProjectOverview,
        requirements: FunctionalRequirements,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> SystemArchitecture:
        architecture = self._call_llm_structured(
            f"""
            You are a System Architecture Agent. Decompose this product into a concise purpose-driven hierarchy.
            Project: {overview.model_dump_json()}
            Requirements: {requirements.model_dump_json()}
            Return SystemArchitecture with a product root and applicable electrical, mechanical, and firmware
            branches. Keep exact parts, nets, connectors, and pins out of this architecture.
            """,
            SystemArchitecture,
            image_bytes,
            image_mime_type,
        )
        return architecture if architecture_tree_is_usable(architecture) else build_default_system_architecture(
            overview,
            requirements,
        )

    def _generate_default_components(
        self,
        user_prompt: str,
        requirements: FunctionalRequirements,
        architecture: SystemArchitecture,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> DefaultComponentSelection:
        templates = get_db_component_templates()
        wrapper = self._call_llm_structured(
            f"""
            You are a Component Selection Agent. Select compatible components from the inventory catalog to
            fulfill the project requirements.
            Requirements: {requirements.model_dump_json()}
            Architecture: {json.dumps(system_context(architecture), indent=2)}
            Catalog: {json.dumps(compact_component_catalog(templates), indent=2)}
            Include a microcontroller and every sensor, actuator, display, passive, and power part the design
            needs. Every physical instance needs a unique ref_des and an exact catalog part_number. Repeated
            parts must be separate instances, never aggregate quantities. Match catalog names, categories,
            prices, and sourcing URLs. Explain each rationale and return empty pin lists; catalog pins are
            hydrated deterministically. Return DefaultComponentSelection.
            """,
            DefaultComponentSelection,
            image_bytes,
            image_mime_type,
        )
        components = hydrate_catalog_components(wrapper.components, templates)
        components, reconciled = reconcile_explicit_catalog_components(
            str(self._active_generation_metadata.get("project_prompt") or user_prompt),
            components,
            templates,
        )
        return DefaultComponentSelection(
            components=expand_component_instances(components),
            reconciled_component_parts=reconciled,
        )

    def _generate_default_wiring(
        self,
        components: List[ComponentInstance],
        requirements: FunctionalRequirements,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> DefaultWiringOutput:
        endpoint_catalog = endpoint_catalog_prompt(build_endpoint_catalog(components))
        wiring_intent: WiringIntent = self._call_llm_structured(
            f"""
            You are a Wiring Intent Agent. Describe the connections needed for the selected components.
            Use only exact endpoint IDs from this catalog; do not generate nested ref_des/pin_id objects
            or canonical net IDs.
            Endpoint catalog: {json.dumps(endpoint_catalog, indent=2)}
            Requirements: {requirements.model_dump_json()}
            Establish compatible ground and power rails, connect required communication and control signals,
            and do not mix incompatible logic voltages. Every non-power/non-ground endpoint may appear in only
            one net. Passive parts bridge nets using one passive endpoint per net. Do not leave critical power
            or ground endpoints unconnected. Return WiringIntent.
            """,
            WiringIntent,
            image_bytes,
            image_mime_type,
        )
        compilation = compile_wiring_intent(components, wiring_intent)
        return DefaultWiringOutput(
            nets=compilation.nets,
            pin_mappings=derive_pin_mappings(components, compilation.nets),
            intent_issues=compilation.issues,
        )

    def _generate_default_validation(
        self,
        user_prompt: str,
        components: List[ComponentInstance],
        requirements: FunctionalRequirements,
        wiring: DefaultWiringOutput,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> DefaultValidationOutput:
        nets = list(wiring.nets)
        pin_mappings = list(wiring.pin_mappings)
        issues = [
            *wiring.intent_issues,
            *validate_circuit(components, nets, requirements, prompt=user_prompt),
        ]
        if issues:
            emit_agent_pipeline_event(
                "default",
                "validation_repair",
                "diagnostics",
                details={
                    "failure_categories": sorted({
                        wiring_failure_category(issue) for issue in issues
                    }),
                },
            )
        is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in issues)
        if not is_valid:
            corrected = self._call_llm_structured(
                f"""
                You are a Wiring Intent Auto-Correction Agent. Correct only the rejected or invalid nets.
                Return replacement intents for existing canonical nets using replace_net_id. Omit valid nets.
                Endpoint catalog: {json.dumps(endpoint_catalog_prompt(build_endpoint_catalog(components)), indent=2)}
                Previous nets: {json.dumps([item.model_dump(mode='json') for item in nets], indent=2)}
                Validation issues: {json.dumps([item.model_dump(mode='json') for item in issues], indent=2)}
                Use endpoint_ids from the catalog only. Keep valid existing nets unchanged. Return WiringIntent.
                """,
                WiringIntent,
                image_bytes,
                image_mime_type,
            )
            repaired = compile_wiring_intent(components, corrected, existing_nets=nets)
            nets = repaired.all_nets
            pin_mappings = derive_pin_mappings(components, nets)
            issues = [
                *repaired.issues,
                *validate_circuit(components, nets, requirements, prompt=user_prompt),
            ]
            is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in issues)
        return DefaultValidationOutput(
            nets=nets,
            pin_mappings=pin_mappings,
            issues=issues,
            is_valid=is_valid,
        )

    def _generate_default_mechanical(
        self,
        user_prompt: str,
        overview: ProjectOverview,
        architecture: SystemArchitecture,
        components: List[ComponentInstance],
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> MechanicalNotes:
        return self._call_llm_structured(
            f"""
            You are a Mechanical/Fabrication and CAD Sourcing Agent. Produce enclosure, mounting, material,
            fabrication, CAD-source, and spatial-layout guidance. Respect the requested form rather than
            defaulting to a rectangular box; exposed and open-frame designs must not invent a closed case.
            User request: {user_prompt}
            Project: {overview.model_dump_json()}
            Mechanical architecture: {json.dumps(system_context(architecture, 'mechanical'), indent=2)}
            Components: {json.dumps(compact_component_context(components), indent=2)}
            Populate physical_form, enclosure_type, fabrication cost, known CAD sources, render dimensions,
            component placements, and spatial relationships. Do not invent source URLs. Return MechanicalNotes.
            """,
            MechanicalNotes,
            image_bytes,
            image_mime_type,
        )

    def _generate_default_assembly(
        self,
        overview: ProjectOverview,
        architecture: SystemArchitecture,
        components: List[ComponentInstance],
        nets: List[ConnectionNet],
        mechanical: MechanicalNotes,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> DefaultAssemblyOutput:
        return self._call_llm_structured(
            f"""
            You are an Assembly Instruction Agent. Produce concrete, sequential physical and electronic build
            instructions, including warnings for batteries, motors, relays, soldering, heat, polarity, and
            moving parts where applicable.
            Project: {overview.model_dump_json()}
            Architecture: {json.dumps(system_context(architecture), indent=2)}
            Components: {json.dumps(compact_component_context(components), indent=2)}
            Nets: {json.dumps(compact_net_context(nets), indent=2)}
            Mechanical guide: {mechanical.model_dump_json()}
            Return DefaultAssemblyOutput.
            """,
            DefaultAssemblyOutput,
            image_bytes,
            image_mime_type,
        )

    def _project_ir_from_stage_run(
        self,
        stage_run: GenerationStageRun,
        *,
        model_validation: LLMProviderValidation,
    ) -> HardwareIntermediateRepresentation:
        overview = stage_run.output("intent_parser", ProjectOverview)
        requirements = stage_run.output("requirements", FunctionalRequirements)
        architecture = stage_run.output("system_architecture", SystemArchitecture)
        selection = stage_run.output("component_selection", DefaultComponentSelection)
        wiring = stage_run.output("wiring_netlist", DefaultWiringOutput)
        validation = stage_run.output("validation_repair", DefaultValidationOutput)
        bom = stage_run.output("bom", DefaultBomOutput)
        mechanical = stage_run.output("mechanical_fabrication", MechanicalNotes)
        assembly_output = stage_run.output("assembly", DefaultAssemblyOutput)
        cad_output = stage_run.output("cad_generation") or {}
        components = list(selection.components) if selection is not None else []
        nets = list(validation.nets if validation is not None else (wiring.nets if wiring is not None else []))
        pin_mappings = derive_pin_mappings(components, nets)
        if overview is not None and bom is not None:
            overview.estimated_cost = bom.estimated_cost
        constraints = []
        if requirements is not None:
            constraints = [
                *requirements.physical_constraints,
                f"Operating Voltage: {requirements.operating_voltage}V",
            ]
        public_metadata = {
            key: value
            for key, value in self._active_generation_metadata.items()
            if key not in {
                "owner_user_id",
                "project_prompt",
                "prior_generation_run",
                "stage_checkpoint",
            }
        }
        failures = [
            record.model_dump(mode="json", exclude={"output"})
            for record in stage_run.records.values()
            if record.status.value in {"failed", "blocked"}
        ]
        generation_status = self._default_generation_status(stage_run)
        issues = list(validation.issues) if validation is not None else []
        cad_record = stage_run.records.get("cad_generation")
        if (
            self._active_generation_metadata.get("cad_required") is True
            and cad_record is not None
            and cad_record.status.value == "failed"
        ):
            generation_status = "failed"
            issues.append(ValidationIssue(
                severity="CRITICAL",
                category="CAD Generation Failure",
                description=str((cad_record.error or {}).get("message") or "Native CAD generation failed."),
                troubleshooting="Install the OpenCAD runtime/adapter or retry native CAD generation.",
            ))
        root_failure = next((
            record
            for record in stage_run.records.values()
            if record.status.value == "failed"
        ), None)
        if generation_status == "failed" and root_failure is not None:
            issues.append(ValidationIssue(
                severity="CRITICAL",
                category="Generation Failure",
                description=str((root_failure.error or {}).get("message") or "Generation failed."),
                troubleshooting="Retry the failed generation stage or review the provider logs.",
            ))
        project_ir = HardwareIntermediateRepresentation(
            overview=overview,
            requirements=requirements,
            system_architecture=architecture,
            components=components,
            nets=nets,
            buses=extract_buses(nets),
            pin_mappings=pin_mappings,
            assembly=list(assembly_output.steps) if assembly_output is not None else [],
            mechanical=mechanical,
            cad_model=cad_output.get("cad_model") if isinstance(cad_output, dict) else None,
            constraints=constraints,
            power_rails=extract_power_rails(components, nets),
            estimated_current_draw_ma=estimate_current_draw(components),
            fabrication_notes=mechanical.fabrication_details if mechanical is not None else [],
            assembly_metadata={
                **public_metadata,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "revision": 1,
                "model_name": getattr(self, "model_name", None),
                "fallback_mode": getattr(model_validation, "fallback_active", False),
                "requested_model": getattr(model_validation, "requested_model", None),
                "actual_model": getattr(model_validation, "actual_model", None),
                "llm_provider": getattr(model_validation, "provider", None),
                "runtime_provider": self.runtime_config.provider,
                "runtime_model": self.runtime_config.model,
                "workflow": "default",
                "generation_status": generation_status,
                "status": "failed" if generation_status == "failed" else generation_status,
                "generation_error": (
                    {
                        "type": str((root_failure.error or {}).get("exception_type") or "GenerationStageError"),
                        "message": str((root_failure.error or {}).get("message") or "Generation failed."),
                    }
                    if generation_status == "failed" and root_failure is not None
                    else None
                ),
                "project_readiness": self._default_project_readiness(stage_run),
                "generation_run": stage_run.snapshot(include_outputs=True),
                "generation_stage_failures": failures,
                "component_reconciliation_added": (
                    selection.reconciled_component_parts if selection is not None else []
                ),
                "cad_generation": (
                    cad_output.get("cad_generation") if isinstance(cad_output, dict) else None
                ),
            },
            project_version_history=[{
                "version": "0.2",
                "description": "Dependency-aware staged default generation",
            }],
            validation=build_validation_summary(issues),
            is_valid=bool(
                validation is not None
                and validation.is_valid
                and not any(issue.severity.upper() == "CRITICAL" for issue in issues)
            ),
        )
        return build_mechanical_render_data(project_ir) if mechanical is not None else project_ir

    @staticmethod
    def _default_generation_status(stage_run: GenerationStageRun) -> str:
        if stage_run.records["system_architecture"].status.value in {"failed", "blocked"}:
            return "failed"
        return stage_run.overall_status

    @staticmethod
    def _default_project_readiness(stage_run: GenerationStageRun) -> str:
        statuses = {stage_id: record.status.value for stage_id, record in stage_run.records.items()}
        if statuses.get("system_architecture") != "succeeded":
            return "draft"
        if statuses.get("cad_generation") in {"failed", "blocked"}:
            return "partial"
        non_package = [status for stage_id, status in statuses.items() if stage_id != "package_project"]
        if (
            non_package
            and all(status == "succeeded" for status in non_package)
            and statuses.get("package_project") in {"running", "succeeded"}
        ):
            return "complete"
        if all(statuses.get(stage_id) == "succeeded" for stage_id in (
            "intent_parser",
            "requirements",
            "system_architecture",
            "component_selection",
            "wiring_netlist",
            "validation_repair",
        )):
            return "core_ready"
        if any(status in {"failed", "blocked"} for status in statuses.values()):
            return "partial"
        return "draft"

    def save_project_to_db(self, prompt: str, ir: HardwareIntermediateRepresentation) -> str:
        """Saves a successfully generated HardwareIntermediateRepresentation to the configured database."""
        ensure_agent_pipeline_active()
        generation_metadata = self._active_generation_metadata or {}
        project_id = canonical_project_uuid(
            (ir.assembly_metadata or {}).get("project_id") or generation_metadata.get("project_id")
        )
        public_generation_metadata = {
            key: value
            for key, value in generation_metadata.items()
            if key not in {"owner_user_id", "project_prompt", "prior_generation_run", "stage_checkpoint"}
        }
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            **public_generation_metadata,
            "project_id": project_id,
        }
        if not self.persist_project:
            return project_id
        try:
            owner_user_id = generation_metadata.get("owner_user_id")
            if owner_user_id:
                persist_chat_project_revision(
                    project_id,
                    owner_user_id,
                    ir,
                    source_job_id=str(generation_metadata.get("frontend_job_id") or f"generation-{uuid.uuid4().hex}"),
                    prompt=str(generation_metadata.get("project_prompt") or prompt),
                    chat_id=generation_metadata.get("chat_id"),
                )
            else:
                persist_legacy_project_projection(
                    project_id=project_id,
                    title=ir.overview.title if ir.overview else "Untitled Forma Project",
                    prompt=str(generation_metadata.get("project_prompt") or prompt),
                    hardware_ir=ir.model_dump(),
                    created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    chat_id=generation_metadata.get("chat_id"),
                    owner_user_id=None,
                    visibility="public",
                )
            try:
                ensure_agent_pipeline_active()
            except PipelineCancelledError:
                owner_user_id = generation_metadata.get("owner_user_id")
                if owner_user_id:
                    update_project_deletion_state(
                        project_id,
                        owner_user_id=owner_user_id,
                        allowed_statuses=["active"],
                        updates={
                            "status": "deletion_pending",
                            "deleted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "deletion_requested_by": owner_user_id,
                        },
                    )
                raise
            logger.info(f"Project saved to database with ID: {project_id}")
            return project_id
        except PipelineCancelledError:
            raise
        except Exception as e:
            logger.error(f"Failed to save project to database: {e}")
            return ""

    def _generate_simulated_project(self, prompt: str, has_image: bool = False) -> HardwareIntermediateRepresentation:
        """High-fidelity, deterministic simulated generator used as fallback when live LLM generation is unavailable."""
        logger.info(f"Generating simulated project package for: '{prompt}'")
        
        prompt_lower = prompt.lower()
        if "test tube" in prompt_lower or "test-tube" in prompt_lower:
            return self._load_simulated_test_tube_project(prompt)
        if has_image or "mp3" in prompt_lower or "music" in prompt_lower or "audio" in prompt_lower or "player" in prompt_lower or "pocket" in prompt_lower:
            return self._load_simulated_mp3_player_project(prompt)
        elif "water" in prompt_lower or "plant" in prompt_lower or "soil" in prompt_lower or "garden" in prompt_lower:
            return self._load_simulated_watering_project(prompt)
        elif "thermostat" in prompt_lower or "temperature" in prompt_lower or "weather" in prompt_lower:
            return self._load_simulated_thermostat_project(prompt)
        else:
            return self._load_simulated_smart_lock_project(prompt)

    def _load_simulated_test_tube_project(self, prompt: str) -> HardwareIntermediateRepresentation:
        """Provide a safe low-voltage test-tube monitor fixture for CLI smoke tests."""
        overview = ProjectOverview(
            title="Test Tube Monitor",
            description=f"A compact low-voltage monitor for a single test tube, compiled for: '{prompt}'",
            difficulty="Beginner",
            estimated_cost=18.00,
            category="Educational Instrumentation",
        )
        requirements = FunctionalRequirements(
            requirements=[
                "Monitor temperature near a single test tube.",
                "Display the latest reading on a small OLED screen.",
                "Run from a safe 5V USB supply.",
            ],
            power_needs="5V USB supply with 3.3V logic regulation.",
            operating_voltage=3.3,
            physical_constraints=["Single 30mm diameter test-tube cavity.", "100mm tube height."],
            safety_notes=["Use a non-contact sensor or sealed probe; do not place electronics in the sample."],
            missing_info=[],
        )
        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="ESP32-WROOM-32D",
                name="ESP32 Controller",
                category="Microcontroller",
                rationale="Reads the sensor and drives the display.",
            ),
            ComponentInstance(
                ref_des="SEN1",
                part_number="TMP117",
                name="Temperature Sensor",
                category="Sensor",
                rationale="Measures the test-tube environment without placing electronics in the sample.",
            ),
            ComponentInstance(
                ref_des="DISP1",
                part_number="OLED-096-I2C",
                name="0.96 inch OLED Display",
                category="Display",
                rationale="Shows the current temperature reading.",
            ),
            ComponentInstance(
                ref_des="PWR1",
                part_number="USB-C-5V",
                name="USB-C Power Input",
                category="Power",
                rationale="Provides a low-voltage supply.",
            ),
        ]
        mechanical = MechanicalNotes(
            physical_form="Cylindrical test-tube sleeve",
            enclosure_type="3D Printed test-tube holder",
            mounting_guidance="Use a separate dry electronics compartment beside the tube cavity.",
            fabrication_details=["30mm inside diameter", "100mm usable height", "1.5mm nominal wall thickness"],
            fabrication_cost_estimate_usd=4.00,
            manufacturability_rating="Easy",
            render_dimensions=MechanicalVector3(x_mm=30, y_mm=30, z_mm=100),
        )
        validation_issues = validate_circuit(components, [], requirements)
        project_ir = HardwareIntermediateRepresentation(
            hardware_ir_version="0.1",
            overview=overview,
            requirements=requirements,
            components=components,
            nets=[],
            assembly=[],
            mechanical=mechanical,
            constraints=requirements.physical_constraints,
            estimated_current_draw_ma=120.0,
            fabrication_notes=mechanical.fabrication_details,
            assembly_metadata={
                "status": "active",
                "pipeline": "deterministic simulation",
            },
            project_version_history=[{"version": "0.1", "description": "Initial test-tube monitor fixture"}],
            validation=build_validation_summary(validation_issues),
            is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues),
        )
        project_ir = build_mechanical_render_data(project_ir)
        self.save_project_to_db(prompt, project_ir)
        return project_ir

    def _generate_failed_project(self, error: Exception) -> HardwareIntermediateRepresentation:
        """Return an empty, invalid project that preserves a pipeline failure for callers."""
        error_type = error.__class__.__name__
        error_message = str(error).strip() or error_type
        issue = ValidationIssue(
            severity="CRITICAL",
            category="Generation Failure",
            description=error_message,
            troubleshooting="Retry generation or review the configured provider and model logs.",
        )
        return HardwareIntermediateRepresentation(
            assembly_metadata={
                "status": "failed",
                "generation_error": {
                    "type": error_type,
                    "message": error_message,
                },
                "fallback_mode": False,
            },
            validation=ValidationSummary(critical=[issue]),
            is_valid=False,
        )

    def _load_simulated_mp3_player_project(self, prompt: str) -> HardwareIntermediateRepresentation:
        """Reference-style Forma project used for prompt+image MP3 player examples."""
        def pin(pin_id: str, name: str, pin_type: str, voltage: Optional[float] = None) -> PinDefinition:
            return PinDefinition(pin_id=pin_id, name=name, pin_type=pin_type, voltage=voltage, description=name)

        overview = ProjectOverview(
            title="Pocket MP3 Player",
            description=f"A slim, portable MP3 player powered by an ESP32 audio stack and compiled for: '{prompt}'",
            difficulty="Intermediate",
            estimated_cost=45.05,
            category="Portable Audio"
        )
        requirements = FunctionalRequirements(
            requirements=[
                "Play local MP3 files from expandable microSD storage.",
                "Show track, battery, and playback state on a small color display.",
                "Provide physical play/pause, next, and previous buttons.",
                "Charge a compact Li-Po battery through USB-C and expose a 3.5mm audio output."
            ],
            power_needs="3.7V Li-Po battery with USB-C charging and protected 3.3V logic regulation.",
            operating_voltage=3.3,
            physical_constraints=[
                "Slim and portable",
                "Pocket-sized",
                "Rectangular body",
                "Rounded edges",
                "Color display",
                "Physical buttons",
                "USB-C connectivity",
                "Bluetooth integration",
                "Minimalist design",
                "Lightweight",
                "Ergonomic"
            ],
            safety_notes=[
                "Use a protected Li-Po charging module and verify battery polarity before power-up.",
                "Keep audio amplifier output isolated from logic pins and enclosure fasteners."
            ],
            missing_info=[]
        )

        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="ESP32-WROOM-32E",
                name="ESP32-WROOM-32E Module",
                category="Microcontroller",
                quantity=1,
                unit_price=5.50,
                rationale="Main Controller. Provides Wi-Fi, Bluetooth, and enough GPIO for display, storage, buttons, and audio control.",
                pins=[
                    pin("3V3", "3.3V Power", "Power", 3.3),
                    pin("GND", "Ground", "Ground", 0.0),
                    pin("GPIO21", "I2C SDA", "I2C", 3.3),
                    pin("GPIO22", "I2C SCL", "I2C", 3.3),
                    pin("GPIO18", "SPI SCK", "SPI", 3.3),
                    pin("GPIO23", "SPI MOSI", "SPI", 3.3),
                    pin("GPIO19", "SPI MISO", "SPI", 3.3),
                    pin("GPIO5", "SD CS", "Digital", 3.3),
                    pin("GPIO25", "Audio BCLK", "Digital", 3.3),
                    pin("GPIO26", "Audio LRCLK", "Digital", 3.3),
                    pin("GPIO27", "Audio DIN", "Digital", 3.3),
                    pin("GPIO32", "Play/Pause", "Digital", 3.3),
                    pin("GPIO33", "Next", "Digital", 3.3),
                    pin("GPIO34", "Previous", "Digital", 3.3)
                ]
            ),
            ComponentInstance(
                ref_des="SPK1",
                part_number="SPK-8OHM-1W",
                name="8 Ohm 1W Mini Speaker",
                category="Actuator",
                quantity=1,
                unit_price=2.00,
                rationale="Small Internal Speaker. Provides onboard audio playback without headphones.",
                pins=[pin("SPK+", "Speaker Positive", "Passive"), pin("SPK-", "Speaker Negative", "Passive")]
            ),
            ComponentInstance(
                ref_des="BAT1",
                part_number="LiPo-3V7-500mAh",
                name="3.7V 500mAh Li-Po Battery",
                category="Power",
                quantity=1,
                unit_price=8.00,
                rationale="Rechargeable Power Source. Small battery for a pocket-sized portable design.",
                pins=[pin("POS", "Battery Positive", "Power", 3.7), pin("NEG", "Battery Negative", "Ground", 0.0)]
            ),
            ComponentInstance(
                ref_des="PWR1",
                part_number="TP4056-USB-C",
                name="TP4056 USB-C Li-Ion Charger Module",
                category="Power",
                quantity=1,
                unit_price=3.00,
                rationale="Li-Po Charging & Power Management. Charges the battery and provides protection.",
                pins=[
                    pin("USB5V", "USB-C 5V Input", "Power", 5.0),
                    pin("BAT+", "Battery Charge Positive", "Power", 3.7),
                    pin("BAT-", "Battery Charge Negative", "Ground", 0.0),
                    pin("OUT+", "Protected Positive Output", "Power", 3.3),
                    pin("OUT-", "Protected Ground Output", "Ground", 0.0)
                ]
            ),
            ComponentInstance(ref_des="BTN1", part_number="TACT-6MM", name="Play/Pause Button", category="Passives", quantity=1, unit_price=0.35, rationale="Primary transport control.", pins=[pin("A", "Switch A", "Passive"), pin("B", "Switch B", "Passive")]),
            ComponentInstance(ref_des="BTN2", part_number="TACT-6MM", name="Next Track Button", category="Passives", quantity=1, unit_price=0.35, rationale="Advances to the next track.", pins=[pin("A", "Switch A", "Passive"), pin("B", "Switch B", "Passive")]),
            ComponentInstance(ref_des="BTN3", part_number="TACT-6MM", name="Previous Track Button", category="Passives", quantity=1, unit_price=0.35, rationale="Returns to the previous track.", pins=[pin("A", "Switch A", "Passive"), pin("B", "Switch B", "Passive")]),
            ComponentInstance(
                ref_des="SD1",
                part_number="MICROSD-SPI",
                name="Expandable Storage Module",
                category="Passives",
                quantity=1,
                unit_price=2.50,
                rationale="Stores MP3 files on removable media.",
                pins=[
                    pin("VCC", "3.3V Power", "Power", 3.3),
                    pin("GND", "Ground", "Ground", 0.0),
                    pin("CS", "Chip Select", "Digital", 3.3),
                    pin("SCK", "Clock", "SPI", 3.3),
                    pin("MOSI", "Data In", "SPI", 3.3),
                    pin("MISO", "Data Out", "SPI", 3.3)
                ]
            ),
            ComponentInstance(ref_des="J1", part_number="TRS-3.5MM", name="3.5mm Headphone Output", category="Passives", quantity=1, unit_price=1.20, rationale="External analog audio output.", pins=[pin("L", "Left Audio", "Passive"), pin("R", "Right Audio", "Passive"), pin("GND", "Audio Ground", "Ground", 0.0)]),
            ComponentInstance(
                ref_des="DAC1",
                part_number="I2S-DAC-AMP",
                name="Audio DAC with Amplifier",
                category="Actuator",
                quantity=1,
                unit_price=4.20,
                rationale="Decodes I2S digital audio and drives speaker/headphone output.",
                pins=[
                    pin("VCC", "3.3V Power", "Power", 3.3),
                    pin("GND", "Ground", "Ground", 0.0),
                    pin("BCLK", "Bit Clock", "Digital", 3.3),
                    pin("LRCLK", "Word Select", "Digital", 3.3),
                    pin("DIN", "Audio Data", "Digital", 3.3),
                    pin("SPK+", "Speaker Positive", "Passive"),
                    pin("SPK-", "Speaker Negative", "Passive"),
                    pin("OUTL", "Headphone Left", "Passive"),
                    pin("OUTR", "Headphone Right", "Passive")
                ]
            ),
            ComponentInstance(ref_des="USB1", part_number="USB-C-16P", name="USB-C Data & Charging Port", category="Power", quantity=1, unit_price=1.50, rationale="USB-C connectivity for charging and file transfer.", pins=[pin("VBUS", "USB 5V", "Power", 5.0), pin("GND", "USB Ground", "Ground", 0.0), pin("D+", "USB Data Plus", "Digital", 3.3), pin("D-", "USB Data Minus", "Digital", 3.3)]),
            ComponentInstance(
                ref_des="DISP1",
                part_number="OLED-COLOR-1.3",
                name="Track Information Display",
                category="Display",
                quantity=1,
                unit_price=9.00,
                rationale="Color display for waveform, battery, and track metadata.",
                pins=[pin("VCC", "3.3V Power", "Power", 3.3), pin("GND", "Ground", "Ground", 0.0), pin("SDA", "I2C SDA", "I2C", 3.3), pin("SCL", "I2C SCL", "I2C", 3.3)]
            ),
            ComponentInstance(ref_des="MCAP1", part_number="CAP-PLAY", name="Play/Pause Button Cap", category="Mechanical", quantity=1, unit_price=0.30, rationale="Tactile exterior control cap.", pins=[]),
            ComponentInstance(ref_des="MCAP2", part_number="CAP-NEXT", name="Next Track Button Cap", category="Mechanical", quantity=1, unit_price=0.30, rationale="Tactile exterior control cap.", pins=[]),
            ComponentInstance(ref_des="MCAP3", part_number="CAP-PREV", name="Previous Track Button Cap", category="Mechanical", quantity=1, unit_price=0.30, rationale="Tactile exterior control cap.", pins=[]),
            ComponentInstance(ref_des="SCR1", part_number="M2-SCREW", name="M2 Enclosure Screws", category="Mechanical", quantity=4, unit_price=0.20, rationale="Secures shell, bezel, and cover.", pins=[]),
            ComponentInstance(ref_des="INS1", part_number="M2-INSERT", name="M2 Heat-Set Inserts", category="Mechanical", quantity=4, unit_price=0.225, rationale="Reusable threaded mounting points.", pins=[]),
            ComponentInstance(ref_des="MECH1", part_number="MP3-SHELL", name="Main Enclosure Shell", category="3D Print", quantity=1, unit_price=2.10, rationale="Primary rounded pocket enclosure.", pins=[]),
            ComponentInstance(ref_des="MECH2", part_number="MP3-FRONT", name="Front Bezel", category="3D Print", quantity=1, unit_price=0.60, rationale="Faceplate for display and controls.", pins=[]),
            ComponentInstance(ref_des="MECH3", part_number="MP3-BACK", name="Back Cover with Speaker Vents", category="3D Print", quantity=1, unit_price=1.00, rationale="Rear cover with acoustic grille.", pins=[]),
            ComponentInstance(ref_des="MECH4", part_number="OLED-MOUNT", name="OLED Display Bezel Mount", category="3D Print", quantity=1, unit_price=0.40, rationale="Aligns display behind window.", pins=[]),
            ComponentInstance(ref_des="MECH5", part_number="ESP32-MOUNT", name="ESP32 Main Controller Mount", category="3D Print", quantity=1, unit_price=0.40, rationale="Internal board mounting platform.", pins=[]),
        ]

        nets = [
            ConnectionNet(net_id="NET_GND", name="System Ground", net_type="Ground", voltage=0.0, pins=[
                PinReference(ref_des="U1", pin_id="GND"), PinReference(ref_des="BAT1", pin_id="NEG"),
                PinReference(ref_des="PWR1", pin_id="BAT-"), PinReference(ref_des="PWR1", pin_id="OUT-"),
                PinReference(ref_des="SD1", pin_id="GND"), PinReference(ref_des="DAC1", pin_id="GND"),
                PinReference(ref_des="USB1", pin_id="GND"), PinReference(ref_des="DISP1", pin_id="GND"),
                PinReference(ref_des="J1", pin_id="GND")
            ]),
            ConnectionNet(net_id="NET_BAT", name="Li-Po Battery Bus", net_type="Power", voltage=3.7, pins=[
                PinReference(ref_des="BAT1", pin_id="POS"), PinReference(ref_des="PWR1", pin_id="BAT+")
            ]),
            ConnectionNet(net_id="NET_USB_5V", name="USB-C Charge Input", net_type="Power", voltage=5.0, pins=[
                PinReference(ref_des="USB1", pin_id="VBUS"), PinReference(ref_des="PWR1", pin_id="USB5V")
            ]),
            ConnectionNet(net_id="NET_3V3", name="Protected 3.3V Logic Rail", net_type="Power", voltage=3.3, pins=[
                PinReference(ref_des="PWR1", pin_id="OUT+"), PinReference(ref_des="U1", pin_id="3V3"),
                PinReference(ref_des="SD1", pin_id="VCC"), PinReference(ref_des="DAC1", pin_id="VCC"),
                PinReference(ref_des="DISP1", pin_id="VCC")
            ]),
            ConnectionNet(net_id="NET_I2C_SDA", name="Display SDA", net_type="I2C", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO21"), PinReference(ref_des="DISP1", pin_id="SDA")
            ]),
            ConnectionNet(net_id="NET_I2C_SCL", name="Display SCL", net_type="I2C", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO22"), PinReference(ref_des="DISP1", pin_id="SCL")
            ]),
            ConnectionNet(net_id="NET_SPI_SCK", name="microSD SPI Clock", net_type="SPI", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO18"), PinReference(ref_des="SD1", pin_id="SCK")
            ]),
            ConnectionNet(net_id="NET_SPI_MOSI", name="microSD SPI MOSI", net_type="SPI", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO23"), PinReference(ref_des="SD1", pin_id="MOSI")
            ]),
            ConnectionNet(net_id="NET_SPI_MISO", name="microSD SPI MISO", net_type="SPI", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO19"), PinReference(ref_des="SD1", pin_id="MISO")
            ]),
            ConnectionNet(net_id="NET_SD_CS", name="microSD Chip Select", net_type="Digital", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO5"), PinReference(ref_des="SD1", pin_id="CS")
            ]),
            ConnectionNet(net_id="NET_AUDIO_BCLK", name="I2S Bit Clock", net_type="Digital", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO25"), PinReference(ref_des="DAC1", pin_id="BCLK")
            ]),
            ConnectionNet(net_id="NET_AUDIO_LRCLK", name="I2S Word Select", net_type="Digital", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO26"), PinReference(ref_des="DAC1", pin_id="LRCLK")
            ]),
            ConnectionNet(net_id="NET_AUDIO_DIN", name="I2S Audio Data", net_type="Digital", voltage=3.3, pins=[
                PinReference(ref_des="U1", pin_id="GPIO27"), PinReference(ref_des="DAC1", pin_id="DIN")
            ]),
            ConnectionNet(net_id="NET_SPK", name="Amplified Speaker Output", net_type="Analog", voltage=None, pins=[
                PinReference(ref_des="DAC1", pin_id="SPK+"), PinReference(ref_des="SPK1", pin_id="SPK+"),
                PinReference(ref_des="DAC1", pin_id="SPK-"), PinReference(ref_des="SPK1", pin_id="SPK-")
            ]),
        ]

        pin_mappings = [
            PinMappingEntry(mcu_pin="GPIO21/GPIO22", connected_to="Color display I2C bus", net_name="NET_I2C_SDA/SCL"),
            PinMappingEntry(mcu_pin="GPIO18/GPIO23/GPIO19/GPIO5", connected_to="microSD SPI module", net_name="NET_SPI"),
            PinMappingEntry(mcu_pin="GPIO25/GPIO26/GPIO27", connected_to="I2S DAC amplifier", net_name="NET_AUDIO_I2S"),
            PinMappingEntry(mcu_pin="GPIO32/GPIO33/GPIO34", connected_to="Playback control buttons", net_name="NET_BUTTONS")
        ]
        assembly = [
            AssemblyStep(step_num=1, title="Print enclosure set", description="Print the main shell, front bezel, back cover, OLED mount, and ESP32 mount in matte PLA or PETG with the button cap tolerances preserved.", affected_components=["MECH1", "MECH2", "MECH3", "MECH4", "MECH5"]),
            AssemblyStep(step_num=2, title="Build power subsystem", description="Connect the Li-Po cell to the TP4056 charger module and route protected 3.3V output to the ESP32, display, DAC, and microSD module.", danger_flag=True, danger_message="Verify Li-Po polarity before plugging in USB-C power.", affected_components=["BAT1", "PWR1", "USB1"]),
            AssemblyStep(step_num=3, title="Wire display, storage, and audio", description="Wire the display over I2C, the microSD module over SPI, and the DAC amplifier over I2S. Keep speaker leads away from the antenna side of the ESP32.", affected_components=["DISP1", "SD1", "DAC1", "SPK1", "U1"]),
            AssemblyStep(step_num=4, title="Install buttons and close enclosure", description="Mount the tactile switches behind the front controls, press-fit the printed caps, install heat-set inserts, and close the enclosure with M2 screws.", affected_components=["BTN1", "BTN2", "BTN3", "MCAP1", "MCAP2", "MCAP3", "SCR1", "INS1"])
        ]
        mechanical = MechanicalNotes(
            enclosure_type="3D Printed Rounded Pocket Enclosure",
            mounting_guidance="Use internal bosses for the ESP32, DAC, charger, microSD module, and OLED bezel. Route USB-C and headphone cutouts to the short edges.",
            fabrication_details=[
                "Overall body target: 100mm x 54mm x 21mm.",
                "2.0mm wall thickness with 0.4mm display lip.",
                "Heat-set M2 inserts for serviceable back cover.",
                "Speaker vent slots on rear or side wall."
            ],
            fabrication_cost_estimate_usd=6.00,
            cad_sources=[
                MechanicalSource(
                    name="Adafruit Walkmp3rson CAD files",
                    source_type="Reference CAD",
                    url="https://learn.adafruit.com/walkmp3rson-personal-mp3-tape-player/cad-files",
                    file_formats=["STL", "STEP", "Fusion 360"],
                    license="Adafruit Learn reference design; verify reuse terms on source page",
                    estimated_unit_price_usd=0.00,
                    notes="Use as a portable audio enclosure reference and adapt cutouts for ESP32, OLED, USB-C, buttons, and speaker."
                ),
                MechanicalSource(
                    name="ESP32 Project Enclosure with OLED SSD1306",
                    source_type="Open STL",
                    url="https://3dgo.app/models/printables/94864",
                    file_formats=["STL"],
                    license="Creative Commons attribution-sharealike listing mirror; verify original Printables license",
                    estimated_unit_price_usd=0.00,
                    notes="Good reference for ESP32/OLED internal mounting and display bezel geometry."
                )
            ],
            manufacturability_rating="Moderate"
        )
        validation_issues = validate_circuit(components, nets, requirements)
        validation_summary = build_validation_summary(validation_issues)
        project_ir = HardwareIntermediateRepresentation(
            hardware_ir_version="0.1",
            overview=overview,
            requirements=requirements,
            components=components,
            nets=nets,
            buses=extract_buses(nets),
            pin_mappings=pin_mappings,
            assembly=assembly,
            mechanical=mechanical,
            constraints=requirements.physical_constraints,
            power_rails=extract_power_rails(components, nets),
            estimated_current_draw_ma=180.0,
            fabrication_notes=mechanical.fabrication_details,
            assembly_metadata={
                "status": "active",
                "model_name": self.model_name,
                "llm_provider": self.llm_provider.provider_name,
                "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                "runtime_provider": self.runtime_config.provider,
                "requested_model": self.runtime_config.requested_model or self.runtime_config.model,
                "runtime_model": self.runtime_config.model,
                "provider_overridden": self.runtime_config.provider_overridden,
                "model_overridden": self.runtime_config.model_overridden,
                "pipeline": "deterministic simulation + ADK-style hardware agents",
                "product_visual": "pocket_mp3_player",
                "image_features": requirements.physical_constraints,
                "render_dimensions": {"x_mm": 100, "y_mm": 21, "z_mm": 54}
            },
            project_version_history=[{"version": "0.1", "description": "Initial prompt and image driven MP3 player design"}],
            validation=validation_summary,
            is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues)
        )
        project_ir = build_mechanical_render_data(project_ir)
        self.save_project_to_db(prompt, project_ir)
        return project_ir

    def _load_simulated_watering_project(self, prompt: str) -> HardwareIntermediateRepresentation:
        overview = ProjectOverview(
            title="Auto-Grow Plant Moisture Monitor & Watering System",
            description=f"An automated soil-sensing irrigation and environment dashboard compiled for: '{prompt}'",
            difficulty="Intermediate",
            estimated_cost=11.00,
            category="Smart Home"
        )
        requirements = FunctionalRequirements(
            requirements=[
                "Monitor real-time soil moisture and environmental temperature.",
                "Turn on a 5V relay to activate an irrigation water pump when moisture drops below 30%.",
                "Display current soil and environmental readings on a sharp 0.96 inch OLED screen.",
                "Log all data points and warnings over a WiFi-connected database endpoint."
            ],
            power_needs="5V USB Wall Supply, powering MCU, Relay, and OLED screen.",
            operating_voltage=3.3,
            physical_constraints=["Water-resistant sensor probes.", "Enclosure footprint under 10x10x5cm."],
            safety_notes=["Keep relay AC connection/switching terminals isolated from water lines.", "Operate pump with separate power grounds if electrical noise interferes with readings."],
            missing_info=[]
        )
        
        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="ESP32-WROOM-32D",
                name="ESP32 NodeMCU Development Board",
                category="Microcontroller",
                quantity=1,
                unit_price=4.50,
                rationale="Provides dual core processor, WiFi connectivity, and plenty of GPIOs for sensor and relay control.",
                pins=self._get_pins_for_part("ESP32-WROOM-32D")
            ),
            ComponentInstance(
                ref_des="SEN1",
                part_number="DHT22",
                name="DHT22 Temperature & Humidity Sensor",
                category="Sensor",
                quantity=1,
                unit_price=2.80,
                rationale="Collects environmental temperature and relative humidity to guard against heat-stress.",
                pins=self._get_pins_for_part("DHT22")
            ),
            ComponentInstance(
                ref_des="ACT1",
                part_number="Relay-5V-1Ch",
                name="5V 1-Channel Optocoupled Relay Module",
                category="Actuator",
                quantity=1,
                unit_price=1.20,
                rationale="Safely switches power to the 5V water pump actuator using low-voltage ESP32 GPIO pins.",
                pins=self._get_relay_pins_3v3_input()
            ),
            ComponentInstance(
                ref_des="DISP1",
                part_number="SSD1306-I2C",
                name="0.96 inch OLED Display (I2C)",
                category="Display",
                quantity=1,
                unit_price=2.50,
                rationale="Displays quick status updates, soil moisture %, and humidity readouts locally.",
                pins=self._get_pins_for_part("SSD1306-I2C")
            ),
            ComponentInstance(
                ref_des="PWR1",
                part_number="USB-5V-Plug",
                name="5V USB Wall Power Supply",
                category="Power",
                quantity=1,
                unit_price=1.50,
                rationale="Provides the regulated 5V rail feeding ESP32 VIN and the relay coil supply.",
                pins=self._get_pins_for_part("USB-5V-Plug")
            )
        ]

        overview.estimated_cost = sum(c.unit_price * component_instance_count(c) for c in components)

        nets = [
            ConnectionNet(
                net_id="NET_GND",
                name="System Ground",
                net_type="Ground",
                voltage=0.0,
                pins=[
                    PinReference(ref_des="U1", pin_id="GND"),
                    PinReference(ref_des="SEN1", pin_id="GND"),
                    PinReference(ref_des="ACT1", pin_id="GND"),
                    PinReference(ref_des="DISP1", pin_id="GND"),
                    PinReference(ref_des="PWR1", pin_id="GND")
                ]
            ),
            ConnectionNet(
                net_id="NET_3V3",
                name="3.3V Power Rail",
                net_type="Power",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="3V3"),
                    PinReference(ref_des="SEN1", pin_id="VCC"),
                    PinReference(ref_des="DISP1", pin_id="VCC")
                ]
            ),
            ConnectionNet(
                net_id="NET_5V",
                name="5V Power Rail",
                net_type="Power",
                voltage=5.0,
                pins=[
                    PinReference(ref_des="PWR1", pin_id="5V"),
                    PinReference(ref_des="U1", pin_id="VIN"),
                    PinReference(ref_des="ACT1", pin_id="VCC")
                ]
            ),
            ConnectionNet(
                net_id="NET_I2C_SDA",
                name="I2C Serial Data",
                net_type="I2C",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D21"),
                    PinReference(ref_des="DISP1", pin_id="SDA")
                ]
            ),
            ConnectionNet(
                net_id="NET_I2C_SCL",
                name="I2C Serial Clock",
                net_type="I2C",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D22"),
                    PinReference(ref_des="DISP1", pin_id="SCL")
                ]
            ),
            ConnectionNet(
                net_id="NET_DHT_DATA",
                name="DHT22 Sensor Connection",
                net_type="Digital",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D27"),
                    PinReference(ref_des="SEN1", pin_id="DATA")
                ]
            ),
            ConnectionNet(
                net_id="NET_RELAY_IN",
                name="Relay Command Line",
                net_type="Digital",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D25"),
                    PinReference(ref_des="ACT1", pin_id="IN")
                ]
            )
        ]

        pin_mappings = [
            PinMappingEntry(mcu_pin="D21", connected_to="SSD1306 SDA Pin", net_name="NET_I2C_SDA"),
            PinMappingEntry(mcu_pin="D22", connected_to="SSD1306 SCL Pin", net_name="NET_I2C_SCL"),
            PinMappingEntry(mcu_pin="D27", connected_to="DHT22 Sensor Pin", net_name="NET_DHT_DATA"),
            PinMappingEntry(mcu_pin="D25", connected_to="Relay Switch Command", net_name="NET_RELAY_IN")
        ]

        assembly = [
            AssemblyStep(
                step_num=1,
                title="Prepare Components & Power rails",
                description="Place the ESP32 board onto a half-sized breadboard. Connect PWR1 5V to ESP32 VIN and the relay VCC rail, then tie PWR1 GND to the shared ground rail. Connect ESP32 3V3 to the low-voltage sensor/display rail.",
                danger_flag=False,
                affected_components=["U1", "PWR1"]
            ),
            AssemblyStep(
                step_num=2,
                title="Assemble & Power the DHT22 Temperature Sensor",
                description="Connect DHT22 Pin 1 (VCC) to the 3.3V breadboard rail. Connect Pin 4 (GND) to the GND rail. Connect DHT22 Pin 2 (DATA) directly to the ESP32 pin D27. Place a 10k resistor between VCC and DATA to act as an external pull-up if necessary.",
                danger_flag=False,
                affected_components=["SEN1", "U1"]
            ),
            AssemblyStep(
                step_num=3,
                title="Wire up the SSD1306 OLED Display over I2C",
                description="Mount the OLED screen. Run VCC to 3.3V, GND to GND, SDA to ESP32 Pin D21, and SCL to ESP32 Pin D22. This sets up the serial hardware bus.",
                danger_flag=False,
                affected_components=["DISP1", "U1"]
            ),
            AssemblyStep(
                step_num=4,
                title="Install and Wire the 5V Relay Module",
                description="Connect Relay VCC to the PWR1 5V rail and GND to system ground. Connect the 3.3V-compatible signal input (IN) of the relay to ESP32 Pin D25.",
                danger_flag=True,
                danger_message="Never connect AC mains electricity to the relay terminals without proper enclosure insulated blocks!",
                affected_components=["ACT1", "U1"]
            )
        ]

        mechanical = MechanicalNotes(
            enclosure_type="3D Printed",
            mounting_guidance="Use M3 brass standoffs inside a pre-measured PLA project box. Drill rubber routing holes for moisture probes and power.",
            fabrication_details=[
                "Wall thickness: 2.0mm.",
                "Infill: 20% grid pattern.",
                "Material: Green or Black PLA.",
                "Ventilation grills on the side of the housing to keep the DHT22 breathing correctly."
            ],
            fabrication_cost_estimate_usd=10.00,
            cad_sources=[
                MechanicalSource(
                    name="Hammond 1554BGY watertight enclosure CAD",
                    source_type="Vendor CAD",
                    url="https://www.digikey.ca/en/products/detail/hammond-manufacturing/1554BGY/1090730",
                    file_formats=["STEP", "IGES"],
                    license="Vendor CAD reference for Hammond 1554BGY; verify distributor terms",
                    estimated_unit_price_usd=18.93,
                    notes="Use as an off-the-shelf sealed enclosure baseline; drill only low-side cable glands for probes and power."
                ),
                MechanicalSource(
                    name="ESP32 Project Enclosure with OLED SSD1306",
                    source_type="Open STL",
                    url="https://3dgo.app/models/printables/94864",
                    file_formats=["STL"],
                    license="Creative Commons attribution-sharealike listing mirror; verify original Printables license",
                    estimated_unit_price_usd=0.00,
                    notes="Reference display bezel and ESP32 standoff placement if a fully printed enclosure is preferred."
                )
            ],
            manufacturability_rating="Easy"
        )

        validation_issues = validate_circuit(components, nets, requirements)
        validation_summary = build_validation_summary(validation_issues)
        power_rails = extract_power_rails(components, nets)
        buses = extract_buses(nets)
        current_draw = estimate_current_draw(components)

        project_ir = HardwareIntermediateRepresentation(
            hardware_ir_version="0.1",
            overview=overview,
            requirements=requirements,
            components=components,
            nets=nets,
            buses=buses,
            pin_mappings=pin_mappings,
            assembly=assembly,
            mechanical=mechanical,
            constraints=requirements.physical_constraints,
            power_rails=power_rails,
            estimated_current_draw_ma=current_draw,
            fabrication_notes=mechanical.fabrication_details,
            assembly_metadata={
                "status": "active",
                "model_name": self.model_name,
                "llm_provider": self.llm_provider.provider_name,
                "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                "runtime_provider": self.runtime_config.provider,
                "requested_model": self.runtime_config.requested_model or self.runtime_config.model,
                "runtime_model": self.runtime_config.model,
                "provider_overridden": self.runtime_config.provider_overridden,
                "model_overridden": self.runtime_config.model_overridden,
                "pipeline": "deterministic simulation + ADK-style hardware agents",
                "schematic": {
                    "canvas": {"width": 1180, "height": 720},
                    "placements": {
                        "PWR1": {"x": 80, "y": 80},
                        "U1": {"x": 460, "y": 300},
                        "SEN1": {"x": 250, "y": 410},
                        "DISP1": {"x": 870, "y": 380},
                        "ACT1": {"x": 870, "y": 120}
                    }
                }
            },
            project_version_history=[{"version": "0.1", "description": "Initial fallback design generation"}],
            validation=validation_summary,
            is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues)
        )

        project_ir = build_mechanical_render_data(project_ir)
        self.save_project_to_db(prompt, project_ir)
        return project_ir

    def _load_simulated_thermostat_project(self, prompt: str) -> HardwareIntermediateRepresentation:
        overview = ProjectOverview(
            title="Smart Nest-Style Environmental Thermostat Controller",
            description=f"Intelligent wall-mounted environment controller with climate regulation compiled for: '{prompt}'",
            difficulty="Intermediate",
            estimated_cost=11.50,
            category="Smart Home"
        )
        requirements = FunctionalRequirements(
            requirements=[
                "Collect high-precision altitude, pressure, and ambient temperature readings.",
                "Display custom menu, heat indices, and setpoint targets on OLED.",
                "Actuate solid-state heating elements or fan control through an optoisolated relay switch.",
                "Maintain low-power standby during battery backups."
            ],
            power_needs="5V stationary adapter feed. Rechargeable battery backup requires a charger/boost module not represented in this low-voltage wiring preset.",
            operating_voltage=3.3,
            physical_constraints=["Standard wall box mounting.", "Total weight under 150g."],
            safety_notes=["Incorporate thermal fusing if controlling resistive heater elements.", "Double check voltage bounds before wiring rechargeable LiPos."],
            missing_info=[]
        )
        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="ESP32-WROOM-32D",
                name="ESP32 NodeMCU Development Board",
                category="Microcontroller",
                quantity=1,
                unit_price=4.50,
                rationale="Onboard Bluetooth and WiFi allow web setpoints, scheduling, and logging integrations.",
                pins=self._get_pins_for_part("ESP32-WROOM-32D")
            ),
            ComponentInstance(
                ref_des="SEN1",
                part_number="BMP280",
                name="BMP280 Barometric Pressure & Temp Sensor",
                category="Sensor",
                quantity=1,
                unit_price=1.80,
                rationale="Performs precision temperature tracking to regulate comfortable setpoints within 0.1C.",
                pins=self._get_pins_for_part("BMP280")
            ),
            ComponentInstance(
                ref_des="ACT1",
                part_number="Relay-5V-1Ch",
                name="5V 1-Channel Optocoupled Relay Module",
                category="Actuator",
                quantity=1,
                unit_price=1.20,
                rationale="Switches active HVAC furnace control logic safely.",
                pins=self._get_relay_pins_3v3_input()
            ),
            ComponentInstance(
                ref_des="DISP1",
                part_number="SSD1306-I2C",
                name="0.96 inch OLED Display (I2C)",
                category="Display",
                quantity=1,
                unit_price=2.50,
                rationale="Renders a real-time UI showing current temp vs user target setpoints.",
                pins=self._get_pins_for_part("SSD1306-I2C")
            ),
            ComponentInstance(
                ref_des="PWR1",
                part_number="USB-5V-Plug",
                name="5V Stationary Adapter Feed",
                category="Power",
                quantity=1,
                unit_price=1.50,
                rationale="Provides regulated 5V input for ESP32 VIN and the relay coil supply.",
                pins=self._get_pins_for_part("USB-5V-Plug")
            )
        ]
        
        nets = [
            ConnectionNet(
                net_id="NET_GND",
                name="Ground Wire",
                net_type="Ground",
                voltage=0.0,
                pins=[
                    PinReference(ref_des="U1", pin_id="GND"),
                    PinReference(ref_des="SEN1", pin_id="GND"),
                    PinReference(ref_des="ACT1", pin_id="GND"),
                    PinReference(ref_des="DISP1", pin_id="GND"),
                    PinReference(ref_des="PWR1", pin_id="GND")
                ]
            ),
            ConnectionNet(
                net_id="NET_3V3",
                name="3.3V Power Line",
                net_type="Power",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="3V3"),
                    PinReference(ref_des="SEN1", pin_id="VCC"),
                    PinReference(ref_des="DISP1", pin_id="VCC")
                ]
            ),
            ConnectionNet(
                net_id="NET_5V",
                name="5V Adapter Rail",
                net_type="Power",
                voltage=5.0,
                pins=[
                    PinReference(ref_des="PWR1", pin_id="5V"),
                    PinReference(ref_des="U1", pin_id="VIN"),
                    PinReference(ref_des="ACT1", pin_id="VCC")
                ]
            ),
            ConnectionNet(
                net_id="NET_I2C_SDA",
                name="I2C Serial Data",
                net_type="I2C",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D21"),
                    PinReference(ref_des="DISP1", pin_id="SDA"),
                    PinReference(ref_des="SEN1", pin_id="SDA")
                ]
            ),
            ConnectionNet(
                net_id="NET_I2C_SCL",
                name="I2C Serial Clock",
                net_type="I2C",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D22"),
                    PinReference(ref_des="DISP1", pin_id="SCL"),
                    PinReference(ref_des="SEN1", pin_id="SCL")
                ]
            ),
            ConnectionNet(
                net_id="NET_RELAY_CTRL",
                name="Furnace Control Net",
                net_type="Digital",
                voltage=3.3,
                pins=[
                    PinReference(ref_des="U1", pin_id="D25"),
                    PinReference(ref_des="ACT1", pin_id="IN")
                ]
            )
        ]

        pin_mappings = [
            PinMappingEntry(mcu_pin="D21", connected_to="OLED/BMP280 Data", net_name="NET_I2C_SDA"),
            PinMappingEntry(mcu_pin="D22", connected_to="OLED/BMP280 Clock", net_name="NET_I2C_SCL"),
            PinMappingEntry(mcu_pin="D25", connected_to="Climate Control Relay Trigger", net_name="NET_RELAY_CTRL")
        ]

        assembly = [
            AssemblyStep(
                step_num=1,
                title="Wire 5V adapter and low-voltage rails",
                description="Seat the ESP32 on your proto-board. Connect PWR1 5V to ESP32 VIN and relay VCC, then tie PWR1 GND to the shared ground rail. Use ESP32 3V3 only for the BMP280 and OLED logic rail.",
                danger_flag=True,
                danger_message="Do not attach a bare LiPo directly to VIN. Add a proper charger/boost module before implementing battery backup.",
                affected_components=["U1", "PWR1", "ACT1"]
            ),
            AssemblyStep(
                step_num=2,
                title="Construct Shared I2C Bus",
                description="Connect SCL on both OLED and BMP280 to ESP32 Pin D22. Connect SDA on both display and pressure sensor to ESP32 Pin D21. Power them with 3.3V power rails and verify no crosstalk.",
                danger_flag=False,
                affected_components=["SEN1", "DISP1", "U1"]
            ),
            AssemblyStep(
                step_num=3,
                title="Integrate HVAC Switching relay",
                description="Power the relay coil from the PWR1 5V rail. Send the 3.3V-compatible input line (IN) to ESP32 GPIO pin D25. Route furnace control lines into Normally Open (NO) and Common (COM) blocks.",
                danger_flag=True,
                danger_message="Unplug structural heating supplies before hooking up high voltage terminals!",
                affected_components=["ACT1", "U1"]
            )
        ]

        mechanical = MechanicalNotes(
            enclosure_type="Custom Acrylic",
            mounting_guidance="Screw back-plate onto dry-wall with standard drywall screws. Clip the acrylic cover overlay on top for clean bezel appearance.",
            fabrication_details=[
                "Front-facing slot for BMP280 air-exposure.",
                "Laser cut clear acrylic viewing window.",
                "Mounting holes spaced 60mm vertically to match US wall standards."
            ],
            fabrication_cost_estimate_usd=7.50,
            cad_sources=[
                MechanicalSource(
                    name="Thermostat trim plate STL candidate index",
                    source_type="Reference CAD",
                    url="https://www.stlfinder.com/3dmodels/thermostat-trim-plate/",
                    file_formats=["STL"],
                    license="Model-specific licenses vary; verify selected model before fabrication",
                    estimated_unit_price_usd=0.00,
                    notes="Use as a geometry reference for wall coverage, then generate a project-specific acrylic DXF for the OLED/BMP280 layout."
                ),
                MechanicalSource(
                    name="Google Nest thermostat wallmount candidate index",
                    source_type="Paid STL",
                    url="https://cults3d.com/es/etiquetas/termostato%2Bnest%2Be%2Buk",
                    file_formats=["STL"],
                    license="Commercial model listing; verify current download price and license before reuse",
                    estimated_unit_price_usd=4.02,
                    notes="Useful reference for wall-mount clip geometry and cable pass-through placement."
                )
            ],
            manufacturability_rating="Moderate"
        )

        validation_issues = validate_circuit(components, nets, requirements)
        validation_issues.append(ValidationIssue(
            severity="INFO",
            category="Scope Gap",
            description="The battery-backup requirement is documented, but this preset wiring only models the safe 5V adapter-powered thermostat core.",
            troubleshooting="Add a LiPo charger/protection module and boost or power-path regulator before implementing rechargeable backup operation."
        ))
        validation_summary = build_validation_summary(validation_issues)
        power_rails = extract_power_rails(components, nets)
        buses = extract_buses(nets)
        current_draw = estimate_current_draw(components)

        project_ir = HardwareIntermediateRepresentation(
            hardware_ir_version="0.1",
            overview=overview,
            requirements=requirements,
            components=components,
            nets=nets,
            buses=buses,
            pin_mappings=pin_mappings,
            assembly=assembly,
            mechanical=mechanical,
            constraints=requirements.physical_constraints,
            power_rails=power_rails,
            estimated_current_draw_ma=current_draw,
            fabrication_notes=mechanical.fabrication_details,
            assembly_metadata={
                "status": "active",
                "model_name": self.model_name,
                "llm_provider": self.llm_provider.provider_name,
                "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                "runtime_provider": self.runtime_config.provider,
                "requested_model": self.runtime_config.requested_model or self.runtime_config.model,
                "runtime_model": self.runtime_config.model,
                "provider_overridden": self.runtime_config.provider_overridden,
                "model_overridden": self.runtime_config.model_overridden,
                "pipeline": "deterministic simulation + ADK-style hardware agents",
                "schematic": {
                    "canvas": {"width": 1180, "height": 720},
                    "placements": {
                        "PWR1": {"x": 80, "y": 80},
                        "U1": {"x": 460, "y": 300},
                        "SEN1": {"x": 250, "y": 420},
                        "DISP1": {"x": 870, "y": 380},
                        "ACT1": {"x": 870, "y": 120}
                    }
                }
            },
            project_version_history=[{"version": "0.1", "description": "Initial fallback design generation"}],
            validation=validation_summary,
            is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues)
        )

        project_ir = build_mechanical_render_data(project_ir)
        self.save_project_to_db(prompt, project_ir)
        return project_ir

    def _load_simulated_smart_lock_project(self, prompt: str) -> HardwareIntermediateRepresentation:
        overview = ProjectOverview(
            title="Biometric & Keyless Bluetooth Smart Deadbolt",
            description=f"A smart lock mechanism utilizing servos, status indicator LEDs, and low power bluetooth.",
            difficulty="Beginner",
            estimated_cost=14.35,
            category="Smart Home"
        )
        requirements = FunctionalRequirements(
            requirements=[
                "Physically retract a deadbolt lock using a high-torque SG90 micro-servo motor.",
                "Display lock/unlock status locally with a green or red LED indicator.",
                "Accept secure bluetooth encryption handshakes to release deadbolt.",
                "Accept external physical push-buttons as deadbolts bypass."
            ],
            power_needs="5V external power bank or Micro-USB feed.",
            operating_voltage=5.0,
            physical_constraints=["Must fit inside deadbolt door handle cavities.", "Low power standby."],
            safety_notes=["Incorporate manual lock override lever to avoid physical lockouts during total power failures.", "Operate servo logic through clean voltage buffers."],
            missing_info=[]
        )
        components = [
            ComponentInstance(
                ref_des="U1",
                part_number="Arduino-Nano-V3",
                name="Arduino Nano v3.0",
                category="Microcontroller",
                quantity=1,
                unit_price=3.20,
                rationale="Highly portable controller with plenty of digital PWM pins to manage servo positions easily.",
                pins=self._get_pins_for_part("Arduino-Nano-V3")
            ),
            ComponentInstance(
                ref_des="ACT1",
                part_number="SG90-Servo",
                name="SG90 Micro Servo Motor",
                category="Actuator",
                quantity=1,
                unit_price=2.00,
                rationale="Provides exact rotational control (0-180deg) to throw the manual deadbolt lever.",
                pins=self._get_pins_for_part("SG90-Servo")
            ),
            ComponentInstance(
                ref_des="LED1",
                part_number="LED-Red-Generic",
                name="Standard Red LED (5mm)",
                category="Passives",
                quantity=1,
                unit_price=0.10,
                rationale="Indicates current lock/locked visual feedback for physical debugging.",
                pins=self._get_pins_for_part("LED-Red-Generic")
            ),
            ComponentInstance(
                ref_des="R1",
                part_number="Resistor-220R",
                name="220 Ohm Carbon Film Resistor (1/4W)",
                category="Passives",
                quantity=1,
                unit_price=0.05,
                rationale="Protects LED1 from overloading by limiting current from Arduino GPIO pins.",
                pins=self._get_pins_for_part("Resistor-220R")
            ),
            ComponentInstance(
                ref_des="PWR1",
                part_number="USB-5V-Plug",
                name="5V USB Power Bank Feed",
                category="Power",
                quantity=1,
                unit_price=1.50,
                rationale="Provides a regulated 5V input for the Arduino Nano and SG90 servo without routing a 3.7V LiPo into VIN.",
                pins=self._get_pins_for_part("USB-5V-Plug")
            )
        ]

        overview.estimated_cost = sum(c.unit_price * component_instance_count(c) for c in components)

        nets = [
            ConnectionNet(
                net_id="NET_GND",
                name="Ground Wire",
                net_type="Ground",
                voltage=0.0,
                pins=[
                    PinReference(ref_des="U1", pin_id="GND"),
                    PinReference(ref_des="ACT1", pin_id="GND"),
                    PinReference(ref_des="LED1", pin_id="CATHODE"),
                    PinReference(ref_des="PWR1", pin_id="GND")
                ]
            ),
            ConnectionNet(
                net_id="NET_5V",
                name="5V Power Rail",
                net_type="Power",
                voltage=5.0,
                pins=[
                    PinReference(ref_des="PWR1", pin_id="5V"),
                    PinReference(ref_des="U1", pin_id="5V"),
                    PinReference(ref_des="ACT1", pin_id="5V")
                ]
            ),
            ConnectionNet(
                net_id="NET_SERVO_PWM",
                name="Servo Signal Wire",
                net_type="PWM",
                voltage=5.0,
                pins=[
                    PinReference(ref_des="U1", pin_id="D9"),
                    PinReference(ref_des="ACT1", pin_id="PWM")
                ]
            ),
            ConnectionNet(
                net_id="NET_LED_DRIVE",
                name="LED Signal Net",
                net_type="Digital",
                voltage=5.0,
                pins=[
                    PinReference(ref_des="U1", pin_id="D3"),
                    PinReference(ref_des="R1", pin_id="1")
                ]
            ),
            ConnectionNet(
                net_id="NET_LED_RESISTOR",
                name="Resistor to LED Anode",
                net_type="Digital",
                voltage=2.0,
                pins=[
                    PinReference(ref_des="R1", pin_id="2"),
                    PinReference(ref_des="LED1", pin_id="ANODE")
                ]
            ),
        ]

        pin_mappings = [
            PinMappingEntry(mcu_pin="D9", connected_to="SG90 Servo PWM Command", net_name="NET_SERVO_PWM"),
            PinMappingEntry(mcu_pin="D3", connected_to="220R Current Limiter Input", net_name="NET_LED_DRIVE")
        ]

        assembly = [
            AssemblyStep(
                step_num=1,
                title="Mount micro controller and 5V feed",
                description="Secure the Arduino Nano on a mini breadboard with its USB connector accessible. Bring the 5V USB power-bank feed to the positive rail and common ground rail before attaching loads.",
                danger_flag=False,
                affected_components=["U1", "PWR1"]
            ),
            AssemblyStep(
                step_num=2,
                title="Wire the Servo motor",
                description="Plug the brown wire of the SG90 to common GND. Connect the red wire to the regulated 5V rail from PWR1, not to an unregulated LiPo or VIN feed. Connect the orange control wire to PWM pin D9 on the Nano.",
                danger_flag=False,
                affected_components=["ACT1", "U1"]
            ),
            AssemblyStep(
                step_num=3,
                title="Configure current limiting Status indicator",
                description="Connect a 220 Ohm resistor (R1) between Arduino pin D3 and the long lead (Anode) of the Red LED. Run a short wire from the flat edge (Cathode) of the LED to common system ground.",
                danger_flag=False,
                affected_components=["LED1", "R1", "U1"]
            )
        ]

        mechanical = MechanicalNotes(
            enclosure_type="3D Printed",
            mounting_guidance="Standard deadbolt faceplate installation. Feed physical lock override pin through structural center.",
            fabrication_details=[
                "Print in robust PETG or ABS to stand up to physical forcing.",
                "Infill density: 40% with tri-hexagon pattern.",
                "Custom slot on the internal face to allow backup mechanical turn-keys."
            ],
            fabrication_cost_estimate_usd=5.50,
            cad_sources=[
                MechanicalSource(
                    name="Arduino-based electronic lock files",
                    source_type="Open STL",
                    url="https://www.thingiverse.com/thing:2350856/files",
                    file_formats=["STL"],
                    license="Thingiverse file license; verify on source page",
                    estimated_unit_price_usd=0.00,
                    notes="Reference layout includes Arduino Nano and SG90-class servo mounting for lock actuation."
                ),
                MechanicalSource(
                    name="Motorized door lock for SG90 servo",
                    source_type="Open STL",
                    url="https://cults3d.com/en/3d-model/gadget/motorized-door-lock-for-sg90-servo/similar-designs",
                    file_formats=["STL"],
                    license="Cults listing marked free in search result; verify license before reuse",
                    estimated_unit_price_usd=0.00,
                    notes="Use the SG90 servo bracket and linkage proportions as a reference for the deadbolt adapter."
                )
            ],
            manufacturability_rating="Moderate"
        )

        validation_issues = validate_circuit(components, nets, requirements)
        validation_issues.append(ValidationIssue(
            severity="INFO",
            category="Scope Gap",
            description="The project title and requirements mention Bluetooth, biometric unlock, and bypass buttons, but this minimal wiring pass only represents the 5V servo deadbolt core, status LED, and current-limiting resistor.",
            troubleshooting="Add a BLE-capable controller or module, fingerprint reader, and debounced bypass inputs before claiming those functions are electrically implemented."
        ))
        validation_summary = build_validation_summary(validation_issues)
        power_rails = extract_power_rails(components, nets)
        buses = extract_buses(nets)
        current_draw = estimate_current_draw(components)

        project_ir = HardwareIntermediateRepresentation(
            hardware_ir_version="0.1",
            overview=overview,
            requirements=requirements,
            components=components,
            nets=nets,
            buses=buses,
            pin_mappings=pin_mappings,
            assembly=assembly,
            mechanical=mechanical,
            constraints=requirements.physical_constraints,
            power_rails=power_rails,
            estimated_current_draw_ma=current_draw,
            fabrication_notes=mechanical.fabrication_details,
            assembly_metadata={
                "status": "active",
                "model_name": self.model_name,
                "llm_provider": self.llm_provider.provider_name,
                "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                "runtime_provider": self.runtime_config.provider,
                "requested_model": self.runtime_config.requested_model or self.runtime_config.model,
                "runtime_model": self.runtime_config.model,
                "provider_overridden": self.runtime_config.provider_overridden,
                "model_overridden": self.runtime_config.model_overridden,
                "pipeline": "deterministic simulation + ADK-style hardware agents",
                "schematic": {
                    "canvas": {"width": 1180, "height": 680},
                    "placements": {
                        "PWR1": {"x": 300, "y": 60},
                        "U1": {"x": 460, "y": 320},
                        "ACT1": {"x": 870, "y": 120},
                        "R1": {"x": 760, "y": 340},
                        "LED1": {"x": 1010, "y": 340}
                    }
                }
            },
            project_version_history=[{"version": "0.1", "description": "Initial fallback design generation"}],
            validation=validation_summary,
            is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues)
        )

        project_ir = build_mechanical_render_data(project_ir)
        self.save_project_to_db(prompt, project_ir)
        return project_ir

    def _get_pins_for_part(self, part_number: str) -> List[PinDefinition]:
        """Fetch pin template mapping from components database directly."""
        try:
            db_template = get_component_template_by_part_number(part_number)
            if db_template:
                return [PinDefinition(**pin) for pin in db_template.pins]
            return []
        except Exception:
            # Absolute hardcoded fallback to keep simulated run bulletproof
            for comp in SEED_COMPONENTS:
                if comp["part_number"] == part_number:
                    return [PinDefinition(**pin) for pin in comp["pins"]]
            return []

    def _get_relay_pins_3v3_input(self) -> List[PinDefinition]:
        pins = []
        for pin in self._get_pins_for_part("Relay-5V-1Ch"):
            if pin.pin_id == "IN":
                pins.append(PinDefinition(
                    pin_id=pin.pin_id,
                    name=pin.name,
                    pin_type=pin.pin_type,
                    voltage=3.3,
                    description="3.3V-compatible logic input to trigger optocoupled relay module"
                ))
            else:
                pins.append(pin)
        return pins

SEED_COMPONENTS = [
    {
        "part_number": "ESP32-WROOM-32D",
        "name": "ESP32 NodeMCU Development Board",
        "category": "Microcontroller",
        "description": "Powerful WiFi + Bluetooth MCU, perfect for IoT, smart home, and cloud-connected automation.",
        "price": 4.50,
        "pins": [
            {"pin_id": "3V3", "name": "3.3V Power Out", "pin_type": "Power", "voltage": 3.3, "description": "3.3V Regulated Output"},
            {"pin_id": "GND", "name": "Ground", "pin_type": "Ground", "voltage": 0.0, "description": "System Ground Reference"},
            {"pin_id": "EN", "name": "Enable / Reset", "pin_type": "Passive", "voltage": 3.3, "description": "Reset pin, active low"},
            {"pin_id": "D25", "name": "GPIO25 / DAC_CH1", "pin_type": "Digital", "voltage": 3.3, "description": "DAC / General GPIO"},
            {"pin_id": "D22", "name": "GPIO22 / I2C_SCL", "pin_type": "I2C", "voltage": 3.3, "description": "Primary I2C SCL"},
            {"pin_id": "D21", "name": "GPIO21 / I2C_SDA", "pin_type": "I2C", "voltage": 3.3, "description": "Primary I2C SDA"},
            {"pin_id": "D27", "name": "GPIO27 / ADC_CH17", "pin_type": "Digital", "voltage": 3.3, "description": "General GPIO"},
            {"pin_id": "VIN", "name": "External Power In", "pin_type": "Power", "voltage": 5.0, "description": "5V Unregulated Input"}
        ],
        "use_cases": ["iot", "wifi", "bluetooth", "smart-home", "robotics", "automation", "controller", "mcu"]
    },
    {
        "part_number": "Arduino-Nano-V3",
        "name": "Arduino Nano v3.0",
        "category": "Microcontroller",
        "description": "Compact ATmega328P microcontroller board. Ideal for lightweight, non-wireless, breadboard-friendly physical computing.",
        "price": 3.20,
        "pins": [
            {"pin_id": "5V", "name": "5V Power Out", "pin_type": "Power", "voltage": 5.0, "description": "5V Regulated Power Output"},
            {"pin_id": "3V3", "name": "3.3V Power Out", "pin_type": "Power", "voltage": 3.3, "description": "3.3V Regulated Power Output"},
            {"pin_id": "GND", "name": "Ground", "pin_type": "Ground", "voltage": 0.0, "description": "System Ground"},
            {"pin_id": "VIN", "name": "Voltage Input", "pin_type": "Power", "voltage": 12.0, "description": "7V-12V Input (regulated down to 5V)"},
            {"pin_id": "D3", "name": "Digital 3 / PWM", "pin_type": "PWM", "voltage": 5.0, "description": "GPIO / PWM / Interrupt 1"},
            {"pin_id": "D9", "name": "Digital 9 / PWM", "pin_type": "PWM", "voltage": 5.0, "description": "GPIO / PWM"}
        ],
        "use_cases": ["robotics", "learning", "prototyping", "mcu", "basic-electronics", "wearable"]
    },
    {
        "part_number": "DHT22",
        "name": "DHT22 Temperature & Humidity Sensor",
        "category": "Sensor",
        "description": "High-accuracy digital relative temperature and humidity sensor module with single-bus interface.",
        "price": 2.80,
        "pins": [
            {"pin_id": "VCC", "name": "VCC Power", "pin_type": "Power", "voltage": 3.3, "description": "Supports 3.3V to 5.0V Supply"},
            {"pin_id": "DATA", "name": "Signal Out", "pin_type": "Digital", "voltage": 3.3, "description": "Single-wire digital data out (requires pullup)"},
            {"pin_id": "NC", "name": "No Connection", "pin_type": "Passive", "voltage": 0.0, "description": "Do not connect"},
            {"pin_id": "GND", "name": "Ground", "pin_type": "Ground", "voltage": 0.0, "description": "Power ground reference"}
        ],
        "use_cases": ["weather-station", "environmental-monitor", "temperature", "humidity", "smart-home", "gardening"]
    },
    {
        "part_number": "BMP280",
        "name": "BMP280 Barometric Pressure & Temp Sensor",
        "category": "Sensor",
        "description": "High-precision digital altimeter/pressure sensor with I2C and SPI interfaces. Operates at 3.3V.",
        "price": 1.80,
        "pins": [
            {"pin_id": "VCC", "name": "Power VCC", "pin_type": "Power", "voltage": 3.3, "description": "1.8V to 3.6V Supply Input"},
            {"pin_id": "GND", "name": "Ground", "pin_type": "Ground", "voltage": 0.0, "description": "Ground"},
            {"pin_id": "SCL", "name": "I2C SCL / SPI SCK", "pin_type": "I2C", "voltage": 3.3, "description": "Clock Pin"},
            {"pin_id": "SDA", "name": "I2C SDA / SPI MOSI", "pin_type": "I2C", "voltage": 3.3, "description": "Data Input/Output Pin"},
            {"pin_id": "CSB", "name": "Chip Select (SPI)", "pin_type": "SPI", "voltage": 3.3, "description": "SPI CSB, active low (pull high for I2C)"},
            {"pin_id": "SDO", "name": "SPI MISO / I2C Address Select", "pin_type": "Digital", "voltage": 3.3, "description": "Address LSB / MISO"}
        ],
        "use_cases": ["barometer", "weather-station", "altimeter", "drones", "smart-watch"]
    },
    {
        "part_number": "Relay-5V-1Ch",
        "name": "5V 1-Channel Optocoupled Relay Module",
        "category": "Actuator",
        "description": "Safely switches high-voltage AC or DC appliances using low-voltage logic from MCUs. Actuated by active-low or active-high logic.",
        "price": 1.20,
        "pins": [
            {"pin_id": "VCC", "name": "Module Power (5V)", "pin_type": "Power", "voltage": 5.0, "description": "5V Relay coil power"},
            {"pin_id": "GND", "name": "Module Ground", "pin_type": "Ground", "voltage": 0.0, "description": "System Ground"},
            {"pin_id": "IN", "name": "Signal Input", "pin_type": "Digital", "voltage": 5.0, "description": "Logic input to trigger coil (optocoupled)"},
            {"pin_id": "COM", "name": "Switch Common Terminal", "pin_type": "Passive", "voltage": 250.0, "description": "High-power common pole"},
            {"pin_id": "NO", "name": "Switch Normally Open Terminal", "pin_type": "Passive", "voltage": 250.0, "description": "Connected to COM only when energized"},
            {"pin_id": "NC", "name": "Switch Normally Closed Terminal", "pin_type": "Passive", "voltage": 250.0, "description": "Connected to COM by default"}
        ],
        "use_cases": ["home-automation", "smart-plug", "ac-switching", "motor-control", "valve-control"]
    },
    {
        "part_number": "SSD1306-I2C",
        "name": "0.96 inch OLED Display (I2C)",
        "category": "Display",
        "description": "128x64 pixels resolution organic LED display. Sharp, contrasty display controlled over simple I2C.",
        "price": 2.50,
        "pins": [
            {"pin_id": "VCC", "name": "Power VCC", "pin_type": "Power", "voltage": 3.3, "description": "Supports 3.3V or 5V Power Input"},
            {"pin_id": "GND", "name": "Ground", "pin_type": "Ground", "voltage": 0.0, "description": "Ground Reference"},
            {"pin_id": "SCL", "name": "I2C Serial Clock", "pin_type": "I2C", "voltage": 3.3, "description": "I2C SCL"},
            {"pin_id": "SDA", "name": "I2C Serial Data", "pin_type": "I2C", "voltage": 3.3, "description": "I2C SDA"}
        ],
        "use_cases": ["user-interface", "smart-thermostat", "clock", "dashboard", "smart-home"]
    },
    {
        "part_number": "Battery-LiPo-3.7V",
        "name": "3.7V Lithium Polymer Battery (1200mAh)",
        "category": "Power",
        "description": "Rechargeable, high-density LiPo power pack. Essential for wearable and off-grid wireless hardware setups.",
        "price": 5.50,
        "pins": [
            {"pin_id": "POS", "name": "Positive Lead (Red)", "pin_type": "Power", "voltage": 3.7, "description": "Positive terminal"},
            {"pin_id": "NEG", "name": "Negative Lead (Black)", "pin_type": "Ground", "voltage": 0.0, "description": "Negative reference terminal"}
        ],
        "use_cases": ["portable-power", "wearables", "iot-nodes", "drones", "off-grid"]
    },
    {
        "part_number": "SG90-Servo",
        "name": "SG90 Micro Servo Motor",
        "category": "Actuator",
        "description": "High-torque lightweight 180-degree micro servo. Excellent for robotic joints, steering, and physical actuators.",
        "price": 2.00,
        "pins": [
            {"pin_id": "5V", "name": "Power VCC (Red)", "pin_type": "Power", "voltage": 5.0, "description": "5.0V nominal power input"},
            {"pin_id": "GND", "name": "Ground (Brown)", "pin_type": "Ground", "voltage": 0.0, "description": "Power ground reference"},
            {"pin_id": "PWM", "name": "Control Signal (Orange)", "pin_type": "PWM", "voltage": 5.0, "description": "PWM pulse 50Hz, 1ms to 2ms width"}
        ],
        "use_cases": ["robotics", "robotic-arm", "rc-car", "smart-door-lock", "hobbies"]
    },
    {
        "part_number": "LED-Red-Generic",
        "name": "Standard Red LED (5mm)",
        "category": "Passives",
        "description": "Standard 5mm red light emitting diode. Useful for simple indicator signals. Needs current-limiting resistor.",
        "price": 0.10,
        "pins": [
            {"pin_id": "ANODE", "name": "Anode (+) Long Lead", "pin_type": "Passive", "voltage": 2.0, "description": "Positive terminal (needs 1.8V - 2.2V forward drop)"},
            {"pin_id": "CATHODE", "name": "Cathode (-) Flat Lead", "pin_type": "Ground", "voltage": 0.0, "description": "Ground Reference Pin"}
        ],
        "use_cases": ["status-indicator", "debugging", "blinky", "diagnostics"]
    },
    {
        "part_number": "Resistor-220R",
        "name": "220 Ohm Carbon Film Resistor (1/4W)",
        "category": "Passives",
        "description": "Ideal size for current-limiting standard LEDs driven from 5V or 3.3V microcontroller pins.",
        "price": 0.05,
        "pins": [
            {"pin_id": "1", "name": "Lead 1", "pin_type": "Passive", "voltage": None, "description": "Bidirectional passive pin"},
            {"pin_id": "2", "name": "Lead 2", "pin_type": "Passive", "voltage": None, "description": "Bidirectional passive pin"}
        ],
        "use_cases": ["current-limiting", "led-protection", "basic-circuit"]
    }
]
