import assert from "node:assert/strict";
import test from "node:test";
import { waitForExport, pause } from "../app/forma-workspace/export-job.ts";
const job = "2585fa73-4851-4e9a-80cc-c0f3b0055826";
const identity = { project_id: "project", printer_id: "ender", source_step_sha256: "digest" };
const complete = { ...identity, status: "completed", gcode: { sha256: "abc", download_url: "/projects/project/exports/gcode/abc" } };
function options(overrides = {}) {
  return { apiUrl: "/api", projectId: "project", printerId: "ender", sourceSha256: "digest",
    signal: new AbortController().signal, request: async (_url: string, _signal: AbortSignal) => complete,
    onState: (_state: "queued" | "running") => {}, sleep: async (_ms: number, _signal: AbortSignal) => {}, ...overrides };
}
test("queued/running/completed polls actual project job, never worker URL", async () => {
  const urls: string[] = [], states: string[] = [];
  const result = await waitForExport({ ...identity, status: "queued", job_id: job }, options({
    onState: (s: string) => states.push(s),
    request: async (url: string) => { urls.push(url); return urls.length === 1 ? { ...identity, status: "running", job_id: job } : complete; },
  }));
  assert.deepEqual(states, ["queued", "running"]);
  assert.equal(urls.length, 2);
  assert.ok(urls.every((u) => u === `/api/projects/project/exports/gcode/jobs/${job}`));
  assert.equal(result, complete);
});
test("legacy synchronous completion still works", async () => {
  assert.equal(await waitForExport(complete, options({ request: () => assert.fail("no polling") })), complete);
});
test("changed project revision is rejected", async () => {
  await assert.rejects(waitForExport({ ...complete, source_step_sha256: "old" }, options()), /project changed/);
});
test("worker failures and invalid IDs are not success", async () => {
  await assert.rejects(waitForExport({ ...identity, status: "failed" }, options()), /failed/);
  await assert.rejects(waitForExport({ ...identity, status: "running", job_id: "../../secret" }, options()), /invalid/);
});
test("pending job does not poll forever", async () => {
  await assert.rejects(waitForExport({ ...identity, status: "running", job_id: job }, options({ timeoutMs: -1 })), /longer/);
});
test("project navigation cancels polling", async () => {
  const controller = new AbortController(); controller.abort();
  await assert.rejects(waitForExport(complete, options({ signal: controller.signal })), { name: "AbortError" });
});
test("delay is abortable", async () => {
  const controller = new AbortController();
  const pending = pause(10000, controller.signal); controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
});
