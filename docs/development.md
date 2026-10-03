# Development

This project is research-oriented and welcomes contributors. Keep changes focused and explain scope clearly in PRs.

## Contribution workflow
1. Fork the repo and create a feature branch.
2. Run the existing frontend build/lint if your change touches the UI.
3. Add or update documentation when you change behavior.
4. Open a PR with a concise summary and testing notes.

## Local commands
Backend (from repo root):
```bash
source .venv/bin/activate
uvicorn apps.api.main:app --reload --port 8000
```

The combined dev launcher starts the backend and frontend together and writes backend and uvicorn logs to `.logs/backend-dev.log` for the local LOGS tab:

```bash
./scripts/development/dev.sh
```

On Windows PowerShell, use the native launcher instead:

```powershell
.\scripts\development\dev.ps1
```

On first run it installs missing dependencies, defaults to local auth with
SQLite without selecting an LLM, and stores a generated encryption key in
`.forma/local-secrets.env`. The connected host agent authors the Hardware Intermediate Representation;
Forma validates and compiles it deterministically. Set `FORMA_USER_SECRETS_KEY`
explicitly when the workspace must use an existing encrypted settings store.

If you run uvicorn directly and want the frontend LOGS tab to show backend output, set `BACKEND_LOG_FILE=.logs/backend-dev.log`.

Tests:
```bash
./scripts/quality/test.sh
```

Windows PowerShell equivalent:

```powershell
.\.venv\Scripts\python.exe -m compileall -q apps/api forma_core evals scripts tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Frontend:
```bash
cd apps/web
npm run dev
npm run lint
npm run build
```

Rust integrations are developed in [`isayahc/Forma-Rust`](https://github.com/isayahc/Forma-Rust):
```bash
git clone https://github.com/isayahc/Forma-Rust.git
cd Forma-Rust
cargo run --manifest-path rust/Cargo.toml -p forma-edge -- linux-snapshot
```

## Adding a new agent
1. Define or extend the relevant Pydantic schema in `apps/api/models.py`.
2. Add a new step in `apps/api/agents/orchestrator.py`.
3. Ensure the agent’s output is merged into the Hardware Intermediate Representation.
4. Update docs in `docs/agents.md` and `docs/architecture.md`.

## Extending validation rules
1. Add a new rule function in `apps/api/validation.py`.
2. Emit a structured `ValidationIssue` with severity and troubleshooting.
3. Re-run validation in the pipeline and update `docs/validation.md`.

## Adding seed components
1. Add new entries in `apps/api/seed_db.py`.
2. Re-run `python3 apps/api/seed_db.py` to repopulate the database.
3. Ensure pin definitions are complete and typed (power/ground/digital/etc).

## Frontend development tips
- Main UI: `apps/web/app/page.tsx`
- Styling: Tailwind + custom CSS in `apps/web/app/globals.css`
- Example IRs: `apps/web/public/examples/`
- CAD models: add a `cad_model` to the Hardware Intermediate Representation. The project CAD tab accepts
  serialized mesh payloads directly, or uses `NEXT_PUBLIC_OPENCAD_URL` to import
  an authorized browser-loadable CAD URL or fetch an OpenCAD shape mesh.
