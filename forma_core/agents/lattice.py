"""Built-in Lattice agent cards for Forma project namespaces."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forma_core.agents.contracts import LatticeAgentCard, LatticeCapability, LatticeSchemaContract
from forma_core.workspaces.projects.context_governance import DEFAULT_CONTEXT_GOVERNANCE_POLICY
from forma_core.workspaces.projects.objects import ProjectNamespaceDescriptor, list_project_namespaces


class NamespaceAgentQuestion(BaseModel):
    namespace: str = Field(..., description="Target Forma namespace, such as product.mech or product.bom.")
    prompt: str = Field(..., description="User request or upstream-agent instruction for this namespace.")
    project_context: dict[str, Any] = Field(default_factory=dict, description="Relevant project context.")
    current_payload: dict[str, Any] = Field(default_factory=dict, description="Existing namespace payload, if any.")
    constraints: list[str] = Field(default_factory=list, description="Constraints the namespace agent must preserve.")


class NamespaceAgentResult(BaseModel):
    namespace: str = Field(..., description="Namespace this result updates.")
    summary: str = Field(..., description="Short explanation of the proposed namespace update.")
    payload_patch: dict[str, Any] = Field(default_factory=dict, description="Structured patch or replacement payload.")
    dependencies: list[str] = Field(default_factory=list, description="Other namespaces that should be checked.")
    validation_notes: list[str] = Field(default_factory=list, description="Checks, risks, or review notes.")
    handoff_actions: list[dict[str, Any]] = Field(default_factory=list, description="Suggested downstream actions.")


NamespaceAgentKind = Literal[
    "meta",
    "docs",
    "history",
    "overview",
    "architecture",
    "electrical",
    "bom",
    "mech",
    "firmware",
    "visuals",
    "validation",
    "assembly",
]


NAMESPACE_AGENT_PROFILES: dict[str, dict[str, Any]] = {
    "project.meta": {
        "kind": "meta",
        "summary": "Owns project identity, runtime metadata, source usage, and workspace-level state.",
        "inputs": ["project_id", "runtime_metadata", "source_usage", "workflow_state"],
        "outputs": ["project_metadata", "source_usage", "revision_metadata"],
        "tools": ["job metadata", "source usage", "runtime debug config"],
        "handoffs": ["forma.debug_config", "forma.a2a.get_job", "forma.a2a.list_jobs"],
    },
    "project.docs": {
        "kind": "docs",
        "summary": "Owns build documentation, human-readable guidance, validation reports, and release notes.",
        "inputs": ["hardware_ir", "assembly_steps", "validation_notes", "fabrication_notes"],
        "outputs": ["build_docs", "release_notes", "review_summary"],
        "tools": ["documentation generation", "schema validation"],
        "handoffs": ["forma.lattice.list_agents"],
    },
    "project.history": {
        "kind": "history",
        "summary": "Owns revision lineage, iteration decisions, and namespace change history.",
        "inputs": ["revision", "source_project_id", "iteration_request", "namespace_versions"],
        "outputs": ["version_history", "decision_log", "namespace_revision_map"],
        "tools": ["job metadata", "audit log"],
        "handoffs": ["forma.a2a.get_job", "forma.a2a.list_jobs"],
    },
    "product.overview": {
        "kind": "overview",
        "summary": "Owns product intent, requirements, constraints, cost target, and top-level project description.",
        "inputs": ["prompt", "user_goal", "requirements", "constraints"],
        "outputs": ["overview", "functional_requirements", "missing_info"],
        "tools": ["clarifying questions", "schema validation"],
        "handoffs": ["forma.generate_project"],
    },
    "product.architecture": {
        "kind": "architecture",
        "summary": "Owns the purpose-driven system hierarchy and routes scoped context to discipline specialists.",
        "inputs": ["overview", "requirements", "constraints"],
        "outputs": ["system_architecture", "system_interfaces", "detail_owners"],
        "tools": ["system decomposition", "schema validation", "dependency mapping"],
        "handoffs": ["forma.lattice.get_agent_card", "forma.lattice.list_agents"],
    },
    "product.electrical": {
        "kind": "electrical",
        "summary": "Owns electrical architecture, components, nets, buses, pin mappings, and power rails.",
        "inputs": ["requirements", "component_catalog", "selected_components", "nets"],
        "outputs": ["components", "nets", "buses", "pin_mappings", "power_rails"],
        "tools": ["component catalog", "circuit validation", "datasheet lookup"],
        "handoffs": ["forma.validate_circuit"],
    },
    "product.bom": {
        "kind": "bom",
        "summary": "Owns bill of materials, quantities, sourcing records, substitutions, and cost rollups.",
        "inputs": ["components", "mechanical_sources", "sourcing_constraints", "budget"],
        "outputs": ["bom_items", "estimated_cost", "sourcing_risks", "substitutions"],
        "tools": ["component catalog", "external sourcing", "cost estimation"],
        "handoffs": ["forma.lattice.get_agent_card", "forma.lattice.list_agents"],
    },
    "product.mech": {
        "kind": "mech",
        "summary": "Owns enclosure, CAD/fabrication sources, component placement, dimensions, and mechanical constraints.",
        "inputs": ["components", "dimensions", "mounting_constraints", "fabrication_process"],
        "outputs": ["mechanical_notes", "component_placements", "spatial_relationships", "fabrication_details"],
        "tools": ["CAD search", "mechanical fit checks", "fabrication estimation"],
        "handoffs": ["forma.lattice.get_agent_card"],
    },
    "product.firmware": {
        "kind": "firmware",
        "summary": "Owns firmware notes, pin behavior, control logic, embedded interfaces, and software assumptions.",
        "inputs": ["pin_mappings", "requirements", "control_logic", "interfaces"],
        "outputs": ["firmware_notes", "pin_behavior", "control_flow", "embedded_risks"],
        "tools": ["firmware generation", "pin validation", "protocol docs"],
        "handoffs": ["forma.validate_circuit"],
    },
    "product.visuals": {
        "kind": "visuals",
        "summary": "Owns generated product imagery, render metadata, visual sequences, and presentation assets.",
        "inputs": ["hardware_ir", "mechanical_placements", "visual_style", "render_constraints"],
        "outputs": ["visual_spec", "render_metadata", "image_prompts", "video_prompts"],
        "tools": ["image generation", "video review", "render validation"],
        "handoffs": ["forma.generate_project"],
    },
    "product.validation": {
        "kind": "validation",
        "summary": "Owns circuit validation, safety checks, operation status, and review gates.",
        "inputs": ["components", "nets", "mechanical_notes", "assembly_steps"],
        "outputs": ["validation_summary", "safety_notes", "blocking_issues", "review_gates"],
        "tools": ["circuit validation", "safety review", "schema validation"],
        "handoffs": ["forma.validate_circuit"],
    },
    "product.assembly": {
        "kind": "assembly",
        "summary": "Owns step-by-step physical assembly, build workflow, and builder-facing sequencing.",
        "inputs": ["components", "nets", "mechanical_notes", "validation_summary"],
        "outputs": ["assembly_steps", "build_sequence", "danger_flags", "affected_components"],
        "tools": ["build planning", "safety review", "documentation generation"],
        "handoffs": ["forma.lattice.get_agent_card"],
    },
}


def namespace_agent_id(namespace: str) -> str:
    return namespace.strip().lower()


def namespace_action(namespace: str) -> str:
    return f"{namespace_agent_id(namespace)}.update"


def namespace_contract_id(namespace: str) -> str:
    return f"{namespace_agent_id(namespace)}.v0"


def namespace_agent_card(descriptor: ProjectNamespaceDescriptor) -> LatticeAgentCard:
    profile = NAMESPACE_AGENT_PROFILES.get(descriptor.name, {})
    agent_id = namespace_agent_id(descriptor.name)
    action = namespace_action(descriptor.name)
    contract = LatticeSchemaContract.from_models(
        id=namespace_contract_id(descriptor.name),
        name=f"{descriptor.label} Contract",
        purpose=f"Update and audit the {descriptor.name} namespace: {descriptor.description}",
        input_model=NamespaceAgentQuestion,
        output_model=NamespaceAgentResult,
        extraction_prompt=(
            f"Extract only the information needed to update {descriptor.name}. Preserve adjacent namespaces "
            "unless a dependency note explains why another agent should be consulted."
        ),
        metadata={
            "namespace": descriptor.name,
            "scope": descriptor.scope,
            "agent_kind": profile.get("kind", descriptor.name.rsplit(".", 1)[-1]),
        },
    )

    capability = LatticeCapability(
        id=action,
        label=f"{descriptor.label} Agent",
        description=profile.get("summary", descriptor.description),
        inputs=profile.get("inputs", ["project_context", "current_payload", "constraints"]),
        outputs=profile.get("outputs", ["payload_patch", "validation_notes", "handoff_actions"]),
        actions=[action, "forma.lattice.get_agent_card"],
    )

    return LatticeAgentCard(
        agent_id=agent_id,
        namespace=descriptor.name,
        name=f"{descriptor.label} Agent",
        version="0.1.0",
        domain=descriptor.description,
        summary=profile.get("summary", descriptor.description),
        capabilities=[capability],
        contracts=[contract],
        runtime_boundary=(
            f"{descriptor.label} owns the {descriptor.name} namespace contract; Forma owns orchestration, "
            "provider routing, validation, persistence, and cross-namespace coordination."
        ),
        tools_needed=profile.get("tools", ["schema validation", "project object inspection"]),
        handoff_actions=profile.get("handoffs", ["forma.lattice.list_agents"]),
        safety_limits=[
            "Return namespace-scoped updates only.",
            "Declare dependencies instead of silently rewriting unrelated namespaces.",
            "Require human review for safety-critical, costly, or irreversible changes.",
        ],
        human_review_triggers=[
            "safety-critical issue",
            "cross-namespace dependency",
            "irreversible fabrication or procurement decision",
            "missing source data",
        ],
        tags=["lattice", "namespace-agent", descriptor.scope, descriptor.name],
        metadata={
            "namespace": descriptor.name,
            "scope": descriptor.scope,
            "agent_kind": profile.get("kind", descriptor.name.rsplit(".", 1)[-1]),
            "context_governance": DEFAULT_CONTEXT_GOVERNANCE_POLICY.recipient_manifest(descriptor.name),
        },
    )


def default_namespace_agent_cards() -> list[LatticeAgentCard]:
    return [namespace_agent_card(descriptor) for descriptor in list_project_namespaces()]


__all__ = [
    "NAMESPACE_AGENT_PROFILES",
    "NamespaceAgentKind",
    "NamespaceAgentQuestion",
    "NamespaceAgentResult",
    "default_namespace_agent_cards",
    "namespace_action",
    "namespace_agent_card",
    "namespace_agent_id",
    "namespace_contract_id",
]
