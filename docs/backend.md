# Backend

The backend is a **FastAPI** service that orchestrates agents, validates netlists, renders diagrams, and stores generated projects.

## Key modules
- `apps/api/main.py` – FastAPI app and API routes
- `apps/api/a2a.py` – A2A broker, REST/WebSocket/TCP/MCP handlers
- `forma_core/generation.py` – high-level generation API
- `forma_core/agents/orchestrator.py` – multi-agent pipeline
- `forma_core/models.py` – Pydantic Hardware Intermediate Representation schemas
- `forma_core/validation.py` – rule-based electrical checks
- `forma_core/llm_providers.py` – provider-agnostic structured LLM adapters
- `forma_core/image_providers.py` – optional generated product image adapters
- `forma_core/config/runtime.py` – deployment and runtime gating helpers
- `forma_core/observability.py` – optional Langfuse tracing helpers
- `forma_core/database.py` – SQLAlchemy models + DB setup
- `forma_core/utils.py` – Mermaid and SVG schematic generation
- `apps/api/storage.py` – Supabase Storage image uploads, disabled in development mode
- `apps/api/seed_db.py` – seed component templates

## API endpoints
- `POST /api/generate` – asynchronously run the pipeline off the API event loop and return IR + diagrams
- `POST /api/alpha-signups` – capture alpha launch interest while deployed generation is gated
- `GET /api/a2a/capabilities` – inspect agent transports and actions
- `PUT /api/a2a/agents/{agent_id}` – register an agent listener
- `POST /api/a2a/messages` – submit or broker an A2A message
- `GET /api/a2a/agents/{agent_id}/events` – long-poll queued A2A events
- `GET /api/a2a/jobs` – list persisted A2A job metadata, including generation `source_usage`
- `GET /api/a2a/jobs/{job_id}` – fetch one persisted A2A job metadata record, including generation `source_usage`
- `GET /api/data-sources` – list optional generation context sources and their limits
- `GET /api/logs/backend` – tail recent backend and uvicorn log lines for the frontend LOGS tab
- `WebSocket /api/a2a/socket/{agent_id}` – bidirectional A2A event stream
- `POST /api/mcp` and `POST /api/a2a/mcp` – MCP-style JSON-RPC tool endpoint
- `POST /api/validate` – validate a user-supplied netlist
- `GET /api/components` – list component templates
- `GET /api/projects` – list generated projects
- `GET /api/projects/{project_id}` – fetch a stored project
- `POST /api/seed` – re-seed the component database
- `GET /api/debug/config` – inspect LLM, database, image-provider, and image-storage resolution (no secrets)
- `GET /api/runtime/config` – canonical user-scoped generation contract used by the frontend (selected/configured LLMs, image behavior, workflow default, and provider-setup requirements)

## Orchestration layer
The orchestrator runs an **ADK-style 7-agent pipeline** (implemented in `forma_core/agents/orchestrator.py`). Live agent calls go through `forma_core.llm`, which exposes a provider-agnostic structured JSON interface that maps directly to the Hardware Intermediate Representation. If no live provider is configured (or generation fails), the backend falls back to deterministic example projects for a reliable local demo.

## Reusable core package
Generation behavior is packaged under `forma_core` so the API server, CLI, smoke tests, workers, and future services all share one implementation. Use `forma_core.generation` for high-level generation, `forma_core.models` for Hardware Intermediate Representation schemas, `forma_core.validation` for electrical checks, `forma_core.llm` for provider resolution and structured generation, `forma_core.images` for image providers and visual prompt construction, `forma_core.runtime` for deployment gating, and `forma_core.selectors` for parsing `provider/model` selectors. The legacy backend core modules are compatibility wrappers.

