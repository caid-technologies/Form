# Supported project foundation

Caid's foundation uses four required principles: inspectable artifacts,
deterministic boundaries, human authority, and open interfaces. These contracts
apply to the ordinary hardware workflow. The implementation adds no application
domains and requires no second generation architecture.

## Artifact contract

`forma-project` version 1 retains its project identity, typed HardwareIR, and
project-relative artifact declarations. Each deliverable declares SHA-256, media
type and optional byte size. A portable native build contains the authored
HardwareIR, generated OpenCAD source, editable feature tree, STEP, STL and
available OBJ/3MF exports. JSON remains readable without a running service.

`forma.project-inspection` version 1 is a recomputed report, distinct from the
design. It records schema and electrical checks, artifact integrity, geometry
input binding, supported native inspection, assumptions, limitations and review
status. Unknown or stale evidence cannot become a passing check. A valid report
does not establish manufacturing readiness or physical performance.

The project and inspection JSON Schemas are available through:

```sh
forma-core project schema --kind manifest
forma-core project schema --kind inspection
```

The same contracts are published as standalone
[portable project](schemas/forma-project-v1.schema.json),
[inspection](schemas/forma-project-inspection-v1.schema.json), and
[review declaration](schemas/forma-engineering-review-v1.schema.json) schemas.
The portable schema includes typed HardwareIR and required artifact integrity
metadata. The legacy import reader continues to accept older declarations so
they can be inspected and upgraded explicitly.

Existing version-1 projects and raw HardwareIR documents remain readable.
Unsupported wrapper versions are rejected. Older geometry without the new input
binding or portable source evidence is reported as unverified; recompile its
typed CAD operations to establish fresh evidence. Imported data never executes
bundled Python source.

## Normal local workflow

Use the supported Form/Open-CAD version pair from the release verification
record. Python 3.12 is used by the release gate. Install native dependencies
explicitly before compilation; a compile never installs software automatically.
For development, install Form and Open-CAD from their checkouts:

```sh
python -m venv .venv
# Activate .venv using the command for your shell.
python -m pip install -c scripts/quality/foundation-constraints.txt -e ".[backend,dev]" "../Open-CAD/packages/opencad[occt]"
python .agents/skills/forma-hardware/scripts/cad.py check
forma-core project compile examples/foundation/plate.json --directory output/plate
forma-core project inspect output/plate
forma-core project export output/plate --bundle output/plate.zip
forma-core project reopen output/plate.zip --directory output/reopened
```

Compilation uses explicit typed `mechanical.cad_operations` and the shared native
boundary used by MCP. It writes into a new directory, preserving the previous
design on failure. Inspection and export support the existing HardwareIR project
contract, including electrical designs. Export includes failed engineering checks
when the design and artifact bytes remain inspectable; artifact corruption blocks
export. Reopen verifies the entire archive before creating a project directory.

Edit `project_ir` in `forma-project.json`, then compile into another new directory.
Changed geometry inputs invalidate prior CAD evidence; changing the project or
artifact declarations invalidates the old review. Reopening a ZIP preserves exact
artifact bytes and project identity. An inspection always recomputes current
checks instead of trusting the archived report.

## Human authority

Generation and validation never grant approval. An engineer can record a local
declaration against the exact inspected project:

```sh
forma-core project review output/plate --decision approved --reviewer "Your name" --note "Scope and limitations of the review"
```

The portable receipt explicitly says `declaration_not_authenticated`; a caller
can edit a file or type a name. Applications that need authenticated approval
must bind it to their authenticated user and exact revision. This format does
not claim identity verification, certification, fabrication approval, or a
physical test. Agent-facing MCP tools expose inspection and contracts; they do
not expose a tool for awarding human approval.

## Shared interfaces

Python consumers use `compile_project`, `inspect_project`, and
`record_project_review` from `forma_core.workspaces.projects.inspection`.
`export_project_bundle`, `import_project_bundle`, and `local_artifact_reader` live
in `forma_core.workspaces.projects.portable`. Hosted consumers supply an
authorized byte resolver; hosted tools never accept arbitrary local paths.

The restricted session MCP exposes `forma.opencode.project_contract` and
`forma.opencode.inspect_project`, alongside existing read/update/compile/validate
tools. The application and MCP retain their owner and revision guards. Local
`forma-oss status` and `forma-core validate` use the same deterministic inspector.
The component library reuses existing database and private artifact storage;
see [its contract and deployment migration](component-asset-library.md).

Authenticated application clients use
`GET /projects/{project_id}/foundation/inspection` and
`GET /projects/{project_id}/foundation/bundle`. Both accept an optional exact
`revision_id`, require project ownership, and use the same canonical adapter as
MCP. A bundle download does not discover or include files from a newer revision.
Native builds retain source and feature-tree bytes in durable private storage,
so removing a worker's temporary directory does not erase the editable source.

## Release gates

The `Supported project foundation` workflow builds the distribution and installs
the wheel with a pinned Open-CAD commit, then runs outside the source checkout.
It checks native create/compile/inspect/review/export/reopen/edit/recompile,
stale review detection, actual exported dimensions, component reuse, integrity
failures, SDK/CLI/MCP contracts, and the full existing test suite on Linux and
Windows. Built artifacts and acceptance evidence are retained together.

A supported release requires successful checks for both repositories at their
exact proposed commits. Missing native dependencies, skipped native acceptance,
baseline test failures, and incomplete migrations block release. Record the
tested version pair and deployment prerequisites in the release notes before
publishing or changing the hosted default. See `foundation-verification.md` for
the current executed evidence.
