# Test Suite

The offline Python tests are grouped by the concern they exercise:

- `agents/` — orchestration, clarification, reconciliation, and continuity.
- `api/` — API routes, authentication, access control, and storage boundaries.
- `integrations/` — external sources, artifact services, and user integrations.
- `jobs/` — background jobs, streams, progress, and prior-job context.
- `observability/` — diagnostics, safe error visibility, and logging.
- `opencode/` — authenticated agent authoring, sessions, tools, and continuity.
- `persistence/` — database selection, schema compatibility, and repositories.
- `projects/` — project models, iteration, output, validation, and media prompts.
- `providers/` — LLM and image provider selection, runtime, and repair behavior.
- `terminal/` — terminal images, dashboards, and package boundaries.
- `tooling/` — package metadata, CLIs, scripts, examples, and benchmarks.

Run the complete suite from the repository root:

```bash
./scripts/quality/test.sh
```

Each concern directory is a Python package so `unittest` discovery can recurse into it.
