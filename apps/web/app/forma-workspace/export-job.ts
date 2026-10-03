export type GcodeExportResult = {
  status: "completed";
  project_id: string;
  source_step_sha256: string;
  printer_id: string;
  printer: string;
  material?: string | null;
  nozzle_mm?: number | null;
  layer_height_mm: number;
  slicer: string;
  profile_name?: string | null;
  profile_sha256?: string | null;
  gcode: { filename: string; sha256: string; size_bytes: number; download_url: string; preview: string };
  warnings?: string[];
};

type Pending = { status: "queued" | "running"; job_id: string };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
    const cancel = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", cancel); resolve(); }, ms);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export async function waitForExport(
  initial: unknown,
  options: {
    apiUrl: string; projectId: string; printerId: string; sourceSha256: string;
    signal: AbortSignal;
    request: (url: string, signal: AbortSignal) => Promise<unknown>;
    onState: (state: "queued" | "running") => void;
    sleep?: typeof pause;
    timeoutMs?: number;
  },
): Promise<GcodeExportResult> {
  let payload = initial;
  let jobId: string | undefined;
  const deadline = Date.now() + (options.timeoutMs ?? 180000);
  while (true) {
    options.signal.throwIfAborted();
    if (!payload || typeof payload !== "object") throw new Error("Invalid export response.");
    const value = payload as Record<string, unknown>;
    if (value.project_id !== options.projectId || value.printer_id !== options.printerId
        || value.source_step_sha256 !== options.sourceSha256) {
      throw new Error("The project changed while slicing. Refresh Exports and generate again.");
    }
    if (value.status === "completed") {
      const result = payload as GcodeExportResult;
      if (!result.gcode?.sha256 || !result.gcode?.download_url) throw new Error("The export returned no G-code artifact.");
      return result;
    }
    if ((value.status !== "queued" && value.status !== "running")
        || typeof value.job_id !== "string" || !UUID.test(value.job_id)) {
      throw new Error("The slicing job failed or returned an invalid status.");
    }
    if (jobId && jobId !== value.job_id) throw new Error("The slicing response changed job identity.");
    jobId = value.job_id;
    if (Date.now() >= deadline) throw new Error("Slicing is taking longer than expected. Check the mini-PC before generating again.");
    options.onState((payload as Pending).status);
    await (options.sleep ?? pause)(1000, options.signal);
    payload = await options.request(
      `${options.apiUrl}/projects/${encodeURIComponent(options.projectId)}/exports/gcode/jobs/${jobId}`,
      options.signal,
    );
  }
}
