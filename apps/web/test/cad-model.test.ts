import assert from "node:assert/strict";
import { test } from "node:test";

import { nativeStepArtifact, projectCadModel, resolveCadModel } from "../lib/cad-model.ts";

const mesh = {
  shapeId: "body",
  vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0],
  faces: [0, 1, 2],
};

test("a stored STEP stays renderable without a separately configured OpenCAD server", () => {
  const cad = { adapter: "forma-opencad", format: "step", project_id: "11111111-1111-4111-8111-111111111111", stored_sha256: "a".repeat(64), path: "C:/private/assembly.step", meshes: [mesh] };
  assert.equal(resolveCadModel(cad)?.kind, "meshes");
  assert.deepEqual(nativeStepArtifact(cad), { projectId: cad.project_id, sha256: cad.stored_sha256 });
  assert.equal(nativeStepArtifact({ ...cad, project_id: "https://untrusted.example" }), null);
  assert.equal(nativeStepArtifact({ ...cad, stored_sha256: "../private" }), null);
  assert.equal(nativeStepArtifact({ ...cad, stored_sha256: null }), null);
});

test("project CAD models are read from the canonical payload", () => {
  assert.equal(projectCadModel({ cad_model: "body.step" }), "body.step");
  assert.equal(projectCadModel({ mechanical: { cad_model: { shape_id: "body" } } }), null);
  assert.equal(projectCadModel({ assembly_metadata: { cad_model: "legacy.step" } }), null);
});

test("mechanical placements do not resolve into a CAD model", () => {
  const project = { mechanical: { component_placements: [{ ref_des: "U1" }] } };

  assert.equal(projectCadModel(project), null);
  assert.equal(resolveCadModel(projectCadModel(project)), null);
});

test("renderable adapter meshes normalize into a viewport payload", () => {
  const resolved = resolveCadModel({ meshes: [mesh] });

  assert.equal(resolved?.kind, "meshes");
  assert.deepEqual(resolved?.kind === "meshes" ? resolved.meshes[0] : null, mesh);
});

test("shape and browser-loadable file sources resolve for OpenCAD", () => {
  const shape = resolveCadModel({ shape_id: "body", api_url: "https://cad.example.test/" });
  const nestedShape = resolveCadModel({ adapter: { shape_id: "nested-body", api_url: "https://cad.example.test" } });
  const file = resolveCadModel({ url: "https://assets.example.test/enclosure.STEP?signature=ok" });

  assert.deepEqual(shape, { kind: "shape", shapeId: "body", apiBaseUrl: "https://cad.example.test" });
  assert.deepEqual(nestedShape, { kind: "shape", shapeId: "nested-body", apiBaseUrl: "https://cad.example.test" });
  assert.deepEqual(file, {
    kind: "file",
    url: "https://assets.example.test/enclosure.STEP?signature=ok",
    filename: "enclosure.STEP",
    sourceKind: "http",
  });
});

test("unresolved local paths and raw S3 locations never become browser fetches", () => {
  const localPath = resolveCadModel({ path: "C:\\models\\body.step" });
  const s3Uri = resolveCadModel({ s3_uri: "s3://private-bucket/body.step" });
  const signedS3 = resolveCadModel({ s3_uri: "s3://private-bucket/body.step", signed_url: "https://signed.example.test/body.step?token=ok" });

  assert.equal(localPath?.kind, "unsupported");
  assert.equal(s3Uri?.kind, "unsupported");
  assert.equal(signedS3?.kind, "file");
});
