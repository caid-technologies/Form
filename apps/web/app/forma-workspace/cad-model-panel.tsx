"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Box, LoaderCircle } from "lucide-react";
import type { MeshPayload, OpenCadApiClient } from "opencad-viewport";

import { nativeStepArtifact, nativeStepDownloadPath, resolveCadModel } from "../../lib/cad-model";
import { webConfig } from "../../lib/config";

const OpenCadViewport = dynamic(
  () => import("opencad-viewport").then((module) => module.Viewport3D),
  {
    ssr: false,
    loading: () => <CadModelState icon={<LoaderCircle className="h-7 w-7 animate-spin" />} message="Loading CAD viewport..." />,
  },
);

type CadModelPanelProps = {
  cadModel: unknown;
  apiUrl?: string;
  getHeaders?: () => Promise<Record<string, string>>;
  revisionId?: string;
  shared?: boolean;
};

export default function CadModelPanel({ cadModel, apiUrl, getHeaders, revisionId, shared = false }: CadModelPanelProps) {
  const descriptor = useMemo(() => resolveCadModel(cadModel), [cadModel]);
  const artifact = useMemo(() => nativeStepArtifact(cadModel), [cadModel]);
  const headersRef = useRef(getHeaders);
  headersRef.current = getHeaders;
  const [meshes, setMeshes] = useState<MeshPayload[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    setMeshes([]);
    setError(null);
    setLoading(false);

    if (!descriptor || descriptor.kind === "meshes") return;
    if (descriptor.kind === "unsupported") {
      setError(descriptor.reason);
      return;
    }

    const apiBaseUrl = descriptor.apiBaseUrl || webConfig.openCadBaseUrl;
    const kernelUrl = descriptor.kernelUrl || webConfig.openCadKernelUrl || undefined;
    if (!apiBaseUrl) {
      setError("OpenCAD backend is not configured for this CAD model source.");
      return;
    }

    setLoading(true);
    void (async () => {
      try {
        const { OpenCadApiClient } = await import("opencad-viewport");
        if (cancelled) return;
        const api = new OpenCadApiClient(apiBaseUrl, kernelUrl);
        const snapshotUrl = artifact && apiUrl && revisionId ? `${apiUrl}${nativeStepDownloadPath(artifact, revisionId).replace("/history/", shared ? "/shared/" : "/history/")}` : null;
        const headers = snapshotUrl ? await headersRef.current?.() : undefined;
        const mesh = descriptor.kind === "shape"
          ? await api.getMesh(descriptor.shapeId)
          : await loadFileMesh(api, snapshotUrl || descriptor.url, descriptor.filename, controller.signal, headers);
        if (cancelled) return;
        setMeshes([mesh]);
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Could not load the CAD model.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [descriptor, artifact, apiUrl, revisionId, shared]);

  if (!descriptor) {
    return <CadModelState icon={<Box className="h-7 w-7" />} message="No CAD model attached to this project." />;
  }
  if (descriptor.kind === "unsupported") {
    return <CadModelState icon={<AlertTriangle className="h-7 w-7" />} message={descriptor.reason} />;
  }
  if (descriptor.kind === "meshes") {
    return <CadViewport meshes={descriptor.meshes} />;
  }
  if (loading) {
    return <CadModelState icon={<LoaderCircle className="h-7 w-7 animate-spin" />} message="Preparing CAD model..." />;
  }
  if (!error && !meshes.length) {
    return <CadModelState icon={<LoaderCircle className="h-7 w-7 animate-spin" />} message="Preparing CAD model..." />;
  }
  if (error) {
    return <CadModelState icon={<AlertTriangle className="h-7 w-7" />} message={error} />;
  }
  return <CadViewport meshes={meshes} />;
}

async function loadFileMesh(
  api: OpenCadApiClient,
  url: string,
  filename: string,
  signal: AbortSignal,
  headers?: Record<string, string>,
): Promise<MeshPayload> {
  const response = await (headers ? fetch(url, { signal, headers }) : fetch(url, { signal }));
  if (!response.ok) throw new Error(`Could not fetch CAD model (${response.status}).`);
  const blob = await response.blob();
  signal.throwIfAborted();
  const file = new File([blob], filename, { type: blob.type || "application/octet-stream" });
  const imported = await api.importCadFile(file);
  signal.throwIfAborted();
  return api.getMesh(imported.shape_id);
}

function CadViewport({ meshes }: { meshes: MeshPayload[] }) {
  return (
    <div className="forma-cad-viewport h-full min-h-[420px] w-full">
      <OpenCadViewport meshes={meshes} />
    </div>
  );
}

function CadModelState({ icon, message }: { icon: React.ReactNode; message: string }) {
  return (
    <div className="flex h-full min-h-[420px] w-full items-center justify-center bg-[var(--forma-page)] px-6 text-center">
      <div className="max-w-md">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] text-[var(--forma-text-muted)]">
          {icon}
        </div>
        <p className="mt-4 text-xs font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">{message}</p>
      </div>
    </div>
  );
}
