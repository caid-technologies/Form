"""Typed lifecycle state for progressively expensive project representations.

The canonical system topology remains ``HardwareIntermediateRepresentation.system_architecture``.  This
module tracks the derived representations (visuals, geometry, CAD, exports),
their source fingerprints, and the visual approval gate.  The serialized state
is currently embedded in ``HardwareIntermediateRepresentation.assembly_metadata`` so existing 0.2 IR
consumers remain compatible while the lifecycle itself stays strongly typed at
all mutation boundaries.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, SystemArchitecture, SystemNode


DESIGN_LIFECYCLE_METADATA_KEY = "design_lifecycle"
DESIGN_LIFECYCLE_SCHEMA_VERSION = "1.0"


class CostClass(str, Enum):
    """Coarse resource class used before exact provider costs are known."""

    NEGLIGIBLE = "negligible"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class FailureRisk(str, Enum):
    """Expected probability that an operation needs repair or regeneration."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Reversibility(str, Enum):
    """How cheaply the result can be replaced after downstream work exists."""

    CHEAP = "cheap"
    MODERATE = "moderate"
    EXPENSIVE = "expensive"


class FidelityLevel(str, Enum):
    """Increasingly expensive levels of design fidelity."""

    SEMANTIC = "semantic"
    LOGICAL = "logical"
    VISUAL = "visual"
    GEOMETRY = "geometry"
    COMPONENT_CAD = "component_cad"
    ASSEMBLY_CAD = "assembly_cad"
    MANUFACTURING = "manufacturing"


class OperationCost(BaseModel):
    """Planner-visible cost estimate for one operation or pipeline stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latency: CostClass = CostClass.LOW
    compute: CostClass = CostClass.LOW
    monetary: CostClass = CostClass.NEGLIGIBLE
    failure_risk: FailureRisk = FailureRisk.LOW
    reversibility: Reversibility = Reversibility.CHEAP
    estimated_monetary_cost_usd: Optional[float] = Field(default=None, ge=0.0)


class RepresentationKind(str, Enum):
    """Durable representation derived from the canonical system topology."""

    TOPOLOGY = "topology"
    SYSTEM_IMAGE = "system_image"
    SYSTEM_RENDER = "system_render"
    GEOMETRY_SPEC = "geometry_spec"
    COMPONENT_CAD = "component_cad"
    ASSEMBLY_CAD = "assembly_cad"
    MANUFACTURING = "manufacturing"


class RepresentationStatus(str, Enum):
    """Lifecycle status for an independently cacheable representation."""

    PENDING = "pending"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"


class VisualApprovalPolicy(str, Enum):
    """Continuation policy after the whole-system visual is available."""

    STOP_BEFORE_CAD = "stop_before_cad"
    REQUIRE_APPROVAL = "require_approval"
    AUTO_APPROVE_VISUAL = "auto_approve_visual"


class VisualApprovalStatus(str, Enum):
    """State of the whole-system visual gate."""

    NOT_REQUESTED = "not_requested"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class ChangeKind(str, Enum):
    """High-level user/agent change used to select the cheapest useful layer."""

    ARCHITECTURE = "architecture"
    COMPONENT = "component"
    AESTHETIC = "aesthetic"
    DIMENSION = "dimension"
    ASSEMBLY = "assembly"
    MANUFACTURING = "manufacturing"


class DesignRepresentation(BaseModel):
    """One independently reusable and invalidatable design artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    node_id: str
    kind: RepresentationKind
    fidelity: FidelityLevel
    source_fingerprint: str
    status: RepresentationStatus = RepresentationStatus.READY
    uri: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)
    cost: OperationCost = Field(default_factory=OperationCost)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VisualApprovalGate(BaseModel):
    """Persisted gate separating concept validation from expensive CAD work."""

    model_config = ConfigDict(extra="forbid")

    policy: VisualApprovalPolicy = VisualApprovalPolicy.AUTO_APPROVE_VISUAL
    status: VisualApprovalStatus = VisualApprovalStatus.NOT_REQUESTED
    visual_artifact_id: Optional[str] = None
    visual_fingerprint: Optional[str] = None
    feedback: Optional[str] = None


