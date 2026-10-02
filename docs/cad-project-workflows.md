# CAD workflows in the Form project

Professional CAD workflows use Form's authenticated project backend and the
existing **Exports → CAD geometry** area. The existing workspace, theme,
navigation and export cards are preserved. Four actions open a dialog using
Form's existing styles: **Export to CAD**, **Rebuild editable model**,
**Import CAD**, and **CAD activity**. API, MCP and local CLI contracts remain
available for authorized clients.

## User journey

1. Open a saved project or choose **Import CAD**. Preserve the original file with
   its source application, SHA-256, owner and parent revision. An optional STEP
   preview is linked to that original; it never replaces the preserved bytes.
2. Choose a STEP geometry export or an editable rebuild. Geometry export explains
   that original feature history is excluded. Rebuild accepts extracted/reviewed
   history or supported saved Form operations; imported native files require
   history bound to their exact source hash and application.
3. Inspect source feature names, dimensions, provenance, target representations
   and losses before approving a package. Unsupported features block execution;
   inferred features require an explicit review note.
4. Send the reviewed package to a customer-controlled CAD worker or run it
   manually in the licensed target. Form shows queued/running/failed/cancelled
   states. The included Fusion worker runs in Fusion's script thread; NX and
   Onshape require an operator-supplied executor. No Onshape OAuth connector is
   included. Cancellation invalidates the upload attempt; stopping an already
   running CAD process is the local operator's responsibility.
5. Return a result ZIP containing actual native files, a STEP preview and bound
   evidence. Inspect source/target measurements, parameter regeneration,
   metadata differences and geometry in Form. Optional native tessellations
   render without a separate geometry service; STEP-only previews require the
   configured OpenCAD backend. Both comparison views use the same view scale.
6. Add a review note and accept a passing result. A new immutable revision
   adopts the returned STEP as the project's current CAD model and attaches its
   native files and evidence. Previous originals and results remain available
   from their exact revisions. Failed checks cannot be accepted.

The local browser fixture uses synthetic models/evidence and is not a licensed
CAD validation. Do not interpret its accepted revision as engineering approval.

| Area | Implemented behavior | Validation boundary |
| --- | --- | --- |
| Export | Request SOLIDWORKS, Onshape or Fusion; package the saved STEP with explicit metadata and import helper | Geometry handoff; no original feature history |
| Migration | Submit reviewed/extracted history or use saved Form box/cylinder operations; return blockers, losses and feature mappings | Supported primitives only; unsupported features block |
| Native execution | Queue/claim a single customer-worker attempt; return measured native files and tessellations | Fusion executor included; NX/Onshape executors supplied by customer; licensed pilot pending |
| Review | Return hashed native files and bound reports; inspect geometry, parameters and metadata; adopt an accepted model | Report-only uploads cannot be accepted; checks are customer reported |
| Project lifecycle | Save immutable revisions and private checksum-addressed artifacts; flag outdated runs after design changes | Earlier artifacts remain downloadable from their exact snapshot |
| Agent access | Same actions through MCP and local CLI primitives | No proprietary GrokBot API or model-provider dependency |

## Data and state

Each run records source project revision/UUID, source hash, target, a design
fingerprint, artifact hashes, creation/update times and review evidence. Actions
append an immutable project revision. `expected_revision` prevents a stale caller
from overwriting newer state; the repository's compare-and-swap also rejects a
change between reading and committing. Reusing a request UUID with the same
payload is idempotent; changing its payload is rejected.

Workflow metadata and artifact references survive ordinary design edits.
The fingerprint excludes workflow records and revision bookkeeping. Any other
saved design change makes a previous run stale and blocks building/reviewing it;
start a new plan. This conservative comparison can mark a run outdated for
non-geometric changes too. History labels identify CAD saves.

Migration states: `blocked` or `ready_for_rebuild` → `awaiting_native_execution`
→ optional `queued_for_worker` → `running_in_cad` → `checks_passed` or `needs_repair` → `accepted_by_reviewer` or
`rejected_by_reviewer`. Acceptance requires passing reported checks, returned native files, a STEP
preview and a review note. Worker claims expire after 30 minutes; cancellation
and replacement attempts reject late worker uploads. Failed attempts may be
explicitly requeued. There is no automatic CAD execution retry. Uploading replacement evidence clears the current decision, while previous
decisions remain in immutable history. Approving inferred features requires an
explicit flag and note and never bypasses unsupported features. Downloading a
package does not change its native execution state.

Native uploads are reports, not signed attestations. An authorized owner can
supply their own measurements. Volume/bounds are insufficient to prove matching
surfaces or topology; the stored comparison retains this limitation.
Material is reference text, not a native material-library assignment.

