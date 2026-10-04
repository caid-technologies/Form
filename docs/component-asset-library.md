# Persistent component asset library

Forma stores reusable component geometry in the existing application database
and private `ProjectArtifactStorage` backend. Project-local files are disposable.
The component template catalog still owns electrical part definitions; this
library owns CAD versions, provenance and representations, not another BOM.

## Library-first contract

1. Resolve **manufacturer + part number + explicit revision + every fit-affecting
   variant**. `unknown` is an explicit identity value, never a wildcard. Do not
   infer a precise SKU from a photo. Aliases are scoped to the same manufacturer,
   revision and variants and cannot silently substitute another revision.
2. Call `forma.opencode.asset_search` with that identity. The exact match uses
   normalized Unicode/case/whitespace, preserving punctuation and variant values.
3. A `hit` returns a stable `asset_id`, immutable content-derived `version`, match
   evidence, provenance, original/derived checksums and validation limitations.
   Attach that version; **do not search the web, download from the source or
   convert the same representation again**. Reading the durable private blob is
   still necessary; a library hit does not mean zero storage I/O.
4. A `miss` returns `cad_sourcing_required`. `ComponentAssetLibrary.resolve`
   accepts the #530 sourcing callback and checks that its returned identity is
   exact before registration. #530 is a separate, unimplemented external-search
   specialist: this PR supplies its integration boundary, not a fake web result.
   Until that specialist is available, use an uploaded model or obtain explicit
   approval for a labeled generated approximation.
5. Register original bytes and any derived representations before reporting them
   reusable. Each derived file references its source checksum and conversion
   parameters. License and usage information are required; `unknown` may be
   recorded but does not grant permission to redistribute.
6. Attach the pinned asset version with a unique physical `instance_id`, mm
   position, and XYZ Euler rotation in degrees. The reference is stored in a new
   canonical project revision and the revision's artifact list. Attachment is
   idempotent; a racing project edit is rejected for retry. Later agent IR saves
   preserve server-authored pins. Assembly construction/geometry import remains
   the main agent's responsibility; attaching a reference alone does not build
   or render an assembly.

## Typed agent/API surface

Restricted OpenCode tools are `asset_search`, `asset_inspect`, `asset_register`,
`asset_attach`, and `asset_revalidate`, all under `forma.opencode.`. Each exposes
one `request_json` string with the complete typed schema in its description; this
avoids the Vertex adapter's nested `$ref` limitation. Payload validation occurs
inside the authorized handler. Callers cannot supply owner, workspace or storage
paths. Register transfers base64 representations (8 MiB request ceiling); the
Python registration service can handle the existing storage backend's size limit.
No URL in provenance is fetched by these tools.

Example search request:

```json
{"identity":{"manufacturer":"TowerPro","part_number":"SG90","revision":"photo-reference-v1","variants":{"control":"positional","case":"23x12.2x29-mm-reference","model":"generated-envelope"}},"allow_approximate":true}
```

Only deliberately approved approximations use `allow_approximate: true`.
A failed or unverified import cannot be attached. Valid STEP geometry with the
wrong envelope is rejected. Multiple usable versions return
`needs_clarification`; select one explicitly, rather than silently using the
latest version. Re-registering identical content/provenance ignores only the
retrieval timestamp and returns the first immutable record.

Fetch a representation with the same scoped connector capability:

```text
GET /api/opencode/component-assets/{asset_id}/{version}/{representation}
X-Forma-OpenCode-Capability: <session capability>
```

Signed-in project owners have the equivalent route at
`/api/opencode/projects/{project_id}/component-assets/{asset_id}/{version}/{representation}`.
Both routes resolve ownership from the current project, verify the byte count and
SHA-256 and return private, non-cacheable attachment responses. No public storage
URL is exposed. An unauthorized and a nonexistent asset have the same response.

## Persistence, refresh and privacy

- SQLite uses the existing application database. Hosted Supabase uses the new
  `component_asset_versions` migration. Apply it before deploying this API;
  schema readiness checks intentionally detect a missing migration.
- Original and derived bytes use the **existing private artifact bucket** with a
  library scope, so deleting a source project does not delete reused components.
  Local development needs a persistent `SQLITE_DATABASE_URL` and
  `FORMA_CLI_ARTIFACT_STORAGE_DIR`. SQLite/WAL requires a filesystem supporting its
  locking semantics; do not treat ephemeral worker disk as durable storage.
- Hosted Supabase/S3 use the existing `FORMA_CLI_ARTIFACT_*` configuration.
  Metadata always follows the selected database provider; no extra SQLite
  registry is silently created in hosted mode.
- Privacy scope is `(owner_user_id, workspace_id)` resolved from the project.
  Personal projects share that owner's personal scope. Even two different owners
  in the same workspace cannot read each other's uploads. This initial library
  intentionally has **no public catalog or automatic sharing**.
- Metadata rows are immutable and keyed by asset/version. Source dates, license,
  units, dimensions and conversion parameters are retained. Equivalent concurrent
  registrations share one row and deterministic blob keys; a interrupted write
  cannot expose metadata until every representation is stored. Local writes use
  independent temporary filenames and atomic replacement.
- `asset_revalidate` imports existing bytes again with the current validator.
  Changed validation produces a new version. Changed source geometry or metadata
  is explicitly registered as a new version; existing project pins stay intact.
- Missing/corrupt bytes return `recovery_required`, never trigger an implicit
  source download. Re-register the same bytes to repair storage, or register and
  deliberately attach a replacement. Storage outages are actionable failures,
  not successful hits.
- Unreferenced blobs from a failed registration may remain; no garbage collector
  is introduced here. Explicit owner-library deletion/retention controls remain
  follow-up work and should precede large-scale catalog ingestion.

## Validation and metrics

OCCT imports STEP, normalizes to mm, checks BREP validity and the declared envelope.
Other original formats or a missing OCCT runtime are stored as `unverified` and
cannot be silently reused. A geometry/envelope check does **not** prove supplier
identity, mounting fit, assembly clearance, load capacity or manufacturability.
Generated reference models retain `approximate` status even when these geometric
checks pass. Derived bytes have integrity and derivation checks, not an
independent certification that a mesh exactly matches the STEP.

Structured `component_asset_library` log events and per-instance counters record
hits, misses, sourcing callbacks, blob writes, registrations and integrity
failures. The source provider should record its own searches/downloads/conversions;
the SG90 example emits these counts explicitly.

Run:

```sh
python -m pytest tests/integrations/test_component_assets.py tests/opencode/test_component_assets.py tests/integrations/test_project_artifacts.py -q
```

See [the SG90 arm demonstration](../examples/sg90-library-arm/README.md) for actual
STEP geometry, four physical instances, two canonical projects and process-restart
reuse. This generated-model demonstration replaces the proposed board example;
it does not claim that #530's web retrieval has shipped.
