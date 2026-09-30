import assert from "node:assert/strict";
import { test } from "node:test";
import { openCodeDesignNotice, reduceOpenCodeTurn, type OpenCodeEvent, type OpenCodeTurnState } from "../lib/opencode.ts";

const initial: OpenCodeTurnState = {
  content: "Waiting for Forma Agent.",
  assistantMessage: null,
  status: "loading",
  terminalEvent: null,
};

test("saved draft and partial outcomes preserve agent text without claiming design completion", () => {
  for (const readiness of ["draft", "partial"] as const) {
    const answer = reduceOpenCodeTurn(initial, event("assistant_message", { message: "Saved your requirements." }), "command");
    const result = reduceOpenCodeTurn(answer, event("completed", { design_outcome: { project_readiness: readiness } }), "command");
    assert.equal(result.status, "success"); // execution success, not design readiness
    assert.ok(result.content.startsWith("Saved your requirements."));
    assert.ok(result.content.includes(openCodeDesignNotice(readiness)!));
  }
  assert.equal(openCodeDesignNotice("complete"), null);
  assert.match(openCodeDesignNotice(undefined)!, /not been verified/);
});

function event(kind: OpenCodeEvent["kind"], overrides: Partial<OpenCodeEvent> = {}): OpenCodeEvent {
  return {
    kind, event_id: "command:terminal", sequence: 1, session_id: "session",
    project_id: "reserved-project", status: null, message: null, revision_id: null,
    error: null, created_at: "2026-09-12T00:00:00Z", ...overrides,
  };
}

test("connector unavailability is recoverable and never completes the turn", () => {
  const waiting = reduceOpenCodeTurn(initial, event("connector_unavailable"), "command");
  assert.equal(waiting.status, "loading");
  assert.equal(waiting.terminalEvent, null);
  assert.match(waiting.content, /reconnect/);
  const working = reduceOpenCodeTurn(waiting, event("working"), "command");
  assert.match(working.content, /working/);
  assert.equal(working.terminalEvent, null);
});

test("assistant text survives progress, reconnect, and completion in the same event page", () => {
  const events = [
    event("assistant_message", { event_id: "answer", message: "Hello!" }),
    event("progress"), event("connector_unavailable"), event("completed"),
  ];
  const result = events.reduce((state, next) => reduceOpenCodeTurn(state, next, "command"), initial);
  assert.equal(result.content, "Hello!");
  assert.equal(result.status, "success");
  assert.equal(result.terminalEvent?.kind, "completed");
  assert.equal("projectId" in result, false);
});

test("completion without an answer does not claim a project was created", () => {
  const result = reduceOpenCodeTurn(initial, event("completed"), "command");
  assert.equal(result.content, "Forma Agent finished responding.");
  assert.equal("projectId" in result, false);
});

test("late terminal events from a prior command do not stop the next turn", () => {
  assert.equal(reduceOpenCodeTurn(initial, event("completed", { event_id: "prior:terminal" }), "command"), initial);
});

test("failure and cancellation finish without attaching the reserved project", () => {
  for (const kind of ["failed", "cancelled"] as const) {
    const result = reduceOpenCodeTurn(initial, event(kind), "command");
    assert.equal(result.status, kind === "failed" ? "error" : "cancelled");
    assert.equal(result.terminalEvent?.kind, kind);
    assert.equal("projectId" in result, false);
  }
  const cancelled = reduceOpenCodeTurn(initial, event("cancelled", { event_id: "cancelled_session" }), "command");
  assert.equal(cancelled.status, "cancelled");
});

test("historical availability events after completion do not erase success", () => {
  const completed = reduceOpenCodeTurn(initial, event("completed"), "command");
  assert.equal(reduceOpenCodeTurn(completed, event("connector_unavailable"), "command"), completed);
});

test("commands inherit the runtime model and ignore legacy browser selections", async () => {
  const { normalizeOpenCodeModel, submitOpenCodeCommand } = await import("../lib/opencode.ts");
  assert.equal(normalizeOpenCodeModel("  "), null);
  assert.equal(normalizeOpenCodeModel(" google/gemini-2.5-flash "), "google/gemini-2.5-flash");
  assert.equal(normalizeOpenCodeModel("openrouter/anthropic/claude-sonnet-4"), "openrouter/anthropic/claude-sonnet-4");
  assert.throws(() => normalizeOpenCodeModel("bare-model"), /provider\/model/);
  const previousFetch = globalThis.fetch;
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  let selection = "google/gemini-2.5-flash";
  const requests: { url: string; body: Record<string, unknown> }[] = [];
  Object.defineProperty(globalThis, "window", { configurable: true, value: { localStorage: { getItem: () => selection } } });
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(String(init?.body));
    requests.push({ url: String(url), body });
    return Response.json({ command_id: "command", session_id: "same-chat", project_id: "project", operation: "project_message", status: "queued", model: body.model ?? null });
  };
  try {
    for (const saved of ["google/gemini-2.5-flash", "invalid-model", ""]) {
      selection = saved;
      assert.equal((await submitOpenCodeCommand("https://forma.example", {}, "same-chat", "Next")).model, null);
    }
    Object.defineProperty(globalThis, "window", { configurable: true, value: {
      get localStorage() { throw new Error("Browser storage unavailable"); },
    } });
    await submitOpenCodeCommand("https://forma.example", {}, "same-chat", "Next");
    assert.equal(requests.length, 4);
    for (const request of requests) {
      assert.ok(request.url.endsWith("/sessions/same-chat/commands"));
      assert.deepEqual(Object.keys(request.body).sort(), ["idempotency_key", "message"]);
      assert.equal(request.body.message, "Next");
      assert.match(String(request.body.idempotency_key), /^web-.+/);
    }
  } finally {
    globalThis.fetch = previousFetch;
    if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("connector timeout finishes the matching turn and exposes its safe diagnostic code", () => {
  const waiting = reduceOpenCodeTurn(initial, event("connector_unavailable"), "command");
  const expired = reduceOpenCodeTurn(waiting, event("failed", {
    error: { code: "connector_timeout", message: "Forma Agent did not reconnect within five minutes. Retry this request when it is available.", correlation_id: "event_command:terminal" },
    status: "failed",
  }), "command");
  assert.equal(expired.status, "error");
  assert.match(expired.content, /Retry this request/);
  assert.match(expired.content, /Code: connector_timeout/);
  assert.equal(reduceOpenCodeTurn(expired, event("completed"), "command"), expired);
  assert.equal(reduceOpenCodeTurn(expired, event("working"), "command"), expired);
});
