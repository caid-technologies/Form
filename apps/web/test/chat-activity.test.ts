import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CHAT_ACTIVITY_STALE_MS, chatActivity, chatOperation, collectChatOperations,
  readChatActivity, settleChatActivityMessages,
  type ChatActivityMessage, type ChatActivityObservations,
} from "../lib/chat-activity.ts";

const pending: ChatActivityMessage = {
  id: "assistant-1", status: "loading", content: "Build agents are generating the design.",
  contextProjectId: "project-1", buildPlanId: "build-plan-1",
};
const noLiveRequests = new Set<string>();
const operation = chatOperation(pending)!;
const response = (status: number, data: unknown): typeof fetch => async () => new Response(JSON.stringify(data), { status });

test("five restored build snapshots are checked together without displaying five running builds", () => {
  const threads = Object.fromEntries(Array.from({ length: 5 }, (_, index) => [
    `chat-${index}`, [{ ...pending, id: `message-${index}`, contextProjectId: `project-${index}` }],
  ]));
  assert.equal(collectChatOperations(threads).length, 5);
  for (const messages of Object.values(threads)) {
    assert.equal(chatActivity(messages, {}, noLiveRequests)?.state, "checking");
  }
});

test("a saved planned build gets a queued indicator after a GET, without execution", async () => {
  const calls: Array<{ url: string; method: string | undefined }> = [];
  const result = await readChatActivity("https://example.invalid", operation, {}, new AbortController().signal,
    async (url, init) => {
      calls.push({ url: String(url), method: init?.method });
      return new Response(JSON.stringify({ status: "planned" }));
    });
  assert.deepEqual(calls, [{ url: "https://example.invalid/projects/project-1/build/plans/build-plan-1", method: "GET" }]);
  assert.equal(chatActivity([pending], { [operation.key]: result }, noLiveRequests)?.state, "queued");
});

test("completed unopened chats settle locally and do not resume their spinner on a subsequent read", async () => {
  const result = await readChatActivity("", operation, {}, new AbortController().signal, response(200, { status: "succeeded" }));
  const observations = { [operation.key]: result };
  const settled = settleChatActivityMessages([pending], observations, noLiveRequests);
  assert.equal(settled[0].status, "success");
  assert.equal((settled[0] as ChatActivityMessage & { projectId?: string }).projectId, "project-1");
  assert.equal(chatActivity(settled, observations, noLiveRequests), undefined);
  assert.equal(settleChatActivityMessages(settled, observations, noLiveRequests), settled);
  assert.equal(pending.status, "loading");
});

test("missing jobs show an unknown outcome and never become successful or cancelled", async () => {
  const result = await readChatActivity("", operation, {}, new AbortController().signal, response(404, {}));
  const original = [pending];
  assert.equal(result.state, "interrupted");
  assert.equal(result.outcome, undefined);
  assert.equal(settleChatActivityMessages(original, { [operation.key]: result }, noLiveRequests), original);
});

test("network errors, HTTP failures, and malformed responses leave builds eligible for recovery", async () => {
  for (const request of [response(503, {}), response(200, { status: "surprise" }),
    (async () => { throw new TypeError("Connection lost"); }) as typeof fetch]) {
    const result = await readChatActivity("", operation, {}, new AbortController().signal, request);
    assert.equal(result.state, "reconnecting");
    assert.equal(result.outcome, undefined);
    assert.equal(settleChatActivityMessages([pending], { [operation.key]: result }, noLiveRequests)[0].status, "loading");
  }
});

test("authentication failures do not change the saved outcome", async () => {
  for (const status of [401, 403]) {
    const result = await readChatActivity("", operation, {}, new AbortController().signal, response(status, {}));
    assert.equal(result.state, "interrupted");
    assert.equal(result.outcome, undefined);
  }
});

test("confirmed running activity expires when no recent observation is available", () => {
  const observations: ChatActivityObservations = { [operation.key]: { state: "running", label: "Running", observedAt: 1_000 } };
  assert.equal(chatActivity([pending], observations, noLiveRequests, 2_000)?.state, "running");
  assert.equal(chatActivity([pending], observations, noLiveRequests, 1_001 + CHAT_ACTIVITY_STALE_MS)?.state, "reconnecting");
});

test("an old loading message without a job ID cannot keep a later completed turn spinning", () => {
  const orphan = { id: "orphan", status: "loading", content: "Thinking…" };
  const messages = [orphan, { id: "later", status: "success", content: "Completed another request." }];
  assert.equal(chatActivity(messages, {}, noLiveRequests)?.state, "interrupted");
  assert.equal(chatActivity(messages, {}, new Set([orphan.id]))?.state, "running");
});

test("legacy A2A jobs and build plans use different endpoints and duplicate references share one read", () => {
  const job = { id: "legacy", status: "loading", content: "Working", pipelineProgress: { jobId: "job_frontend_1" } };
  assert.equal(chatOperation(job)?.path, "/a2a/jobs/job_frontend_1");
  assert.equal(chatOperation({ ...job, pipelineProgress: { jobId: "generation-orphan" } }), null);
  assert.equal(collectChatOperations({ a: [pending, job], b: [pending] }).length, 2);
});

test("terminal reads do not overwrite answers still being delivered by the active browser request", async () => {
  const result = await readChatActivity("", operation, {}, new AbortController().signal, response(200, { status: "succeeded" }));
  const messages = [pending];
  assert.equal(settleChatActivityMessages(messages, { [operation.key]: result }, new Set([pending.id])), messages);
});

test("failed, partial, and cancelled plans retain their distinct terminal outcomes", async () => {
  for (const [outcome, expected] of [["failed", "error"], ["partial", "error"], ["cancelled", "cancelled"]]) {
    const result = await readChatActivity("", operation, {}, new AbortController().signal, response(200, { status: outcome }));
    assert.equal(settleChatActivityMessages([pending], { [operation.key]: result }, noLiveRequests)[0].status, expected);
  }
});

test("the caller can abort progress requests", async () => {
  const controller = new AbortController();
  let requestSignal: AbortSignal | null | undefined;
  await readChatActivity("", operation, {}, controller.signal, async (_url, init) => {
    requestSignal = init?.signal;
    controller.abort();
    throw new DOMException("Aborted", "AbortError");
  });
  assert.equal(requestSignal, controller.signal);
  assert.equal(controller.signal.aborted, true);
});