## A2A layer
The A2A layer exposes Forma to external agents as a tool server and lightweight broker. REST long-polling, WebSocket, and MCP-style JSON-RPC are always mounted and authenticated in hosted mode. Job metadata uses the primary application database, so local jobs share `SQLITE_DATABASE_URL` with projects and hosted jobs share the Supabase schema. The TCP JSONL listener is opt-in with `A2A_SOCKET_ENABLED=true`; it remains loopback/local-only without a service credential.

LLM configuration behavior:

- Runtime precedence is fixed in one backend resolver: explicit request override, saved integration, environment, then provider default. `/api/runtime/config` is the client authority; clients must not reconstruct readiness or defaults from environment variables or integration form fields.
- `LOG_LEVEL`: backend logging level, for example `INFO` or `DEBUG`
- `BACKEND_LOG_FILE`: optional log file for backend and uvicorn logs, for example `./forma-backend.log`. The development launchers default this to `.logs/backend-dev.log` so the frontend LOGS tab can tail local backend output.
- `FORMA_DEBUG=true`: include redacted traceback/context debug payloads in API errors and failed job metadata; this also defaults backend logging to `DEBUG` when `LOG_LEVEL` is unset
- `FORMA_DEVELOPMENT_MODE=true`: selects SQLite for the complete application database even when remote Supabase env vars are present; Supabase Storage writes are disabled and image data stays inline in the SQLite project record. `FORMA_DEV_MODE` is a compatibility alias.
- `FORMA_DEPLOYMENT_MODE`: `local` by default or `hosted`; invalid values fail startup. Hosted mode requires a configured deployment provider or signed-in user's BYOK provider for `/api/generate` and cannot run with `FORMA_DEVELOPMENT_MODE=true`.
- `FORMA_HOSTED_CHAT_ENABLED`: Reversible hosted-chat maintenance flag. It defaults to `true` locally and `false` in hosted mode. When disabled, hosted chat/generation mutations return `503` while chat/project reads, local CLI compilation, and CLI project uploads remain available.
- `FORMA_AUTHORING_MODE_ENABLED`: Enables the Forma Agent authoring UI. In hosted Clerk mode, every signed-in Clerk or Forma CLI user can create projects through `/opencode/sessions`; no admin role or email allowlist is required. Anonymous callers and service identities cannot use user authoring endpoints, and projects/sessions remain scoped to their owner. `FORMA_OPENCODE_ALLOWED_EMAILS` is no longer used. Forma Agent authoring and its context/readiness routes remain available to signed-in users while the legacy hosted-chat pipeline is disabled.
- `REDIS_URL`: Redis connection URL for cached `/projects` and `/my/projects` responses. In production, set it or the complete Upstash REST pair below, plus `REDIS_CACHE_PREFIX`; runtime cache failures still fall back to the database.
- `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`: server-only Upstash REST credentials that can replace `REDIS_URL`, which avoids persistent Redis socket requirements on serverless deployments.
- `PROJECTS_CACHE_TTL_SECONDS`: project-list cache lifetime in seconds, default `60`; successful project writes invalidate all list variants immediately.
- `REDIS_CACHE_PREFIX`: Redis key namespace, required when `FORMA_DEVELOPMENT_MODE=false` and defaulting to `forma` only for development-mode cache usage.
- `REDIS_SOCKET_TIMEOUT_SECONDS`: Redis connect/read timeout, default `0.25`; failures open a 30-second local circuit breaker.
- `LLM_PROVIDER`: `vertex`, `anthropic`, `baseten`, `gemini`, `gmi`, `huggingface`, `cloudflare`, `nvidia`, `openai`, `openai-compatible`, `runpod`, `runpod-serverless`, or `simulation`. Use `runpod` for Runpod OpenAI-compatible/vLLM endpoints and `runpod-serverless` for queue-style `/runsync` workers.
- `LLM_MODEL`: provider model ID
- `/api/generate` accepts optional `provider` and `model` fields for runtime switching. The backend validates them before generation and records requested/actual provider/model metadata on the project.
- `/api/generate` accepts `data_sources: ["past_jobs"]` and an optional `past_jobs_limit` (1-8, default 3). This retrieves the signed-in owner's relevant completed generation jobs, compacts their stored project outputs into bounded prompt context, and requires no embedding model or vector database. The current request always takes precedence over historical examples.
- `LLM_ALLOWED_PROVIDERS`: optional comma-separated allowlist for runtime provider overrides. If unset, configured providers detected from env plus `simulation` are allowed.
- `VERTEX_AI_ALLOWED_MODELS` / `OPENAI_ALLOWED_MODELS` / `BASETEN_ALLOWED_MODELS` / `HUGGINGFACE_ALLOWED_MODELS` / `CLOUDFLARE_ALLOWED_MODELS` / `NVIDIA_ALLOWED_MODELS` / `OPENAI_COMPATIBLE_ALLOWED_MODELS` / `GEMINI_ALLOWED_MODELS` / `RUNPOD_ALLOWED_MODELS`: optional comma-separated allowlists for runtime model overrides. If unset, runtime model overrides are limited to configured default/fallback models for the selected provider.
- `GOOGLE_CLOUD_PROJECT` / `VERTEX_AI_PROJECT`, `GOOGLE_CLOUD_LOCATION` / `VERTEX_AI_LOCATION`, and `VERTEX_AI_MODEL`: Vertex AI routing when `LLM_PROVIDER=vertex`; authentication uses Google Cloud Application Default Credentials. `VERTEX_AI_MODEL` defaults to `gemini-3.7-flash`.
- `GCP_PROJECT_NUMBER`, `GCP_SERVICE_ACCOUNT_EMAIL`, `GCP_WORKLOAD_IDENTITY_POOL_ID`, and `GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID`: optional keyless Vertex authentication for Vercel through workload identity federation. The runtime exchanges Vercel's per-request OIDC token for short-lived Google credentials.
- `OPENAI_API_KEY`: first-party OpenAI API key when `LLM_PROVIDER=openai`
- `OPENAI_MODEL`: first-party OpenAI model alias for `LLM_MODEL`
- `OPENAI_RESPONSE_FORMAT`: OpenAI response format, defaulting to `json_schema`; `json_object` and `none` are also supported
- `OPENAI_TIMEOUT_SECONDS`: first-party OpenAI read timeout, defaulting to `300`
- `OPENAI_REASONING_EFFORT`: optional reasoning effort for GPT-5/o-series models, for example `low`
- `OPENAI_TEMPERATURE`: optional first-party OpenAI sampling temperature. Omitted by default so models that only support their default temperature can run
- `OPENAI_PROJECT_ID` / `OPENAI_ORG_ID`: optional OpenAI project and organization routing headers
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`: optional Langfuse project keys. When both are set, Forma traces each generation request and structured LLM step.
- `LANGFUSE_BASE_URL`: optional Langfuse host, defaulting to `https://cloud.langfuse.com`.
- `LANGFUSE_TRACING_ENVIRONMENT` / `LANGFUSE_TRACING_RELEASE`: optional trace attributes for environment and release filtering.
- `LANGFUSE_MAX_FIELD_CHARS`: optional per-field payload cap for traced prompt/output previews, defaulting to `20000`.
- `LANGFUSE_ENABLED=false`: explicit opt-out when project keys are present in the runtime environment.
- `IMAGE_OUTPUT_ENABLED=true`: make product concept image generation the default. Requests can opt in per job with `generate_image=true`
- `IMAGE_PROVIDER`: `vertex`, `openai`, `openai-compatible`, `gmi`, `together`, `huggingface`, or `none`
- `VERTEX_AI_IMAGE_MODEL`: Nano Banana model on Vertex AI; defaults to `gemini-3.1-flash-image`
- `VERTEX_AI_IMAGE_RESOLUTION` / `VERTEX_AI_IMAGE_ASPECT_RATIO`: Vertex image size controls, defaulting to `1K` and `1:1`
- `OPENAI_IMAGE_MODEL`: first-party OpenAI image model, for example `gpt-image-2`
- `OPENAI_IMAGE_SIZE`: image output size, for example `1024x1024`
- `HUGGINGFACE_IMAGE_MODEL` / `HUGGINGFACE_IMAGE_INFERENCE_PROVIDER`: Hugging Face text-to-image model and underlying inference provider when `IMAGE_PROVIDER=huggingface`
- `HUGGINGFACE_IMAGE_MODEL_REVISION` / `HUGGINGFACE_IMAGE_MODEL_LICENSE`: optional policy metadata recorded with stored Hugging Face image outputs
- `SUPABASE_S3_ENDPOINT`: explicit endpoint required for direct S3-compatible image uploads; Supabase-client uploads derive their endpoint from `SUPABASE_URL`
- `SUPABASE_S3_BUCKET`: Supabase Storage bucket for reference and generated product images, defaulting to `contents`
- `SUPABASE_S3_ACCESS_KEY_ID` / `SUPABASE_S3_SECRET_ACCESS_KEY`: optional S3-compatible fallback credentials. The normal backend path writes through the Supabase client using `SUPABASE_URL` plus the service-role/secret key; `FORMA_DEVELOPMENT_MODE=true` disables these image uploads
- `SUPABASE_IMAGE_SIGNED_URL_SECONDS`: lifetime for refreshed Supabase Storage read URLs when projects are loaded, defaulting to `86400`
- `SUPABASE_STORAGE_PUBLIC_BASE_URL`: optional public object URL base; defaults from `SUPABASE_URL` or the S3 endpoint
- `LLM_FALLBACK_MODEL`: optional fallback model
- `LLM_TIMEOUT_SECONDS`: generic read timeout. OpenAI-compatible endpoints default to `90`
- `LLM_REASONING_EFFORT`: optional generic reasoning effort for compatible endpoints that support it
- `LLM_TEMPERATURE`: optional generic sampling temperature. OpenAI-compatible endpoints default to `0.2`; set `default`, `none`, or `omit` to omit it
- `RUNPOD_MAX_TOKENS` / `LLM_MAX_TOKENS`: output token budget for structured generation. A budget is now always sent. When no `*_MAX_TOKENS` is set it defaults to `8192`, and large schemas (for example `MechanicalNotes`, whose JSON schema is ~6,656 chars) are raised to a `6000` floor so big records are not truncated mid-string. Small schemas keep the configured value. Set `RUNPOD_MAX_TOKENS=8192` on parti-base backends.
- Structured calls do one bounded retry: on a validation failure the request is re-sent once with a larger budget (doubled, floored at `6000`, capped at `16384`). Truncated-but-recoverable JSON is salvaged with `json-repair` (structure closed, half-written trailing item pruned, then full pydantic validation), so a completion cut off at the token cap still yields a valid record. If both attempts fail the backend returns `502 llm_output_invalid` instead of the generic `500 generation_failed`.
- `RUNPOD_RESPONSE_FORMAT` / `LLM_RESPONSE_FORMAT`: response format for the endpoint, defaulting to `json_object`. Set `json_schema` for vLLM grammar-constrained JSON (requires vLLM >= 0.6.3 on the Runpod worker; the first request for a large schema pays a grammar-compile latency). `json_schema` does not prevent truncation, so the token budget, salvage, and retry above still apply.
- `BASETEN_API_KEY` / `BASETEN_BASE_URL`: Baseten Model APIs configuration when `LLM_PROVIDER=baseten` or a request uses `provider=baseten`. `BASETEN_BASE_URL` defaults to `https://inference.baseten.co/v1`.
- `BASETEN_MODEL`: Baseten model slug, for example `deepseek-ai/DeepSeek-V4-Pro`
- `HF_TOKEN` / `HUGGINGFACE_API_KEY` / `HUGGINGFACE_HUB_TOKEN`: Hugging Face Inference Providers token when `LLM_PROVIDER=huggingface` or a request uses `provider=huggingface`
- `HUGGINGFACE_BASE_URL`: Hugging Face OpenAI-compatible router URL. Defaults to `https://router.huggingface.co/v1`
- `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY`: Anthropic Claude key when `LLM_PROVIDER=anthropic` or a request uses `provider=anthropic`
- `ANTHROPIC_MODEL`: Claude model ID. Defaults to `claude-opus-5`.
- `ANTHROPIC_BASE_URL`: Claude API base URL. Defaults to `https://api.anthropic.com/v1`
- `HUGGINGFACE_MODEL`: Hugging Face model ID, for example `Qwen/Qwen2.5-Coder-3B-Instruct:nscale`
- `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`: Cloudflare AI configuration when `LLM_PROVIDER=cloudflare` or a request uses `provider=cloudflare`. The OpenAI-compatible base URL is derived from the account ID; `CLOUDFLARE_BASE_URL` can override it.
- `CLOUDFLARE_MODEL`: Cloudflare Workers AI model ID. Defaults to the Free-plan-compatible `@cf/google/gemma-4-26b-a4b-it`; `CLOUDFLARE_RESPONSE_FORMAT` defaults to `json_schema`.
- `NVIDIA_API_KEY` / `NVIDIA_BASE_URL`: NVIDIA Build/NIM configuration when `LLM_PROVIDER=nvidia` or a request uses `provider=nvidia`. `NVIDIA_BASE_URL` defaults to `https://integrate.api.nvidia.com/v1`.
- `NVIDIA_MODEL`: NVIDIA model slug, for example `nvidia/z-ai/glm-5.2`
- `RUNPOD_API_KEY` / `RUNPOD_OPENAI_BASE_URL`: Runpod OpenAI-compatible/vLLM configuration when `LLM_PROVIDER=runpod` or a request uses `provider=runpod`
- `RUNPOD_ENDPOINT_ID` / `RUNPOD_ENDPOINT_URL`: Runpod Serverless queue configuration when `LLM_PROVIDER=runpod-serverless` or a request uses `provider=runpod-serverless`
- `RUNPOD_MODEL_ENDPOINTS`: optional JSON object mapping model IDs to Runpod endpoint IDs or endpoint URLs when each model is hosted on a separate Serverless endpoint
- `RUNPOD_INPUT_TEMPLATE`: optional JSON payload template for Runpod workers. Use `{prompt}` and, for single-endpoint multi-model workers, `{model}` placeholders.
- `RUNPOD_TIMEOUT_SECONDS`: Runpod HTTP read timeout. Defaults to `1200` for 10-15 minute cold starts or long generations.
- `RUNPOD_POLL_TIMEOUT_SECONDS`: Runpod Serverless `/status` polling timeout. Defaults to `1200`.
- `RUNPOD_EXECUTION_TIMEOUT_MS` / `RUNPOD_TTL_MS`: Runpod Serverless job policy values. Use `1200000` for 20-minute generation windows.
- `RUNPOD_PARTI_SEED_TIMEOUT_SECONDS`: optional timeout for the `caid-technologies/parti-base` seed call. Defaults to `RUNPOD_TIMEOUT_SECONDS`; set lower to fall back to catalog repair faster.
- `STRICT_LLM=true` (default) fails fast when model availability validation is enabled and the requested model is unavailable
- `STRICT_LLM=false` allows fallback to the configured fallback model
- Gemini-specific env vars remain supported as aliases for existing deployments

