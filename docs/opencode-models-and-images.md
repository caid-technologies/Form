# OpenCode model switching and project images

The agent model and image model are independent. An OpenCode agent can use one
provider for reasoning and Forma's backend image tool can use another image
provider.

For the current OpenAI-first rollout, use `FORMA_OPENCODE_MODEL=openai/gpt-5.6-sol`
on the connector service for the text/agent default, and configure the Forma
backend with `IMAGE_PROVIDER=openai` plus an OpenAI image API key. Browser chat
requests inherit the runtime default and ignore previously saved browser model choices.

## Switch the agent model

Set `FORMA_OPENCODE_MODEL` to a `provider/model` ID in the connector service
environment and restart the service. Leave it unset to use OpenCode's configured
default. The chat composer does not expose model controls or send a model override.
Previously queued commands with an explicit API model retain that selection
when leased again after a restart.

Use `opencode models` on the runtime to find
IDs supported by its providers. The selected model must be usable by the account
running the connector. An unavailable explicit model fails through OpenCode's
normal error path; the connector does not retry it with another model.

Resolution order:

1. `model` on the Forma command, if supplied.
2. Optional `FORMA_OPENCODE_MODEL` in the connector service environment.
3. OpenCode's own configuration/default resolution for that process.

The connector passes the resolved explicit choice as
`model: { providerID, modelID }` to `POST /session/:id/prompt_async`.
Model IDs may contain further slashes (for example, an OpenRouter model).
The generated project policy continues to own the restricted agent and tools.

OpenCode's interactive `/models` selection affects that OpenCode environment;
the service can run under a different Windows account and creates fresh local
sessions. Use an explicit service default for predictable
hosted requests. Null commands deliberately inherit the default at execution
time; only explicit command selections are pinned across retries.

### Vertex tool schema compatibility

The restricted `update_project`, `compile_project`, and `validate_project` tools
advertise `project_ir` as a **JSON string**. Serialize the complete HardwareIntermediateRepresentation
object into that argument; its parameter description includes the canonical
HardwareIntermediateRepresentation JSON Schema, including nested requirements and recursive definitions.
The backend decodes and validates it with the same HardwareIntermediateRepresentation model after
capability authorization. Older callers can continue sending an object.

This avoids an OpenCode Vertex adapter limitation that drops `$ref`/`$defs` from
function parameters. With an embedded object schema, Vertex can reject fields
such as `project_ir.overview` for lacking a type before the agent runs any tool.
That also blocks image requests, because the request advertises the authoring
tools alongside the image tool. Updating the backend and restarting it refreshes
the tool definitions for new OpenCode sessions; changing credentials or the
image model does not address this schema error.

## Generate an image

Ask Forma Agent to generate a concept image of the saved project. The restricted
MCP surface includes `forma.opencode.generate_image` with two arguments:

```json
{
  "prompt": "Generate an isometric concept image of this bracket",
  "request_id": "bracket-concept-1"
}
```

The capability supplies project and owner identity. The tool resolves the backend's
`IMAGE_PROVIDER` through Forma's existing provider factory, generates one image,
and attaches it to a project revision
through Forma's existing image storage and project persistence. It does not run
CAD generation. The project preview displays the saved image after the command
completes and the workspace reloads the project. Future IR edits preserve that
image. Image bytes and storage URLs are omitted from restricted project-tool
responses to avoid putting them in the agent context.

Image prompts preserve the saved design's geometry, part count, materials and
constraints. Mechanical designs are not described as electronics builds; boards,
displays, wiring and other features must already belong to the saved design.
The prompt includes saved CAD operations and mechanism parameters when present.
CAD-only image sequences change the camera without inventing hidden internals.
Images remain concept visualizations, not exact CAD renders. Request a new image
after updating the backend; already saved images are not regenerated automatically.

### Conversation continuity

OpenCode conversations save through `/chats` even when legacy hosted generation
is disabled. Opening a project reads its saved transcript before showing any
project context; it never saves a fabricated user message from the project title.
The chat list prefers the actual conversation already linked to the project.

For projects affected before transcript persistence was enabled, the authenticated
`GET /opencode/projects/{project_id}/history` endpoint recovers the last 40 recorded
turns across sessions. It checks project ownership before decrypting user requests
and returns only user messages and sanitized public agent replies. Reading this
archive does not overwrite a saved transcript or start another agent command.
Recovery depends on the retained command/event records and their existing
encryption key; it cannot recover messages that were never recorded.

