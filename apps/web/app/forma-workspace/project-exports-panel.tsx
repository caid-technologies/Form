"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, ChevronDown, Download, FileBox, FileJson, FileText, Loader2, Printer, RefreshCw, TriangleAlert } from "lucide-react";
import { webConfig } from "../../lib/config";
import { useFormaAuth } from "../../lib/forma-auth";
import { waitForExport, type GcodeExportResult } from "./export-job";

type ExportPrinter = {
  printer_id: string; display_name: string; nozzle_mm: number; material: string;
  layer_height_mm: number; available: boolean; unavailable_reason?: string | null;
};
type DownloadableArtifact = {
  filename: string;
  sha256: string;
  size_bytes?: number | null;
  download_url: string;
};
type MeshExportFormat = "stl" | "3mf" | "obj";
type Manifest = {
  project_id: string;
  step: DownloadableArtifact;
  mesh_exports?: Partial<Record<MeshExportFormat, DownloadableArtifact>>;
  printers: ExportPrinter[];
};
type SliceState = "idle" | "queued" | "running" | "completed" | "failed";

function normalizeApiUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  return !trimmed ? "/api" : trimmed.endsWith("/api") ? trimmed : `${trimmed}/api`;
}
const API_URL = normalizeApiUrl(webConfig.apiBaseUrl);
const MESH_EXPORT_FORMATS: MeshExportFormat[] = ["stl", "3mf", "obj"];