## Validation
Validation is run after the netlist step. Critical issues trigger a repair loop that re-invokes the wiring agent before finalizing the IR.

## Startup behavior
On startup the server:
- Validates hosted persistence configuration and refuses an implicit SQLite fallback
- Initializes the selected DB schema
- Auto-seeds component templates if the catalog is empty

## Running locally
Run the server from the repo root:

```bash
uvicorn apps.api.main:app --reload --port 8000
```

To make backend logs visible in the local frontend LOGS tab when running uvicorn directly, set `BACKEND_LOG_FILE`:

```bash
BACKEND_LOG_FILE=.logs/backend-dev.log uvicorn apps.api.main:app --reload --port 8000
```

## Local FormaWorker

The OpenCode execution worker runs as a restricted Windows service named
`FormaBackend` and binds only to `127.0.0.1:8000`. It is not the browser API
origin and must not be published through Cloudflare Tunnel. Cloud Forma remains
authoritative for browser traffic, accepted projects, revisions, and display.

Use `FORMA_DEPLOYMENT_MODE=local` for the worker and keep local execution state
separate from cloud production persistence. The worker uses local execution mode
for authoring, compilation, validation, and
resume state, then sends accepted snapshots outbound through the authenticated
CLI delivery flow. Host service provisioning, account creation, ACLs, and worker
environment values belong to `caid-technologies/local-server-config`; keep
passwords, service tokens, and provider keys out of this repository.

