# AI-assisted CAD migrations

Forma now provides a schema, deterministic migration planner, and native rebuild
package generator through `forma-core cad-migrate`. The four external CAD routes are:

| Source → target | Reconstructed representation |
| --- | --- |
| SOLIDWORKS → Onshape | FeatureScript custom feature with editable named lengths, ordered sketches/extrusions and booleans |
| SOLIDWORKS → Siemens NX | NX Open Python journal creating named expressions and ordered parametric block/cylinder features |
| Creo → Siemens NX | The same NX target adapter, from Creo-derived source intent |
| Autodesk Inventor → Fusion 360 | Fusion Python script creating named parameters, dimensioned sketches and timeline extrusions |

Saved Form `mechanical.cad_operations` can also be mapped to Fusion, Onshape or NX.
This preserves declared box/cylinder operations with independent named dimensions;
it does not infer history from a STEP file. Axis engravings remain unsupported.

This is a bounded reconstruction workflow, not a general-purpose proprietary CAD
file converter. A read-only Inventor COM extractor is included for a narrow part
subset. Automatic feature recognition from STEP and licensed vendor runtimes
are **not** bundled. Supply source history
from a native CAD extraction or a reviewed reconstruction. A STEP export alone
cannot recover the original design intent, constraints or feature history.

## Local workflow

```sh
forma-core cad-migrate routes
forma-core cad-migrate schema > cad-history.schema.json
forma-core cad-migrate plan source-history.json --target onshape --output plan.json
forma-core cad-migrate build source-history.json --target onshape --output rebuild.zip
```

Use `nx` for either SOLIDWORKS/Creo → NX and `fusion360` for Inventor → Fusion.
The source system in the history must match the requested route. `plan` exits 2
with an inspectable report when blocked. `build` emits no ZIP when blocked.
Outputs never overwrite an existing file. No command uploads CAD or calls an LLM.

The [synthetic bracket example](../examples/cad-migrations/bracket.solidworks.json)
demonstrates a plate plus mounting hole, shared dimensions and metadata. Its
zero source hash is an explicit placeholder; it is not native extraction evidence.

```sh
forma-core cad-migrate plan examples/cad-migrations/bracket.solidworks.json --target nx
forma-core cad-migrate build examples/cad-migrations/bracket.solidworks.json --target nx --output bracket-nx.zip
```

For a real source model, an extraction process must supply:

1. The source product/version, document name, SHA-256 of the native source file,
   and original length units (`mm`, `cm`, `m` or `in`). Source hashes are preserved
   but not independently verified by planning. The Inventor extractor hashes the
   saved active file before and after extraction and refuses dirty documents.
2. A complete ordered feature inventory, including unsupported/suppressed items.
   Keep stable source IDs, source feature types and dependencies. Declare
   `inventory_complete` only after checking the source tree.
3. Named dimensions, metadata, and evidence for each reconstructed feature.
   `source_api`, `human`, and `ai_inferred` identify where an operation came from.
   Record observed evidence, not an assertion that an AI's answer is verified.

Unknown schema fields are rejected, so extra source semantics cannot disappear
silently. Add an `unsupported` inventory item for each source feature that cannot
be mapped. The complete inventory is a caller assertion; Forma cannot detect a
source feature the extraction process omitted.

## Reconstruction scope and review

Version 1 supports one linear solid history: an initial rectangular or circular
XY profile extruded along +Z, followed by rectangle/circle extrusion joins or
cuts. Origins are fixed in the source frame. All lengths normalize to millimeters;
named width, height, radius and depth parameters retain references in the target.
There is a 200-feature/200-parameter limit and a 10,000 mm dimension/placement bound.
JSON history input is limited to 2 MiB. Arbitrary equations or executable code
are not accepted as dimensions.

Every AI-inferred feature requires review. After reviewing the evidence and the
proposed mapping, regenerate using `--approve-inferred`. This is an explicit
caller acknowledgement, not proof that a particular reviewer approved a design.
Confidence below 0.8 still blocks the feature; a model confidence score is not
an engineering validation. The flag never bypasses unsupported-feature,
suppression, inventory, dependency, route or dimension checks.

Original arbitrary sketch constraints, equations, assemblies, mates, drawings,
PMI/GD&T, configurations, patterns, blends/fillets and suppressed features are
outside this version. Inventor/Creo/SOLIDWORKS features with those semantics must
be inventoried as unsupported. Do not flatten them or omit them to make a plan pass.

## Rebuild packages and native checks

Every ZIP contains the original `source-history.json`, normalized `rebuild.json`,
`plan.json`, target program, instructions, and a checksum manifest. Python target
programs reject a changed `rebuild.json`; edit the source contract and regenerate.

