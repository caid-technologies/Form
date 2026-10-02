"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, Download, History, Loader2, FileBox, X } from "lucide-react";
import { useFormaAuth } from "../../lib/forma-auth";
import { useProjectHistory } from "./project-history";

const CadModelPanel = dynamic(() => import("./cad-model-panel"), { ssr: false });
type Artifact = { filename: string; sha256: string; media_type: string };
type Geometry = { body_count: number; volume_mm3: number; minimum_mm: number[]; maximum_mm: number[] };
type Feature = { source_id: string; source_name: string; source_type: string; target: string; status: string; source_dimensions_mm: Record<string, number>; review_guidance: string; provenance: { evidence: string } };
type Run = { id: string; kind: string; target: string; status: string; stale: boolean; source_revision: number;
  artifacts: Record<string, Artifact>; source_geometry?: Geometry; source_system?: string;
  plan?: { blockers: { code: string; feature_id: string | null }[]; limitations: string[]; features: Feature[] };
  comparison?: { checks: Record<string, boolean>; parameter_checks: Record<string, boolean>; source_geometry: Geometry | null; target_geometry: Geometry; metadata_differences: { property: string; source: unknown; target: unknown }[] };
  execution?: { worker_id?: string; failure?: string } };
type Snapshot = { project_id: string; revision: number; revision_id: string; step_available: boolean; form_history_available: boolean; runs: Run[];
  source?: Artifact & { system: string }; routes: { source: string; target: string }[] };
type Mode = "export" | "migration" | "import" | "activity";
const targets = { solidworks: "SOLIDWORKS", onshape: "Onshape", fusion360: "Autodesk Fusion", nx: "Siemens NX" };
const button = "inline-flex items-center justify-center gap-2 rounded-lg border border-[var(--forma-border)] px-3 py-2 text-xs transition-colors hover:bg-[var(--forma-surface-muted)] disabled:opacity-40";
const input = "w-full rounded-lg border border-[var(--forma-border)] bg-[var(--forma-page)] px-3 py-2 text-sm";
const human = (text: string) => text.replaceAll("_", " ");
const states: Record<string, string> = { ready_for_rebuild: "Plan ready for review", blocked: "Plan needs changes", awaiting_native_execution: "Ready to run in CAD", queued_for_worker: "Waiting for your CAD worker", running_in_cad: "Running in your CAD application", checks_passed: "Reported checks passed — review the model", needs_repair: "Results need attention", accepted_by_reviewer: "Accepted and saved", rejected_by_reviewer: "Changes requested", package_created: "Export package ready", cancelled: "Execution cancelled", worker_failed: "CAD execution failed" };

function message(value: unknown, fallback: string): string {
  const detail = (value as { detail?: { message?: string } })?.detail;
  return detail?.message || fallback;
}

