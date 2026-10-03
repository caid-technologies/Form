import type { MeshPayload } from "opencad-viewport";

export type CadModelDescriptor =
  | { kind: "meshes"; meshes: MeshPayload[] }
  | {
      kind: "shape";
      shapeId: string;
      apiBaseUrl?: string;
      kernelUrl?: string;
    }
  | {
      kind: "file";
      url: string;
      filename: string;
      sourceKind: "http" | "path" | "s3";
      apiBaseUrl?: string;
      kernelUrl?: string;
    }
  | { kind: "unsupported"; reason: string };

type PlainRecord = Record<string, unknown>;

const CAD_FILENAME_PATTERN = /\.(?:step|stp|stl)$/i;

function asRecord(value: unknown): PlainRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as PlainRecord
    : null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function numberArray(value: unknown): number[] | null {
  if (Array.isArray(value) && value.every((item) => typeof item === "number" && Number.isFinite(item))) {
    return value;
  }
  if (ArrayBuffer.isView(value) && !(value instanceof DataView)) {
    const values = Array.from(value as unknown as ArrayLike<number>);
    return values.every((item) => Number.isFinite(item)) ? values : null;
  }
  return null;
}

function normalizeMesh(value: unknown, index: number): MeshPayload | null {
  const record = asRecord(value);
  if (!record) return null;

  const vertices = numberArray(record.vertices);
  const faces = numberArray(record.faces);
  if (!vertices || !faces || vertices.length < 3 || faces.length < 3) return null;

  const shapeId = nonEmptyString(record.shapeId ?? record.shape_id ?? record.id) || `cad-model-${index + 1}`;
  const normals = numberArray(record.normals) || undefined;
  const mesh: MeshPayload = {
    shapeId,
    vertices,
    faces,
  };
  const name = nonEmptyString(record.name);
  if (normals) mesh.normals = normals;
  if (name) mesh.name = name;
  return mesh;
}

function meshesFromValue(value: unknown): MeshPayload[] {
  if (Array.isArray(value)) {
    return value.map(normalizeMesh).filter((mesh): mesh is MeshPayload => Boolean(mesh));
  }

  const record = asRecord(value);
  if (!record) return [];

  if (Array.isArray(record.articulated_bodies)) {
    const meshes = record.articulated_bodies.flatMap(meshesFromValue);
    if (meshes.length) return meshes;
  }

  for (const key of ["meshes", "mesh_payloads", "render_meshes"]) {
    if (key in record) {
      const meshes = meshesFromValue(record[key]);
      if (meshes.length) return meshes;
    }
  }

  const directMesh = normalizeMesh(record, 0);
  if (directMesh) return [directMesh];

  for (const key of ["mesh", "adapter", "model", "payload"]) {
    if (key in record) {
      const meshes = meshesFromValue(record[key]);
      if (meshes.length) return meshes;
    }
  }

  return [];
}

function trimTrailingSlashes(value: string | null): string | undefined {
  const normalized = value?.replace(/\/+$/, "");
  return normalized || undefined;
}