- **Onshape:** paste `rebuild.fs` into a new Feature Studio, commit it, then add
  the Forma migration feature in an empty Part Studio. Shared named lengths
  appear in its dialog. Ordered operations live inside one custom feature;
  this does not recreate separate original native feature-tree nodes. The part
  name and a Forma attribute preserve source identity, properties and feature IDs.
  Enterprise metadata-property mapping remains a separate adapter task.
- **NX:** run `rebuild.py` via Journal > Play in a licensed NX Python session,
  with `rebuild.json` beside it. A new unsaved millimeter part contains named
  expressions and equivalent block/cylinder boolean features. These are editable
  parametric primitives, not original sketch features. Source IDs and metadata
  become Forma-prefixed user attributes. Native feature failures undo the rebuild.
- **Fusion:** create a Python script in Utilities > Scripts and Add-Ins, replace
  its Python file with `rebuild.py`, and copy `rebuild.json` beside it. The script
  creates a new unsaved parametric design. Width/height/radius sketch dimensions
  and extrusion depths reference named parameters. A failed rebuild closes that
  new unsaved document. Full source identity/metadata remains in Forma attributes.

Material is reference text in every target; no physical material library or
density is assigned. Native property name/length limits vary by CAD release;
the JSON sidecar remains the full record. All adapters require testing against
the chosen vendor versions. They have **not** been validated in licensed target
applications by the offline test suite.

Python adapters write a unique `execution-*.json` receipt with a feature mapping
and either `rebuilt_unverified` or `failed`. Planning and package creation never
claim native execution or geometric equivalence. Onshape execution is inspected
in the Part Studio and does not generate a local execution receipt.

Before accepting a migrated model, record the source and target versions, source
hash and plan hash, body count, units/orientation, bounding box, volume and
engineering-agreed comparison tolerances. Test parameter edits and regeneration,
inspect metadata, then save to a new native file. The evidence workflow below
compares reported measurements; it does not prove surface/topology equivalence.
Retain rejected feature IDs in the report.

## Inventor extraction and Fusion evidence pilot

On Windows with Inventor running, install `pywin32` in the Python environment
used by `forma-core`. Open and save an `.ipt` part, then run:

```sh
forma-core cad-migrate extract-inventor --output source-history.json
forma-core cad-migrate plan source-history.json --target fusion360 --output plan.json
forma-core cad-migrate build source-history.json --target fusion360 --output rebuild.zip
```

The extractor reads every `PartFeatures` item in browser order. It maps constant,
single-profile rectangle/circle extrusions on global +Z with a distance extent
and zero taper. Multi-region profiles, equations, linked dimensions, arbitrary
planes, suppression and other feature types remain blockers. Source Inventor
parameter names and sketch constraint relationships are **not preserved** by
this adapter: sampled dimensions become independent `F<n>_<dimension>` parameters.
This loss is included in the inventory notes and plan. Inventor database
centimeters and cubic centimeters convert to millimeters and cubic millimeters.
Metadata uses built-in property-set IDs independent of UI language.
Custom iProperties retain their original labels and text values as JSON strings
under stable `custom_<hash>` keys; this avoids silently losing labels with spaces
or Unicode. Credential-like property names are rejected, and values remain
subject to the contract's size limits. Mapping these entries into an enterprise
property dictionary still requires an organization-specific adapter.

Fusion's generated script measures the rebuilt single solid, perturbs each named
parameter by 1%, recomputes and checks feature health and geometry change, restores
the original values, and checks the restored measurements. Unused or geometry-neutral
parameters fail the edit check and require repair/review; they are not silently
counted as editable. It exports new UUID-named STEP and F3D files and writes
`evidence-*.json` beside the script. No existing native file is overwritten.

```sh
forma-core cad-migrate verify source-history.json --target fusion360 \
  evidence-EXECUTION_ID.json --output comparison.json
```

`verify` binds target, source SHA and normalized-history SHA, then checks the feature
inventory, every named parameter result, metadata, one solid body, volume and
absolute bounds. Fixed tolerances are 0.01 mm on bounds and the larger of 0.01 mm³
or 0.1% on volume. A missing source baseline cannot pass. Exit 0 means reported
checks passed; exit 2 means repair is needed. Reports are user-supplied evidence,
not attested execution, and `geometry_equivalence_verified` remains false.
NX and Onshape can use the same evidence schema with a customer-native measurement
runner; automatic report collection for those applications is not implemented.

Pilot fixture: create a 40 × 20 × 4 mm Inventor rectangular extrusion at the
origin, then cut a 3 mm radius circle centered at (10, 10) through the same 4 mm.
Expected analytic volume is `3200 - 36*pi` mm³, one body, bounds (0,0,0)–(40,20,4).
Extract the real source; do not substitute the synthetic example's zero hash.
Also test a tapered extrusion, fillet, suppressed feature, linked dimension and
disjoint cut as blocked cases. Record source/target versions, hashes, native
files, report, feature gaps and reviewer decision. Validate surfaces independently
in the CAD kernel before an enterprise pilot claims engineering equivalence.