Artifacts use existing `ProjectArtifactStorage` (local, Supabase or S3) with
project-scoped keys. No database schema change is needed. Downloads require
active ownership and an artifact reference in the requested revision; content
hashes are verified on the server. Clients should also compare downloaded bytes
with the published artifact hash. Artifact-store failures do not commit a
workflow revision. Failed concurrent commits may leave unreferenced
content-addressed blobs; existing
project purge removes them. This release caps a project at 50 runs, a run at 10
review decisions and HTTP/MCP JSON actions at 2 MiB. Original/result binary uploads are capped at
50 MiB; result ZIPs allow at most six entries and four hashed native outputs,
rejecting unsafe paths, extra files, invalid meshes and mismatched checksums.
Large enterprise batch migration
needs a dedicated durable job/retention layer.

## API and agent clients

All project routes use the existing authenticated owner identity:

- `GET /cad-capabilities`: targets, routes, source-history and native-evidence schemas.
- `GET /projects/{id}/cad-workflows`: latest revision and run status.
- `POST /projects/{id}/cad-workflows`: action below.
- `POST /projects/{id}/cad-workflows/source?filename=...&system=...&expected_revision=...&request_id=...`:
  raw original bytes; optional `preview_of` binds a STEP preview to the original.
- `POST /projects/{id}/cad-workflows/runs/{run_uuid}/result?attempt=...`:
  raw ZIP, including `evidence.json`, target native file (or Onshape document
  reference), STEP and optional `source.mesh.json`/`target.mesh.json`. Worker
  uploads require the active attempt ID; omit it for explicitly manual results.
- `GET /projects/{id}/cad-workflows/artifacts/{revision_uuid}/{sha256}`:
  private, checksum-verified download from a particular saved revision.

```json
{
  "action": "plan",
  "request_id": "93670980-6fbd-4d84-a9e8-5010d337e3cc",
  "expected_revision": 3,
  "target": "fusion360",
  "history": {"...": "validated forma-cad-history document"}
}
```

The example history is a placeholder; obtain its actual schema from discovery.
Actions are `export`, `plan`, `form-history`, `build`, `queue`, `claim`, `cancel`,
`worker-failed`, `evidence`, and `review`. Actions on an existing run reference
`run_id`; `worker-failed` also requires its active `attempt_id`. Report-only
evidence uses the published report schema and clears prior native attachments;
review supplies `decision` (`accept`/`reject`) and `note`.

MCP exposes `forma.cad_capabilities` and `forma.cad_workflows`. Call the latter
with `{ "project_id": "..." }` to read, then add a typed `command` containing the
same action body to save. Use a fresh UUID for a new action and reuse it only for
an identical retry. Human review must be authorized explicitly in the host
conversation; do not accept results merely because an agent can call the tool.
The service records the authenticated actor and note; it cannot attest that a
human, rather than a delegated agent, performed the inspection.

Cursor can discover these tools through Form's existing MCP configuration.
GrokBot deployments with an authorized MCP client or local command runner can
use the same contracts. No new provider adapter, vendor credentials or automatic
CAD upload is introduced. Customer data sharing remains governed by the host's
policy. Form does not sell or promote SpaceXAI and implies no SpaceXAI, xAI,
Autodesk or other vendor partnership, endorsement or certified integration.

## Verification

```sh
python -m unittest tests.integrations.test_cad_export tests.integrations.test_cad_migrations tests.integrations.test_cad_native_evidence tests.integrations.test_cad_worker tests.projects.test_cad_workflows tests.projects.test_project_history -v
```

The integration tests exercise real API routes, SQLite revisions and private
local artifact storage. They cover export creation, history planning, rebuild
packages, evidence validation, review gates, persistence, stale results,
ownership, concurrent revision conflicts, import provenance, worker attempts, bounded native
ZIP/mesh validation, accepted model adoption and idempotent retries. Source CAD and
native evidence are synthetic. For manual API checks, the existing loopback
fixture can be started with `.venv/bin/python -m tests.projects.cad_workflows_fixture`.
It binds loopback only and uses disposable storage; never deploy it.

Live pilot instructions and remaining native gates are in
[AI-assisted CAD migrations](ai-cad-migrations.md). SOLIDWORKS/Creo source
extraction, NX/Onshape automated evidence collection, native topology comparison,
enterprise property dictionaries and licensed version coverage remain future
adapters. Keep these limitations visible in early-adopter discussions.


For the actual frontend check, start the loopback fixture and the real web app:

```sh
FORMA_AUTH_MODE=local NEXT_PUBLIC_API_URL=http://127.0.0.1:8016 npm run dev -- --hostname 127.0.0.1 --port 3016
```

Run the web command from `apps/web`. Open
`http://127.0.0.1:3016/project/ca000000-0000-4000-8000-000000000001` and use
**Exports**. The fixture has no real CAD geometry, external CAD credentials or
production data. Verify desktop/mobile dialogs, empty-geometry gating, import,
plan approval, worker status, native inspection, acceptance and reload. A
simulated executor checks this lifecycle only; the licensed bracket pilot in the
migration guide remains the release gate.