function sourceUrlFromRecord(record: PlainRecord): { url: string; sourceKind: "http" | "path" | "s3" } | null {
  const explicitUrl = nonEmptyString(
    record.signed_url
      ?? record.browser_url
      ?? record.signedUrl
      ?? record.browserUrl
      ?? record.download_url
      ?? record.downloadUrl
      ?? record.file_url
      ?? record.fileUrl
      ?? record.href
      ?? record.url,
  );
  if (explicitUrl) {
    if (/^s3:\/\//i.test(explicitUrl)) return { url: explicitUrl, sourceKind: "s3" };
    if (/^https?:\/\//i.test(explicitUrl)) return { url: explicitUrl, sourceKind: "http" };
    if (explicitUrl.startsWith("/")) return { url: explicitUrl, sourceKind: "path" };
    return { url: explicitUrl, sourceKind: "path" };
  }

  const s3Uri = nonEmptyString(record.s3_uri ?? record.s3Uri ?? record.bucket_uri ?? record.bucketUri ?? record.uri);
  if (s3Uri) return { url: s3Uri, sourceKind: "s3" };

  const bucket = nonEmptyString(record.bucket);
  const key = nonEmptyString(record.key ?? record.object_key ?? record.objectKey);
  if (bucket && key) return { url: `s3://${bucket}/${key}`, sourceKind: "s3" };

  const path = nonEmptyString(record.path ?? record.file_path ?? record.model_path ?? record.filePath ?? record.modelPath);
  return path ? { url: path, sourceKind: "path" } : null;
}

function filenameForSource(record: PlainRecord, sourceUrl: string): string {
  const explicitName = nonEmptyString(record.filename ?? record.file_name ?? record.name);
  if (explicitName && CAD_FILENAME_PATTERN.test(explicitName)) return explicitName;

  const pathname = sourceUrl.split("?", 1)[0];
  const lastSegment = pathname.split("/").pop() || "";
  const decodedSegment = (() => {
    try {
      return decodeURIComponent(lastSegment);
    } catch {
      return lastSegment;
    }
  })();
  return CAD_FILENAME_PATTERN.test(decodedSegment) ? decodedSegment : "cad-model.step";
}

function sourceDescriptor(value: unknown): CadModelDescriptor {
  if (typeof value === "string") {
    return sourceDescriptor({ url: value });
  }

  const record = asRecord(value);
  if (!record) {
    return { kind: "unsupported", reason: "The CAD model must be a file source, adapter object, or mesh payload." };
  }

  const shapeId = nonEmptyString(record.shapeId ?? record.shape_id);
  const apiBaseUrl = trimTrailingSlashes(
    nonEmptyString(record.api_url ?? record.apiUrl ?? record.opencad_url ?? record.openCadUrl ?? record.base_url),
  );
  const kernelUrl = trimTrailingSlashes(nonEmptyString(record.kernel_url ?? record.kernelUrl));
  if (shapeId) {
    return {
      kind: "shape",
      shapeId,
      ...(apiBaseUrl ? { apiBaseUrl } : {}),
      ...(kernelUrl ? { kernelUrl } : {}),
    };
  }

  const source = sourceUrlFromRecord(record);
  if (!source) {
    for (const key of ["adapter", "model", "payload"]) {
      if (key in record) {
        const nested = sourceDescriptor(record[key]);
        if (nested.kind !== "unsupported") return nested;
      }
    }
    return { kind: "unsupported", reason: "The CAD model did not include a shape ID, file URL, path, S3 URI, or renderable meshes." };
  }
  if (source.sourceKind === "s3") {
    return {
      kind: "unsupported",
      reason: "An S3 CAD model needs an authorized signed URL or mesh payload before it can be rendered.",
    };
  }
  if (source.sourceKind === "path") {
    return {
      kind: "unsupported",
      reason: "A server-local CAD path must be resolved to an authorized browser URL or mesh payload.",
    };
  }

  return {
    kind: "file",
    url: source.url,
    filename: filenameForSource(record, source.url),
    sourceKind: source.sourceKind,
    ...(apiBaseUrl ? { apiBaseUrl } : {}),
    ...(kernelUrl ? { kernelUrl } : {}),
  };
}

export function projectCadModel(project: unknown): unknown {
  const record = asRecord(project);
  return record?.cad_model ?? null;
}

export function nativeStepArtifact(value: unknown): { projectId: string; sha256: string } | null {
  const cad = asRecord(value);
  if (cad?.adapter !== "forma-opencad" || cad.format !== "step") return null;
  const projectId = nonEmptyString(cad.project_id);
  const sha256 = nonEmptyString(cad.stored_sha256);
  if (!projectId || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(projectId)
      || !sha256 || !/^[0-9a-f]{64}$/.test(sha256)) return null;
  return { projectId, sha256 };
}

export function nativeStepDownloadPath(artifact: { projectId: string; sha256: string }, revisionId?: string): string {
  const project = encodeURIComponent(artifact.projectId);
  return revisionId
    ? `/projects/${project}/history/${encodeURIComponent(revisionId)}/cad/${artifact.sha256}`
    : `/opencode/projects/${project}/cad/${artifact.sha256}`;
}

export function resolveCadModel(value: unknown): CadModelDescriptor | null {
  if (value === null || value === undefined || value === "") return null;

  const meshes = meshesFromValue(value);
  if (meshes.length) return { kind: "meshes", meshes };
  return sourceDescriptor(value);
}
