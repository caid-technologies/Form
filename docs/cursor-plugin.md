# Install Forma for Cursor / Grok Bot

The `forma` Cursor plugin packages the existing `forma-hardware` skill, including
its CAD adapter, setup references, and portable HTTP client. It also registers
Forma's Streamable HTTP MCP endpoint at `http://127.0.0.1:8000/mcp`.
The host agent authors Hardware Intermediate Representation; Forma compiles,
validates, renders, and persists the accepted design. Installing the plugin does
not start a backend, install Python dependencies, or configure a model provider.

## Install locally in Cursor

With Git installed, clone the complete plugin into Cursor's local plugin directory
(the same command works in a POSIX shell or PowerShell):

```sh
git clone https://github.com/caid-technologies/Form-OSS.git "$HOME/.cursor/plugins/local/forma"
```

Restart Cursor or run **Developer: Reload Window**, then open **Customize** and
check that `forma` and its `forma-hardware` skill appear. Use a real copy of the
repository: Cursor skips symlinks pointing outside the local plugin directory.
If the destination already exists, review that installation before updating it;
do not overwrite local configuration. Team policy can disable local plugin imports,
and an installed Marketplace plugin with the same name takes precedence.

This is the local installation path until a Marketplace listing is approved.
Once published, install the verified Caid Technologies listing through Cursor's
Marketplace instead of keeping a duplicate local installation.

## Choose the execution mode

| Mode | Setup | Available operations |
| --- | --- | --- |
| CLI only | Install the Python core; disable the plugin's `forma` MCP server | Local validation, project status, managed CAD builds, CAD migration planning and packages |
| Local MCP | Start the local backend; enable the plugin's `forma` server | Native `forma.*` tools, including deterministic compilation and persistence |
| Protected cloud MCP | Configure an HTTPS endpoint and bearer credential; disable the local server | The same tools against the configured cloud backend |

### Local MCP

Start the application from a separate development checkout using
[`dev.sh` or `dev.ps1`](setup.md). These launchers configure local authentication
and dependencies. Keep the backend running while using MCP. No Forma account or
MCP token is required for the local mode. The plugin uses `/mcp`, not the separate
OpenCode session endpoint `/opencode/mcp`.

In Cursor's MCP settings, enable `forma` and confirm that its tools include
`forma.compile_project` and `forma.validate_circuit`. Ask the agent:

> Use forma-hardware to author a 3.3 V temperature monitor with an OLED. Compile
> the IR, inspect the validation findings, and save the project artifacts.

Use `authoring_agent: "other"` for Cursor and Grok Bot; this is an existing
supported value. Do not select `forma.generate_project` unless the user explicitly
wants the backend's separately configured LLM.

### Protected cloud MCP

Disable the plugin's local `forma` server. Merge the `forma-cloud` entry from
[mcp-cloud.example.json](../.cursor-plugin/mcp-cloud.example.json) into your
user-level `~/.cursor/mcp.json` (preserve any existing servers).
Set `FORMA_MCP_URL` to your stable HTTPS `/mcp` or `/api/mcp` URL and
`FORMA_AUTH_TOKEN` to the dedicated server-side `FORMA_MCP_API_KEY` (at least
32 characters) or a valid Clerk **administrator** bearer token. Set these in the
environment that launches Cursor, then restart it. Keep real credentials out of
repository files, prompts, and screenshots. The checked-in example contains only
Cursor environment placeholders; it is not automatically enabled by the plugin.

Enable `forma-cloud` and verify tool discovery. See [agent authentication and
deployment details](agent-clients.md). Loopback points to the machine running
Cursor, so use a reachable protected endpoint for remote or sandboxed agents.

### CLI only and Grok Bot

For local commands, install the core from a Forma checkout with
`python -m pip install .`, then follow the skill's
[CLI-only reference](../.agents/skills/forma-hardware/references/cli-only.md).
Disable the local MCP entry when no backend is running. The skill's `forma.py`
script is an HTTP client, so it still requires a backend; it is not an offline
compiler. `forma-core validate` and the CAD migration commands run locally.

Grok Bot deployments with a supported Cursor plugin loader can use the same
package. Otherwise, install the **complete** `.agents/skills/forma-hardware`
directory through the host's Agent Skills mechanism and use its authorized local
command runner. Add MCP only if that deployment supports Streamable HTTP and can
reach the backend. No proprietary Grok Bot API, universal plugin-loader support,
or certified integration is assumed. CAD migration scope and native application
requirements are documented in [AI-assisted CAD migrations](ai-cad-migrations.md).

Bot template/persona sharing is a separate catalog and does not install this
plugin. Forma is independent OSS; this package implies no SpaceXAI/xAI or other
vendor partnership.

## Verification and Marketplace submission

### Revision-bound CAD actions

The backend also advertises `forma.cad_capabilities` and `forma.cad_workflows`
for professional CAD export, migration planning, native-evidence comparison and
review. They share the project UI's ownership, revision and evidence checks.
See [CAD project workflows](cad-project-workflows.md) for request examples,
review authorization and the GrokBot-compatible, provider-neutral boundary.


The repository root is the single plugin root. `.cursor-plugin/plugin.json`
points directly to the canonical `.agents/skills` directory and `mcp.json`;
there is no generated skill copy or multi-plugin marketplace manifest to maintain.
The logo is the existing committed Caid asset.

Run the offline package checks from the repository root:

```sh
python -m unittest tests.tooling.test_cursor_plugin -v
```

Before submission, a maintainer should:

- [ ] Merge the packaging into the public repository's default branch and verify
  all manifest paths and the logo can be fetched without authentication.
- [ ] Confirm the plugin name `forma` is available and the publisher identity is
  Caid Technologies. Use the manifest description, homepage, and repository URL
  as the listing metadata.
- [ ] Retain `LICENSE` and the manifest's `MPL-2.0` identifier. MPL-2.0 is a weak
  copyleft license, not MIT-style permissive licensing; this packaging does not
  relicense existing code or assets.
- [ ] Install in a clean Cursor profile, verify skill discovery, connect to a
  local backend, and complete the compile/validate prompt above. Repeat tool
  discovery against a protected backend and verify a missing/invalid token is
  rejected. Record the Cursor version and results; automated package tests do
  not verify Cursor's UI or an actual Grok Bot deployment.
- [ ] Submit `https://github.com/caid-technologies/Form-OSS` at
  [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish) and
  complete publisher details and manual review. Packaging does not publish a listing.
- [ ] After approval, record the actual Marketplace URL here. An optional
  cursor.directory listing is a separate submission.

Format and installation references: [Cursor plugins](https://cursor.com/docs/plugins),
[plugin reference](https://cursor.com/docs/reference/plugins), and
[MCP configuration](https://cursor.com/docs/mcp).
