# Agents

Forma uses an **ADK-style** multi-agent workflow implemented in `forma_core/agents`. Each agent writes structured artifacts into the Hardware Intermediate Representation.

## Pipeline overview
0. Context clarification → 1. Safety guardrails → 2. Intent Parser → 3. Requirements → 4. System Architecture → 5. Component Selection → 6. Wiring/Netlist (+ repair loop) → 7. BOM → 8. Mechanical/Fabrication → 9. Assembly Instructions → 10. Mechanical render enrichment

## Agent responsibilities

### Context Clarifier Agent

**Input:** Prompt (+ optional reference image)

**Output:** Up to three focused questions before generation

**Goal:** Capture missing human requirements, including the intended system shape, silhouette, or form factor. Shape choices are passed through as explicit requirements instead of allowing downstream agents to assume a rectangular enclosure.

### Safety Guardrail (pre-check)
**Input:** Prompt
**Output:** Either a normal pipeline run, or a safety-blocked Hardware Intermediate Representation
**Goal:** Block high-risk categories early (weapons, medical, mains AC, automotive control, high-power battery packs).

### Intent Parser Agent
**Input:** Prompt (+ optional image)  
**Output:** `ProjectOverview`  
**Goal:** Convert intent into a concise, high-level project summary.

### Requirements Agent
**Input:** Prompt + `ProjectOverview`  
**Output:** `FunctionalRequirements`  
**Goal:** Extract functional requirements, power needs, constraints, and missing info.

### System Architecture Agent
**Input:** Overview + requirements

**Output:** `SystemArchitecture`

**Goal:** Decompose the complete product into a purpose-driven tree such as electrical → power/control/sensing and mechanical → enclosure/mounting. Every node explains why it exists, its interfaces, and which specialist owns its details. Exact parts, nets, and pins are excluded at this level.

### Component Selection Agent
**Input:** Requirements + system tree + compact seed component catalog

**Output:** `ComponentInstance[]`  
**Goal:** Choose compatible parts by system role. Repeated parts are emitted as one physical instance per reference designator; exact catalog pinouts are hydrated deterministically after selection.

### Wiring/Netlist Agent
**Input:** Components + requirements  
**Output:** `ConnectionNet[]` + `PinMappingEntry[]`  
**Goal:** Wire pins into power, ground, and signal nets.

If validation produces CRITICAL issues, the orchestrator runs a one-step **auto-correction** prompt and re-validates.

### BOM Agent
**Input:** Component list  
**Output:** `PartDefinition[]`, `BOMLineItem[]`, and updated `ProjectOverview.estimated_cost`
**Goal:** Store shared part data once, aggregate physical instance references into procurement rows, and calculate deterministic extended and total costs.

### Mechanical/Fabrication Agent
**Input:** Mechanical system branch + pin-free component summaries

**Output:** `MechanicalNotes`  
**Goal:** Preserve the requested physical form and suggest appropriate housing or open-frame structure, mounting, and fabrication details.

The agent may also emit `render_dimensions`, `component_placements`, `spatial_relationships`, and an optional `cad_model` source for the OpenCAD project tab. `cad_model` should identify one canonical model, not a directory or a collection of STEP files.

### Assembly Instruction Agent
**Input:** System tree + pin-free component/net summaries + mechanical notes

**Output:** `AssemblyStep[]`  
**Goal:** Produce step-by-step build instructions with safety flags.

## State transitions
```mermaid
flowchart LR
  A[Prompt] --> B[ProjectOverview]
  B --> C[FunctionalRequirements]
  C --> D[SystemArchitecture tree]
  D --> E[ComponentInstance[]]
  E --> F[ConnectionNet[] + PinMappingEntry[]]
  F --> G[Validation + repair loop]
  G --> H[MechanicalNotes]
  H --> I[AssemblyStep[]]
  I --> J[Hardware Intermediate Representation]
```

## Notes
- Web-research artifact stages run as a dependency graph. Successful outputs are checkpointed independently, failed dependencies block only downstream work, and unrelated stages continue.
- Validation can trigger a **repair loop** that re-invokes the wiring agent.
- If a live LLM provider isn’t configured (or generation fails), the backend uses a deterministic **simulation fallback** backed by the example projects.
- The pipeline is designed to swap models or add agents without rewriting the core IR schema.
- External agents can call or listen to Forma through the A2A layer documented in `docs/a2a.md`.
- Specialized worker execution boundaries use the versioned contracts and capability registry documented in `docs/worker-contracts.md`.
- Prompt context is projected by ownership: only wiring and electrical validation receive physical pins; architecture, mechanical, and assembly agents receive compact system-level views.
- Dependency-aware concurrent execution and restart recovery are documented in `docs/worker-orchestration.md`.
- Frozen-brief generation and canonical revision persistence are documented in `docs/generation-worker.md`.
- Domain-scoped context sharing, field allowlists, and handoff receipts are documented in `docs/context-governance.md`.
