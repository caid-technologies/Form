from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, Field

from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    PinMappingEntry,
    PinReference,
    ValidationIssue,
)


class EndpointCatalogEntry(BaseModel):
    """A stable, model-facing identifier for one declared component pin."""

    endpoint_id: str
    ref_des: str
    pin_id: str
    component_name: str
    component_category: str
    pin_name: str
    pin_type: str
    voltage: Optional[float] = None
    direction: Optional[str] = None
    power_role: Optional[str] = None
    interface: Optional[str] = None


class NetIntent(BaseModel):
    """A requested connection expressed only in catalog endpoint IDs."""

    name: str
    net_type: str
    voltage: Optional[float] = None
    endpoint_ids: List[str] = Field(default_factory=list)
    replace_net_id: Optional[str] = None


class WiringIntent(BaseModel):
    nets: List[NetIntent] = Field(default_factory=list)


class RejectedNetIntent(BaseModel):
    intent_index: int
    intent: NetIntent
    issues: List[ValidationIssue] = Field(default_factory=list)


class WiringCompilationResult(BaseModel):
    nets: List[ConnectionNet] = Field(default_factory=list)
    preserved_nets: List[ConnectionNet] = Field(default_factory=list)
    rejected: List[RejectedNetIntent] = Field(default_factory=list)

    @property
    def all_nets(self) -> List[ConnectionNet]:
        """Return preserved nets followed by newly compiled or replaced nets."""
        return [*self.preserved_nets, *self.nets]

    @property
    def issues(self) -> List[ValidationIssue]:
        return [issue for rejected in self.rejected for issue in rejected.issues]


def make_endpoint_id(ref_des: str, pin_id: str) -> str:
    return f"{ref_des}.{pin_id}"


def build_endpoint_catalog(components: Sequence[ComponentInstance]) -> Dict[str, EndpointCatalogEntry]:
    """Build the sole endpoint vocabulary exposed to a wiring model."""
    catalog: Dict[str, EndpointCatalogEntry] = {}
    for component in components:
        for pin in component.pins:
            endpoint_id = make_endpoint_id(component.ref_des, pin.pin_id)
            catalog[endpoint_id] = EndpointCatalogEntry(
                endpoint_id=endpoint_id,
                ref_des=component.ref_des,
                pin_id=pin.pin_id,
                component_name=component.name,
                component_category=component.category,
                pin_name=pin.name,
                pin_type=pin.pin_type,
                voltage=pin.voltage,
                direction=pin.direction,
                power_role=pin.power_role,
                interface=pin.interface,
            )
    return dict(sorted(catalog.items()))


def endpoint_catalog_prompt(catalog: Dict[str, EndpointCatalogEntry]) -> Dict[str, dict]:
    """Return a compact JSON-safe catalog without duplicating endpoint identity."""
    return {
        endpoint_id: entry.model_dump(exclude={"endpoint_id"}, exclude_none=True)
        for endpoint_id, entry in catalog.items()
    }


def compatible_endpoint_candidates(
    intent: NetIntent,
    catalog: Dict[str, EndpointCatalogEntry],
    *,
    used_endpoint_ids: Iterable[str] = (),
) -> List[str]:
    """Narrow repair choices using known voltage, interface, and pin-use constraints."""
    used = set(used_endpoint_ids)
    requested_interface = intent.net_type.strip().lower()
    candidates: List[str] = []
    for endpoint_id, entry in catalog.items():
        if endpoint_id in used and not _is_shareable_across_nets(entry):
            continue
        if intent.voltage is not None and entry.voltage is not None:
            if abs(intent.voltage - entry.voltage) > 0.5:
                continue
        if entry.interface and requested_interface in {"i2c", "spi", "uart", "pwm", "analog"}:
            if entry.interface.lower() != requested_interface:
                continue
        candidates.append(endpoint_id)
    return candidates


def _validation_issue(category: str, description: str, troubleshooting: str) -> ValidationIssue:
    return ValidationIssue(
        severity="CRITICAL",
        category=category,
        description=description,
        troubleshooting=troubleshooting,
    )


def _canonical_net_id(name: str, net_type: str, reserved_ids: set[str]) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "_", (name or net_type or "NET").upper()).strip("_") or "NET"
    base = stem if stem.startswith("NET_") else f"NET_{stem}"
    candidate = base
    suffix = 2
    while candidate in reserved_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    reserved_ids.add(candidate)
    return candidate


