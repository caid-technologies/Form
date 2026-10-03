import { normalizeOpenCadMotionTracks, type OpenCadMotionTrack } from "./opencad-motion-preview.ts";

export type ArticulatedBody = {
  shapeId: string;
  targetRef: string;
  name: string;
  color: string;
  center: [number, number, number];
  markerRadius: number;
  vertices: number[];
  faces: number[];
};

export type ArticulatedMotion = {
  id: string;
  label: string;
  durationSeconds: number;
  loop: boolean;
  bodies: ArticulatedBody[];
  tracks: OpenCadMotionTrack[];
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function numbers(value: unknown): value is number[] {
  return Array.isArray(value) && value.every((n) => typeof n === "number" && Number.isFinite(n));
}

export function normalizeArticulatedMotion(bodyData: unknown, kinematics: unknown): ArticulatedMotion | null {
  const root = record(kinematics);
  const mechanism = record(root?.mechanism);
  if (root?.source !== "opencad" || root.coordinate_system !== "z-up" || !mechanism || !Array.isArray(bodyData) || !bodyData.length) return null;
  if (!Array.isArray(root.tracks) || root.tracks.length !== bodyData.length) return null;
  if (typeof mechanism.id !== "string" || typeof mechanism.label !== "string" ||
      typeof mechanism.duration_seconds !== "number" || !Number.isFinite(mechanism.duration_seconds) || mechanism.duration_seconds <= 0) return null;
  const bodies: ArticulatedBody[] = [];
  for (const item of bodyData) {
    const body = record(item);
    const mesh = record(body?.mesh);
    if (body?.units !== "mm" || body.coordinate_system !== "z-up" || typeof body.shape_id !== "string" || !body.shape_id ||
        typeof body.target_ref !== "string" || !body.target_ref || mesh?.shapeId !== body.shape_id ||
        !numbers(mesh.vertices) || mesh.vertices.length < 9 || mesh.vertices.length % 3 ||
        !numbers(mesh.faces) || !mesh.faces.length || mesh.faces.length % 3 ||
        !mesh.faces.every((n) => Number.isInteger(n) && n >= 0 && n < (mesh.vertices as number[]).length / 3) ||
        !numbers(body.center_mm) || body.center_mm.length !== 3) return null;
    bodies.push({
      shapeId: body.shape_id, targetRef: body.target_ref,
      name: typeof body.name === "string" ? body.name : body.target_ref,
      color: typeof body.color === "string" && /^#[0-9a-f]{6}$/i.test(body.color) ? body.color : "#3b82f6",
      center: body.center_mm as [number, number, number],
      markerRadius: typeof body.marker_radius_mm === "number" && Number.isFinite(body.marker_radius_mm) && body.marker_radius_mm > 0 ? body.marker_radius_mm : 0,
      vertices: mesh.vertices, faces: mesh.faces,
    });
  }
  if (new Set(bodies.map((b) => b.targetRef)).size !== bodies.length || new Set(bodies.map((b) => b.shapeId)).size !== bodies.length) return null;
  const tracks = normalizeOpenCadMotionTracks(kinematics, new Set(bodies.map((b) => b.targetRef)));
  if (tracks.length !== bodies.length || new Set(tracks.map((t) => t.targetRef)).size !== bodies.length) return null;
  for (const body of bodies) {
    const track = tracks.find((t) => t.targetRef === body.targetRef);
    const raw = (root.tracks as unknown[]).map(record).find((t) => t?.target_ref === body.targetRef);
    if (!track || record(raw?.joint)?.child_shape_id !== body.shapeId || track.samples.length < 2 ||
        track.samples.length !== root.sample_count ||
        track.samples[0].progress !== 0 || track.samples.at(-1)?.progress !== 1) return null;
    // Every body must use the same clock and valid, nonzero quaternions.
    if (track.samples.length !== tracks[0].samples.length || track.samples.some((sample, i) =>
      sample.progress !== tracks[0].samples[i].progress || (i > 0 && sample.progress <= track.samples[i - 1].progress) ||
      Math.hypot(...sample.transform.rotation_quaternion_xyzw) < 1e-9)) return null;
  }
  return { id: mechanism.id, label: mechanism.label, durationSeconds: mechanism.duration_seconds, loop: mechanism.loop === true, bodies, tracks };
}

export function advanceMechanismProgress(progress: number, seconds: number, duration: number, loop: boolean): number {
  const next = progress + Math.max(0, seconds) / duration;
  return loop ? next % 1 : Math.min(1, next);
}
