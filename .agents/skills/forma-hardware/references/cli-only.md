# CLI-only hardware workflow

Use these operations when the host has an authorized local command runner but no
Forma MCP backend. Install the core with `python -m pip install .` from a Forma
source checkout. The host still owns design authoring and its model connection.

1. Create the project directory with `scripts/create_project.py` and author the IR
   using `references/hardware-ir.md`.
2. Validate the saved IR or manifest locally:

   ```sh
   forma-core validate /absolute/project/forma-project.json --output /absolute/project/validation.json
   forma-oss status --path /absolute/project
   ```

3. For CAD, follow `references/cad.md`, run `scripts/cad.py setup`, and use its
   `build` command. Setup can download the managed runtime; CAD builds require
   native OCCT. Do not claim CAD artifacts exist before inspecting the outputs.
4. For supported source-history migration, run `forma-core cad-migrate routes`
   and `forma-core cad-migrate schema`. Author the source-history JSON from
   approved evidence, then run `plan` before `build`. Inspect blocked features
   and obtain review of AI-inferred features; package creation is not native CAD
   execution or evidence of geometric equivalence. See the repository's
   [migration guide](https://github.com/caid-technologies/Form-OSS/blob/main/docs/ai-cad-migrations.md).

Local validation does not perform MCP compilation, rendering, persistence, or
cloud synchronization. Report that distinction when presenting the result.
Do not repeatedly retry `scripts/forma.py` without a backend: that bundled script
is an HTTP client. Enable MCP when deterministic `forma.compile_project` output
and persistence are needed, then continue with the skill's compile workflow.