def _is_shareable_across_nets(entry: EndpointCatalogEntry) -> bool:
    return entry.pin_type.lower() in {"power", "ground", "passive"}


def _unknown_endpoint_issue(
    endpoint_id: str,
    component_refs: set[str],
) -> ValidationIssue:
    ref_des, separator, pin_id = endpoint_id.partition(".")
    if not separator or ref_des not in component_refs:
        return _validation_issue(
            "Unknown Component Reference",
            f"Endpoint '{endpoint_id}' references an unknown component '{ref_des or endpoint_id}'.",
            "Choose an endpoint ID exactly as it appears in the endpoint catalog.",
        )
    return _validation_issue(
        "Unknown Pin Reference",
        f"Endpoint '{endpoint_id}' references undeclared pin '{pin_id}' on component '{ref_des}'.",
        "Choose a declared pin from the component's endpoint catalog entries.",
    )


def compile_wiring_intent(
    components: Sequence[ComponentInstance],
    wiring: WiringIntent,
    *,
    existing_nets: Sequence[ConnectionNet] = (),
) -> WiringCompilationResult:
    """Compile model intent into canonical nets while rejecting only invalid intents."""
    catalog = build_endpoint_catalog(components)
    component_refs = {component.ref_des for component in components}
    existing_by_id = {net.net_id: net for net in existing_nets}
    reserved_ids = set(existing_by_id)
    endpoint_to_net: Dict[str, str] = {}
    for net in existing_nets:
        for pin in net.pins:
            endpoint_to_net[make_endpoint_id(pin.ref_des, pin.pin_id)] = net.net_id

    compiled: List[ConnectionNet] = []
    preserved = list(existing_nets)
    replaced_net_ids: set[str] = set()
    rejected: List[RejectedNetIntent] = []
    for index, intent in enumerate(wiring.nets):
        intent_issues: List[ValidationIssue] = []
        replacement_id = str(intent.replace_net_id or "").strip() or None
        replacement_net = existing_by_id.get(replacement_id) if replacement_id else None
        if replacement_id and replacement_net is None:
            intent_issues.append(_validation_issue(
                "Unknown Replacement Net",
                f"Wiring intent '{intent.name}' targets unknown net '{replacement_id}'.",
                "Set replace_net_id to an existing canonical net ID or omit it for a new net.",
            ))
        elif replacement_id in replaced_net_ids:
            intent_issues.append(_validation_issue(
                "Duplicate Replacement Net",
                f"Wiring intent '{intent.name}' attempts to replace net '{replacement_id}' more than once.",
                "Return one replacement intent per canonical net.",
            ))
        elif replacement_net is not None:
            for pin in replacement_net.pins:
                endpoint_to_net.pop(make_endpoint_id(pin.ref_des, pin.pin_id), None)
            reserved_ids.discard(replacement_id)

        if not intent.endpoint_ids:
            intent_issues.append(_validation_issue(
                "Empty Net",
                f"Wiring intent '{intent.name}' contains no endpoints.",
                "Select at least one endpoint from the endpoint catalog or remove the net intent.",
            ))

        counts = Counter(intent.endpoint_ids)
        for endpoint_id, count in counts.items():
            if count > 1:
                intent_issues.append(_validation_issue(
                    "Duplicate Endpoint",
                    f"Endpoint '{endpoint_id}' appears {count} times in wiring intent '{intent.name}'.",
                    "List each physical endpoint at most once per net.",
                ))

        entries: List[EndpointCatalogEntry] = []
        unique_endpoint_ids = sorted(set(intent.endpoint_ids))
        for endpoint_id in unique_endpoint_ids:
            entry = catalog.get(endpoint_id)
            if entry is None:
                intent_issues.append(_unknown_endpoint_issue(endpoint_id, component_refs))
                continue
            entries.append(entry)
            previous_net = endpoint_to_net.get(endpoint_id)
            if previous_net and not _is_shareable_across_nets(entry):
                intent_issues.append(_validation_issue(
                    "Pin Conflict",
                    f"Signal endpoint '{endpoint_id}' is already assigned to net '{previous_net}' and cannot also join '{intent.name}'.",
                    "Choose an unused compatible signal endpoint or keep both connections on one shared bus net.",
                ))

        pin_types = {entry.pin_type.lower() for entry in entries}
        if "power" in pin_types and "ground" in pin_types:
            intent_issues.append(_validation_issue(
                "Short Circuit",
                f"Wiring intent '{intent.name}' directly connects power and ground endpoints.",
                "Separate positive power and return endpoints into distinct nets.",
            ))

        power_entries = [entry for entry in entries if entry.pin_type.lower() == "power"]
        if power_entries and all(entry.power_role for entry in power_entries):
            source_entries = [
                entry
                for entry in power_entries
                if entry.power_role in {"source", "regulated_output"}
            ]
            input_entries = [entry for entry in power_entries if entry.power_role == "input"]
            if input_entries and not source_entries:
                intent_issues.append(_validation_issue(
                    "Missing Power Source",
                    f"Power intent '{intent.name}' connects supply inputs without a source or regulated output.",
                    "Add one explicit source or regulated-output endpoint; equal pin voltage is not sufficient.",
                ))
            elif len(source_entries) > 1:
                intent_issues.append(_validation_issue(
                    "Power Source Conflict",
                    f"Power intent '{intent.name}' connects multiple source or regulated-output endpoints.",
                    "Use one explicit source for the rail unless the power-sharing topology is separately validated.",
                ))

        if intent_issues:
            if replacement_net is not None:
                for pin in replacement_net.pins:
                    endpoint_to_net[make_endpoint_id(pin.ref_des, pin.pin_id)] = replacement_net.net_id
            rejected.append(RejectedNetIntent(intent_index=index, intent=intent, issues=intent_issues))
            continue

        net = ConnectionNet(
            net_id=replacement_id or _canonical_net_id(intent.name, intent.net_type, reserved_ids),
            name=intent.name,
            net_type=intent.net_type,
            voltage=intent.voltage,
            pins=[PinReference(ref_des=entry.ref_des, pin_id=entry.pin_id) for entry in entries],
        )
        compiled.append(net)
        reserved_ids.add(net.net_id)
        if replacement_id:
            preserved = [item for item in preserved if item.net_id != replacement_id]
            replaced_net_ids.add(replacement_id)
        for endpoint_id in unique_endpoint_ids:
            endpoint_to_net[endpoint_id] = net.net_id

    return WiringCompilationResult(nets=compiled, preserved_nets=preserved, rejected=rejected)


