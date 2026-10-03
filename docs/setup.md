# Local Setup

Forma OSS runs a FastAPI backend and a Next.js frontend. Supabase is supported for deployment through the Supabase client; the backend falls back to SQLite for local use.

For detailed CLI commands and runtime settings, see the
[CLI and runtime reference](runtime-reference.md). For other topics, use the
[documentation index](README.md).

## Prerequisites
- **Python 3.11+**
- **Node.js 18+**
- **Supabase project** (optional, recommended for deployed persistent storage)
- **Docker** (optional, for containerized frontend and backend images)

## OpenCode local setup

Forma can run entirely locally while OpenCode supplies the model and authors
the Hardware Intermediate Representation. Local generation, validation, rendering, and project status do
not require a Forma account. The account is only needed when a project is
uploaded to Forma Cloud.

The website provides copyable installers for macOS/Linux and Windows:

- [Install Forma for OpenCode](https://caid-technologies.us/install/opencode)

From an existing checkout, the equivalent setup commands are:

```bash
python3 scripts/development/setup-opencode.py --root . --workspace "$HOME/forma-workspace" --install-cli
./scripts/development/dev.sh
```

On Windows PowerShell:

```powershell
py -3 .\scripts\development\setup-opencode.py --root . --workspace "$HOME\forma-workspace" --install-cli
.\scripts\development\dev.ps1
```

Open the local workspace in a second terminal after the services start:

```bash
cd ~/forma-workspace
opencode mcp list
opencode
```

The local MCP endpoint is `http://127.0.0.1:8000/mcp`. OpenCode should call
`forma.compile_project`, which performs deterministic normalization,
validation, schematic generation, and local persistence without invoking a
server-side LLM or substituting simulation output.

The final compiled response must be written back to the local
`forma-project.json`. The shared skill's bundled client supports this directly
and materializes the returned validation, Mermaid, and SVG artifacts beside the
manifest:

```bash
python .agents/skills/forma-hardware/scripts/forma.py compile \
  "$HOME/forma-workspace/<project-id>/forma-project.json" \
  --authoring-agent opencode \
  --output "$HOME/forma-workspace/<project-id>/compiled-project.json" \
  --update-project
```

To upload later, authenticate explicitly and push the local project:

```bash
forma-oss login
forma-oss projects push --path "$HOME/forma-workspace/<project-id>"
```

Provider credentials and local secrets are excluded from the upload payload.

## Docker setup
From the repo root:

```bash
docker compose up --build
```

This builds `forma-backend:local` and `forma-frontend:local`, starts the API on port `8000`, starts the UI on port `3000`, and keeps SQLite data in the `forma-data` Docker volume.

The Compose backend defaults to:

```env
FORMA_DEPLOYMENT_MODE=local
FORMA_DEVELOPMENT_MODE=false
DATABASE_BACKEND=sqlite
SQLITE_DATABASE_URL=sqlite:////data/forma.db
LLM_PROVIDER=simulation
```

Compose also starts an ephemeral Redis service and configures the backend to
cache project gallery responses for 60 seconds. Project writes invalidate the
cache immediately; Redis outages fall back to the primary database.

Host-side `DATABASE_BACKEND` and `SQLITE_DATABASE_URL` values are intentionally
ignored by Compose so a repo-root `.env` cannot accidentally route the backend
container to a database on its own loopback interface. Live-provider variables
still pass through normally.

To intentionally use Supabase from Compose, set
`COMPOSE_DATABASE_BACKEND=supabase` and use a URL reachable from the container.
For a Supabase CLI instance running on the Docker host:

```bash
COMPOSE_DATABASE_BACKEND=supabase \
SUPABASE_URL=http://host.docker.internal:54321 \
docker compose up --build
```

Use the API port reported by `supabase status` if it differs from `54321`.

If you publish the backend on a different host or port, rebuild the frontend with a matching browser-visible API URL:

```bash
BACKEND_PORT=8010 NEXT_PUBLIC_API_URL=http://localhost:8010 docker compose up --build
```

## Backend setup (FastAPI)
From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r apps/api/requirements.txt
```

To start the backend and frontend together on Windows PowerShell, run the
native development launcher from the repository root:

```powershell
.\scripts\development\dev.ps1
```

If local PowerShell execution policy blocks scripts, invoke it with a
process-scoped bypass:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\development\dev.ps1
```

Both `requirements.txt` and `apps/api/requirements.txt` list only third-party
runtime dependencies. Do not add local package paths such as `.[backend]` or
`../..`; Vercel may resolve dependency files from a service subdirectory and
turn those into invalid deployment-relative paths. The `forma_core` source is bundled
into the backend function through `vercel.json` `includeFiles`, which keeps
deployments on the current monorepo source without relying on a stale PyPI
wheel. `vercel.json` also excludes local databases, logs, frontend artifacts,
Rust build output, examples, docs, and tests from the backend function bundle.

Vercel installs the committed `apps/api/uv.lock` with frozen dependency
resolution. When backend dependencies change, update
`apps/api/pyproject.toml`, both requirements files, and the lockfile together:

```bash
uv lock --project apps/api
uv lock --project apps/api --check
uv sync --project apps/api --frozen --no-dev
```

The backend deployment smoke workflow checks that the lockfile is current,
then imports both API entrypoints and exercises PDF extraction and rendering
using only the frozen runtime dependencies. A successful `pip install` from
requirements alone does not verify the dependency set used by Vercel.

### Environment variables
Recommended: create a repo-root `.env` (see `.env.example`).

```env
# Supabase persistence through the Supabase Python client.
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key_here
# Or, for newer Supabase projects:
# SUPABASE_SECRET_KEY=your_secret_key_here

# Local fallback / explicit SQLite
# FORMA_DEVELOPMENT_MODE=true
# DATABASE_BACKEND=sqlite
SQLITE_DATABASE_URL=sqlite:///./forma.db

# Project gallery cache. REDIS_CACHE_PREFIX plus either REDIS_URL or both
# Upstash REST variables are required when FORMA_DEVELOPMENT_MODE is false;
# development mode may leave these unset to read directly from the database.
REDIS_URL=redis://localhost:6379/0
# UPSTASH_REDIS_REST_URL=https://your-database.upstash.io
# UPSTASH_REDIS_REST_TOKEN=your_rest_token
# PROJECTS_CACHE_TTL_SECONDS=60
# REDIS_CACHE_PREFIX=forma
# REDIS_SOCKET_TIMEOUT_SECONDS=0.25

# Deployment-only alpha gate
# FORMA_DEPLOYMENT_MODE=hosted
# Hosted chat defaults to disabled in hosted deployments. Enable only after an
# end-to-end smoke test of the hosted chat path.
# FORMA_HOSTED_CHAT_ENABLED=true

# Live LLM generation
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_anthropic_api_key_here
ANTHROPIC_MODEL=claude-opus-5
STRICT_LLM=true

# Optional Google Gemini
# LLM_PROVIDER=gemini
# GEMINI_API_KEY=your_gemini_api_key_here
# GEMINI_MODEL=gemini-3.7-flash

# Optional Google Vertex AI
# LLM_PROVIDER=vertex
# GOOGLE_CLOUD_PROJECT=your_google_cloud_project_id
# GOOGLE_CLOUD_LOCATION=global
# VERTEX_AI_MODEL=gemini-3.7-flash

# Optional first-party OpenAI settings
# LLM_PROVIDER=openai
# OPENAI_API_KEY=your_openai_api_key_here
# OPENAI_MODEL=gpt-5.6-sol
# OPENAI_RESPONSE_FORMAT=json_schema
# OPENAI_VALIDATE_MODELS=false
# OPENAI_TIMEOUT_SECONDS=300
# OPENAI_REASONING_EFFORT=low
# OPENAI_TEMPERATURE=1
# OPENAI_PROJECT_ID=your_openai_project_id_here
# OPENAI_ORG_ID=your_openai_org_id_here

# Optional Langfuse observability
# LANGFUSE_PUBLIC_KEY=pk-lf-your_public_key_here
# LANGFUSE_SECRET_KEY=sk-lf-your_secret_key_here
# LANGFUSE_BASE_URL=https://cloud.langfuse.com
# LANGFUSE_TRACING_ENVIRONMENT=local
# LANGFUSE_TRACING_RELEASE=dev
# LANGFUSE_MAX_FIELD_CHARS=20000
# LANGFUSE_ENABLED=false

# Optional generated product image output
IMAGE_OUTPUT_ENABLED=false
IMAGE_PROVIDER=openai
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_IMAGE_SIZE=1024x1024
# OPENAI_IMAGE_QUALITY=medium
# OPENAI_IMAGE_OUTPUT_FORMAT=png
# For Nano Banana through the same Vertex AI project and ADC used by the LLM:
# IMAGE_PROVIDER=vertex
# VERTEX_AI_IMAGE_MODEL=gemini-3.1-flash-image
# VERTEX_AI_IMAGE_RESOLUTION=1K
# VERTEX_AI_IMAGE_ASPECT_RATIO=1:1

# Optional Supabase Storage upload for reference/product images.
# Uses the Supabase client with SUPABASE_URL plus the service-role/secret key.
SUPABASE_S3_BUCKET=contents
# SUPABASE_S3_REGION=us-east-1
# Optional direct S3-compatible mode; all three values are required.
# FORMA_IMAGE_STORAGE_BACKEND=s3-compatible
# SUPABASE_S3_ENDPOINT=https://your-project-ref.storage.supabase.co/storage/v1/s3
# SUPABASE_S3_ACCESS_KEY_ID=your_supabase_s3_access_key_id
# SUPABASE_S3_SECRET_ACCESS_KEY=your_supabase_s3_secret_access_key
# SUPABASE_IMAGE_SIGNED_URL_SECONDS=86400
# SUPABASE_STORAGE_PUBLIC_BASE_URL=https://your-project-ref.supabase.co

# Optional private storage for forma-oss project artifacts.
# Supabase-primary hosted deployments use the private bucket through the
# service-role client; SQLite/local deployments use the local directory.
# FORMA_CLI_ARTIFACT_BUCKET=cli-project-artifacts
# FORMA_CLI_ARTIFACT_STORAGE_BACKEND=supabase
# FORMA_CLI_ARTIFACT_STORAGE_DIR=./.forma/cli-artifacts
# FORMA_CLI_ARTIFACT_MAX_BYTES=52428800

# Generic provider aliases
# LLM_API_KEY=your_provider_api_key_here
# LLM_MODEL=gpt-5.6-sol
# LLM_FALLBACK_MODEL=your_fallback_model_here

# Optional for OpenAI-compatible providers
# LLM_PROVIDER=openai-compatible
# LLM_BASE_URL=http://localhost:11434/v1
# LLM_ALLOW_NO_API_KEY=true
# LLM_TIMEOUT_SECONDS=90
# LLM_REASONING_EFFORT=low
# LLM_TEMPERATURE=0.2

# Optional TCP JSONL A2A socket
A2A_SOCKET_ENABLED=false
A2A_SOCKET_HOST=127.0.0.1
A2A_SOCKET_PORT=8766
```

Notes:
- Supabase mode uses `SUPABASE_URL` plus `SUPABASE_SERVICE_ROLE_KEY` or `SUPABASE_SECRET_KEY`; it does not use a Postgres connection string.
- Do not use anon, publishable, or `NEXT_PUBLIC_` Supabase keys for the backend. They obey RLS and cannot seed these tables by default.
- Set `FORMA_AUTH_MODE=local` for a Clerk-free local workspace or `FORMA_AUTH_MODE=clerk` for required Clerk sign-in and per-user settings.
- Set an optional `FORMA_A2A_API_KEY` of at least 32 random characters when a remote agent host needs a durable bearer credential for A2A REST, WebSocket, TCP, or MCP transports. The existing `FORMA_MCP_API_KEY` is also accepted for compatibility; Clerk administrator sessions remain accepted where applicable.

Hosted browser access is deny-by-default. Set `FORMA_CORS_ORIGINS` to a comma-separated allowlist of exact frontend origins, for example `https://app.example.com`; wildcard origins are rejected when credentialed CORS is enabled. Request bodies, prompts, inline images, generation rates, and daily generation quotas are bounded by the `FORMA_MAX_*`, `FORMA_GENERATION_RATE_LIMIT`, and `FORMA_GENERATION_QUOTA` settings shown in `apps/api/.env.example`. Service credentials are server-side bearer credentials only and must not be placed in `NEXT_PUBLIC_*` variables or browser bundles.
- Local unauthenticated A2A/MCP access is allowed only when both `FORMA_DEPLOYMENT_MODE=local` and `FORMA_AUTH_MODE=local` are explicitly set.
- `FORMA_USER_SECRETS_KEY` is mandatory in every backend runtime; startup logs a critical error and fails when it is absent. It must be a high-entropy server-only secret. Losing or rotating it without a migration makes existing saved API keys undecryptable.
- Local workspace settings are always encrypted: Supabase-primary environments use `workspace_integration_configs`, while SQLite-primary environments use an encrypted local file even when unrelated Supabase credentials are present. `FORMA_WORKSPACE_SECRETS_KEY` may provide a separate workspace key; otherwise `FORMA_USER_SECRETS_KEY` is used.
- `FORMA_DEVELOPMENT_MODE=true` selects SQLite for the complete application database when Supabase points at a remote project. For local Supabase testing, `DATABASE_BACKEND=supabase` is honored when `SUPABASE_URL` points at localhost/127.0.0.1. Development mode still disables Supabase Storage writes, so reference and product image data is stored inline unless development mode is disabled. `FORMA_DEV_MODE` is a compatibility alias.
- `FORMA_DEVELOPMENT_MODE=false` requires the selected LLM provider and exact model to pass a live availability check before generation begins. Production rejects simulation, providers that cannot verify model availability, and fallback models.
- If Supabase client variables are missing, local mode falls back to `SQLITE_DATABASE_URL` or `sqlite:///./forma.db`; hosted mode fails closed instead of selecting SQLite.
- `DATABASE_BACKEND` can be `supabase` or `sqlite`.
- `REDIS_URL` and `REDIS_CACHE_PREFIX` are required at backend startup whenever `FORMA_DEVELOPMENT_MODE` is false. Development mode can omit them and fall back directly to the primary database.
- Docker Compose uses `COMPOSE_DATABASE_BACKEND` instead and defaults it to `sqlite`; this prevents host-only loopback Supabase URLs from breaking the container quickstart. `COMPOSE_SQLITE_DATABASE_URL` optionally overrides the container SQLite URL.
- Image storage and encrypted integration stores follow `DATABASE_BACKEND`. Supabase credentials alone do not activate them when `DATABASE_BACKEND=sqlite`; use `FORMA_IMAGE_STORAGE_BACKEND=supabase` or the workspace/user integration backend overrides for an intentional exception.
- CLI project artifacts use the private `FORMA_CLI_ARTIFACT_BUCKET` (default `cli-project-artifacts`). `FORMA_CLI_ARTIFACT_STORAGE_BACKEND` may select `supabase`, `s3-compatible`, or `local`; the default follows the primary database backend. Each artifact is stored under a project-scoped hash key and is validated against its declared SHA-256 and media type.
- Provider availability is `environment configured OR (BYOK enabled AND BYOK configured)`. Environment variables remain workspace/platform defaults, saved BYOK values overlay matching fields, and clearing or disabling BYOK reveals the environment fallback. Generated provider/model allowlists include both sources, so either source can make a provider available without suppressing the other.
- After those inputs are applied, `GET /api/runtime/config` is authoritative for the frontend. Resolution precedence is request override, saved integration, environment, then provider default; the browser does not repeat this merge.
- `FORMA_DEPLOYMENT_MODE=hosted` requires a configured deployment provider or signed-in user's BYOK provider for generation. Hosted mode cannot run with `FORMA_DEVELOPMENT_MODE=true`.
- `FORMA_HOSTED_CHAT_ENABLED` defaults to `true` for local deployments and `false` for hosted deployments. While it is `false`, the frontend shows the local-first maintenance state and the API rejects hosted chat/generation mutations without disabling read-only project access, local CLI generation, or CLI uploads.
- `LLM_PROVIDER` can be `vertex`, `anthropic`, `baseten`, `gemini`, `gmi`, `huggingface`, `cloudflare`, `nvidia`, `openai`, `openai-compatible`, `runpod`, `runpod-serverless`, or `simulation`. Use `runpod` for Runpod OpenAI-compatible/vLLM endpoints and `runpod-serverless` for queue-style `/runsync` workers.
- `/api/generate` accepts optional `provider` and `model` fields for runtime switching, for example `{"provider":"openai","model":"gpt-4o-mini"}`.
- Use `LLM_ALLOWED_PROVIDERS` plus provider-specific model allowlists (`VERTEX_AI_ALLOWED_MODELS`, `OPENAI_ALLOWED_MODELS`, `BASETEN_ALLOWED_MODELS`, `HUGGINGFACE_ALLOWED_MODELS`, `CLOUDFLARE_ALLOWED_MODELS`, `NVIDIA_ALLOWED_MODELS`, `OPENAI_COMPATIBLE_ALLOWED_MODELS`, `GEMINI_ALLOWED_MODELS`, `RUNPOD_ALLOWED_MODELS`) to control what clients can select at runtime.
- `GOOGLE_CLOUD_PROJECT` (or `VERTEX_AI_PROJECT`), `GOOGLE_CLOUD_LOCATION` (or `VERTEX_AI_LOCATION`), and `VERTEX_AI_MODEL` configure Vertex AI. It authenticates with Application Default Credentials; use `gcloud auth application-default login` locally or an attached service account in production.
- `OPENAI_API_KEY` enables first-party OpenAI live structured generation when `LLM_PROVIDER=openai`.
- `OPENAI_RESPONSE_FORMAT` defaults to `json_schema` for OpenAI. You can set it to `json_object` for older JSON mode or `none` to omit `response_format`.
- `OPENAI_TIMEOUT_SECONDS` controls the per-request OpenAI read timeout and defaults to `300`.
- `OPENAI_REASONING_EFFORT` can lower latency for GPT-5/o-series reasoning models, for example `low`.
- `OPENAI_TEMPERATURE` is optional and omitted by default for first-party OpenAI so models that only support their default temperature can run.
- `OPENAI_PROJECT_ID` and `OPENAI_ORG_ID` are optional routing headers for accounts that need explicit project or organization selection.
- `BASETEN_API_KEY` enables Baseten Model APIs when `LLM_PROVIDER=baseten`; `BASETEN_BASE_URL` defaults to `https://inference.baseten.co/v1`.
- `BASETEN_MODEL` selects the Baseten model slug, for example `deepseek-ai/DeepSeek-V4-Pro`.
- `HF_TOKEN`, `HUGGINGFACE_API_KEY`, or `HUGGINGFACE_HUB_TOKEN` enables Hugging Face Inference Providers when `LLM_PROVIDER=huggingface`; `HUGGINGFACE_BASE_URL` defaults to `https://router.huggingface.co/v1`.
- `ANTHROPIC_API_KEY` or `CLAUDE_API_KEY` enables Claude when `LLM_PROVIDER=anthropic`; `ANTHROPIC_BASE_URL` defaults to `https://api.anthropic.com/v1`.
- `HUGGINGFACE_MODEL` selects the Hugging Face model ID, for example `Qwen/Qwen2.5-Coder-3B-Instruct:nscale`.
- `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` enable Cloudflare AI when `LLM_PROVIDER=cloudflare`; `CLOUDFLARE_BASE_URL` can override the derived OpenAI-compatible endpoint.
- `CLOUDFLARE_MODEL` selects the Cloudflare Workers AI model ID and defaults to the Free-plan-compatible `@cf/google/gemma-4-26b-a4b-it`; structured generation defaults to `CLOUDFLARE_RESPONSE_FORMAT=json_schema`.
- `CLOUDFLARE_ENABLE_THINKING=false` is the structured-generation default. Set it to `true` only when hidden reasoning is worth reducing the token budget available for the JSON answer.
- `NVIDIA_API_KEY` enables NVIDIA Build/NIM APIs when `LLM_PROVIDER=nvidia`; `NVIDIA_BASE_URL` defaults to `https://integrate.api.nvidia.com/v1`.
- `NVIDIA_MODEL` selects the NVIDIA model slug, for example `nvidia/z-ai/glm-5.2`.
- `EXTERNAL_SOURCE_PROVIDER` controls external source research for `workflow=web_research`. Firecrawl is the only active provider for now; legacy `auto` or `tavily` values are normalized to `firecrawl`.
- `FORMA_DEFAULT_GENERATION_WORKFLOW` selects the initial workflow shown by the frontend: `web_research` (default) or `default` (Catalog). Explicit workflow selections in generation requests take precedence.
- `FIRECRAWL_API_KEY` or `FIRECRAWL_MCP_COMMAND` enables Firecrawl research. `FIRECRAWL_SEARCH_LIMIT` and `FIRECRAWL_MCP_TIMEOUT_SECONDS` tune search behavior.
- Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to enable Langfuse tracing for full generation requests and every structured LLM step. `GET /api/debug/config` reports whether tracing is active without exposing secrets. Set `LANGFUSE_ENABLED=false` to disable tracing even when keys are present.
- A configured image provider makes generated product concept images the frontend default. API clients can still opt out with `generate_image=false`; `IMAGE_OUTPUT_ENABLED` remains the environment-level default for non-frontend callers.
- `IMAGE_PROVIDER` can be `vertex`, `openai`, `openai-compatible`, `gmi`, `together`, `huggingface`, or `none`.
- For `IMAGE_PROVIDER=vertex`, `VERTEX_AI_IMAGE_MODEL` defaults to `gemini-3.1-flash-image` (Nano Banana 2) and reuses the configured Vertex project, location, and Application Default Credentials. `VERTEX_AI_IMAGE_RESOLUTION`, `VERTEX_AI_IMAGE_ASPECT_RATIO`, and `VERTEX_AI_IMAGE_OUTPUT_FORMAT` control the output.
- `OPENAI_IMAGE_MODEL` selects the image model. The example default is `gpt-image-2`.
- `OPENAI_IMAGE_SIZE`, `OPENAI_IMAGE_QUALITY`, and `OPENAI_IMAGE_OUTPUT_FORMAT` tune generated image output.
- For `IMAGE_PROVIDER=openai`, image generation uses `OPENAI_IMAGE_API_KEY` or `OPENAI_API_KEY` and `OPENAI_IMAGE_BASE_URL` or `OPENAI_BASE_URL`. It does not inherit `LLM_API_KEY` or `LLM_BASE_URL`; those belong to text-model routing and OpenAI-compatible providers.
- For `IMAGE_PROVIDER=openai-compatible`, use `IMAGE_BASE_URL`/`IMAGE_API_KEY` or the compatible `LLM_BASE_URL`/`LLM_API_KEY` pair when you intentionally want a non-OpenAI image endpoint.
- For `IMAGE_PROVIDER=huggingface`, use a Hugging Face fine-grained token with the Inference Providers permission (`HF_TOKEN` or `HUGGINGFACE_API_KEY`) plus `HUGGINGFACE_IMAGE_MODEL`. Record `HUGGINGFACE_IMAGE_INFERENCE_PROVIDER`, `HUGGINGFACE_IMAGE_MODEL_REVISION`, and `HUGGINGFACE_IMAGE_MODEL_LICENSE` when storing outputs.
- When `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`/`SUPABASE_SECRET_KEY` are set, uploaded reference images and generated product images are stored in the Supabase Storage bucket from `SUPABASE_S3_BUCKET` (default `contents`) through the Supabase client. S3-compatible credentials are only a fallback. `FORMA_DEVELOPMENT_MODE=true` disables this storage path.
- `SUPABASE_IMAGE_SIGNED_URL_SECONDS` controls how long refreshed Supabase Storage read URLs live when projects are loaded. It defaults to `86400`.
- `LLM_API_KEY` is a generic provider key alias. Gemini aliases (`GEMINI_API_KEY` or `GOOGLE_API_KEY`) are still supported.
- `LLM_TIMEOUT_SECONDS` controls the generic provider read timeout. OpenAI-compatible endpoints default to `90`.
- `RUNPOD_MAX_TOKENS` (alias `LLM_MAX_TOKENS`) sets the structured-generation output token budget. A budget is always sent now; it defaults to `8192` when unset, and large schemas such as `MechanicalNotes` are raised to a `6000` floor so big JSON records are not truncated mid-string. Set `RUNPOD_MAX_TOKENS=8192` on parti-base backends.
- Structured calls retry once with a larger budget on a validation failure and salvage truncated-but-recoverable JSON with `json-repair` before validating. If both attempts still fail the API returns `502 llm_output_invalid` rather than the old generic `500 generation_failed`.
- `RUNPOD_RESPONSE_FORMAT` (alias `LLM_RESPONSE_FORMAT`) defaults to `json_object`. Set `json_schema` for vLLM grammar-constrained JSON (requires vLLM >= 0.6.3 on the worker and adds first-request grammar-compile latency for large schemas); it does not by itself prevent truncation, so the token budget and retry still apply.
- `RUNPOD_TIMEOUT_SECONDS` controls Runpod OpenAI-compatible read timeout. It defaults to `1200` so 10-15 minute cold starts or long generations can finish.
- `RUNPOD_POLL_TIMEOUT_SECONDS` controls Runpod Serverless status polling timeout. It defaults to `1200`.
- `RUNPOD_EXECUTION_TIMEOUT_MS` and `RUNPOD_TTL_MS` control Runpod Serverless job policy values. Use `1200000` for 20-minute generation windows.
- `RUNPOD_PARTI_SEED_TIMEOUT_SECONDS` controls only the `caid-technologies/parti-base` seed call and defaults to `RUNPOD_TIMEOUT_SECONDS`.
- `LLM_REASONING_EFFORT` passes reasoning effort to compatible endpoints that support it.
- `LLM_TEMPERATURE` controls generic provider sampling. OpenAI-compatible endpoints default to `0.2`; set `default`, `none`, or `omit` to omit it.
- With `STRICT_LLM=true`, generation fails fast when model availability validation is enabled and `LLM_MODEL` is unavailable.
- With `STRICT_LLM=false`, the backend may fall back to `LLM_FALLBACK_MODEL`.
- OpenAI-compatible endpoints can use `LLM_BASE_URL`; local endpoints that do not require auth can set `LLM_ALLOW_NO_API_KEY=true`.
- Runpod OpenAI-compatible/vLLM endpoints can use `RUNPOD_API_KEY` plus `RUNPOD_OPENAI_BASE_URL`. Runpod Serverless queue workers can use `RUNPOD_API_KEY` plus `RUNPOD_ENDPOINT_ID` or `RUNPOD_ENDPOINT_URL`. If each queue-style model has a different endpoint, set `RUNPOD_MODEL_ENDPOINTS` to a JSON mapping of model IDs to endpoint IDs or URLs.
- A2A job metadata uses the primary application database. Existing rows in the retired `forma_jobs.db` file are imported into the primary SQLite database on startup without deleting the legacy file.
- A2A REST, WebSocket, and MCP routes are always mounted. The TCP JSONL socket starts only when `A2A_SOCKET_ENABLED=true`.

### Seed the component database
The server auto-seeds templates on startup if the `component_templates` table is empty.

Optional manual seed:
```bash
python3 apps/api/seed_db.py
```

### Run the backend
Run from the repo root so `apps.api.*` imports resolve correctly:

```bash
uvicorn apps.api.main:app --reload --port 8000
```

Claude one-liner:
```bash
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=your_anthropic_api_key_here ANTHROPIC_MODEL=claude-opus-5 uvicorn apps.api.main:app --reload --port 8000
```

Gemini one-liner:
```bash
LLM_PROVIDER=gemini GEMINI_API_KEY=your_gemini_api_key_here GEMINI_MODEL=gemini-3.7-flash uvicorn apps.api.main:app --reload --port 8000
```

Vertex AI one-liner:
```bash
LLM_PROVIDER=vertex GOOGLE_CLOUD_PROJECT=your-project-id GOOGLE_CLOUD_LOCATION=global VERTEX_AI_MODEL=gemini-3.7-flash uvicorn apps.api.main:app --reload --port 8000
```

API docs: http://localhost:8000/api/docs

## Frontend setup (Next.js)
```bash
cd apps/web
npm install
npm run dev
```

UI: http://localhost:3000

## Optional: validate a netlist
You can submit a netlist to `POST /api/validate` to test validation rules without running the full pipeline.
