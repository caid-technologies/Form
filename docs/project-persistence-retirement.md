# Project Persistence Retirement

## Current Boundary

`projects` and `project_revisions` are the authoritative project identity and state
stores. Authenticated hosted generation, iteration, CLI pushes, and MCP compilation
write canonical revisions first. `generated_projects`, `cli_projects`, and
`cli_project_revisions` are compatibility projections used only by adapters that
serve existing clients or perform retention cleanup.

The web API reads through `resolve_project_for_read` and the canonical gallery
inventory. CLI response shaping remains an explicit compatibility adapter until all
CLI clients consume canonical project responses.

## Migration Verification

Run the reconciliation tool in dry-run mode before applying any repair:

```text
python scripts/operations/reconcile-projects.py --dry-run --audit artifacts/project-reconciliation.json --json
python scripts/operations/reconcile-projects.py --apply --audit artifacts/project-reconciliation-apply.json --json
```

Every report records the project ID, source channel, ownership decision, status,
mismatches, and repair actions. Failed records can be retried without rerunning
successful projects:

```text
python scripts/operations/reconcile-projects.py --apply --retry-failed --audit artifacts/project-reconciliation-apply.json --json
```

Do not run schema retirement while the report contains unresolved failures or
unrepaired mismatches. The tool never deletes legacy rows.

## Usage Metrics And Retirement Gate

Before removing a compatibility adapter, instrument and review:

- legacy projection fallback reads by endpoint and source channel;
- legacy projection writes by caller and source channel;
- CLI clients using legacy response fields;
- reconciliation drift and failed repair counts.

Retirement requires zero fallback reads and writes for one complete retention window,
successful reconciliation, and a reviewed client migration. Until then, retain the
legacy tables and projection adapters in read-only or repair-only mode as appropriate.

## Rollback And Retention

Rollback is application-level: route reads back through the compatibility adapter and
stop applying projection repairs. Canonical identities and revisions are immutable
source records and must not be deleted as part of a rollback. Keep the reconciliation
JSON reports and deployment commit that produced them with operational records.

Schema retirement is a separate, reviewed migration. It must include a tested backup,
a restore rehearsal, an export of retained project records, and a documented data
retention/deletion date. No table drop or destructive cleanup is included in the
canonical migration work.