New agent sessions inherit the original brief and up to 15 recent requests for
the same owner, project and connector. Rendering or recompiling also preserves
the original saved brief and chat identity. Deploy both the web frontend and
backend for chat restoration; image prompt changes require the backend update.

## Configure the image provider

Configure these settings on the **Forma backend serving the restricted MCP URL**:

### OpenAI

```dotenv
# Supply the key securely in the backend environment; do not commit it.
IMAGE_PROVIDER=openai
OPENAI_IMAGE_API_KEY=<your OpenAI API key>
OPENAI_IMAGE_BASE_URL=https://api.openai.com/v1
OPENAI_IMAGE_MODEL=gpt-image-2.5-sunburst
OPENAI_IMAGE_SIZE=1024x1024
OPENAI_IMAGE_QUALITY=medium
OPENAI_IMAGE_OUTPUT_FORMAT=png
```

`OPENAI_API_KEY` is the existing fallback if `OPENAI_IMAGE_API_KEY` is absent.
The example explicitly selects `gpt-image-2.5-sunburst`; Forma's code-level
fallback remains unchanged, so deployments can pin a different GPT Image model
without a code change. OpenCode login credentials are not a substitute for the
backend image API key.

### GMI Cloud

```dotenv
# Supply the key securely in the backend environment; do not commit it.
IMAGE_PROVIDER=gmi
GMI_IMAGE_API_KEY=<your GMI API key>
GMI_IMAGE_MODEL=gpt-image-2-generate
GMI_IMAGE_SIZE=1024x1024
GMI_IMAGE_QUALITY=medium
GMI_IMAGE_OUTPUT_FORMAT=png
```

`GMI_API_KEY`, `GMI_CLOUD_API_KEY`, and `GMICLOUD_API_KEY` are existing key
fallbacks. The provider handles request-queue submission and polling for this
model. Other supported GMI models use their existing model-specific settings.
Set `IMAGE_PROVIDER=gmi` explicitly when OpenAI credentials are also present.
A missing GMI key fails the request; it never silently switches to OpenAI.

GMI queue results can contain a temporary image URL. The tool downloads it before
saving, so local projects retain inline data and remote projects use Forma's image
storage rather than depending on a temporary provider URL. Downloads require
public HTTPS destinations, validate redirects, pin the resolved address while
verifying TLS against the hostname, and accept only PNG, JPEG, or WebP up to
30 MiB. Provider URLs and credentials are never included in tool responses.

Provider, model, endpoint, and credentials remain server-owned; the agent cannot
override them in tool arguments. Without `IMAGE_PROVIDER`, the existing provider
factory infers a provider from backend credentials, preferring OpenAI when both
OpenAI and GMI keys are present. `IMAGE_PROVIDER=none` (or another disabled alias)
disables this tool. `IMAGE_OUTPUT_ENABLED=false` disables automatic image output
but still permits an explicit image-tool request with a configured provider.
Images are generated only by an explicit tool call, not on every authoring turn.

Reuse `request_id` for a retry of a saved image request. A completed request
returns its existing revision without another provider call; changing the prompt
under that ID is rejected. This is completed-request deduplication, not an
exactly-once billing guarantee: a crash after the provider succeeds but before
the revision is stored, or simultaneous requests on separate backend workers,
can generate twice. No provider calls are automatically retried by this tool.

Concept images do not establish CAD geometry, dimensions, or manufacturability.
Storage/provider errors return bounded messages without raw provider responses.

## Deploy the paired changes

1. Apply `20260917000100_opencode_command_model.sql` to Supabase before running
   the new backend. Existing SQLite databases add the nullable column on startup.
2. Deploy the Forma backend and set its image provider, credentials, and model.
3. Deploy the paired `local-server-config` connector update and restart its
   service. Existing commands without a model remain compatible.
4. Deploy the Forma web update after the connector. An older connector ignores
   the new model field and does not allow the image tool.
5. Verify on the mini-PC: with `FORMA_OPENCODE_MODEL=openai/gpt-5.6-sol`,
   send a chat request using the configured runtime model,
   generate one OpenAI image, reload the project, and
   make a CAD edit to confirm the image remains.

Local automated checks use simulated provider/connector responses. Live OpenCode
model access, paid provider generation and Windows service rollout require host
acceptance after deployment.

## References

- [OpenCode model selection](https://opencode.ai/docs/models/)
- [OpenCode server API](https://opencode.ai/docs/server/)
- [OpenAI image generation API](https://developers.openai.com/api/docs/guides/image-generation)