export default function CadHandoffDialog({ projectId, apiUrl, enabled }: { projectId: string; apiUrl: string; enabled: boolean }) {
  const { getToken } = useFormaAuth();
  const history = useProjectHistory();
  const [mode, setMode] = useState<Mode | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [target, setTarget] = useState("fusion360");
  const [runId, setRunId] = useState("");
  const [historyModel, setHistoryModel] = useState<Record<string, unknown> | null>(null);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [sourceSystem, setSourceSystem] = useState("inventor");
  const [note, setNote] = useState("");
  const [partNumber, setPartNumber] = useState("");
  const [partRevision, setPartRevision] = useState("");
  const [approveInferred, setApproveInferred] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showPreview, setShowPreview] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const request = useRef<AbortController | null>(null);
  const running = useRef(false);
  const openButton = useRef<HTMLButtonElement | null>(null);
  const run = snapshot?.runs.find((item) => item.id === runId);
  const headers = useCallback(async () => {
    const token = await getToken();
    return { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  }, [getToken]);
  const url = `${apiUrl}/projects/${encodeURIComponent(projectId)}/cad-workflows`;
  const readSnapshot = useCallback(async (signal: AbortSignal) => {
    const response = await fetch(url, { headers: await headers(), cache: "no-store", signal });
    const payload = await response.json();
    if (!response.ok) throw new Error(message(payload, "CAD workflows could not be loaded."));
    if (payload.project_id !== projectId) throw new Error("The response belongs to a different project.");
    if (!signal.aborted) setSnapshot(payload);
  }, [url, headers, projectId]);
  const isOpen = Boolean(mode);
  useEffect(() => {
    if (!isOpen) return;
    const controller = new AbortController();
    void readSnapshot(controller.signal).catch((cause) => { if (!controller.signal.aborted) setError(String(cause.message)); });
    const node = dialog.current;
    node?.showModal();
    return () => { controller.abort(); node?.close(); request.current?.abort(); openButton.current?.focus(); };
  }, [isOpen, readSnapshot]);
  useEffect(() => {
    if (!mode || !snapshot?.runs.some((item) => ["queued_for_worker", "running_in_cad"].includes(item.status))) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      if (!running.current) void readSnapshot(controller.signal).catch((cause) => { if (!controller.signal.aborted) setError(cause.message); });
    }, 4000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [mode, snapshot, readSnapshot]);

  const perform = async (operation: (signal: AbortSignal) => Promise<void>) => {
    if (running.current) return;
    running.current = true; setBusy(true); setError("");
    const controller = new AbortController(); request.current = controller;
    try { await operation(controller.signal); }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "CAD action failed."); }
    finally { running.current = false; setBusy(false); }
  };
  const refreshProject = async () => {
    if (history) { history.loadPage(); await history.config.loadLatest(new AbortController().signal); }
  };
  const applyResponse = async (response: Response, selectNew: boolean, signal: AbortSignal) => {
    const payload = await response.json();
    if (!response.ok) {
      if (response.status === 409) await readSnapshot(signal);
      throw new Error(message(payload, "The CAD action could not be saved."));
    }
    if (signal.aborted) return;
    setSnapshot(payload);
    if (selectNew) setRunId(payload.runs.at(-1).id);
  };
  const act = (action: string, extra: Record<string, unknown> = {}, selectNew = false) => perform(async (signal) => {
    if (!snapshot) return;
    const response = await fetch(url, { method: "POST", headers: { ...await headers(), "Content-Type": "application/json" }, signal,
      body: JSON.stringify({ action, request_id: crypto.randomUUID(), expected_revision: snapshot.revision, ...(selectNew ? {} : { run_id: runId }), ...extra }) });
    await applyResponse(response, selectNew, signal);
    if (action === "review") await refreshProject();
  });
  const upload = (file: File, result: boolean, preview = false) => perform(async (signal) => {
    if (!snapshot) return;
    if (file.size > 50 * 1024 * 1024) throw new Error("Choose a file smaller than 50 MB.");
    const query = new URLSearchParams({ filename: file.name, system: preview ? snapshot.source?.system || sourceSystem : sourceSystem, expected_revision: String(snapshot.revision), request_id: crypto.randomUUID() });
    if (preview && snapshot.source) query.set("preview_of", snapshot.source.sha256);
    const response = await fetch(result ? `${url}/runs/${runId}/result` : `${url}/source?${query}`, {
      method: "POST", headers: { ...await headers(), "Content-Type": result ? "application/zip" : "application/octet-stream" }, body: file, signal });
    await applyResponse(response, false, signal);
    if (!result) { setSourceFile(null); if (!preview) setHistoryModel(null); setMode("migration"); await refreshProject(); }
  });
  const download = (artifact: Artifact) => perform(async (signal) => {
    if (!snapshot) return;
    const response = await fetch(`${url}/artifacts/${snapshot.revision_id}/${artifact.sha256}`, { headers: await headers(), signal, cache: "no-store" });
    if (!response.ok) throw new Error("Could not download the saved CAD file.");
    const bytes = await response.arrayBuffer();
    const actual = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))).map((value) => value.toString(16).padStart(2, "0")).join("");
    if (actual !== artifact.sha256) throw new Error("The downloaded CAD file failed its integrity check.");
    const objectUrl = URL.createObjectURL(new Blob([bytes], { type: artifact.media_type }));
    const link = document.createElement("a"); link.href = objectUrl; link.download = artifact.filename;
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  });
  const source = (historyModel?.source as { system?: string } | undefined)?.system || snapshot?.source?.system || "form";
  const allowed = mode === "export" ? ["solidworks", "onshape", "fusion360"] : snapshot?.routes.filter((item) => item.source === source).map((item) => item.target) || ["fusion360", "onshape", "nx"];
  const selectedTarget = allowed.includes(target) ? target : allowed[0] || "fusion360";
  const open = (next: Mode, event: React.MouseEvent<HTMLButtonElement>) => { openButton.current = event.currentTarget; setMode(next); setRunId(""); setError(""); setNote(""); setApproveInferred(false); setShowPreview(false); };
  const chooseHistory = async (file: File | undefined) => {
    if (!file) return;
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error("Design history must be smaller than 2 MB.");
      const value = JSON.parse(await file.text());
      if (value.format !== "forma-cad-history") throw new Error("Choose design history extracted or reviewed for this CAD model.");
      setHistoryModel(value); setError("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not read design history."); }
  };
  return <>
    <div className="mt-3 flex flex-wrap gap-2">
      <button type="button" className={button} disabled={!enabled} onClick={(event) => open("export", event)}><Download className="h-3.5 w-3.5" />Export to CAD</button>
      <button type="button" className={button} disabled={!enabled} onClick={(event) => open("migration", event)}><RefreshCw className="h-3.5 w-3.5" />Rebuild editable model</button>
      <button type="button" className={button} disabled={!enabled} onClick={(event) => open("import", event)}><FileBox className="h-3.5 w-3.5" />Import CAD</button>
      <button type="button" className={button} disabled={!enabled} onClick={(event) => open("activity", event)}><History className="h-3.5 w-3.5" />CAD activity</button>
    </div>
    <dialog ref={dialog} aria-labelledby="cad-handoff-title" onCancel={() => setMode(null)} className="m-auto max-h-[90dvh] w-[calc(100%_-_2rem)] max-w-4xl overflow-y-auto rounded-xl border border-[var(--forma-border)] bg-[var(--forma-page)] p-0 text-[var(--forma-text)] shadow-2xl backdrop:bg-black/60">
      <div className="sticky top-0 z-10 flex items-center justify-between gap-4 border-b border-[var(--forma-border)] bg-[var(--forma-surface)] px-5 py-4">
        <div><h2 id="cad-handoff-title" className="text-base font-semibold">{mode === "export" ? "Export geometry" : mode === "import" ? "Import an existing CAD model" : mode === "activity" ? "CAD activity" : "Rebuild an editable model"}</h2><p className="mt-1 text-xs text-[var(--forma-text-muted)]">Saved project revision {snapshot?.revision || "…"}</p></div>
        <button type="button" aria-label="Close CAD workflow" className={button} onClick={() => setMode(null)}><X className="h-4 w-4" /></button>
      </div>
      <div className="space-y-5 p-5">
        {busy && <p role="status" className="flex items-center gap-2 text-xs"><Loader2 className="h-4 w-4 animate-spin" />Saving CAD workflow…</p>}
        {error && <p role="alert" className="rounded-lg border border-[rgb(var(--forma-red-rgb)/0.3)] p-3 text-sm">{error}</p>}
        {!snapshot && !error && <p className="text-sm">Loading your saved CAD model…</p>}
        {snapshot?.source && <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--forma-border)] p-3 text-xs"><span>Original preserved: <strong>{snapshot.source.filename}</strong> · {targets[snapshot.source.system as keyof typeof targets] || human(snapshot.source.system)}</span><button type="button" className={button} disabled={busy} onClick={() => void download(snapshot.source!)}>Download original</button></div>}
        {mode === "import" && snapshot && <>
          <p className="text-sm text-[var(--forma-text-muted)]">Keep the original file and its source details in this project. A STEP file carries geometry; an editable rebuild also requires extracted or reviewed design history.</p>
          <label className="block text-sm">Source application<select className={`${input} mt-2`} value={sourceSystem} onChange={(event) => setSourceSystem(event.target.value)}><option value="inventor">Autodesk Inventor</option><option value="solidworks">SOLIDWORKS</option><option value="creo">Creo</option><option value="form">STEP geometry</option></select></label>
          <label className="block text-sm">Original CAD file<input type="file" className="mt-2 block w-full text-xs" accept=".ipt,.iam,.sldprt,.sldasm,.prt,.asm,.step,.stp" onChange={(event) => setSourceFile(event.target.files?.[0] || null)} /></label>
          <button type="button" className={button} disabled={busy || !sourceFile} onClick={() => sourceFile && void upload(sourceFile, false)}>Preserve original in project</button>
        </>}
        {(mode === "export" || mode === "migration") && !run && snapshot && <>
          <p className="text-sm text-[var(--forma-text-muted)]">{mode === "export" ? "Export your saved STEP geometry with metadata and an import helper. The original feature history is not included." : "Review expected editability and any missing features before a new model is created in your CAD application."}</p>
          <label className="block text-sm">Destination<select className={`${input} mt-2`} value={selectedTarget} onChange={(event) => setTarget(event.target.value)}>{allowed.map((item) => <option key={item} value={item}>{targets[item as keyof typeof targets]}</option>)}</select></label>
          {mode === "export" ? <>
            <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Part number<input className={`${input} mt-2`} value={partNumber} onChange={(event) => setPartNumber(event.target.value)} /></label><label className="text-sm">Part revision<input className={`${input} mt-2`} value={partRevision} onChange={(event) => setPartRevision(event.target.value)} /></label></div>
            {!snapshot.step_available && <p className="text-sm">Import a STEP file or save generated CAD geometry first.</p>}
            <button type="button" className={button} disabled={busy || !snapshot.step_available} onClick={() => void act("export", { target: selectedTarget, metadata: { part_number: partNumber, revision: partRevision } }, true)}>Create export package</button>
          </> : <>
            {snapshot.form_history_available && !snapshot.source && <button type="button" className={button} disabled={busy} onClick={() => void act("form-history", { target: selectedTarget }, true)}>Review this project&apos;s design history</button>}
            <label className="block text-sm">Design history from your source CAD application<input type="file" accept=".json" className="mt-2 block w-full text-xs" onChange={(event) => void chooseHistory(event.target.files?.[0])} /></label>
            <p className="text-xs text-[var(--forma-text-muted)]">Inventor extraction is available in the Form CAD tools. Other sources require reviewed history. A STEP file alone cannot recover the original feature tree.</p>
            <button type="button" className={button} disabled={busy || !historyModel} onClick={() => void act("plan", { target: selectedTarget, history: historyModel }, true)}>Review rebuild plan</button>
            {snapshot.source && !snapshot.step_available && <label className="block text-sm">Source STEP preview (optional)<input type="file" accept=".step,.stp" className="mt-2 block w-full text-xs" onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file, false, true); }} /></label>}
          </>}
        </>}
        {mode === "activity" && snapshot && !run && <div className="space-y-2">{snapshot.runs.filter((item) => item.kind !== "source" && item.kind !== "source-preview").reverse().map((item) => <button type="button" key={item.id} className={`${button} w-full justify-between text-left`} onClick={() => { setRunId(item.id); setNote(""); }}><span>{item.kind === "export" ? "Geometry export" : "Editable rebuild"} · {targets[item.target as keyof typeof targets]}</span><span>{item.stale ? "Older design" : states[item.status] || human(item.status)}</span></button>)}{!snapshot.runs.some((item) => item.kind === "export" || item.kind === "migration") && <p className="text-sm">No CAD handoffs yet. Export geometry or review an editable rebuild to begin.</p>}</div>}
        {run && <>
          <div><h3 className="text-sm font-semibold">{targets[run.target as keyof typeof targets]} · {states[run.status] || human(run.status)}</h3><p className="mt-1 text-xs text-[var(--forma-text-muted)]">Based on project revision {run.source_revision}</p></div>
          {run.stale && <p role="alert" className="text-sm">The design has changed. Start a new plan; these saved files remain available.</p>}
          {run.plan && <>
            <h3 className="text-sm font-semibold">1. Review the source and rebuild plan</h3>
            {run.plan.blockers.length > 0 && <div role="alert" className="rounded-lg border border-[rgb(var(--forma-yellow-rgb)/0.4)] p-3 text-sm"><strong>Resolve these before rebuilding</strong><ul className="mt-2 list-inside list-disc">{run.plan.blockers.map((item, index) => <li key={index}>{item.feature_id && `${item.feature_id}: `}{human(item.code)}</li>)}</ul></div>}
            <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b border-[var(--forma-border)]"><th className="py-2 pr-3">Source feature</th><th className="py-2 pr-3">Rebuild representation</th><th className="py-2">Review</th></tr></thead><tbody>{run.plan.features.map((feature) => <tr key={feature.source_id} className="border-b border-[var(--forma-border)]"><td className="py-3 pr-3 align-top"><strong>{feature.source_name || feature.source_id}</strong><p className="mt-1">{Object.entries(feature.source_dimensions_mm).map(([key, value]) => `${key} ${value.toFixed(2)} mm`).join(" · ")}</p><details className="mt-1"><summary>Source evidence</summary><p className="mt-1 break-words">{feature.provenance.evidence}</p></details></td><td className="py-3 pr-3 align-top">{feature.target}</td><td className="py-3 align-top">{feature.review_guidance}</td></tr>)}</tbody></table></div>
            <details className="text-xs"><summary className="cursor-pointer font-medium">Expected editability and losses</summary><ul className="mt-2 list-inside list-disc space-y-2 text-[var(--forma-text-muted)]">{run.plan.limitations.map((text) => <li key={text}>{text}</li>)}</ul></details>
          </>}
          {run.kind === "migration" && ["blocked", "ready_for_rebuild"].includes(run.status) && <>
            {run.plan?.blockers.some((item) => item.code === "inferred_feature_requires_review") && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={approveInferred} onChange={(event) => setApproveInferred(event.target.checked)} />I reviewed the inferred features against the source model.</label>}
            {approveInferred && <label className="block text-sm">What did you verify?<textarea className={`${input} mt-2`} value={note} onChange={(event) => setNote(event.target.value)} maxLength={2000} /></label>}
            <button type="button" className={button} disabled={busy || run.stale || Boolean(run.plan?.blockers.some((item) => item.code !== "inferred_feature_requires_review")) || Boolean(run.plan?.blockers.length && (!approveInferred || !note.trim()))} onClick={() => void act("build", { approve_inferred: approveInferred, note })}>Approve plan and prepare rebuild</button>
          </>}
          {run.kind === "migration" && run.artifacts.package && !run.stale && <>
            <h3 className="text-sm font-semibold">2. Execute in your CAD application</h3>
            {run.execution?.worker_id && <p className="text-xs">Worker: {run.execution.worker_id}</p>}
            {run.execution?.failure && <p role="alert" className="text-sm">{run.execution.failure}</p>}
            {["awaiting_native_execution", "worker_failed", "cancelled"].includes(run.status) && <button type="button" className={button} disabled={busy} onClick={() => void act("queue")}>Send to customer CAD worker</button>}
            {["queued_for_worker", "running_in_cad"].includes(run.status) && <div className="flex flex-wrap items-center gap-3 text-sm"><span role="status">{states[run.status]}</span><button type="button" className={button} disabled={busy} onClick={() => void act("cancel")}>Cancel execution</button><p className="w-full text-xs text-[var(--forma-text-muted)]">Cancellation stops accepting this attempt. If CAD is already running, stop it in your local CAD application.</p></div>}
            <details className="text-xs"><summary className="cursor-pointer font-medium">Connect a worker or run the package manually</summary><p className="mt-2 text-[var(--forma-text-muted)]">Your customer-controlled worker must be installed in the licensed CAD environment and authorized for this project. The Fusion worker is included; NX and Onshape require your own executor. Form does not execute CAD in this browser.</p><p className="mt-2 break-all">Project: {projectId}</p><a className="mt-2 inline-block underline" href="https://github.com/caid-technologies/Form-OSS/blob/feature/cad-native-validation/docs/ai-cad-migrations.md#customer-controlled-execution-worker" target="_blank" rel="noreferrer">Worker setup and native execution instructions</a></details>
            {!["queued_for_worker", "running_in_cad"].includes(run.status) && <label className="block text-sm">Return native model and validation evidence<input type="file" accept=".zip" className="mt-2 block w-full text-xs" disabled={busy} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file, true); }} /><span className="mt-1 block text-xs text-[var(--forma-text-muted)]">Result ZIP: evidence.json, target native model and STEP preview. Every file must match its reported checksum.</span></label>}
          </>}
          {run.comparison && <>
            <h3 className="text-sm font-semibold">3. Inspect the results and save a revision</h3>
            <div className="grid gap-2 sm:grid-cols-2">{Object.entries(run.comparison.checks).map(([check, passed]) => <p key={check} className="rounded-lg border border-[var(--forma-border)] p-2 text-xs">{passed ? "Passed" : "Needs attention"}: {human(check)}</p>)}</div>
            <GeometryComparison source={run.comparison.source_geometry} target={run.comparison.target_geometry} />
            <details className="text-xs" open><summary className="font-medium">Parameter regeneration</summary><ul className="mt-2 space-y-1">{Object.entries(run.comparison.parameter_checks).map(([name, passed]) => <li key={name}>{name}: {passed ? "Changed and regenerated" : "Needs attention"}</li>)}</ul></details>
            <div className="text-xs"><h4 className="font-medium">Metadata differences</h4>{run.comparison.metadata_differences.length ? run.comparison.metadata_differences.map((item) => <p key={item.property} className="mt-1 break-words">{item.property}: {JSON.stringify(item.source)} → {JSON.stringify(item.target)}</p>) : <p className="mt-1">No reported metadata differences.</p>}</div>
            {run.artifacts.target_step && <><button type="button" className={button} onClick={() => setShowPreview((value) => !value)}>{showPreview ? "Hide" : "Inspect"} source and rebuilt geometry</button>{showPreview && snapshot && <div className="grid gap-3 lg:grid-cols-2">{["source_step", "target_step"].map((key) => <div key={key} className="overflow-hidden rounded-lg border border-[var(--forma-border)]"><p className="border-b border-[var(--forma-border)] p-3 text-sm">{key === "source_step" ? "Source geometry" : "Rebuilt geometry"}</p>{(run.artifacts[key] || run.artifacts[key === "source_step" ? "source_mesh" : "target_mesh"]) ? <NativePreview projectId={projectId} revisionId={snapshot.revision_id} apiUrl={apiUrl} headers={headers} step={run.artifacts[key]} mesh={run.artifacts[key === "source_step" ? "source_mesh" : "target_mesh"]} sourceGeometry={run.comparison?.source_geometry} targetGeometry={run.comparison?.target_geometry} /> : <p className="p-4 text-sm">No source STEP preview is attached. The original native file remains preserved.</p>}</div>)}</div>}</>}
            <p className="text-xs text-[var(--forma-text-muted)]">These are customer-reported checks. Matching volume and bounds does not establish equal surfaces or topology. Inspect the native model and parameter behavior before accepting.</p>
            {!run.stale && <><label className="block text-sm">Review note<textarea className={`${input} mt-2`} value={note} maxLength={2000} onChange={(event) => setNote(event.target.value)} placeholder="Record the model, parameter and metadata checks you performed." /></label><div className="flex flex-wrap gap-2"><button type="button" className={button} disabled={busy || !note.trim() || run.status !== "checks_passed" || !run.artifacts.target_step || !Object.keys(run.artifacts).some((key) => key.startsWith("native_"))} onClick={() => void act("review", { decision: "accept", note })}>Accept and save new revision</button><button type="button" className={button} disabled={busy || !note.trim()} onClick={() => void act("review", { decision: "reject", note })}>Request changes</button></div></>}
          </>}
          <div className="flex flex-wrap gap-2">{Object.entries(run.artifacts).filter(([key]) => ["package", "source", "history", "evidence", "comparison", "target_step"].includes(key) || key.startsWith("native_")).map(([key, artifact]) => <button type="button" key={key} className={button} disabled={busy} onClick={() => void download(artifact)}><Download className="h-3.5 w-3.5" />{key === "package" ? "Download package" : artifact.filename}</button>)}</div>
          <button type="button" className={button} disabled={busy} onClick={() => { setRunId(""); setMode("activity"); setNote(""); setApproveInferred(false); }}>Back to CAD activity</button>
        </>}
      </div>
    </dialog>
  </>;
}