Validate the cloud environment without printing secrets:

```bash
python scripts/operations/verify-production-env.py --environment production --require-live-clerk
```

Then verify local authoring and explicit delivery:

```bash
forma-oss init ./my-project
forma-oss build "a low-voltage plant monitor" --path ./my-project
forma-oss login
forma-oss projects deliver --path ./my-project --yes --json
```

Run generation directly through the sole Forma Core CLI with `--llm provider/model`:

```bash
forma-core generate "plant watering monitor" --llm openai/gpt-5.5
forma-core generate "plant watering monitor" --llm runpod/caid-technologies/parti-base
```

Live CLI generation and iteration are strict: provider, model, and pipeline failures return a nonzero exit code instead of producing fallback output. Simulated output requires the explicit `--simulation` flag.

Smoke-test configured LLM providers with a tiny structured prompt:

```bash
./scripts/models/verify-llm-providers.py --list
./scripts/models/verify-llm-providers.py --config-only
./scripts/models/verify-llm-providers.py --save
./scripts/models/run-llm-smoke-tests.py
./scripts/models/verify-llm-providers.py --llm openai/gpt-5.5
./scripts/models/verify-llm-providers.py --llm runpod/caid-technologies/parti-base --timeout-seconds 1200
./scripts/models/verify-llm-providers.py --llm baseten/deepseek-ai/DeepSeek-V4-Pro
./scripts/models/verify-llm-providers.py --llm huggingface/Qwen/Qwen2.5-Coder-3B-Instruct:nscale
./scripts/models/verify-llm-providers.py --llm cloudflare/@cf/google/gemma-4-26b-a4b-it
./scripts/models/verify-llm-providers.py --llm nvidia/nvidia/z-ai/glm-5.2
./scripts/models/sample.py "Describe a low-voltage plant watering monitor with OLED status"
./scripts/models/sample_async.py --concurrency 4 "Describe a low-voltage plant watering monitor with OLED status"
```

Saved smoke-test reports are written to `.logs/llm-smoke/` by default, with `.logs/llm-smoke/latest.json` overwritten on each saved run. `scripts/models/sample.py` writes model comparison reports to `.logs/model-samples/` and `.logs/model-samples/latest.json`. `scripts/models/sample_async.py` writes the same report format while running selected models concurrently with `--concurrency`. The automated runner accepts `LLM_SMOKE_LLM`, `LLM_SMOKE_CONFIG_ONLY`, `LLM_SMOKE_TIMEOUT_SECONDS`, and `LLM_SMOKE_OUTPUT_DIR`.

Run against Claude:

```bash
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=your_anthropic_api_key_here ANTHROPIC_MODEL=claude-opus-5 uvicorn apps.api.main:app --reload --port 8000
```
