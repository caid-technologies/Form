import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { normalizeArticulatedMotion, advanceMechanismProgress } from "../lib/articulated-motion.ts";
import { openCadSampleAtProgress } from "../lib/opencad-motion-preview.ts";

const fixture = JSON.parse(readFileSync(new URL("../public/examples/spur_gear_pair.json", import.meta.url), "utf8")).cad_model;

test("plays both native gear meshes on one clock after serialization", () => {
  const saved = JSON.parse(JSON.stringify(fixture));
  const motion = normalizeArticulatedMotion(saved.articulated_bodies, saved.kinematics)!;
  assert.equal(motion.bodies.length, 2);
  for (const t of [0, .137, .25, .5, .777, 1]) {
    const [a, b] = motion.tracks.map((track) => openCadSampleAtProgress(track, t));
    assert.equal(a.progress, b.progress);
    assert.ok(Math.abs(b.value + a.value / 2) < 1e-9);
  }
  assert.ok(Math.abs(advanceMechanismProgress(.99, .12, 6, true) - .01) < 1e-10);
  assert.equal(advanceMechanismProgress(.99, .12, 6, false), 1);
});

test("rejects missing bodies, mismatched identities, invalid indices and unsynchronized samples", () => {
  for (const mutate of [
    (f: any) => f.articulated_bodies.pop(),
    (f: any) => { f.articulated_bodies[0].mesh.shapeId = "different"; },
    (f: any) => { f.articulated_bodies[0].mesh.faces[0] = -1; },
    (f: any) => { f.kinematics.tracks[0].samples[1].progress = .9; },
    (f: any) => { f.kinematics.tracks[0].samples[0].transform.rotation_quaternion_xyzw = [0, 0, 0, 0]; },
  ]) {
    const f = structuredClone(fixture);
    mutate(f);
    assert.ok(normalizeArticulatedMotion(f.articulated_bodies, f.kinematics) === null);
  }
});
