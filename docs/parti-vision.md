# Parti-Vision through Runpod

Forma's default generation workflow recognizes `caid-technologies/parti-vision`
as the current Parti model and retains `caid-technologies/parti-base` for legacy
text-only use. Both use one seed adapter and the existing Runpod provider.

## Configure and run

Serve the actual model on a Runpod OpenAI-compatible/vLLM endpoint. The endpoint
must advertise the same model ID you select; changing Forma's configuration does
not install or deploy the model on a worker.

Add these values to your local `.env`, using your own endpoint and credential:

```dotenv
LLM_PROVIDER=runpod
RUNPOD_OPENAI_BASE_URL=https://api.runpod.ai/v2/YOUR_ENDPOINT/openai/v1
RUNPOD_API_KEY=YOUR_RUNPOD_KEY
RUNPOD_MODEL=caid-technologies/parti-vision
FORMA_STRICT_GENERATION=true
```

If your deployment restricts providers/models, include `runpod` in
`LLM_ALLOWED_PROVIDERS` and the selected model in `RUNPOD_ALLOWED_MODELS`.
Existing `RUNPOD_OPENAI_MODEL` or `LLM_MODEL` values take precedence over
`RUNPOD_MODEL`; remove stale overrides when changing the configured default.

From an installed checkout:

```bash
forma-core generate "A low-voltage desk climate monitor" --workflow default --provider runpod --model caid-technologies/parti-vision --output parti-project.json
```

With a reference image:

```bash
forma-core generate "Use this sketch for a desk climate monitor" --workflow default --provider runpod --model caid-technologies/parti-vision --image-file sketch.png --output parti-project.json
```

These commands call your configured endpoint and may incur provider charges.
The seed adapter belongs to `--workflow default`; this change does not reroute
the separate `web_research` workflow or hosted OpenCode chat. Use the OpenAI
endpoint ending in `/openai/v1`, not the queue-style `/runsync` API.

## Model upgrades and legacy behavior

For a future vision-compatible Parti checkpoint or a custom served name, set
`RUNPOD_PARTI_MODEL=your-org/parti-vision-v2`. This registers that identity with the
same adapter and supplies the Runpod default if the existing model overrides are
unset. An explicit `--model` still takes precedence. No orchestrator change is
needed. The configured alias must implement the Parti seed and image-input
contract; this setting is not automatic model-capability discovery.

Select `caid-technologies/parti-base` explicitly for legacy text-only requests.
Its existing seed parsing, quality checks, catalog repair, and adapter metadata
remain supported. Image attachments are rejected with a model-specific error
instead of being silently discarded. Unrelated Runpod models keep their existing
generation path.

## Output contract and limits

Forma requests a concise JSON seed containing `project_title`, `summary`, and
`hardware_roles`. The parser retains support for fenced JSON and existing title
and summary aliases. Strict generation rejects missing/malformed seeds,
placeholder summaries, synthetic seeds, and component-like project titles.
Failures identify the selected model; `RUNPOD_PARTI_SEED_TIMEOUT_SECONDS` remains
available for the seed request.

Reference-image bytes are sent as an OpenAI-compatible `image_url` data URI only
in the Parti-Vision seed request. Text-only requests remain plain text. Images
are not copied into unrelated text-only pipeline stages. PNG, JPEG, WebP, and GIF
MIME types are accepted at this boundary; the serving runtime must support the
chosen image format.

This is an upgrade of the existing **seed plus deterministic catalog-repair**
integration. It does not import every field of a complete Parti blueprint or
guarantee that final catalog geometry reproduces a reference image. Model and
adapter identity are recorded in project metadata. Existing generation fallback
policy is preserved; the example enables strict mode to fail on unusable output.

Offline regression coverage:

```bash
python -m unittest tests.agents.test_parti tests.providers.test_llm_runtime tests.providers.test_structured_repair -v
```

The tests mock provider responses. A live Runpod text/image smoke test is still
required to validate a particular deployed checkpoint.

Model reference: [Parti-Vision model card](https://huggingface.co/caid-technologies/parti-vision).
