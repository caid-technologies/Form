# Hardware Intermediate Representation

Forma’s **Hardware Intermediate Representation** is a typed, versioned JSON schema built with Pydantic. It is the single source of truth for generated projects and is intentionally structured for validation, UI rendering, and future export formats.

## Why a typed Intermediate Representation?
- **Consistency:** Every agent writes into the same schema.
- **Validation-ready:** Rules can reason about pins, nets, and voltages.
- **UI-friendly:** The React Flow canvas can render nodes/edges directly.
- **Diffable:** Changes across versions are explicit and comparable.

## Top-level structure
The core schema lives in `forma_core/workspaces/projects/models.py` and includes:

- **hardware_ir_version** – schema version string.
- **overview** – `ProjectOverview` with title, description, difficulty, category.
- **requirements** – `FunctionalRequirements` with power, constraints, safety notes.
- **system_architecture** – purpose-driven `SystemArchitecture` tree of discipline and nested subsystem nodes.
- **part_definitions** – shared, source-agnostic `PartDefinition` identity, specifications, pinout, dimensions, datasheet, sourcing, and unit-price records.
- **components** – physical `ComponentInstance` occurrences; every record has one unique reference designator and points to a part definition.
- **bom** – deterministic `BOMLineItem` aggregation over physical instance references.
- **nets** – list of `ConnectionNet` objects (netlist connections).
- **buses** – `BusConnection` definitions (I2C/SPI/UART groups).
- **pin_mappings** – `PinMappingEntry` for MCU signal mapping.
- **assembly** – ordered `AssemblyStep` list.
- **mechanical** – `MechanicalNotes` for enclosure and fabrication.
- **constraints** – extra design constraints and notes.
- **power_rails** – summarized `PowerRail` entries.
- **estimated_current_draw_ma** – rough peak current estimate.
- **fabrication_notes** – free-form manufacturing notes.
- **validation** – `ValidationSummary` with categorized issues.
- **is_valid** – boolean status after validation.

Additional fields commonly populated at runtime:
- **assembly_metadata** – backend-populated metadata such as generation timestamp, resolved model name, and render stats.
- **mechanical.render_dimensions** – overall envelope dimensions used by the 3D viewer.
- **mechanical.component_placements** – per-component placement records for the 3D viewer.
- **mechanical.spatial_relationships** – helpful offsets/alignment relationships.
- **mechanical.motion_intents** – agent-authored motion intent keyed by component references. Rigid revolute/prismatic intent is resolved into OpenCAD kinematic joints during CAD generation; `cad_model.kinematics` stores OpenCAD-evaluated pose tracks for MECH playback. Compliant intent remains separate from the rigid-joint contract.
- **mechanical.mechanism_benchmark** – optional bounded additive-mechanism benchmark configuration for the print-in-place captive hinge or monolithic flexure hinge. Generated CAD records manufacturing metadata in `cad_model.mechanism`; flexure visualization uses a separate non-structural `cad_model.compliant_preview` contract.
- **cad_model.assembly_tree** – OpenCAD's semantic `AssemblyTree` serialized after Forma's system hierarchy and component identities are bound to generated geometry. This is distinct from OpenCAD's parametric feature tree: it describes what the product is made of, not the sequence of CAD operations.
- **cad_model.assembly_tree_path** – path to the versioned OpenCAD assembly snapshot emitted with the CAD artifacts. Forma remains the source of semantic hierarchy while OpenCAD owns the canonical assembly object and serialization contract.

## Key relationships
- **SystemArchitecture → SystemNode:** The complete product nests electrical, mechanical, firmware, and more specific systems. Each node records why it exists, its responsibilities, interfaces, abstract component roles, and detail owner.
- **ComponentInstance → PartDefinition:** Each physical occurrence references shared part identity and pin data by `part_definition_id`.
- **BOMLineItem → ComponentInstance:** Each procurement row aggregates `instance_refs`; its quantity must equal the number of those references.
- **ConnectionNet → PinReference:** Nets reference component pins by `ref_des` + `pin_id`.
- **BusConnection → ConnectionNet:** Buses group nets for higher-level comms.
- **ValidationSummary → ValidationIssue:** Structured diagnostics live inside the IR.

## Validation-aware generation
The IR is produced in a loop:
1. Agents generate components and nets.
2. Rule-based validation runs on the netlist.
3. Critical issues trigger a wiring repair step.
4. Validation results are embedded back into the IR.

This makes the IR more than a snapshot—it’s a record of what was checked and why the design is considered safe within MVP scope.

## Deterministic wiring compilation

Wiring agents do not author canonical `ConnectionNet` objects or duplicate `pin_mappings`. They receive a compact
endpoint catalog keyed by stable IDs such as `U1.GPIO21` and return `WiringIntent` objects containing endpoint IDs.
Application code resolves those IDs, rejects unknown or conflicting endpoints, assigns canonical net IDs, and derives
MCU `pin_mappings` from the compiled nets. A repair response may replace a named rejected net with `replace_net_id`;
unrelated valid nets remain unchanged. Power rails with explicit source/input semantics are checked separately from
signal connectivity, so equal nominal voltage alone does not establish a valid power source.

## Hardware Intermediate Representation 0.2 migration

Hardware Intermediate Representation 0.2 separates physical design state from procurement aggregation. Repeated parts are represented as independently addressable instances (`M1`, `M2`, `M3`, `M4`), while the BOM can still contain one row with quantity four. Nets and mechanical placements always target a physical instance, and validation rejects duplicate or unknown references, unknown pins when a pinout is defined, mismatched BOM quantities, and non-deterministic extended prices.

The validator reads 0.1 quantity-bearing component records and expands them deterministically. Shared legacy identity and pin fields are moved into `part_definitions`, BOM rows are derived, and the serialized result is emitted as 0.2. Runtime compatibility properties remain available to existing Python consumers during the transition, but new JSON must not use aggregate component quantities.

## Image inputs
When `image_data` is provided to `POST /api/generate`, the backend uploads the reference image to Supabase Storage when the Supabase service-role/secret key is configured and `FORMA_DEVELOPMENT_MODE` is not enabled, then records `assembly_metadata.reference_image_url`, `reference_image_s3_bucket`, and `reference_image_s3_key`. If storage is not configured or `FORMA_DEVELOPMENT_MODE=true`, it falls back to `assembly_metadata.reference_image_data`.

When image output is requested, the backend records `assembly_metadata.image_output_status` as `succeeded` or `failed`. It also records structured operation entries in `assembly_metadata.operation_statuses`, including `image_generation` and, when applicable, `image_storage`. On success, it uploads the generated product concept image to Supabase Storage when the Supabase service-role/secret key is configured and `FORMA_DEVELOPMENT_MODE` is not enabled, then records `assembly_metadata.product_image_url`, `product_image_s3_bucket`, and `product_image_s3_key` along with `product_image_provider`, `product_image_model`, and `product_image_size`. In development mode, the product image stays inline in the SQLite project record. If the image model is unavailable, misconfigured, returns no image, or errors, the job still keeps the hardware intermediate representation and records `assembly_metadata.image_output_error`, `image_output_error_type`, and `product_image_error`.
