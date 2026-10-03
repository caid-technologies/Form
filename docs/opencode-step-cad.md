# OpenCode solid CAD and STEP delivery

Follow-ups now receive the original saved user request and the 15 most recent
requests from the same connector/session/owner/project. This includes cancelled
attempts and works for commands saved before the connector upgrade. History stays
encrypted in the command store; it is decrypted only for the scoped lease response.
Deploy the matching local-server-config connector change to consume this context.
If an older request predates encrypted command storage, its text cannot be recovered.

`create_project` returns an existing project unchanged. Continuing a conversation
must read and update the current project rather than initialize another empty draft.

## Solid geometry

The restricted compile tool accepts `mechanical.cad_operations`: a bounded list of
box and Z-axis cylinder primitives, combined with add/cut operations. All positions
and dimensions are in millimeters. The first operation must add. Arbitrary Python,
shell execution, and unrestricted MCP tools are not enabled.

For a 20 mm cube with engraved X/Y/Z on its positive faces:

```json
{
  "components": [],
  "nets": [],
  "mechanical": {
    "enclosure_type": "Solid",
    "mounting_guidance": "None",
    "manufacturability_rating": "Easy",
    "cad_operations": [{
      "shape": "box",
      "size": {"x_mm": 20, "y_mm": 20, "z_mm": 20},
      "axis_labels": true
    }]
  }
}
```

Labels are engravings, so the outside dimensions remain exactly 20 mm. No dummy
electronics are needed. The existing enclosure generator remains available for
legacy projects without explicit solid operations. Curves, fillets, arbitrary
text, and free-form solids are not supported by this initial declarative surface;
the agent must report that limitation rather than substitute an unrelated shape.

Compile exports real native STEP and STL geometry, embeds the triangulated preview
in `cad_model.meshes`, and stores STEP bytes in project-scoped private artifact
storage. Each export uses a separate directory so later edits cannot overwrite an
earlier export. An explicit CAD build/storage failure is an MCP tool error and
does not replace the saved revision. A CAD-only project is complete only when its
native build succeeded and returned both the stored artifact identity and preview.
This means generated geometry, not fabrication or physical validation.

The CAD panel renders these meshes without a separate browser-facing OpenCAD
server. Download STEP uses authenticated ownership checks against the latest
project's artifact hash and checks the downloaded bytes before serving them.

## Backend runtime

The **Forma API process**, where the restricted compile tool runs, needs the
native OpenCAD runtime and writable CAD workspace. The Docker image now installs
and checks the pinned runtime and includes the adapter. For a Python deployment,
run with the same interpreter/environment as the API, from the Forma-OSS root:

```sh
python .agents/skills/forma-hardware/scripts/cad.py setup
python .agents/skills/forma-hardware/scripts/cad.py check
```

Set `FORMA_CAD_WORKSPACE` to a directory writable by the API service account, and
configure the existing private artifact storage (Supabase in hosted deployments;
`FORMA_CLI_ARTIFACT_STORAGE_BACKEND=local` for local development). Installing the
runtime only in the Node connector environment is insufficient. Native CAD needs
a host/container that supports OCCT; this change does not make a size-limited
serverless function a CAD worker. No live service or tunnel settings are changed.

Validation gate:

```sh
FORMA_CAD_RUN_INTEGRATION_TESTS=true python -m pytest tests/projects/test_solid_cad.py tests/opencode/test_cad.py tests/opencode/test_context.py
```

After deploying both repositories and the API CAD runtime, continue the existing
cube conversation and request the STEP again. Verify the rendered labelled cube,
download, and dimensions. A completely new chat should also work from the original
cube brief; a new chat is not required to repair a preserved conversation.