The generated scripts and extractor have offline contract tests. Licensed
Inventor/Fusion execution remains a release gate and has not been performed here.

## GrokBot / Cursor and enterprise pilots

The host agent owns AI reconstruction and the conversation. Forma supplies the
schema, deterministic validation, capability report and target artifacts. Cursor
can invoke the CLI through its terminal. GrokBot deployments with an authorized
local command runner can use the same contract; no proprietary GrokBot API or
certified integration is assumed. A host agent should fetch the schema, extract
or interpret approved source evidence, label AI-inferred features, request review
of that evidence, and call `plan` before `build`.

Keep native source data in the customer's approved environment. Whether the
host agent sends it to a model provider is controlled by that host's data policy,
not by these local commands. Credentials are never embedded in rebuild packages.

Forma remains an independent open-source tool. The workflow and PR positioning
do not sell or promote SpaceXAI or imply a SpaceXAI, xAI, Autodesk or other vendor
partnership. Early-adopter feedback should identify what imported/rebuilt, which
source feature IDs failed, property gaps, editability, and observed geometry
differences. Customer conversations and relationship claims are not validation
evidence and are not published as product claims.

## Validation and next adapter work

Run `python -m unittest tests.integrations.test_cad_migrations -v` or the complete
`./scripts/quality/test.sh`. Tests cover all routes, loss reporting, review gates,
unit conversion, dependency errors, package integrity, code-injection boundaries,
CLI failure behavior and Python syntax. They do not substitute for CAD kernel runs.

Production rollout still requires source extractors for SOLIDWORKS/Creo, vendor
version validation for Inventor, richer sketch/constraint mappings, authenticated
native runners, a CAD-kernel shape comparison, and licensed integration tests.
Use this development PR for bounded
adapter evaluation; do not promise arbitrary legacy-model migration fidelity.

Vendor references used to define the boundary:

- [Onshape importing files and feature-tree limits](https://cad.onshape.com/help/Content/Document/importing_files.htm)
- [Onshape FeatureScript standard library](https://cad.onshape.com/FsDoc/library.html)
- [Fusion sketch dimensions API](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchDimensions_addDistanceDimension.htm)
- [Inventor ordered feature inventory](https://help.autodesk.com/cloudhelp/2024/ENU/Inventor-API/files/PartFeatures.htm)
- [Inventor extrusion definition](https://help.autodesk.com/cloudhelp/2024/ENU/Inventor-API/files/ExtrudeDefinition.htm)
- [Inventor sketch-to-model coordinates](https://help.autodesk.com/cloudhelp/2024/ENU/Inventor-API/files/PlanarSketch_SketchToModelSpace.htm)
- [Fusion native archive export](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExportManager_createFusionArchiveExportOptions.htm)
- [Fusion extrusion extent API](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/ExtrudeFeatureInput_setDistanceExtent.htm)
- [Siemens community discussion of NX Open builders](https://community.sw.siemens.com/s/question/0D54O000061xQOLSA2/c-programming-using-nxopen)

## Customer-controlled execution worker

Form can queue a rebuild for a customer worker. Install this repository's trusted
Python package in the CAD runtime, supply an existing Form owner credential in
`FORMA_CAD_TOKEN`, and set `FORMA_CAD_API_URL` and `FORMA_CAD_PROJECT_ID` in the
customer environment. Credentials are never stored in a rebuild package.

For Fusion, use a Fusion Python script whose `run(context)` calls
`forma_core.cad_migrations.worker.run(context)`. It claims one queued Fusion job,
regenerates the supported program locally, runs on Fusion's script thread, and
returns STEP/F3D bytes plus parameter/geometry/metadata evidence to Form. Invoke
the worker again for the next job. Fusion and the Form package must be installed
in the customer's CAD environment; this is not headless Fusion.

For NX or an authorized Onshape integration, run
`python -m forma_core.cad_migrations.worker --api HTTPS_API/api --project UUID
--target nx --runner /absolute/path/to/customer-cad-runner`. The operator-selected
executable receives a scratch package directory and target. It must execute the
reviewed program and produce one `evidence-*.json` plus the hashed native/STEP
outputs named in that report. Those vendor-specific executors must be supplied
and validated by the customer; the pull protocol is not an implemented Onshape
OAuth connection or a complete NX measurement adapter. No downloaded Python or
operator command from the server is executed; the worker uses installed templates.

An explicit queued attempt is claimed once. Cancel/requeue invalidates an older
attempt; native execution has no automatic retry. Inspect local CAD logs on failure.
The worker requires HTTPS except on loopback, refuses redirects, checks artifact
hashes and bounds uploads. Source/import file hashes bind reviewed history to the
actual preserved bytes. A native output upload remains customer-reported evidence,
not attestation or a CAD-kernel topology comparison. Licensed pilot testing remains
required before release.
