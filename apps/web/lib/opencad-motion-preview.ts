export type OpenCadRigidTransform = {
  translation_mm: [number, number, number];
  rotation_quaternion_xyzw: [number, number, number, number];
};

export type OpenCadMotionSample = {
  progress: number;
  value: number;
  unit: "none" | "radian" | "mm";
  transform: OpenCadRigidTransform;
};

export type OpenCadMotionTrack = {
  id: string;
  label: string;
  type: "fixed" | "revolute" | "prismatic" | "compliant";
  targetRef: string;
  parentRef?: string;
  lower: number;
  upper: number;
  unit: "none" | "radian" | "mm";
  axis: [number, number, number];
  notes?: string;
  samples: OpenCadMotionSample[];
};

type PlainRecord = Record<string, unknown>;

function record(value: unknown): PlainRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as PlainRecord
    : null;
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function vector3(value: unknown): [number, number, number] | null {
  if (!Array.isArray(value) || value.length !== 3) return null;
  const parsed = value.map(finite);
  return parsed.every((item): item is number => item !== null)
    ? [parsed[0], parsed[1], parsed[2]]
    : null;
}

function quaternion(value: unknown): [number, number, number, number] | null {
  if (!Array.isArray(value) || value.length !== 4) return null;
  const parsed = value.map(finite);
  return parsed.every((item): item is number => item !== null)
    ? [parsed[0], parsed[1], parsed[2], parsed[3]]
    : null;
}

function unit(value: unknown): OpenCadMotionSample["unit"] | null {
  return value === "none" || value === "radian" || value === "mm" ? value : null;
}

function type(value: unknown): OpenCadMotionTrack["type"] | null {
  return value === "fixed" || value === "revolute" || value === "prismatic" || value === "compliant" ? value : null;
}

function parseSample(value: unknown): OpenCadMotionSample | null {
  const input = record(value);
  const transform = record(input?.transform);
  const translation = vector3(transform?.translation_mm);
  const rotation = quaternion(transform?.rotation_quaternion_xyzw);
  const progress = finite(input?.progress);
  const sampleValue = finite(input?.value);
  const sampleUnit = unit(input?.unit);
  if (!translation || !rotation || progress === null || sampleValue === null || !sampleUnit) return null;
  return {
    progress: Math.min(1, Math.max(0, progress)),
    value: sampleValue,
    unit: sampleUnit,
    transform: {
      translation_mm: translation,
      rotation_quaternion_xyzw: rotation,
    },
  };
}

export function normalizeOpenCadMotionTracks(
  value: unknown,
  validTargetRefs?: ReadonlySet<string>,
): OpenCadMotionTrack[] {
  const root = record(value);
  if (root?.source !== "opencad" || !Array.isArray(root.tracks)) return [];

  return root.tracks.flatMap((candidate) => {
    const track = record(candidate);
    const joint = record(track?.joint);
    const jointType = type(joint?.type);
    const targetRef = typeof track?.target_ref === "string" ? track.target_ref.trim() : "";
    const parentRef = typeof track?.parent_ref === "string" && track.parent_ref.trim()
      ? track.parent_ref.trim()
      : undefined;
    const id = typeof joint?.id === "string" ? joint.id.trim() : "";
    const label = typeof joint?.label === "string" && joint.label.trim()
      ? joint.label.trim()
      : id;
    const lower = finite(joint?.lower_limit);
    const upper = finite(joint?.upper_limit);
    const jointUnit = unit(joint?.unit);
    const axis = vector3(joint?.axis);
    const samples = Array.isArray(track?.samples)
      ? track.samples.map(parseSample).filter((sample): sample is OpenCadMotionSample => Boolean(sample))
      : [];
    const notes = typeof track?.notes === "string" && track.notes.trim() ? track.notes.trim() : undefined;

    if (!jointType || !targetRef || !id || lower === null || upper === null || !jointUnit || !axis || !samples.length) {
      return [];
    }
    if (validTargetRefs && !validTargetRefs.has(targetRef)) return [];

    samples.sort((a, b) => a.progress - b.progress);
    return [{
      id,
      label,
      type: jointType,
      targetRef,
      parentRef,
      lower,
      upper,
      unit: jointUnit,
      axis,
      notes,
      samples,
    }];
  });
}


export function normalizeCompliantPreviewTracks(
  value: unknown,
  validTargetRefs?: ReadonlySet<string>,
): OpenCadMotionTrack[] {
  const root = record(value);
  if (root?.source !== "forma-compliant-approximation" || !Array.isArray(root.tracks)) return [];

  return root.tracks.flatMap((candidate) => {
    const track = record(candidate);
    const trackType = type(track?.type);
    const id = typeof track?.id === "string" ? track.id.trim() : "";
    const label = typeof track?.label === "string" && track.label.trim() ? track.label.trim() : id;
    const targetRef = typeof track?.target_ref === "string" ? track.target_ref.trim() : "";
    const parentRef = typeof track?.parent_ref === "string" && track.parent_ref.trim()
      ? track.parent_ref.trim()
      : undefined;
    const lower = finite(track?.lower_limit);
    const upper = finite(track?.upper_limit);
    const trackUnit = unit(track?.unit);
    const axis = vector3(track?.axis);
    const samples = Array.isArray(track?.samples)
      ? track.samples.map(parseSample).filter((sample): sample is OpenCadMotionSample => Boolean(sample))
      : [];
    const notes = typeof track?.notes === "string" && track.notes.trim() ? track.notes.trim() : undefined;

    if (trackType !== "compliant" || !id || !targetRef || lower === null || upper === null || !trackUnit || !axis || !samples.length) {
      return [];
    }
    if (validTargetRefs && !validTargetRefs.has(targetRef)) return [];

    samples.sort((a, b) => a.progress - b.progress);
    return [{
      id,
      label,
      type: "compliant",
      targetRef,
      parentRef,
      lower,
      upper,
      unit: trackUnit,
      axis,
      notes,
      samples,
    }];
  });
}

export function openCadSampleAtProgress(track: OpenCadMotionTrack, progress: number) {
  const target = Number.isFinite(progress) ? Math.min(1, Math.max(0, progress)) : 0;
  let nearest = track.samples[0];
  let nearestDistance = Math.abs(nearest.progress - target);
  for (let index = 1; index < track.samples.length; index += 1) {
    const candidate = track.samples[index];
    const distance = Math.abs(candidate.progress - target);
    if (distance < nearestDistance) {
      nearest = candidate;
      nearestDistance = distance;
    }
  }
  return nearest;
}

export function openCadMotionRangeLabel(track: OpenCadMotionTrack) {
  const format = (value: number) => Number.isInteger(value) ? String(value) : value.toFixed(1);
  if (track.unit === "radian") {
    const lower = track.lower * 180 / Math.PI;
    const upper = track.upper * 180 / Math.PI;
    return `${format(lower)} → ${format(upper)}°`;
  }
  if (track.unit === "mm") return `${format(track.lower)} → ${format(track.upper)} mm`;
  return "fixed";
}
