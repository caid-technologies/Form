---
name: forma-hardware
description: Compile, validate, inspect, or generate safe low-voltage maker-electronics projects with Forma. Use for Hardware Intermediate Representation, BOMs, wiring, schematics, mechanical notes, and build instructions in Cursor, Grok Bot, OpenClaw, NemoClaw, OpenCode, Claude Code, or Codex.
---

# Forma Hardware

Use the host agent to author the design and Forma to normalize Hardware Intermediate Representation, apply deterministic electrical checks, and render diagrams. The host agent owns the conversation and the working project state; Forma owns accepted snapshots, deterministic validation, persistence, and display. None of the compile, validate, or render steps below transfer authoring ownership back to Forma — Forma never directs the agent's internal authoring steps. For CAD-capable workflows, use the Forma-owned OpenCAD adapter described in [references/cad.md](references/cad.md). Never replace a failed live generation with simulated output unless the user explicitly requests simulation.

## CAD dependency

When the user asks for a CAD model or STEP/STL export, prepare the managed
runtime before writing or running the model:

```bash
python <skill-directory>/scripts/cad.py setup
```

The setup command reuses compatible OpenCAD `0.2.4` installations only when native OCCT, rigid kinematics, spur gears and gear coupling are available. Otherwise it installs the pinned OpenCAD gear-motion commit `1c417752eb42d29b951784e80f65ac79c3fb6e0e`. It verifies native OCCT before a generation job begins. Do not import OpenCAD from Forma's core package; use the adapter's
`build` command for model execution and export.

## Workspace

At the start of every project run, create a fresh project directory with `scripts/create_project.py` and use the returned path as `PROJECT_DIR`. The helper creates a UUID-named directory under `~/forma-workspace/`; never derive the project ID from the request. Keep every source file and generated artifact inside `PROJECT_DIR`.

```bash
PROJECT_DIR=$(python <skill-directory>/scripts/create_project.py)
```

## Connect

Prefer the native `forma.*` MCP tools when available. Otherwise run the bundled standard-library client:

```bash
python <skill-directory>/scripts/forma.py tools
```

It reads `FORMA_MCP_URL`, defaulting to `http://127.0.0.1:8000/mcp`, and optional `FORMA_AUTH_TOKEN`. Read [references/configuration.md](references/configuration.md) if the server is not connected.

The bundled client also needs a running backend. When only local command execution
is available, follow [references/cli-only.md](references/cli-only.md) for local
validation, CAD builds, and migration planning instead of the MCP compile workflow.

## Author and compile

1. Keep the project within safe low-voltage educational or maker scope. Decline weapons, critical medical or life-support devices, mains AC, automotive control, and unsafe high-power battery requests.
2. Read [references/hardware-ir.md](references/hardware-ir.md), then author complete Hardware Intermediate Representation from the brief. Preserve stated power, dimensions, budget, environment, interfaces, and part preferences. Do not invent verified supplier availability or physical clearances.
3. Save the IR as `$PROJECT_DIR/forma-project.json`.
4. Call `forma.compile_project` with `project_ir` and the correct `authoring_agent` (`openclaw`, `nemoclaw`, `opencode`, `claude`, `codex`, or `other` for Cursor/Grok Bot). With the bundled client:

```bash
python <skill-directory>/scripts/forma.py compile "$PROJECT_DIR/forma-project.json" --authoring-agent openclaw --output "$PROJECT_DIR/compiled-project.json" --update-project
python <skill-directory>/scripts/forma.py compile "$PROJECT_DIR/forma-project.json" --authoring-agent nemoclaw --output "$PROJECT_DIR/compiled-project.json" --update-project
python <skill-directory>/scripts/forma.py compile "$PROJECT_DIR/forma-project.json" --authoring-agent opencode --output "$PROJECT_DIR/compiled-project.json" --update-project
```

When using the bundled client, add `--update-project` so the canonical local
manifest contains the returned compiled Intermediate Representation and can be uploaded later. The
client also writes `validation.json`, `wiring.mmd`, and `schematic.svg` beside
the manifest when those compiler artifacts are returned:

```bash
python <skill-directory>/scripts/forma.py compile "$PROJECT_DIR/forma-project.json" --authoring-agent opencode --output "$PROJECT_DIR/compiled-project.json" --update-project
```

When using a native MCP tool, replace the manifest's `project_ir` with the
returned `project_ir`, preserve the manifest wrapper and artifacts, and update
its `project_id` from the compiler response before uploading. Save returned
`validation`, `mermaid_code`, and `svg_schematic` values as local artifact
files and reference them from the manifest. Never leave the pre-compiled draft
as the uploadable project.

5. Fix practical agent-authored errors and compile again. Treat every `CRITICAL` finding as blocking.
6. Run `forma-oss status --path "$PROJECT_DIR"` before presenting the project.
7. Report artifact paths, power assumptions, major components, and remaining findings. Label the result as an AI-assisted prototype plan.

## Validate existing IR

Call `forma.validate_circuit`, or use:

```bash
python <skill-directory>/scripts/forma.py validate "$PROJECT_DIR/forma-project.json" --output "$PROJECT_DIR/validation.json"
```

## Optional server-side generation

Use `forma.generate_project` only when the user wants Forma's separately configured server-side LLM. Prefer host-authored IR plus `forma.compile_project` for normal OpenClaw, NemoClaw, and OpenCode work.

```bash
python <skill-directory>/scripts/forma.py generate "ESP32 plant monitor with an OLED" --output "$PROJECT_DIR/forma-project.json"
```

## Output integrity

Keep compiled JSON intact inside `PROJECT_DIR`. Do not expose credentials. Do not claim that diagrams, prices, availability, compatibility, or clearances were physically verified unless returned evidence establishes that.
