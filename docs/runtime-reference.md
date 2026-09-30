# CLI and runtime reference

Detailed commands and configuration moved from the project README. Start with
the [setup guide](setup.md) for a first run, or return to the
[documentation index](README.md). Run commands from the repository root unless
a section says otherwise. See [`.env.example`](../.env.example) for configuration
examples.

[Run the app](#run-the-app) · [OpenCode](#opencode-local-workflow) ·
[Python package](#python-package-pypi) · [Docker](#docker) ·
[CLIs](#forma-core-cli) · [Developer utilities](#developer-utilities) ·
[Configuration](#configuration)

## Run the app
From the repo root, use the launcher for your platform:

```bash
./scripts/development/dev.sh
```

On Windows PowerShell:

```powershell
.\scripts\development\dev.ps1
```

This starts the FastAPI backend and Next.js frontend together. It installs
missing local dependencies, defaults to local auth/SQLite without overriding
the host agent's model, and persists a generated encryption key under
`.forma/`. Use `BACKEND_PORT`,
`FRONTEND_PORT`, `BACKEND_HOST`, or `FRONTEND_HOST` to override defaults.

## OpenCode local workflow

The recommended user path is to have OpenCode author the design while Forma
runs locally. The installer creates a local OpenCode workspace, installs the
complete `forma-hardware` skill, configures the local MCP endpoint, installs the
`forma-oss` CLI, and starts the backend and frontend. It does not configure a
simulation model.

Use the public [OpenCode installation page](https://caid-technologies.us/install/opencode)
for copyable commands, or run the platform installer directly:

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/caid-technologies/Forma-OSS/main/scripts/development/install-opencode.sh | bash
```

On Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/caid-technologies/Forma-OSS/main/scripts/development/install-opencode.ps1 | iex
```

OpenCode supplies the configured model, calls the local `forma.compile_project`
tool, and writes the compiled project into `~/forma-workspace`. No Forma login
is needed for local generation, validation, rendering, or status.

## Python Package (PyPI)
The reusable core is published on PyPI as [`caid-forma-core`](https://pypi.org/project/caid-forma-core/). The distribution name is `caid-forma-core`; the Python import package is `forma_core`.

```bash
pip install caid-forma-core
```

```python
import forma_core
from forma_core.generation import HardwarePipelineOrchestrator, list_workflows
from forma_core.models import HardwareIntermediateRepresentation
```

## Docker
Build and run both images from the repo root:

```bash
docker compose up --build
```

The Docker setup runs the backend on port `8000`, the frontend on port `3000`,
Redis for project-list caching, and stores SQLite data in a named Docker volume.
Compose deliberately defaults to SQLite even if the repo `.env` configures a
host-side database. Set `COMPOSE_DATABASE_BACKEND=supabase` only with a
container-reachable `SUPABASE_URL`. Live model-provider variables still pass
through normally.

The Docker backend generates and persists a local encryption key in the
`forma-data` volume when `FORMA_USER_SECRETS_KEY` is not supplied. Production
deployments should provide their own stable server-only key.

If you change the published backend URL, rebuild the frontend with a matching public API URL:

```bash
BACKEND_PORT=8010 NEXT_PUBLIC_API_URL=http://localhost:8010 docker compose up --build
```

## Backend (FastAPI)
From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r apps/api/requirements.txt
```

**Optional: seed the component library**
The server auto-seeds the component library on startup if empty. To seed manually:

```bash
python3 apps/api/seed_db.py
```

**Run the backend:**
```bash
uvicorn apps.api.main:app --reload --port 8000
```

### Forma Core CLI

The core CLI runs generation, validation, and project iteration directly. It does not require the FastAPI backend.
Live CLI operations fail with a nonzero exit code when their requested provider, model, or pipeline fails; they never substitute another model or simulated project. Use `--simulation` only when deterministic simulated output is intentional.

```bash
forma-core workflows
forma-core namespaces
forma-core generate "plant watering monitor" --simulation --output project.json
forma-core generate "plant watering monitor" --llm openai/gpt-5.5 --output project.json
forma-core validate project.json
forma-core iterate project.json "Make the enclosure splash resistant" --namespace product.mech --output revised.json
forma-core iterate project.json "Keep the components but reshape the product as a curved handheld pod" --namespace product.mech --output reshaped.json
python -m forma_core --help
```

### Forma OSS CLI

The first-party `forma-oss` CLI is local-first. `init`, `build`, `status`, and
validation work without an account or network connection. Cloud operations target
the hosted Forma deployment by default; set `FORMA_API_URL` to a local API URL when
developing against a local server. Provider credentials are excluded from project
uploads.

```bash
forma-oss --help
forma-oss version
forma-oss doctor
forma-oss init ./my-project
forma-oss status --path ./my-project
forma-oss render --path ./my-project --output ./my-project/mechanical-render.png
forma-oss build "plant watering monitor" --path ./my-project --provider openai --model gpt-5.5
```

Cloud operations require an explicit login:

```bash
forma-oss login
forma-oss projects push --path ./my-project
forma-oss projects pull --path ./my-project
```

Hosted CLI operations check the package and hosted protocol contract. A
supported older client continues with an upgrade notice; a client below the
hosted minimum is rejected before a cloud mutation or upload. Local generation,
validation, rendering, and offline project work do not require the hosted
compatibility endpoint. `forma-oss version` always reports the installed CLI
version, while `forma-oss doctor` reports compatibility and authentication
status when available.

`projects push` validates and uploads every file declared in the local
manifest. `projects pull` verifies downloaded bytes, restores them beneath the
project directory, and rewrites native CAD paths before updating the manifest.

Provider credentials for direct CLI generation can be stored locally:

```bash
forma-oss keys set openai --scope local
forma-oss keys list --scope both
```

CLI sessions and local provider keys use the operating system credential store
through `keyring`. A plaintext fallback is disabled by default and can only be
enabled explicitly with `FORMA_ALLOW_PLAINTEXT_CREDENTIALS=1`.

### Developer utilities

```bash
./scripts/quality/test.sh
./scripts/models/sample.py "Describe a low-voltage plant watering monitor with OLED status"
./scripts/models/sample_async.py --llm openai/gpt-5.5 --llm runpod/caid-technologies/parti-base "Describe a low-voltage plant watering monitor with OLED status"
curl -X POST http://127.0.0.1:8000/projects/<project-id>/iterate -H 'Content-Type: application/json' -d '{"instruction":"Add battery charging and make the enclosure splash resistant","namespace":"product.mech","provider":"openai","model":"gpt-5.5"}'
./scripts/models/verify-llm-providers.py --list
./scripts/models/verify-llm-providers.py
./scripts/models/verify-llm-providers.py --save
./scripts/models/run-llm-smoke-tests.py
./scripts/models/verify-llm-providers.py --llm openai/gpt-5.5
./scripts/models/verify-llm-providers.py --llm runpod/caid-technologies/parti-base --timeout-seconds 1200
./scripts/models/verify-llm-providers.py --llm baseten/deepseek-ai/DeepSeek-V4-Pro
./scripts/models/verify-llm-providers.py --llm huggingface/Qwen/Qwen2.5-Coder-3B-Instruct:nscale
./scripts/models/verify-llm-providers.py --llm cloudflare/@cf/google/gemma-4-26b-a4b-it
./scripts/models/verify-llm-providers.py --llm nvidia/nvidia/z-ai/glm-5.2
```

`scripts/quality/test.sh` runs the offline unit suite with `unittest` after a Python compile check. `scripts/models/sample.py` sends the same prompt to each configured/allowed provider-model pair and saves a comparison report under `.logs/model-samples/`. `scripts/models/sample_async.py` does the same work concurrently, running one nonblocking task per selected model up to `--concurrency`. `verify-llm-providers.py` discovers the configured runtime provider/model pairs from `.env`, sends a tiny structured JSON prompt, and exits non-zero if any live provider returns invalid output. Use `--config-only` to validate selectors without spending tokens or waiting on long Runpod jobs. Use `--save` or `run-llm-smoke-tests.py` to write timestamped reports under `.logs/llm-smoke/`, plus `.logs/llm-smoke/latest.json`. The automated runner also accepts `LLM_SMOKE_LLM`, `LLM_SMOKE_CONFIG_ONLY`, `LLM_SMOKE_TIMEOUT_SECONDS`, and `LLM_SMOKE_OUTPUT_DIR` for CI or cron-style runs.

Generation and project iteration logic lives in the reusable `forma_core` package, published as the `caid-forma-core` PyPI distribution. New code should import from `forma_core.generation`, `forma_core.iteration`, `forma_core.project_objects`, `forma_core.models`, `forma_core.validation`, `forma_core.llm`, `forma_core.images`, `forma_core.runtime`, and `forma_core.selectors`; the old backend modules are compatibility wrappers. Projects are represented as `FormaProjectObject` values with an object version plus versioned namespaces such as `product.mech`, `product.electrical`, `product.validation`, `product.assembly`, `project.docs`, and `project.history`. `ProjectIterator.iterate_project(...)` takes an existing `HardwareIntermediateRepresentation` plus a natural-language instruction, can target a namespace, returns a full revised `HardwareIntermediateRepresentation`, normalizes revision/history/object metadata, redacts bulky data URLs from LLM context, and reruns circuit validation before returning. A `product.mech` chat or CLI iteration can change shape, dimensions, placement, materials, and fabrication details while preserving the BOM and electrical connectivity. `ProjectSelfCorrectionAgent` builds validation-driven repair instructions and applies them through the same namespace-aware iterator.

Performance benchmarks live under `evals/performance/` and save JSON reports under `.logs/benchmarks/`. See [`evals/README.md`](../evals/README.md) for the performance/quality distinction, shared datasets, reports, and extension guidance.
```bash
./scripts/quality/benchmark.sh
./evals/performance/benchmark_models.py --iterations 1
./evals/performance/benchmark_models.py --live --llm openai/gpt-5.5 --iterations 3 --concurrency 2
```

`benchmark_models.py` defaults to config-only mode so it can run safely without spending provider calls. Add `--live` when you want real LLM latency measurements. Each completed provider/model attempt is also flushed immediately to per-run JSONL and CSV files named `model-job-results-*.jsonl` and `model-job-results-*.csv`, including status, round, completion time, and duration fields.

Benchmark, output, and eval artifacts can be uploaded to a Hugging Face dataset repo:

```bash
export HF_TOKEN=...
export HF_ARTIFACT_REPO_ID=username/forma-metrics

./evals/performance/benchmark_models.py --live --iterations 3 --upload-huggingface
./evals/performance/benchmark_offline.py --upload-huggingface
./scripts/operations/upload-artifacts-to-huggingface.py --artifact-type outputs examples/results
./scripts/operations/upload-artifacts-to-huggingface.py --artifact-type evals .logs/evals
```

The CLI uses `.venv/bin/python` when present and falls back to `python3`. `health`
checks the root, component, and A2A jobs endpoints; `jobs --local` reads the
primary SQLite database directly when the API server is not running. Job
tables include the generation source when known: `Catalog`, `Web Research`, or
both.

To run with Google Vertex AI as the primary LLM provider:
```bash
gcloud auth application-default login
LLM_PROVIDER=vertex GOOGLE_CLOUD_PROJECT=your-project-id GOOGLE_CLOUD_LOCATION=global VERTEX_AI_MODEL=gemini-3.7-flash uvicorn apps.api.main:app --reload --port 8000
```

## Configuration

Environment variables (recommended via a repo-root `.env`; see [`.env.example`](../.env.example)):

### Application, database, and authentication

- `LOG_LEVEL`: Backend logging level, for example `INFO` or `DEBUG`.
- `BACKEND_LOG_FILE`: Optional log file for backend and uvicorn logs, for example `./forma-backend.log`.
- `FORMA_DEBUG`: When `true`, API errors and failed job metadata include redacted traceback/context debug payloads. Intended for trusted local/dev environments.
- `SUPABASE_URL`: Supabase project API URL, for example `https://your-project-ref.supabase.co`.
- `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_SECRET_KEY`: Backend-only Supabase key for writes. Do not use anon/publishable keys.
- `FORMA_DEVELOPMENT_MODE`: When `true`, forces the application database to SQLite, disables Supabase Storage writes, and keeps reference/product image data inline in the SQLite project record. `FORMA_DEV_MODE` remains a compatibility alias.
- `NEXT_PUBLIC_FORMA_DEBUG` / `NEXT_PUBLIC_FORMA_DEV_MODE`: Frontend-visible local/dev flags. The `Keys` integrations UI, `Listening Jobs`, and `Backend Logs` are shown only in Next development mode or when a debug/dev-mode flag is truthy. Keep these unset or `false` in public production builds.
- `DATABASE_BACKEND`: Optional override: `supabase` or `sqlite`.
- `FORMA_IMAGE_STORAGE_BACKEND`: Optional ancillary override (`supabase`, `s3-compatible`, or `local`). By default image storage follows `DATABASE_BACKEND`.
- `FORMA_WORKSPACE_INTEGRATIONS_BACKEND` / `FORMA_USER_INTEGRATIONS_BACKEND`: Optional encrypted-settings storage overrides. By default they follow `DATABASE_BACKEND`, so SQLite mode does not contact Supabase just because credentials are present.
- `SQLITE_DATABASE_URL`: SQLite fallback URL (default: `sqlite:///./forma.db`).
- `FORMA_DEPLOYMENT`: Legacy boolean deployment switch; prefer `FORMA_DEPLOYMENT_MODE=hosted`.
- `FORMA_AUTH_MODE`: Explicitly `local` (Clerk is not mounted and settings belong to the local workspace) or `clerk` (sign-in is required and settings belong to the Clerk user).
- `FORMA_DEPLOYMENT_MODE`: `local` (default) or `hosted`. Other values fail startup.
- `FORMA_HOSTED_CHAT_ENABLED`: Reversible hosted-chat maintenance flag. It defaults to `true` for local deployments and `false` for hosted deployments; set it to `true` only after the hosted chat path passes an end-to-end smoke test. Existing hosted chats remain readable while disabled.
- `FORMA_DEVELOPMENT_MODE`: Strict `true`/`false` switch, defaulting to `false`. Hosted mode cannot run with development mode enabled.
- `FORMA_USER_SECRETS_KEY`: Required for every backend runtime. Startup fails immediately when it is absent. Use a high-entropy server-only value; it encrypts per-user settings and is the workspace-encryption fallback.
- `FORMA_WORKSPACE_SECRETS_KEY`: Optional separate high-entropy key for local/workspace settings. SQLite-primary runtimes use an encrypted file; Supabase-primary runtimes use encrypted `workspace_integration_configs` storage.

### Shared LLM configuration

The backend publishes the resolved, credential-safe client contract at `GET /api/runtime/config`. Its precedence is explicit request override, saved integration, environment, then provider default. The web application uses this response for provider/model choices, image behavior, workflow defaults, and BYOK prompts instead of repeating configuration logic.

- `LLM_PROVIDER`: Live generation provider: `vertex`, `anthropic`, `baseten`, `gemini`, `gmi`, `huggingface`, `cloudflare`, `nvidia`, `openai`, `openai-compatible`, `runpod`, `runpod-serverless`, or `simulation`. Use `runpod` for Runpod OpenAI-compatible/vLLM endpoints and `runpod-serverless` for queue-style `/runsync` workers.
- `LLM_ALLOWED_PROVIDERS`: Optional comma-separated allowlist for per-request provider overrides.
- `VERTEX_AI_ALLOWED_MODELS` / `OPENAI_ALLOWED_MODELS` / `ANTHROPIC_ALLOWED_MODELS` / `BASETEN_ALLOWED_MODELS` / `GEMINI_ALLOWED_MODELS` / `GMI_ALLOWED_MODELS` / `HUGGINGFACE_ALLOWED_MODELS` / `CLOUDFLARE_ALLOWED_MODELS` / `NVIDIA_ALLOWED_MODELS` / `OPENAI_COMPATIBLE_ALLOWED_MODELS` / `RUNPOD_ALLOWED_MODELS`: Optional comma-separated allowlists for per-request model overrides. Without an explicit allowlist, runtime overrides are limited to the configured default/fallback model for that provider.
- `/api/generate` also accepts optional `provider` and `model` fields for runtime switching. Each generated project records the requested provider/model and actual provider/model in `assembly_metadata`.
- In the Keys UI, users can set Runtime Defaults → Preferred model as `provider/model` (for example `anthropic/claude-opus-5` or `huggingface/Qwen/Qwen2.5-Coder-3B-Instruct:nscale`). Forma derives the runtime provider, model, provider allowlist, and model allowlist from saved keys/models automatically.
- `STRICT_LLM`: Set to `true` (default) to fail fast when model validation is enabled and the model is unavailable. Set to `false` to attempt fallback.
- `LLM_API_KEY`: Generic provider API key alias. For Gemini, `GEMINI_API_KEY` or `GOOGLE_API_KEY` still work.
- `LLM_MODEL`: Model to use, for example `gemini-3.7-flash` or an OpenAI/OpenAI-compatible model ID.
- `LLM_FALLBACK_MODEL`: Optional fallback model when `STRICT_LLM=false`.
- `LLM_BASE_URL`: Optional base URL for OpenAI-compatible providers.
- `LLM_TIMEOUT_SECONDS`: Generic read timeout. OpenAI-compatible endpoints default to `90`.
- `LLM_REASONING_EFFORT`: Optional generic reasoning effort for compatible endpoints that support it.
- `LLM_TEMPERATURE`: Optional generic sampling temperature. OpenAI-compatible endpoints default to `0.2`; set `default`, `none`, or `omit` to omit it.

<details>
<summary><strong>Google Vertex AI (primary)</strong></summary>

- Set `LLM_PROVIDER=vertex`, `GOOGLE_CLOUD_PROJECT` (or `VERTEX_AI_PROJECT`), and `GOOGLE_CLOUD_LOCATION` (or `VERTEX_AI_LOCATION`, default `global`).
- `VERTEX_AI_MODEL` selects the Gemini model and defaults to `gemini-3.7-flash`. `VERTEX_AI_FALLBACK_MODEL` configures the optional non-strict fallback.
- Authentication uses Google Cloud Application Default Credentials. Run `gcloud auth application-default login` locally; in production, attach a service account with Vertex AI access. `GOOGLE_APPLICATION_CREDENTIALS` may point to a mounted credential file.
- Vercel deployments can use keyless workload identity federation. Configure Vercel OIDC in a Google Workload Identity Pool, then set `GCP_PROJECT_NUMBER`, `GCP_SERVICE_ACCOUNT_EMAIL`, `GCP_WORKLOAD_IDENTITY_POOL_ID`, and `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`. Forma exchanges the request's short-lived Vercel OIDC token and does not store a service-account key.

</details>

<details>
<summary><strong>OpenAI</strong></summary>

- `OPENAI_API_KEY`: API key for first-party OpenAI when `LLM_PROVIDER=openai`.
- `OPENAI_MODEL`: OpenAI model ID. The default is `gpt-5.6-sol`.
- `OPENAI_RESPONSE_FORMAT`: OpenAI response format. Defaults to `json_schema`; `json_object` and `none` are also supported.
- `OPENAI_TIMEOUT_SECONDS`: First-party OpenAI read timeout. Defaults to `300`.
- `OPENAI_REASONING_EFFORT`: Optional reasoning effort for GPT-5/o-series models, for example `low`.
- `OPENAI_TEMPERATURE`: Optional first-party OpenAI sampling temperature. Omitted by default so models that only support their default temperature can run.
- `OPENAI_PROJECT_ID` / `OPENAI_ORG_ID`: Optional OpenAI project and organization routing headers.

</details>

<details>
<summary><strong>Anthropic</strong></summary>

- `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY`: Anthropic Claude API key when `LLM_PROVIDER=anthropic` or a request uses `provider=anthropic`.
- `ANTHROPIC_MODEL`: Claude model ID. The default is `claude-opus-5`.
- `ANTHROPIC_BASE_URL`: Claude API base URL. Defaults to `https://api.anthropic.com/v1`.
- `ANTHROPIC_JSON_SCHEMA_OUTPUT`: Defaults to `true` and sends Claude JSON schema output config; set `false` to fall back to prompt-only JSON instructions.

</details>

<details>
<summary><strong>Baseten</strong></summary>

- `BASETEN_API_KEY` / `BASETEN_BASE_URL`: Baseten Model APIs configuration when `LLM_PROVIDER=baseten` or a request uses `provider=baseten`. `BASETEN_BASE_URL` defaults to `https://inference.baseten.co/v1`.
- `BASETEN_MODEL`: Baseten model slug, for example `deepseek-ai/DeepSeek-V4-Pro`.

</details>

<details>
<summary><strong>Gemini</strong></summary>

- `GEMINI_API_KEY` / `GOOGLE_API_KEY`: Gemini credentials when `LLM_PROVIDER=gemini` or a request uses `provider=gemini`.
- `GEMINI_MODEL`: Gemini model ID. The default is `gemini-3.7-flash`.

</details>

<details>
<summary><strong>GMI Cloud</strong></summary>

- `GMI_API_KEY` / `GMI_BASE_URL`: GMI Cloud configuration when `LLM_PROVIDER=gmi` or a request uses `provider=gmi`.
- `GMI_MODEL`: GMI model ID.

</details>

<details>
<summary><strong>Hugging Face</strong></summary>

- `HF_TOKEN` / `HUGGINGFACE_API_KEY` / `HUGGINGFACE_HUB_TOKEN`: Hugging Face Inference Providers token when `LLM_PROVIDER=huggingface` or a request uses `provider=huggingface`.
- `HUGGINGFACE_BASE_URL`: Hugging Face OpenAI-compatible router URL. Defaults to `https://router.huggingface.co/v1`.
- `HUGGINGFACE_MODEL`: Hugging Face model ID, for example `Qwen/Qwen2.5-Coder-3B-Instruct:nscale`.

</details>

<details>
<summary><strong>Cloudflare</strong></summary>

- `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`: Cloudflare AI credentials when `LLM_PROVIDER=cloudflare` or a request uses `provider=cloudflare`. The OpenAI-compatible base URL is derived as `https://api.cloudflare.com/client/v4/accounts/<account_id>/ai/v1`; `CLOUDFLARE_BASE_URL` can override it.
- `CLOUDFLARE_MODEL`: Cloudflare Workers AI model ID. Defaults to the Free-plan-compatible `@cf/google/gemma-4-26b-a4b-it`.
- `CLOUDFLARE_RESPONSE_FORMAT`: Cloudflare response format. Defaults to `json_schema`; `json_object` and `none` are also supported.
- `CLOUDFLARE_ENABLE_THINKING`: Enables Cloudflare model-native thinking for structured requests. Defaults to `false` so reasoning cannot consume the entire JSON output budget.

</details>

<details>
<summary><strong>NVIDIA</strong></summary>

- `NVIDIA_API_KEY` / `NVIDIA_BASE_URL`: NVIDIA Build/NIM configuration when `LLM_PROVIDER=nvidia` or a request uses `provider=nvidia`. `NVIDIA_BASE_URL` defaults to `https://integrate.api.nvidia.com/v1`.
- `NVIDIA_MODEL`: NVIDIA model slug, for example `nvidia/z-ai/glm-5.2`.

</details>

<details>
<summary><strong>OpenAI-compatible providers</strong></summary>

Use the shared `LLM_API_KEY`, `LLM_MODEL`, and `LLM_BASE_URL` variables with `LLM_PROVIDER=openai-compatible`. Provider-specific response format, validation, timeout, token, reasoning, and temperature variables are listed in `.env.example`.

</details>

<details>
<summary><strong>Runpod</strong></summary>

- `RUNPOD_API_KEY` / `RUNPOD_OPENAI_BASE_URL`: Runpod OpenAI-compatible/vLLM configuration when `LLM_PROVIDER=runpod` or a request uses `provider=runpod`.
- `RUNPOD_ENDPOINT_ID` / `RUNPOD_ENDPOINT_URL`: Runpod Serverless queue configuration when `LLM_PROVIDER=runpod-serverless` or a request uses `provider=runpod-serverless`.
- `RUNPOD_MODEL_ENDPOINTS`: Optional JSON mapping of Runpod model IDs to endpoint IDs or endpoint URLs when each model uses a different Serverless endpoint.
- A plain Runpod queue URL such as `https://api.runpod.ai/v2/<endpoint-id>` belongs in `RUNPOD_ENDPOINT_URL` with `LLM_PROVIDER=runpod-serverless`; `LLM_PROVIDER=runpod` requires the OpenAI-compatible base URL ending in `/openai/v1`.
- `RUNPOD_TIMEOUT_SECONDS`: Runpod HTTP read timeout. Defaults to `1200` so 10-15 minute cold starts or long generations can finish.
- `RUNPOD_POLL_TIMEOUT_SECONDS`: Runpod Serverless `/status` polling timeout. Defaults to `1200`.
- `RUNPOD_EXECUTION_TIMEOUT_MS` / `RUNPOD_TTL_MS`: Runpod Serverless job policy values. Use `1200000` for 20-minute generation windows.
- `RUNPOD_PARTI_SEED_TIMEOUT_SECONDS`: Optional timeout just for the `caid-technologies/parti-base` seed call. Defaults to `RUNPOD_TIMEOUT_SECONDS`; set lower if you prefer fast catalog repair when Parti is slow.
- `RUNPOD_INPUT_TEMPLATE`: Optional JSON payload template for Runpod workers. Use `{prompt}` and, for single-endpoint multi-model workers, `{model}` placeholders.

</details>

### Observability, media, storage, and external sources

- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`: Optional Langfuse project keys. When both are set, the backend traces each generation request and structured LLM call.
- `LANGFUSE_BASE_URL`: Optional Langfuse host (default `https://cloud.langfuse.com`).
- `LANGFUSE_TRACING_ENVIRONMENT` / `LANGFUSE_TRACING_RELEASE`: Optional Langfuse trace attributes.
- `LANGFUSE_MAX_FIELD_CHARS`: Optional traced payload preview cap (default `20000`).
- `LANGFUSE_ENABLED`: Optional explicit on/off switch. Set to `false` to disable tracing even when keys are present.
- `IMAGE_OUTPUT_ENABLED`: Optional global default for generated product images. The UI and API can opt in per job with `generate_image=true`.
- `IMAGE_PROVIDER`: Image provider. Supports `vertex`, `openai`, `openai-compatible`, `gmi`, `together`, `huggingface`, or `none`.
- `VERTEX_AI_IMAGE_MODEL`: Vertex AI Nano Banana model ID. `gemini-3.1-flash-image` (Nano Banana 2) is the default.
- `VERTEX_AI_IMAGE_RESOLUTION` / `VERTEX_AI_IMAGE_ASPECT_RATIO`: Vertex output controls, defaulting to `1K` and `1:1`.
- `OPENAI_IMAGE_MODEL`: OpenAI image model ID. The example default is `gpt-image-2`.
- `OPENAI_IMAGE_SIZE`: Generated image size, for example `1024x1024`.
- `OPENAI_IMAGE_API_KEY` / `OPENAI_API_KEY`: First-party OpenAI image credentials. `IMAGE_PROVIDER=openai` does not inherit `LLM_API_KEY` or `LLM_BASE_URL`; use `IMAGE_PROVIDER=openai-compatible` plus `IMAGE_BASE_URL`/`IMAGE_API_KEY` or `LLM_BASE_URL`/`LLM_API_KEY` for compatible image endpoints.
- `FIREWORKS_API_KEY`: Enables Video tab self-correction. Auto mode samples the saved video with `ffmpeg`, reviews frames with the Fireworks `kimi-k2p6` image-input model, then applies the review through `ProjectIterator`.
- `FIREWORKS_VIDEO_REVIEW_INPUT_MODE`: `auto` by default. Auto uses the working `kimi-k2p6` frame-review fallback unless native video deployment routing is configured.
- `FIREWORKS_ACCOUNT_ID` / `FIREWORKS_VIDEO_REVIEW_DEPLOYMENT_ID`: Optional Fireworks dedicated deployment routing for native video/audio models. With these set and no explicit frame model override, auto mode uses `qwen3-omni-30b-a3b-instruct`.
- `FIREWORKS_VIDEO_REVIEW_MODEL`: Fireworks review model slug or full deployment path. Defaults to `kimi-k2p6` for frame review. For native video, use `qwen3-omni-30b-a3b-instruct`, `molmo2-4b`, `molmo2-8b`, or a full `accounts/<account>/models/<model>#accounts/<account>/deployments/<deployment>` path.
- `FIREWORKS_BASE_URL` / `FIREWORKS_VIDEO_REVIEW_MAX_FRAMES` / `FIREWORKS_VIDEO_REVIEW_MAX_SECONDS` / `FIREWORKS_VIDEO_REVIEW_NATIVE_FPS` / `FIREWORKS_VIDEO_REVIEW_NATIVE_HEIGHT` / `FIREWORKS_VIDEO_REVIEW_MAX_MEDIA_BYTES` / `FIREWORKS_TIMEOUT_SECONDS`: Optional Fireworks video-review endpoint, preprocessing, and timeout overrides.
- `SUPABASE_S3_ENDPOINT`: Explicit S3-compatible image-storage endpoint. Supabase-client uploads derive their endpoint from `SUPABASE_URL`; direct S3-compatible uploads require this value.
- `SUPABASE_S3_BUCKET`: Supabase Storage bucket for image uploads (default: `contents`).
- `SUPABASE_S3_ACCESS_KEY_ID` / `SUPABASE_S3_SECRET_ACCESS_KEY`: Optional S3-compatible fallback credentials. The normal backend path uploads through the Supabase client with `SUPABASE_URL` plus the service-role/secret key.
- `SUPABASE_IMAGE_SIGNED_URL_SECONDS`: Lifetime for refreshed Supabase Storage read URLs when projects are loaded (default: `86400`).
- `FORMA_CLI_ARTIFACT_BUCKET`: Private bucket for `forma-oss projects push/pull` artifacts (default: `cli-project-artifacts`).
- `FORMA_CLI_ARTIFACT_STORAGE_BACKEND`: Optional `supabase`, `s3-compatible`, or `local` override for CLI artifacts. Supabase-primary hosted deployments use Supabase by default; SQLite/local deployments use the local API storage directory by default.
- `FORMA_CLI_ARTIFACT_STORAGE_DIR`: Local artifact directory when the CLI artifact backend is `local` (default: `./.forma/cli-artifacts`).
- `FORMA_CLI_ARTIFACT_MAX_BYTES`: Maximum size of one transferred artifact (default: `52428800`).
- `FORMA_HOSTED_LATEST_VERSION`: Hosted package version advertised by `/forma/version` (defaults to the installed core version).
- `FORMA_HOSTED_MINIMUM_SUPPORTED_VERSION`: Oldest package version accepted by hosted CLI requests (defaults to the installed core version).
- `FORMA_HOSTED_PROTOCOL_VERSION`: Hosted CLI/API protocol version (default: `1`).
- `FORMA_SUPPORTED_HARDWARE_IR_VERSIONS`: Comma-separated Hardware Intermediate Representation schemas accepted by hosted uploads (default: `0.2`).
- `HF_ARTIFACT_REPO_ID` / `HUGGINGFACE_ARTIFACT_REPO_ID` / `HF_DATASET_REPO_ID`: Optional Hugging Face dataset repo for uploaded benchmark, output, and eval artifacts.
- `HF_ARTIFACT_PATH_PREFIX`: Optional path prefix inside the artifact repo. Defaults to `forma`.
- `EXTERNAL_SOURCE_PROVIDER`: External web/source provider for `workflow=web_research`. Firecrawl is the only active provider for now; legacy `auto` or `tavily` values are normalized to `firecrawl`.
- `FORMA_DEFAULT_GENERATION_WORKFLOW`: Initial frontend workflow, either `web_research` (default) or `default` (Catalog). Request-level workflow selections take precedence.
- `FIRECRAWL_API_KEY` / `FIRECRAWL_MCP_COMMAND`: Enable Firecrawl MCP search and page extraction for the web research workflow.
- `FIRECRAWL_SEARCH_LIMIT` / `FIRECRAWL_MCP_TIMEOUT_SECONDS`: Firecrawl search controls for the web research workflow.

### A2A jobs

- A2A jobs use the database selected by `DATABASE_BACKEND` and `SQLITE_DATABASE_URL`; there is no separate job database.
- `A2A_SOCKET_ENABLED`: Set to `true` to start the optional TCP JSONL A2A socket.
- `A2A_SOCKET_HOST` / `A2A_SOCKET_PORT`: Host and port for the optional TCP JSONL listener.

Set `LLM_PROVIDER=simulation` explicitly when you want deterministic example outputs. The agent-authored `forma.compile_project` path does not call a server-side LLM; live CLI generation failures are reported as errors.

## Frontend (Next.js)
```bash
cd apps/web
npm install
npm run dev
```

Open:
- http://localhost:3000 (UI)
- http://localhost:8000/api/docs (API docs)

Tip: load an example directly with http://localhost:3000/?example=pocket_mp3_player (or any JSON under `apps/web/public/examples/`).