function formatBytes(value?: number | null): string {
  if (!value || value < 1) return "Size unavailable";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const value = payload as Record<string, unknown>;
  const detail = value.detail;
  if (detail && typeof detail === "object") {
    const message = (detail as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return typeof value.message === "string" ? value.message : fallback;
}

type ProjectExportsPanelProps = {
  projectId: string;
  canDownloadAssets: boolean;
  onDownloadJSON: () => void;
  onDownloadMarkdown: () => void;
};

export default function ProjectExportsPanel({
  projectId,
  canDownloadAssets,
  onDownloadJSON,
  onDownloadMarkdown,
}: ProjectExportsPanelProps) {
  const { authRequired, getToken, isLoaded, isSignedIn, openSignIn } = useFormaAuth();
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [selectedPrinterId, setSelectedPrinterId] = useState("");
  const [result, setResult] = useState<GcodeExportResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloadMenuOpen, setDownloadMenuOpen] = useState(false);
  const [sliceState, setSliceState] = useState<SliceState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const generation = useRef<AbortController | null>(null);
  const downloadMenuRef = useRef<HTMLDivElement | null>(null);
  const canRead = isLoaded && (!authRequired || isSignedIn);
  const currentManifest = manifest?.project_id === projectId ? manifest : null;
  const selectedPrinter = currentManifest?.printers.find((p) => p.printer_id === selectedPrinterId);
  const sourceSha = currentManifest?.step.sha256;
  const storageKey = `forma.export.${projectId}.${selectedPrinterId}.${sourceSha || ""}`;
  const busy = sliceState === "queued" || sliceState === "running";

  const requestHeaders = useCallback(async (json = false): Promise<Record<string, string>> => {
    const token = await getToken();
    return { Accept: "application/json", ...(json ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  }, [getToken]);

  const requestJson = useCallback(async (url: string, signal: AbortSignal, body?: object) => {
    const response = await fetch(url, {
      method: body ? "POST" : "GET", headers: await requestHeaders(Boolean(body)),
      body: body ? JSON.stringify(body) : undefined, cache: "no-store", signal,
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) throw new Error(errorMessage(payload, `Export request failed (${response.status}).`));
    return payload;
  }, [requestHeaders]);

  useEffect(() => {
    const controller = new AbortController();
    setManifest(null);
    setResult(null);
    if (!projectId || !canRead) return () => controller.abort();
    setLoading(true);
    setError(null);
    void requestJson(`${API_URL}/projects/${encodeURIComponent(projectId)}/exports`, controller.signal)
      .then((payload) => {
        if (controller.signal.aborted) return;
        const next = payload as Manifest;
        if (next?.project_id !== projectId || !next.step?.sha256 || !Array.isArray(next.printers)) {
          throw new Error("Invalid project exports response.");
        }
        setManifest(next);
        setSelectedPrinterId((current) => next.printers.some((p) => p.printer_id === current)
          ? current : next.printers.find((p) => p.available)?.printer_id || next.printers[0]?.printer_id || "");
      })
      .catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load exports."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, canRead, refresh, requestJson]);

  useEffect(() => {
    if (!downloadMenuOpen) return;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!downloadMenuRef.current?.contains(event.target as Node)) setDownloadMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDownloadMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [downloadMenuOpen]);

  useEffect(() => {
    generation.current?.abort();
    setResult(null);
    setSliceState("idle");
    if (canRead && sourceSha) {
      try {
        const parsed = JSON.parse(localStorage.getItem(storageKey) || "null") as GcodeExportResult | null;
        if (parsed?.project_id === projectId && parsed.printer_id === selectedPrinterId
            && parsed.source_step_sha256 === sourceSha && parsed.gcode?.sha256) {
          setResult(parsed);
          setSliceState("completed");
        }
      } catch { /* Storage is optional; it must not block exports. */ }
    }
    return () => generation.current?.abort();
  }, [storageKey, sourceSha, projectId, selectedPrinterId, canRead]);

  const downloadAuthenticated = useCallback(async (relativeUrl: string, filename: string) => {
    setError(null);
    // Only authenticated artifact routes belonging to this project may be downloaded.
    if (!relativeUrl.startsWith(`/projects/${encodeURIComponent(projectId)}/exports/`)) {
      throw new Error("Invalid project artifact URL.");
    }
    const response = await fetch(`${API_URL}${relativeUrl}`, { headers: await requestHeaders(), cache: "no-store" });
    if (!response.ok) throw new Error(errorMessage(await response.json().catch(() => null), `Download failed (${response.status}).`));
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = objectUrl; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }, [projectId, requestHeaders]);

  const generateGcode = async () => {
    if (!selectedPrinter?.available || !sourceSha || busy) return;
    generation.current?.abort();
    const controller = new AbortController();
    generation.current = controller;
    setResult(null); setError(null); setSliceState("queued");
    try {
      const initial = await requestJson(`${API_URL}/projects/${encodeURIComponent(projectId)}/exports/gcode`,
        controller.signal, { printer_id: selectedPrinterId });
      const next = await waitForExport(initial, {
        apiUrl: API_URL, projectId, printerId: selectedPrinterId, sourceSha256: sourceSha,
        signal: controller.signal, request: requestJson, onState: setSliceState,
      });
      if (controller.signal.aborted) return;
      setResult(next); setSliceState("completed");
      try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* Download still works. */ }
    } catch (cause) {
      if (controller.signal.aborted) return;
      setSliceState("failed");
      setError(cause instanceof Error ? cause.message : "G-code generation failed.");
    }
  };
  const download = (url: string, filename: string) => {
    void downloadAuthenticated(url, filename).catch((cause) => setError(cause instanceof Error ? cause.message : "Download failed."));
  };

  if (authRequired && isLoaded && !isSignedIn) {
    return <div className="flex h-full items-center justify-center p-6"><div className="text-center">
      <FileBox className="mx-auto mb-3 h-7 w-7" /><h3 className="text-sm font-semibold">Sign in to export this project</h3>
      <button type="button" className="mt-4 rounded-lg border px-3 py-2 text-xs"
        onClick={() => openSignIn({ redirectUrl: window.location.href })}>Sign in</button>
    </div></div>;
  }
  if (loading && !currentManifest) return <div className="flex h-full items-center justify-center gap-2 text-xs"><Loader2 className="h-4 w-4 animate-spin" />Loading exports…</div>;

  return <div className="h-full overflow-y-auto bg-[var(--forma-page)] p-4 sm:p-6">
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
      <div>
        <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Project exports</p>
        <h2 className="mt-1 text-lg font-semibold text-[var(--forma-text-strong)]">Download your project assets</h2>
        <p className="mt-1 text-xs text-[var(--forma-text-muted)]">Project data, build documentation, CAD, and fabrication files live in one place.</p>
      </div>
      <section className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4">
        <div className="mb-3">
          <h3 className="text-sm font-semibold text-[var(--forma-text-strong)]">Project files</h3>
          <p className="mt-1 text-xs text-[var(--forma-text-muted)]">Portable source data and builder-facing documentation.</p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <button
            type="button"
            onClick={onDownloadJSON}
            disabled={!canDownloadAssets}
            title={canDownloadAssets ? "Download full project data" : "Files are available only on projects you generated."}
            className="flex items-center justify-between gap-3 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-page)] p-3 text-left transition-colors hover:bg-[var(--forma-surface-muted)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <span className="flex min-w-0 items-center gap-3">
              <FileJson className="h-5 w-5 shrink-0 text-[rgb(var(--forma-cyan-rgb))]" />
              <span className="min-w-0">
                <span className="block text-xs font-semibold text-[var(--forma-text-strong)]">Project JSON</span>
                <span className="mt-1 block text-[10px] text-[var(--forma-text-muted)]">Full Hardware Intermediate Representation and project metadata (.json)</span>
              </span>
            </span>
            <Download className="h-4 w-4 shrink-0 text-[var(--forma-text-muted)]" />
          </button>
          <button
            type="button"
            onClick={onDownloadMarkdown}
            disabled={!canDownloadAssets}
            title={canDownloadAssets ? "Download build documentation" : "Files are available only on projects you generated."}
            className="flex items-center justify-between gap-3 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-page)] p-3 text-left transition-colors hover:bg-[var(--forma-surface-muted)] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <span className="flex min-w-0 items-center gap-3">
              <FileText className="h-5 w-5 shrink-0 text-[rgb(var(--forma-green-rgb))]" />
              <span className="min-w-0">
                <span className="block text-xs font-semibold text-[var(--forma-text-strong)]">Build documentation</span>
                <span className="mt-1 block text-[10px] text-[var(--forma-text-muted)]">Assembly instructions and safety audit (.md)</span>
              </span>
            </span>
            <Download className="h-4 w-4 shrink-0 text-[var(--forma-text-muted)]" />
          </button>
        </div>
        {!canDownloadAssets && (
          <p className="mt-3 text-[11px] text-[var(--forma-text-muted)]">Downloads are available only on projects you generated.</p>
        )}
      </section>
      <div>
        <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Manufacturing</p>
        <p className="mt-1 text-xs text-[var(--forma-text-muted)]">Download the canonical STEP model, portable mesh formats, or generate printer-specific G-code from the same geometry.</p>
      </div>
      {error && <div role="alert" className="flex items-start gap-2 rounded-lg border border-red-400/25 bg-red-500/5 p-3 text-xs text-red-200">
        <TriangleAlert className="h-4 w-4 shrink-0" /><span className="flex-1">{error}</span>
        <button type="button" className="underline" disabled={busy} onClick={() => setRefresh((v) => v + 1)}>Refresh</button></div>}
      <section className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-start gap-3">
            <FileBox className="h-5 w-5" />
            <div>
              <h3 className="text-sm font-semibold">CAD geometry</h3>
              <p className="mt-1 text-xs text-[var(--forma-text-muted)]">{currentManifest?.step.filename || "assembly.step"} · {formatBytes(currentManifest?.step.size_bytes)}</p>
              {sourceSha && <p className="mt-1 font-mono text-[10px]">sha256:{sourceSha.slice(0, 16)}…</p>}
              <p className="mt-1 text-[10px] text-[var(--forma-text-muted)]">STEP is the source of truth; STL, 3MF, and OBJ are generated from the same model.</p>
            </div>
          </div>
          <div ref={downloadMenuRef} className="relative shrink-0">
            <button
              type="button"
              disabled={!currentManifest?.step}
              onClick={() => setDownloadMenuOpen((open) => !open)}
              aria-haspopup="menu"
              aria-expanded={downloadMenuOpen}
              className="inline-flex items-center gap-2 rounded-lg border border-[var(--forma-border)] px-3 py-2 text-xs disabled:opacity-40"
            >
              <Download className="h-4 w-4" />
              Download
              <ChevronDown className={`h-3.5 w-3.5 transition-transform ${downloadMenuOpen ? "rotate-180" : ""}`} />
            </button>
            {downloadMenuOpen && currentManifest && (
              <div
                role="menu"
                aria-label="Download CAD format"
                className="absolute left-0 right-auto top-full z-30 mt-2 w-48 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface)] py-1 shadow-xl sm:left-auto sm:right-0"
              >
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setDownloadMenuOpen(false);
                    download(currentManifest.step.download_url, currentManifest.step.filename);
                  }}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs transition-colors hover:bg-[var(--forma-surface-muted)]"
                >
                  <span className="font-medium">STEP</span>
                  <span className="text-[10px] text-[var(--forma-text-muted)]">Source CAD</span>
                </button>
                {MESH_EXPORT_FORMATS.map((format) => {
                  const artifact = currentManifest.mesh_exports?.[format];
                  return <button
                    key={format}
                    type="button"
                    role="menuitem"
                    disabled={!artifact}
                    title={artifact ? `Download ${format.toUpperCase()}` : `${format.toUpperCase()} is not available for this project revision.`}
                    onClick={() => {
                      if (!artifact) return;
                      setDownloadMenuOpen(false);
                      download(artifact.download_url, artifact.filename);
                    }}
                    className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs transition-colors hover:bg-[var(--forma-surface-muted)] disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <span className="font-medium">{format.toUpperCase()}</span>
                    <span className="text-[10px] text-[var(--forma-text-muted)]">
                      {format === "stl" ? "3D printing" : format === "3mf" ? "Print package" : "Mesh"}
                    </span>
                  </button>;
                })}
              </div>
            )}
          </div>
        </div>
      </section>
      <section className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4">
        <div className="flex items-start gap-3"><Printer className="h-5 w-5" /><div>
          <h3 className="text-sm font-semibold">Printer G-code</h3>
          <p className="mt-1 text-xs text-[var(--forma-text-muted)]">A slicer processes your saved STEP. The agent does not write raw G-code.</p></div></div>
        <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
          <label><span className="mb-1.5 block text-[11px] uppercase">Printer</span>
            <select value={selectedPrinterId} disabled={busy} onChange={(event) => setSelectedPrinterId(event.target.value)}
              className="w-full rounded-lg border border-[var(--forma-border)] bg-[var(--forma-page)] px-3 py-2.5 text-sm">
              {(currentManifest?.printers || []).map((p) => <option key={p.printer_id} value={p.printer_id}>{p.display_name} · {p.nozzle_mm.toFixed(1)} mm</option>)}
            </select></label>
          <button type="button" disabled={!selectedPrinter?.available || busy} onClick={() => void generateGcode()}
            className="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-4 py-2 text-xs font-semibold disabled:opacity-40">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {sliceState === "queued" ? "Queued…" : sliceState === "running" ? "Generating…" : result ? "Regenerate G-code" : "Generate G-code"}</button>
        </div>
        {selectedPrinter && <p className="mt-3 text-[11px] text-[var(--forma-text-muted)]">{selectedPrinter.material} · {selectedPrinter.nozzle_mm.toFixed(1)} mm nozzle · {selectedPrinter.layer_height_mm.toFixed(2)} mm layers</p>}
        {selectedPrinter && !selectedPrinter.available && <p className="mt-3 rounded-lg bg-amber-400/5 p-3 text-xs text-amber-100">{selectedPrinter.unavailable_reason || "The slicing worker is not configured."}</p>}
        {result && sliceState === "completed" && <div className="mt-4 rounded-lg border border-emerald-400/20 bg-emerald-400/5 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex gap-2"><CheckCircle2 className="h-4 w-4 text-emerald-300" /><div>
            <p className="text-sm font-semibold">G-code generated</p>
            <p className="mt-1 text-xs text-[var(--forma-text-muted)]">{result.printer} · {result.slicer} · {formatBytes(result.gcode.size_bytes)}</p>
          </div></div><button type="button" onClick={() => download(result.gcode.download_url, result.gcode.filename)}
            className="inline-flex items-center gap-2 rounded-lg border border-emerald-300/20 px-3 py-2 text-xs text-emerald-100"><Download className="h-4 w-4" />Download G-code</button></div>
          <details className="mt-4"><summary className="cursor-pointer text-xs font-medium">View G-code preview</summary>
            <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-black/20 p-3 text-[10px] leading-4">{result.gcode.preview}</pre></details>
          <p className="mt-3 font-mono text-[10px]">G-code SHA-256: {result.gcode.sha256.slice(0, 16)}…</p>
          {result.profile_sha256 && <p className="text-[10px]">Profile fingerprint: {result.profile_sha256.slice(0, 16)}…</p>}
          <p className="mt-3 text-[10px] text-[var(--forma-text-muted)]">Review the toolpath in your slicer before printing. This export does not connect to or start a printer.</p>
        </div>}
      </section>
    </div>
  </div>;
}
