import assert from "node:assert/strict";
import { test } from "node:test";
import { listOpenCodeEvents, OpenCodePollingError, parseOpenCodePendingTurn } from "../lib/opencode.ts";

test("polling bounds hung auth, fetch and body reads and aborts the transport", async () => {
  const original = globalThis.fetch;
  try {
    let signal: AbortSignal | null | undefined;
    globalThis.fetch = async (_url, init) => { signal = init?.signal; return new Promise(() => {}); };
    await assert.rejects(listOpenCodeEvents("", {}, "session", 0, { timeoutMs: 10 }), (error) => error instanceof OpenCodePollingError && error.code === "timeout");
    assert.equal(signal?.aborted, true);
    let fetches = 0;
    let resolveHeaders!: (value: Record<string, string>) => void;
    globalThis.fetch = async () => { fetches++; return Response.json({ events: [], next_cursor: 0 }); };
    await assert.rejects(listOpenCodeEvents("", () => new Promise((resolve) => { resolveHeaders = resolve; }), "session", 0, { timeoutMs: 10 }), /could not be confirmed/);
    resolveHeaders({});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(fetches, 0, "late auth completion must not send a request");
    globalThis.fetch = async () => new Response(new ReadableStream({ start() {} }));
    await assert.rejects(listOpenCodeEvents("", {}, "session", 0, { timeoutMs: 10 }), (error) => error instanceof OpenCodePollingError && error.code === "timeout");
  } finally { globalThis.fetch = original; }
});

test("poll cancellation settles immediately even when fetch ignores its signal", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Promise(() => {});
    const controller = new AbortController();
    const pending = listOpenCodeEvents("", {}, "session", 0, { signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, { name: "AbortError" });
  } finally { globalThis.fetch = original; }
});

test("HTTP, malformed pages and regressed cursors are safe errors; recovery uses the same cursor", async () => {
  const original = globalThis.fetch;
  try {
    for (const [response, code] of [
      [new Response("secret upstream body", { status: 503 }), "http_error"],
      [new Response("not JSON"), "invalid_response"],
      [Response.json({ events: [], next_cursor: 0 }), "invalid_response"],
    ] as const) {
      globalThis.fetch = async () => response;
      await assert.rejects(listOpenCodeEvents("", {}, "session", 4), (error) => error instanceof OpenCodePollingError && error.code === code && !error.message.includes("secret"));
    }
    globalThis.fetch = async (url, init) => {
      assert.match(String(url), /session\/events\?cursor=4&limit=100$/);
      assert.notEqual(init?.method, "POST");
      return Response.json({ events: [], next_cursor: 4 });
    };
    assert.deepEqual(await listOpenCodeEvents("", {}, "session", 4), { events: [], next_cursor: 4 });
  } finally { globalThis.fetch = original; }
});

test("saved recovery references retain the command, partial reply, and cursor", () => {
  const pending = { sessionId: "session", commandId: "command", projectId: "project", message: "request", cursor: 4, assistantMessage: "partial answer" };
  assert.deepEqual(parseOpenCodePendingTurn(JSON.parse(JSON.stringify(pending))), pending);
  assert.equal(parseOpenCodePendingTurn({ ...pending, cursor: -1 }), null);
  assert.equal(parseOpenCodePendingTurn({ ...pending, commandId: null }), null);
});