def derive_pin_mappings(
    components: Sequence[ComponentInstance],
    nets: Iterable[ConnectionNet],
) -> List[PinMappingEntry]:
    """Derive controller signal mappings from canonical nets."""
    component_lookup = {component.ref_des: component for component in components}
    controller_refs = {
        component.ref_des
        for component in components
        if component.category.strip().lower() in {"microcontroller", "mcu", "single-board computer", "sbc"}
    }
    mappings: List[PinMappingEntry] = []
    for net in nets:
        if net.net_type.strip().lower() in {"power", "ground"}:
            continue
        controller_pins = [pin for pin in net.pins if pin.ref_des in controller_refs]
        peers = [pin for pin in net.pins if pin.ref_des not in controller_refs]
        if not peers:
            continue
        connected_to = ", ".join(
            f"{component_lookup[pin.ref_des].name} ({pin.ref_des}).{pin.pin_id}"
            if pin.ref_des in component_lookup
            else f"{pin.ref_des}.{pin.pin_id}"
            for pin in peers
        )
        for controller_pin in controller_pins:
            mappings.append(PinMappingEntry(
                mcu_pin=controller_pin.pin_id,
                connected_to=connected_to,
                net_name=net.net_id,
            ))
    return mappings


_FAILURE_CATEGORIES = {
    "unknown component reference": "unknown_component_reference",
    "unknown pin reference": "unknown_pin_reference",
    "empty net": "empty_net",
    "duplicate endpoint": "duplicate_endpoint",
    "pin conflict": "pin_conflict",
    "unpowered ic": "unpowered_component",
    "voltage mismatch": "voltage_mismatch",
    "short circuit": "short_circuit",
    "missing power source": "missing_power_net",
    "power source conflict": "intent_compilation_failure",
    "unknown replacement net": "intent_compilation_failure",
    "duplicate replacement net": "intent_compilation_failure",
    "repair exhausted": "repair_exhausted",
}


def wiring_failure_category(issue: ValidationIssue) -> str:
    if issue.category.strip().lower() == "unpowered ic":
        if "ground" in issue.description.lower():
            return "missing_ground_net"
        return "missing_power_net"
    return _FAILURE_CATEGORIES.get(issue.category.strip().lower(), "validation_issue")