function GeometryComparison({ source, target }: { source: Geometry | null; target: Geometry }) {
  const values = (metrics: Geometry | null) => metrics ? [metrics.body_count, `${metrics.volume_mm3.toFixed(3)} mm³`, metrics.minimum_mm.map((v) => v.toFixed(2)).join(", "), metrics.maximum_mm.map((v) => v.toFixed(2)).join(", ")] : ["Missing", "Missing", "Missing", "Missing"];
  const a = values(source), b = values(target);
  return <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr><th className="py-2">Geometry</th><th>Source</th><th>Rebuilt</th></tr></thead><tbody>{["Bodies", "Volume", "Minimum bounds (mm)", "Maximum bounds (mm)"].map((label, index) => <tr className="border-t border-[var(--forma-border)]" key={label}><td className="py-2 pr-3">{label}</td><td className="pr-3">{a[index]}</td><td>{b[index]}</td></tr>)}</tbody></table></div>;
}

function NativePreview({ projectId, revisionId, apiUrl, headers, step, mesh, sourceGeometry, targetGeometry }: { projectId: string; revisionId: string; apiUrl: string; headers: () => Promise<Record<string, string>>; step?: Artifact; mesh?: Artifact; sourceGeometry?: Geometry | null; targetGeometry?: Geometry }) {
  const [model, setModel] = useState<unknown>(null);
  const [error, setError] = useState("");
  const meshSha = mesh?.sha256;
  // Frame both views with the same reported bounds; preserve original model bytes.
  const frame = JSON.stringify([sourceGeometry, targetGeometry]);
  const path = `/projects/${projectId}/cad-workflows/artifacts/${revisionId}`;
  useEffect(() => {
    setModel(null); setError("");
    if (!meshSha) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch(`${apiUrl}${path}/${meshSha}`, { headers: await headers(), signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error("CAD preview could not be downloaded.");
        const bytes = await response.arrayBuffer();
        const actual = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))).map((value) => value.toString(16).padStart(2, "0")).join("");
        if (actual !== meshSha) throw new Error("CAD preview integrity check failed.");
        const payload = JSON.parse(new TextDecoder().decode(bytes));
        const geometries = (JSON.parse(frame) as (Geometry | null)[]).filter((item): item is Geometry => Boolean(item));
        if (geometries.length) {
          const minimum = [0, 1, 2].map((axis) => Math.min(...geometries.map((item) => item.minimum_mm[axis])));
          const maximum = [0, 1, 2].map((axis) => Math.max(...geometries.map((item) => item.maximum_mm[axis])));
          const scale = 30 / Math.max(...maximum.map((value, axis) => value - minimum[axis]));
          payload.vertices = payload.vertices.map((value: number, index: number) => (value - (index % 3 === 2 ? minimum[2] : (minimum[index % 3] + maximum[index % 3]) / 2)) * scale);
        }
        if (!controller.signal.aborted) setModel({ meshes: [{ shapeId: meshSha, name: "Native CAD preview", vertices: payload.vertices, faces: payload.faces }] });
      } catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "CAD preview failed."); }
    })();
    return () => controller.abort();
  }, [apiUrl, path, meshSha, headers, frame]);
  if (error) return <p role="alert" className="p-4 text-sm">{error}</p>;
  if (meshSha && !model) return <p className="p-4 text-sm">Loading native geometry preview…</p>;
  return <div className="h-[420px]"><CadModelPanel cadModel={model} apiUrl={apiUrl} getHeaders={headers} artifactPath={meshSha || !step ? undefined : `${path}/${step.sha256}`} /></div>;
}
