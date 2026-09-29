# Forma connection

The bundled client uses only the Python standard library.

## Environment

- `FORMA_MCP_URL`: MCP endpoint; defaults to `http://127.0.0.1:8000/mcp`.
- `FORMA_AUTH_TOKEN`: optional bearer token for a protected deployment.
- `FORMA_TIMEOUT_SECONDS`: request timeout; defaults to 600 seconds.

Keep tokens in the environment, never in committed configuration or generated projects.

## Start Forma locally

From a Forma checkout, the one-command launcher installs missing backend and
frontend dependencies, enables local auth/SQLite defaults without selecting an
LLM, creates an encrypted local key under `.forma/`, and starts both services:

```bash
./scripts/development/dev.sh
```

On Windows PowerShell, run the native equivalent:

```powershell
.\scripts\development\dev.ps1
```

The launcher honors explicit environment overrides. OpenCode (or another host
agent) supplies the model that authors Hardware Intermediate Representation; `forma.compile_project`
then performs deterministic validation, rendering, and persistence. The
launcher does not set `LLM_PROVIDER` or `LLM_MODEL`. To run only the backend
manually, set a server-only key:

```bash
FORMA_AUTH_MODE=local FORMA_DEPLOYMENT_MODE=local FORMA_DEVELOPMENT_MODE=true \
FORMA_USER_SECRETS_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
uvicorn apps.api.main:app --port 8000
```

The separate `forma.generate_project` path is server-side generation. Use it
only when the backend has an explicitly configured provider and model; it does
not inherit OpenCode's active model through MCP.

## OpenClaw

The project skill is discovered from `.agents/skills/forma-hardware/SKILL.md`. Register the Streamable HTTP server and verify it:

```bash
openclaw mcp set forma '{"url":"http://127.0.0.1:8000/mcp","transport":"streamable-http"}'
openclaw mcp doctor forma --probe
```

## NemoClaw

NemoClaw runs OpenClaw in a deny-by-default OpenShell sandbox. A host-loopback MCP URL is not reachable from that sandbox. Deploy Forma at a stable HTTPS URL, configure a random server-side `FORMA_MCP_API_KEY` of at least 32 characters, and register it as a managed MCP server:

```bash
export FORMA_MCP_TOKEN="your-random-token"
nemoclaw my-sandbox mcp add forma --url https://forma.example.com/api/mcp --env FORMA_MCP_TOKEN
unset FORMA_MCP_TOKEN
nemoclaw my-sandbox mcp status forma --json
```

The `--env` form keeps the raw bearer credential outside the sandbox. Upload this skill into the default OpenClaw workspace:

```bash
nemoclaw my-sandbox upload <skill-directory> /sandbox/.openclaw/workspace/skills/
```

Upload it separately for every agent workspace. NemoClaw rejects loopback and `host.docker.internal` MCP URLs. For a stable RFC1918 endpoint, register the exact TLS hostname with `--trusted-private-host <hostname>`.

## OpenCode

The project skill is discovered from the same `.agents/skills` directory. Add this to `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "forma": {
      "type": "remote",
      "url": "http://127.0.0.1:8000/mcp",
      "enabled": true,
      "oauth": false
    }
  }
}
```

Then run `opencode mcp list`. For a protected server, add `"Authorization": "Bearer {env:FORMA_AUTH_TOKEN}"` under `headers`.

## Cursor / Grok Bot

The Cursor plugin registers the local `forma` MCP server and this complete skill.
For protected cloud use, disable the local server and register a reachable HTTPS
endpoint with `Authorization: Bearer ${env:FORMA_AUTH_TOKEN}` in Cursor's MCP
configuration. Cursor must inherit the credential environment variable when it
starts. See the repository's [plugin installation guide](https://github.com/caid-technologies/Form-OSS/blob/main/docs/cursor-plugin.md).

Use `authoring_agent: "other"` with `forma.compile_project`, or
`--authoring-agent other` with `scripts/forma.py compile`. Grok Bot deployments
need either a compatible plugin/skill loader or an authorized command runner;
do not assume a proprietary API or a particular host installation command.

## Troubleshooting

- Connection refused: run `./scripts/development/dev.sh` (or `.\scripts\development\dev.ps1` on Windows) from a Forma checkout, or set `FORMA_MCP_URL` to a hosted `/api/mcp` endpoint.
- HTTP 401/403: supply either a Clerk admin bearer token or the dedicated `FORMA_MCP_API_KEY`.
- NemoClaw rejects the URL: use a stable HTTPS endpoint, not host loopback; declare an exact private hostname when needed.
- Method not found: update the Forma server and inspect `python scripts/forma.py tools`.
- Timeout: increase `FORMA_TIMEOUT_SECONDS` and inspect the configured provider if using server-side generation.
