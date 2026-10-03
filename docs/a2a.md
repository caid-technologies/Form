# Agent-To-Agent (A2A)

Forma exposes the same hardware generation capability through several agent-friendly transports. In hosted mode,
every transport requires a Clerk session or a configured service credential. The unauthenticated exception applies
only when `FORMA_DEPLOYMENT_MODE=local` and `FORMA_AUTH_MODE=local`; it is never inferred for hosted deployments.

## Transports
- **REST:** `GET /api/a2a/capabilities`, `PUT /api/a2a/agents/{agent_id}`, `POST /api/a2a/messages`, long-poll `GET /api/a2a/agents/{agent_id}/events`, and job metadata lookup under `GET /api/a2a/jobs`
- **WebSocket:** `/api/a2a/socket/{agent_id}`
- **TCP JSONL socket:** optional newline-delimited JSON socket enabled with `A2A_SOCKET_ENABLED=true`
- **MCP Streamable HTTP (JSON responses):** `POST /api/mcp` or `POST /api/a2a/mcp`

`FORMA_A2A_API_KEY` is the preferred service credential for A2A and A2A/MCP transports. The existing
`FORMA_MCP_API_KEY` is also accepted for A2A clients for compatibility. Both credentials must be at least 32 characters and are sent as
`Authorization: Bearer <credential>`. Agent queues are scoped to the authenticated user or service principal that
registered them.

Job metadata is stored in the primary application database selected by `DATABASE_BACKEND`. Local jobs live in the same SQLite file selected by `SQLITE_DATABASE_URL`; hosted jobs live in the same Supabase database as projects and chats. On upgrade, rows from the retired `forma_jobs.db` file are imported without overwriting jobs already present in the primary database. The legacy file is retained. The store keeps compact metadata only: payloads have image data redacted, results are summarized instead of storing full generated IR blobs, and `source_usage` records whether a generation job used the Catalog/data warehouse, Web Research/Firecrawl, past-job context, or a combination.

## Message Shape
```json
{
  "type": "task",
  "job_id": "job-build-001",
  "action": "forma.generate_project",
  "sender": "agent_alpha",
  "recipient": "forma",
  "correlation_id": "build-001",
  "payload": {
    "prompt": "ESP32 soil moisture monitor with OLED",
    "workflow": "default",
    "data_sources": ["past_jobs"],
    "past_jobs_limit": 3,
    "generate_image": false
  }
}
```

Server-owned actions queue an `ack` event followed by a `result` or `error` event for the sender. Messages addressed to another agent are brokered into that agent's queue. Every submitted message is persisted with a `job_id` and lifecycle status.

## Error Contract
Transport errors never return raw exception text, provider responses, database details, prompts, or filesystem paths. A2A error events and REST errors use a stable `code`, public `message`, and generated `correlation_id` in the form `err_<uuid-hex>`. MCP keeps the JSON-RPC numeric error code and places the Forma `code` and `correlation_id` under `error.data`. Use the correlation ID to locate redacted operator diagnostics in server logs.

Persisted job failures use the same public message contract and retain only the error code, error type, and correlation ID in `error_debug`; exception text and tracebacks are not persisted.

`data_sources: ["past_jobs"]` adds lightweight, owner-scoped retrieval over completed generation jobs. Forma ranks recent stored project outputs by lexical overlap with the new prompt and supplies a compact context window to the generator. Retrieval and generation run asynchronously; no embeddings or external retrieval infrastructure are used.

## REST Listen Flow
1. Register an agent:
```bash
curl -X PUT http://localhost:8000/api/a2a/agents/agent_alpha \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $FORMA_A2A_API_KEY" \
  -d '{"name":"Agent Alpha","capabilities":["hardware_planning"],"transports":["rest"]}'
```

2. Send a task:
```bash
curl -X POST http://localhost:8000/api/a2a/messages \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $FORMA_A2A_API_KEY" \
  -d '{"sender":"agent_alpha","recipient":"forma","action":"forma.generate_project","payload":{"prompt":"ESP32 soil moisture monitor with OLED","generate_image":false}}'
```

Set `payload.generate_image` to `true` only for jobs that should call the configured image model. If the image model fails, the hardware job can still succeed; persisted job metadata includes `result_summary.operation_statuses`, `image_output_status=failed`, `image_output_error`, and `image_output_error_type`.

3. Long-poll for queued events:
```bash
curl 'http://localhost:8000/api/a2a/agents/agent_alpha/events?timeout=30&limit=10' \
  -H "Authorization: Bearer $FORMA_A2A_API_KEY"
```

4. Fetch persisted job metadata:
```bash
curl http://localhost:8000/api/a2a/jobs/job-build-001 \
  -H "Authorization: Bearer $FORMA_A2A_API_KEY"
# The administrative list and metrics endpoints require a Clerk/local admin.
curl 'http://localhost:8000/api/a2a/jobs?sender=agent_alpha&status=succeeded' \
  -H "Authorization: Bearer $FORMA_ADMIN_BEARER_TOKEN"
```

## WebSocket
Connect to `/api/a2a/socket/{agent_id}` with an `Authorization: Bearer ...` header and send the same JSON message
shape. The socket receives queued A2A events as JSON objects. It also accepts MCP JSON-RPC envelopes, forwarding
the authenticated context to every MCP tool call. A socket connected to an agent owned by another principal is
rejected.

## TCP JSONL
Set:
```env
A2A_SOCKET_ENABLED=true
A2A_SOCKET_HOST=127.0.0.1
A2A_SOCKET_PORT=8766
```

Local loopback development (`FORMA_DEPLOYMENT_MODE=local` and `FORMA_AUTH_MODE=local`) may send `A2AMessage`
objects directly. All other TCP listeners require a first-line authentication envelope, for example:

```json
{"type":"auth","token":"Bearer <FORMA_A2A_API_KEY>","agent_id":"agent_alpha"}
```

After authentication, each line sent to the socket is an `A2AMessage` JSON object and each line returned by the
socket is an `A2AEvent` JSON object. TCP is disabled when it is not loopback-bound and no service credential is
configured.

## MCP Tools
`POST /api/mcp` supports:
- `initialize`
- `tools/list`
- `tools/call`

Available tools:
- `forma.compile_project`
- `forma.generate_project`

For a default or web-research project with a failed stage, call `forma.generate_project` again with the same `project_id`, workflow, and `retry_stage` (for example `wiring_netlist`). Forma reloads the persisted generation run, reuses successful upstream and independent artifacts, and reruns only the named stage and invalidated dependents. Reusing the same client job ID returns the completed retry idempotently.
- `forma.debug_config`
- `forma.validate_circuit`
- `forma.a2a.send_message`
- `forma.a2a.poll_events`
- `forma.a2a.get_job`
- `forma.a2a.list_jobs`

OpenClaw, NemoClaw, and OpenCode setup examples are documented in [agent-clients.md](agent-clients.md).