class DesignLifecycleState(BaseModel):
    """Typed state machine for progressively expensive project artifacts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = DESIGN_LIFECYCLE_SCHEMA_VERSION
    topology_fingerprint: Optional[str] = None
    representations: list[DesignRepresentation] = Field(default_factory=list)
    visual_gate: VisualApprovalGate = Field(default_factory=VisualApprovalGate)

    def by_id(self) -> dict[str, DesignRepresentation]:
        """Return representation lookup keyed by stable artifact ID."""

        return {item.artifact_id: item for item in self.representations}

    def ready(
        self,
        *,
        node_id: Optional[str] = None,
        kind: Optional[RepresentationKind] = None,
    ) -> list[DesignRepresentation]:
        """Return ready representations matching optional node/kind filters."""

        return [
            item
            for item in self.representations
            if item.status == RepresentationStatus.READY
            and (node_id is None or item.node_id == node_id)
            and (kind is None or item.kind == kind)
        ]


_STAGE_COSTS: dict[str, tuple[FidelityLevel, OperationCost]] = {
    "intent_parser": (
        FidelityLevel.SEMANTIC,
        OperationCost(),
    ),
    "requirements": (
        FidelityLevel.SEMANTIC,
        OperationCost(),
    ),
    "system_architecture": (
        FidelityLevel.SEMANTIC,
        OperationCost(latency=CostClass.LOW, compute=CostClass.LOW),
    ),
    "component_selection": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM),
    ),
    "web_architect": (
        FidelityLevel.SEMANTIC,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM, monetary=CostClass.LOW),
    ),
    "web_component_sourcing": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM, monetary=CostClass.LOW),
    ),
    "wiring_netlist": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM, failure_risk=FailureRisk.MEDIUM),
    ),
    "validation_repair": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM, failure_risk=FailureRisk.MEDIUM),
    ),
    "bom": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.NEGLIGIBLE, compute=CostClass.NEGLIGIBLE),
    ),
    "mechanical_fabrication": (
        FidelityLevel.GEOMETRY,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM, failure_risk=FailureRisk.MEDIUM),
    ),
    "node_visuals": (
        FidelityLevel.VISUAL,
        OperationCost(
            latency=CostClass.HIGH,
            compute=CostClass.HIGH,
            monetary=CostClass.MEDIUM,
            failure_risk=FailureRisk.MEDIUM,
            reversibility=Reversibility.MODERATE,
        ),
    ),
    "system_visual": (
        FidelityLevel.VISUAL,
        OperationCost(
            latency=CostClass.HIGH,
            compute=CostClass.HIGH,
            monetary=CostClass.MEDIUM,
            failure_risk=FailureRisk.MEDIUM,
            reversibility=Reversibility.MODERATE,
        ),
    ),
    "visual_acceptance": (
        FidelityLevel.VISUAL,
        OperationCost(latency=CostClass.NEGLIGIBLE, compute=CostClass.NEGLIGIBLE),
    ),
    "cad_generation": (
        FidelityLevel.ASSEMBLY_CAD,
        OperationCost(
            latency=CostClass.VERY_HIGH,
            compute=CostClass.VERY_HIGH,
            monetary=CostClass.MEDIUM,
            failure_risk=FailureRisk.HIGH,
            reversibility=Reversibility.EXPENSIVE,
        ),
    ),
    "component_cad": (
        FidelityLevel.COMPONENT_CAD,
        OperationCost(
            latency=CostClass.HIGH,
            compute=CostClass.HIGH,
            monetary=CostClass.MEDIUM,
            failure_risk=FailureRisk.HIGH,
            reversibility=Reversibility.MODERATE,
        ),
    ),
    "assembly_cad": (
        FidelityLevel.ASSEMBLY_CAD,
        OperationCost(
            latency=CostClass.VERY_HIGH,
            compute=CostClass.VERY_HIGH,
            monetary=CostClass.MEDIUM,
            failure_risk=FailureRisk.HIGH,
            reversibility=Reversibility.EXPENSIVE,
        ),
    ),
    "manufacturing_exports": (
        FidelityLevel.MANUFACTURING,
        OperationCost(
            latency=CostClass.HIGH,
            compute=CostClass.HIGH,
            monetary=CostClass.LOW,
            failure_risk=FailureRisk.MEDIUM,
            reversibility=Reversibility.EXPENSIVE,
        ),
    ),
    "assembly": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.MEDIUM, compute=CostClass.MEDIUM),
    ),
    "package_project": (
        FidelityLevel.LOGICAL,
        OperationCost(latency=CostClass.NEGLIGIBLE, compute=CostClass.NEGLIGIBLE),
    ),
}


_CHANGE_TARGETS: dict[ChangeKind, RepresentationKind] = {
    ChangeKind.ARCHITECTURE: RepresentationKind.TOPOLOGY,
    ChangeKind.COMPONENT: RepresentationKind.TOPOLOGY,
    ChangeKind.AESTHETIC: RepresentationKind.SYSTEM_IMAGE,
    ChangeKind.DIMENSION: RepresentationKind.GEOMETRY_SPEC,
    ChangeKind.ASSEMBLY: RepresentationKind.ASSEMBLY_CAD,
    ChangeKind.MANUFACTURING: RepresentationKind.MANUFACTURING,
}


def stage_cost(stage_id: str) -> OperationCost:
    """Return conservative planner cost metadata for a generation stage."""

    return _STAGE_COSTS.get(str(stage_id), (FidelityLevel.LOGICAL, OperationCost()))[1]


def stage_fidelity(stage_id: str) -> FidelityLevel:
    """Return the representation fidelity produced by a generation stage."""

    return _STAGE_COSTS.get(str(stage_id), (FidelityLevel.LOGICAL, OperationCost()))[0]


def cheapest_representation_for_change(change: ChangeKind | str) -> RepresentationKind:
    """Choose the least expensive representation capable of resolving a change."""

    normalized = change if isinstance(change, ChangeKind) else ChangeKind(str(change))
    return _CHANGE_TARGETS[normalized]


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def walk_system_nodes(architecture: Optional[SystemArchitecture]) -> Iterator[SystemNode]:
    """Yield the canonical architecture tree in deterministic pre-order."""

    if architecture is None:
        return
    pending = [architecture.root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.children))


def system_node_fingerprint(node: SystemNode) -> str:
    """Fingerprint semantic inputs owned by one system node."""

    return _canonical_hash(node.model_dump(mode="json"))


def architecture_fingerprint(architecture: Optional[SystemArchitecture]) -> Optional[str]:
    """Fingerprint the canonical topology or return ``None`` when it is absent."""

    if architecture is None:
        return None
    return _canonical_hash(architecture.model_dump(mode="json"))


def stable_artifact_id(node_id: str, kind: RepresentationKind | str) -> str:
    """Build a stable project-local representation identifier."""

    kind_value = kind.value if isinstance(kind, RepresentationKind) else str(kind)
    node = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(node_id).strip()).strip("-") or "project"
    return f"design:{node}:{kind_value}"


def load_design_lifecycle(project: HardwareIntermediateRepresentation) -> DesignLifecycleState:
    """Read typed lifecycle state from a HardwareIntermediateRepresentation, tolerating legacy projects."""

    raw = (project.assembly_metadata or {}).get(DESIGN_LIFECYCLE_METADATA_KEY)
    if isinstance(raw, Mapping):
        return DesignLifecycleState.model_validate(raw)
    return DesignLifecycleState()


def save_design_lifecycle(project: HardwareIntermediateRepresentation, state: DesignLifecycleState) -> None:
    """Persist typed lifecycle state without discarding unrelated assembly metadata."""

    project.assembly_metadata = {
        **(project.assembly_metadata or {}),
        DESIGN_LIFECYCLE_METADATA_KEY: state.model_dump(mode="json"),
    }


def bootstrap_design_lifecycle(
    project: HardwareIntermediateRepresentation,
    *,
    policy: VisualApprovalPolicy | str | None = None,
) -> DesignLifecycleState:
    """Synchronize lifecycle state with the current canonical system topology.

    Existing matching topology artifacts remain reusable.  When the topology
    fingerprint changes, dependent representations are marked stale instead of
    being deleted, preserving provenance for debugging and revision history.
    """

    state = load_design_lifecycle(project)
    if policy is not None:
        state.visual_gate.policy = (
            policy if isinstance(policy, VisualApprovalPolicy) else VisualApprovalPolicy(str(policy))
        )
    current_topology = architecture_fingerprint(project.system_architecture)
    prior_topology = state.topology_fingerprint
    state.topology_fingerprint = current_topology

    topology_id = stable_artifact_id("project", RepresentationKind.TOPOLOGY)
    topology = DesignRepresentation(
        artifact_id=topology_id,
        node_id="project",
        kind=RepresentationKind.TOPOLOGY,
        fidelity=FidelityLevel.SEMANTIC,
        source_fingerprint=current_topology or _canonical_hash({"system_architecture": None}),
        status=RepresentationStatus.READY,
        cost=stage_cost("system_architecture"),
        metadata={"system_count": sum(1 for _ in walk_system_nodes(project.system_architecture))},
    )
    upsert_representation(state, topology)

    if prior_topology and current_topology and prior_topology != current_topology:
        invalidate_representations(state, changed_artifact_ids={topology_id}, keep={topology_id})
        state.visual_gate.status = VisualApprovalStatus.NOT_REQUESTED
        state.visual_gate.visual_artifact_id = None
        state.visual_gate.visual_fingerprint = None
        state.visual_gate.feedback = None

    save_design_lifecycle(project, state)
    return state


def upsert_representation(state: DesignLifecycleState, representation: DesignRepresentation) -> None:
    """Insert or replace one representation by stable artifact ID."""

    state.representations = [
        item for item in state.representations if item.artifact_id != representation.artifact_id
    ]
    state.representations.append(representation)


def invalidate_representations(
    state: DesignLifecycleState,
    *,
    changed_artifact_ids: Iterable[str] = (),
    changed_node_ids: Iterable[str] = (),
    keep: Iterable[str] = (),
) -> set[str]:
    """Mark only transitively dependent representations stale.

    Node changes invalidate representations owned by that node.  Artifact
    changes then propagate through explicit ``depends_on`` edges.  Unrelated
    branches remain ready and reusable.
    """

    changed = {str(value) for value in changed_artifact_ids if str(value)}
    changed_nodes = {str(value) for value in changed_node_ids if str(value)}
    kept = {str(value) for value in keep if str(value)}

    for item in state.representations:
        if item.artifact_id not in kept and item.node_id in changed_nodes:
            changed.add(item.artifact_id)

    invalidated = {artifact_id for artifact_id in changed if artifact_id not in kept}
    propagated = True
    while propagated:
        propagated = False
        for item in state.representations:
            if item.artifact_id in kept or item.artifact_id in invalidated:
                continue
            if any(dependency in invalidated for dependency in item.depends_on):
                invalidated.add(item.artifact_id)
                propagated = True

    for item in state.representations:
        if item.artifact_id in invalidated and item.status != RepresentationStatus.FAILED:
            item.status = RepresentationStatus.STALE
    return invalidated


def register_system_visual(
    project: HardwareIntermediateRepresentation,
    *,
    node_id: str,
    source_fingerprint: str,
    uri: Optional[str],
    metadata: Optional[dict[str, Any]] = None,
) -> DesignRepresentation:
    """Register one subsystem/object visual against the topology artifact."""

    state = load_design_lifecycle(project)
    topology_id = stable_artifact_id("project", RepresentationKind.TOPOLOGY)
    representation = DesignRepresentation(
        artifact_id=stable_artifact_id(node_id, RepresentationKind.SYSTEM_IMAGE),
        node_id=node_id,
        kind=RepresentationKind.SYSTEM_IMAGE,
        fidelity=FidelityLevel.VISUAL,
        source_fingerprint=source_fingerprint,
        status=RepresentationStatus.READY,
        uri=uri,
        depends_on=[topology_id],
        cost=stage_cost("node_visuals"),
        metadata=metadata or {},
    )
    upsert_representation(state, representation)
    save_design_lifecycle(project, state)
    return representation


def register_system_render(
    project: HardwareIntermediateRepresentation,
    *,
    source_fingerprint: str,
    uri: Optional[str],
    depends_on: Iterable[str] = (),
    metadata: Optional[dict[str, Any]] = None,
) -> DesignRepresentation:
    """Register the whole-system concept render and arm the visual gate."""

    state = load_design_lifecycle(project)
    topology_id = stable_artifact_id("project", RepresentationKind.TOPOLOGY)
    dependencies = list(dict.fromkeys([topology_id, *[str(value) for value in depends_on if str(value)]]))
    representation = DesignRepresentation(
        artifact_id=stable_artifact_id("project", RepresentationKind.SYSTEM_RENDER),
        node_id="project",
        kind=RepresentationKind.SYSTEM_RENDER,
        fidelity=FidelityLevel.VISUAL,
        source_fingerprint=source_fingerprint,
        status=RepresentationStatus.READY,
        uri=uri,
        depends_on=dependencies,
        cost=stage_cost("system_visual"),
        metadata=metadata or {},
    )
    upsert_representation(state, representation)
    state.visual_gate.visual_artifact_id = representation.artifact_id
    state.visual_gate.visual_fingerprint = representation.source_fingerprint
    state.visual_gate.feedback = None
    state.visual_gate.status = (
        VisualApprovalStatus.APPROVED
        if state.visual_gate.policy == VisualApprovalPolicy.AUTO_APPROVE_VISUAL
        else VisualApprovalStatus.AWAITING_APPROVAL
    )
    save_design_lifecycle(project, state)
    return representation


def record_visual_decision(
    project: HardwareIntermediateRepresentation,
    *,
    approved: bool,
    feedback: Optional[str] = None,
) -> DesignLifecycleState:
    """Record a user/agent visual decision and selectively stale downstream work."""

    state = load_design_lifecycle(project)
    if not state.visual_gate.visual_artifact_id:
        raise ValueError("No whole-system visual is available for approval.")
    state.visual_gate.status = (
        VisualApprovalStatus.APPROVED if approved else VisualApprovalStatus.REJECTED
    )
    state.visual_gate.feedback = str(feedback).strip() if feedback else None
    if not approved:
        invalidate_representations(
            state,
            changed_artifact_ids={state.visual_gate.visual_artifact_id},
            keep={state.visual_gate.visual_artifact_id},
        )
    save_design_lifecycle(project, state)
    return state


def cad_may_execute(project: HardwareIntermediateRepresentation) -> bool:
    """Return whether current policy/gate state permits expensive CAD work."""

    state = load_design_lifecycle(project)
    policy = state.visual_gate.policy
    if policy == VisualApprovalPolicy.AUTO_APPROVE_VISUAL:
        return state.visual_gate.status in {
            VisualApprovalStatus.NOT_REQUESTED,
            VisualApprovalStatus.APPROVED,
        }
    if policy == VisualApprovalPolicy.STOP_BEFORE_CAD:
        return False
    return state.visual_gate.status == VisualApprovalStatus.APPROVED


def register_cad_representation(
    project: HardwareIntermediateRepresentation,
    *,
    node_id: str,
    kind: RepresentationKind,
    source_fingerprint: str,
    uri: Optional[str],
    depends_on: Iterable[str],
    metadata: Optional[dict[str, Any]] = None,
) -> DesignRepresentation:
    """Register a component/assembly CAD artifact with explicit dependencies."""

    if kind not in {RepresentationKind.COMPONENT_CAD, RepresentationKind.ASSEMBLY_CAD}:
        raise ValueError("CAD representations must be component_cad or assembly_cad.")
    state = load_design_lifecycle(project)
    representation = DesignRepresentation(
        artifact_id=stable_artifact_id(node_id, kind),
        node_id=node_id,
        kind=kind,
        fidelity=(
            FidelityLevel.COMPONENT_CAD
            if kind == RepresentationKind.COMPONENT_CAD
            else FidelityLevel.ASSEMBLY_CAD
        ),
        source_fingerprint=source_fingerprint,
        status=RepresentationStatus.READY,
        uri=uri,
        depends_on=list(dict.fromkeys(str(value) for value in depends_on if str(value))),
        cost=stage_cost("component_cad" if kind == RepresentationKind.COMPONENT_CAD else "assembly_cad"),
        metadata=metadata or {},
    )
    upsert_representation(state, representation)
    save_design_lifecycle(project, state)
    return representation


def representation_fingerprint(*values: Any) -> str:
    """Fingerprint arbitrary representation inputs using canonical JSON."""

    return _canonical_hash(values)


__all__ = [
    "ChangeKind",
    "CostClass",
    "DESIGN_LIFECYCLE_METADATA_KEY",
    "DESIGN_LIFECYCLE_SCHEMA_VERSION",
    "DesignLifecycleState",
    "DesignRepresentation",
    "FailureRisk",
    "FidelityLevel",
    "OperationCost",
    "RepresentationKind",
    "RepresentationStatus",
    "Reversibility",
    "VisualApprovalGate",
    "VisualApprovalPolicy",
    "VisualApprovalStatus",
    "architecture_fingerprint",
    "bootstrap_design_lifecycle",
    "cad_may_execute",
    "cheapest_representation_for_change",
    "invalidate_representations",
    "load_design_lifecycle",
    "record_visual_decision",
    "register_cad_representation",
    "register_system_render",
    "register_system_visual",
    "representation_fingerprint",
    "save_design_lifecycle",
    "stable_artifact_id",
    "stage_cost",
    "stage_fidelity",
    "system_node_fingerprint",
    "upsert_representation",
    "walk_system_nodes",
]
