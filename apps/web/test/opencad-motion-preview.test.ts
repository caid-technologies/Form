import assert from "node:assert/strict";
import { test } from "node:test";

import {
  normalizeCompliantPreviewTracks,
  normalizeOpenCadMotionTracks,
  openCadMotionRangeLabel,
  openCadSampleAtProgress,
} from "../lib/opencad-motion-preview.ts";

const kinematics = {
  source: "opencad",
  coordinate_system: "z-up",
  tracks: [
    {
      joint: {
        id: "lid-hinge",
        label: "Open lid",
        type: "revolute",
        parent_shape_id: "base",
        child_shape_id: "lid",
        axis: [0, 0, 1],
        origin_mm: [10, 0, 0],
        lower_limit: 0,
        upper_limit: Math.PI / 2,
        unit: "radian",
      },
      target_ref: "LID",
      parent_ref: "BASE",
      notes: "Open the service lid.",
      samples: [
        {
          progress: 0,
          value: 0,
          unit: "radian",
          transform: {
            translation_mm: [0, 0, 0],
            rotation_quaternion_xyzw: [0, 0, 0, 1],
          },
        },
        {
          progress: 1,
          value: Math.PI / 2,
          unit: "radian",
          transform: {
            translation_mm: [10, -10, 0],
            rotation_quaternion_xyzw: [0, 0, Math.SQRT1_2, Math.SQRT1_2],
          },
        },
      ],
    },
  ],
};

test("normalizes OpenCAD kinematic tracks without re-evaluating joint semantics", () => {
  const tracks = normalizeOpenCadMotionTracks(kinematics, new Set(["LID", "BASE"]));

  assert.equal(tracks.length, 1);
  assert.equal(tracks[0].id, "lid-hinge");
  assert.equal(tracks[0].targetRef, "LID");
  assert.equal(tracks[0].type, "revolute");
  assert.equal(openCadMotionRangeLabel(tracks[0]), "0 → 90°");
});

test("playback selects the nearest pose sample emitted by OpenCAD", () => {
  const [track] = normalizeOpenCadMotionTracks(kinematics);

  assert.equal(openCadSampleAtProgress(track, 0.1).progress, 0);
  assert.equal(openCadSampleAtProgress(track, 0.9).progress, 1);
});

test("tracks with missing targets or malformed transforms are ignored", () => {
  assert.deepEqual(normalizeOpenCadMotionTracks(kinematics, new Set(["OTHER"])), []);
  assert.deepEqual(normalizeOpenCadMotionTracks({ source: "opencad", tracks: [{ joint: {}, target_ref: "LID" }] }), []);
});


test("normalizes compliant previews as explicitly approximate motion tracks", () => {
  const preview = {
    source: "forma-compliant-approximation",
    structural_validation: false,
    tracks: [
      {
        id: "flexure",
        label: "Approximate flexure bend",
        type: "compliant",
        target_ref: "FLEX_MOVING",
        parent_ref: "FLEX_FIXED",
        lower_limit: 0,
        upper_limit: Math.PI / 8,
        unit: "radian",
        axis: [0, 1, 0],
        notes: "Visualization only; not structural validation.",
        samples: [
          {
            progress: 0,
            value: 0,
            unit: "radian",
            transform: {
              translation_mm: [0, 0, 0],
              rotation_quaternion_xyzw: [0, 0, 0, 1],
            },
          },
          {
            progress: 1,
            value: Math.PI / 8,
            unit: "radian",
            transform: {
              translation_mm: [0, 0, 0],
              rotation_quaternion_xyzw: [0, Math.sin(Math.PI / 16), 0, Math.cos(Math.PI / 16)],
            },
          },
        ],
      },
    ],
  };

  const tracks = normalizeCompliantPreviewTracks(
    preview,
    new Set(["FLEX_FIXED", "FLEX_MOVING"]),
  );

  assert.equal(tracks.length, 1);
  assert.equal(tracks[0].type, "compliant");
  assert.equal(tracks[0].targetRef, "FLEX_MOVING");
  assert.equal(openCadMotionRangeLabel(tracks[0]), "0 → 22.5°");
});
