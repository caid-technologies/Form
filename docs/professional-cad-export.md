# Forma Export for professional CAD

`forma-core cad-export` creates a local, reproducible ZIP containing an existing
STEP file, explicit metadata, checksums, and an import helper for SOLIDWORKS,
Onshape, or Autodesk Fusion 360. This is a geometry handoff: STEP does not carry
the original native feature tree. No native `.sldprt` or `.f3d` is fabricated.

```sh
forma-core cad-export --capabilities
forma-core cad-export --step assembly.step --metadata cad-metadata.json \
  --target solidworks --output solidworks-handoff.zip
forma-core cad-export --step assembly.step --target onshape --output onshape-handoff.zip
forma-core cad-export --step assembly.step --target fusion360 --output fusion-handoff.zip
```

The source STEP must already exist (for example, from Forma's CAD authoring
workflow). The project CAD API and MCP tools also create these packages from a
saved revision's private STEP artifact. Part number/revision are selected
explicitly; project and canonical revision IDs are bound by the server. See
[CAD project workflows](cad-project-workflows.md) for API and MCP actions. The
existing demo frontend is unchanged.
Existing output files are rejected. Input is limited to 100 MiB STEP and 1 MiB
metadata. Envelope and checksum checks do not establish solid validity.

Example `cad-metadata.json`:

```json
{
  "title": "Sensor bracket",
  "part_number": "BRK-001",
  "revision": "A",
  "description": "Mounting bracket",
  "material": "6061-T6 (reference only)",
  "source_project_id": "my-forma-project",
  "properties": {"finish": "anodized"}
}
```

These fields are optional. Unknown fields, oversized values and credential-like
property names are rejected. Choose CAD properties deliberately; do not pass a
full project export, provider configuration or credentials file as metadata.

| Target | Helper and prerequisites | Native metadata behavior |
| --- | --- | --- |
| SOLIDWORKS | Windows PowerShell + licensed installed SOLIDWORKS; `./import-solidworks.ps1` (`-LegacyImport` if 3D Interconnect is off) | Forma-prefixed document custom properties |
| Onshape | Manual Import, or Python 3 helper with `ONSHAPE_ACCESS_TOKEN` OAuth bearer token and explicit existing document/workspace IDs | Metadata remains in the package sidecar for deliberate mapping to enterprise property schemas |
| Fusion 360 | Installed Fusion; run packaged Python script using Scripts and Add-Ins | Component name, part number, description; full metadata in Forma attributes |

Unzip before running a helper. Onshape example:

```sh
# Set ONSHAPE_ACCESS_TOKEN using your secret manager; never commit it.
python import-onshape.py --document DOCUMENT_ID --workspace WORKSPACE_ID
```

Only this explicit importer invocation sends geometry to Onshape. Upload is not
automatically retried: translation failure, timeout or an ambiguous response
requires checking the workspace before another upload. The helper supports the
standard `cad.onshape.com` service; enterprise-specific hosts need a reviewed
adapter. It refuses redirects so a bearer token cannot follow a changed host.
SOLIDWORKS and Fusion create new unsaved documents. Review and save them yourself.

Material names are references, not density/material-library assignments. Review
scale, body count, solids, orientation, assemblies and metadata mapping in the
target application before using a result downstream. Mates, drawings, original
sketch constraints and native feature history are outside this export contract.
Native application imports require live testing against your licensed versions.

## GrokBot / Cursor host-agent workflow

Forma is an independent, provider-neutral tool. A host agent can discover target
capabilities, obtain the user's CAD target and explicit metadata, invoke the CLI,
then inspect `manifest.json` and the JSON receipt. Cursor can run these commands
in its terminal. GrokBot can use the same CLI when its deployment exposes an
authorized local command runner; no GrokBot-specific API is assumed. Existing
Forma's existing agent interfaces remain available as documented in [agents](agents.md).

Keep credentials in the user's environment and proprietary CAD in the user's
approved workspace. Packaging does not call a model or send model data anywhere.
Any host-agent model use follows that host's data policy. This integration does
not imply a SpaceXAI, xAI, Autodesk, or other vendor partnership or endorsement.

For early enterprise trials, capture target/version, expected versus observed
units/body count, property mappings, translation status and unsupported objects.
Record customer feedback separately from verification evidence; do not label
an untested import as a successful migration.

## Vendor references

- [SOLIDWORKS LoadFile4](https://help.solidworks.com/2025/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.ISldWorks~LoadFile4.html)
- [Onshape API import and translation](https://onshape-public.github.io/docs/api-adv/translation/)
- [Onshape imported CAD and feature history](https://cad.onshape.com/help/Content/Document/importing_files.htm)
- [Autodesk Fusion file import behavior](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/OpeningFilesFromWebPage_UM.htm)

Offline verification: `python -m unittest tests.integrations.test_cad_export -v`. Tests exercise
packaging, tamper rejection, no-overwrite behavior, and mocked importer control
flow; mocks are not evidence of native CAD compatibility.
