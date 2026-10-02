"""Reviewed importer programs; metadata is always read as data, never inserted into code."""

VERIFY = '''import hashlib
import json
from pathlib import Path

def package_files():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "forma-cad-export" or manifest.get("version") != 1:
        raise ValueError("Unsupported Forma export package")
    for name in ("geometry.step", "metadata.json"):
        data = (root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["files"][name]["sha256"]:
            raise ValueError("Package integrity check failed: " + name)
    return root, json.loads((root / "metadata.json").read_text(encoding="utf-8"))

'''

FUSION = VERIFY + '''def run(context):
    import adsk.core
    import adsk.fusion
    root, metadata = package_files()
    app = adsk.core.Application.get()
    options = app.importManager.createSTEPImportOptions(str(root / "geometry.step"))
    document = app.importManager.importToNewDocument(options)
    if document is None:
        raise RuntimeError("Fusion did not create an imported document")
    design = adsk.fusion.Design.cast(document.products.itemByProductType("DesignProductType"))
    if design is None:
        raise RuntimeError("Imported document has no Fusion design")
    component = design.rootComponent
    component.name = metadata["title"]
    component.partNumber = metadata["part_number"]
    component.description = metadata["description"]
    component.attributes.add("Forma", "metadata", json.dumps(metadata, ensure_ascii=False))
    app.userInterface.messageBox("STEP imported into a new unsaved document. Review geometry and save. "
                                 "Source feature history was not transferred; material is reference metadata only.")
'''

# This standalone program runs outside Forma and has no Forma config dependency.
ONSHAPE = VERIFY + '''import argparse
from os import environ
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, build_opener
from uuid import uuid4

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected redirect; request was not forwarded")

def request(path, token, data=None, content_type="application/json"):
    req = Request("https://cad.onshape.com/api" + path, data=data,
                  headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                           "Content-Type": content_type})
    try:
        with build_opener(NoRedirect).open(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError("Onshape request failed (HTTP %s); check scope and workspace access" % exc.code) from None

def main():
    parser = argparse.ArgumentParser(description="Upload STEP into an existing Onshape workspace")
    parser.add_argument("--document", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    if not all(re.fullmatch(r"[0-9a-fA-F]{24}", value) for value in (args.document, args.workspace)):
        parser.error("Document and workspace IDs must each be 24 hexadecimal characters")
    root, _metadata = package_files()
    token = environ.get("ONSHAPE_ACCESS_TOKEN", "").strip()
    if not token:
        parser.error("Set ONSHAPE_ACCESS_TOKEN to an OAuth token with document write scope")
    boundary = "forma_" + uuid4().hex
    fields = []
    for name, value in (("storeInDocument", "true"), ("translate", "true")):
        fields.append(('--%s\\r\\nContent-Disposition: form-data; name="%s"\\r\\n\\r\\n%s\\r\\n' % (boundary, name, value)).encode())
    fields.append(('--%s\\r\\nContent-Disposition: form-data; name="file"; filename="geometry.step"\\r\\nContent-Type: model/step\\r\\n\\r\\n' % boundary).encode())
    fields.extend([(root / "geometry.step").read_bytes(), ("\\r\\n--%s--\\r\\n" % boundary).encode()])
    result = request("/blobelements/d/%s/w/%s" % (args.document, args.workspace), token,
                     b"".join(fields), "multipart/form-data; boundary=" + boundary)
    translation = result.get("translationId")
    if not isinstance(translation, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", translation):
        raise RuntimeError("Upload returned no translation ID. Check the workspace before retrying; upload may exist.")
    print("Uploaded. Translation ID:", translation, flush=True)
    for _ in range(60):
        state = request("/translations/" + translation, token)
        if state.get("requestState") == "DONE":
            print(json.dumps({"status": "imported", "element_ids": state.get("resultElementIds", []),
                              "native_history_preserved": False, "metadata": "metadata.json (sidecar)"}))
            return
        if state.get("requestState") == "FAILED":
            raise RuntimeError("Onshape translation failed; inspect the uploaded element before retrying")
        time.sleep(2)
    raise RuntimeError("Translation remains pending. Check translation " + translation + " before retrying the upload")

if __name__ == "__main__":
    main()
'''

SOLIDWORKS = '''param([switch]$LegacyImport)
$ErrorActionPreference = "Stop"
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') -Raw | ConvertFrom-Json
if ($manifest.format -ne 'forma-cad-export' -or $manifest.version -ne 1) { throw 'Unsupported export package' }
foreach ($name in @('geometry.step', 'metadata.json')) {
    $digest = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $name) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne $manifest.files.$name.sha256) { throw "Package integrity check failed: $name" }
}
$metadata = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'metadata.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$sw = New-Object -ComObject SldWorks.Application
$sw.Visible = $true
$step = Join-Path $PSScriptRoot 'geometry.step'
$data = $sw.GetImportFileData($step)
$errors = 0
$options = if ($LegacyImport) { 'r' } else { '' }
$doc = $sw.LoadFile4($step, $options, $data, [ref]$errors)
if ($null -eq $doc -or $errors -ne 0) { throw "SOLIDWORKS import failed with code $errors" }
$properties = $doc.Extension.CustomPropertyManager('')
foreach ($key in @('title', 'part_number', 'revision', 'description', 'material', 'source_project_id')) {
    # swCustomInfoText=30; swCustomPropertyReplaceValue=2. No material-library assignment.
    $code = $properties.Add3("Forma.$key", 30, [string]$metadata.$key, 2)
    if ($code -ne 0) { throw "Could not set Forma.$key (code $code); document remains open for review" }
}
foreach ($property in $metadata.properties.PSObject.Properties) {
    $code = $properties.Add3("Forma.Property.$($property.Name)", 30, [string]$property.Value, 2)
    if ($code -ne 0) { throw "Could not set custom property (code $code); document remains open for review" }
}
Write-Output 'STEP imported into a new document. Inspect geometry and use Save As. Source history was not transferred.'
'''
