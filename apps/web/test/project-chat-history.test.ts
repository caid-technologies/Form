import assert from "node:assert/strict";
import { test } from "node:test";
import { hasConversationHistory, loadProjectChatHistory } from "../lib/project-chat-history.ts";

const options = {
  apiUrl: "https://forma.test/api", chatId: "chat-1", projectId: "project-1",
  headers: { Authorization: "Bearer test-user" }, recoverOpenCode: true,
};
const realMessages = [
  { id: "cmd:user", role: "user", content: "Make a mechanical gear.", projectId: "project-1" },
  { id: "cmd:assistant", role: "assistant", content: "Saved the gear.", projectId: "project-1" },
];
const placeholder = [
  { id: "old-user", role: "user", content: "OpenCode project" },
  { id: "old-assistant", role: "assistant", projectId: "project-1", content: "Gear is the active project for this chat." },
];

test("synthetic project context and malformed records are not conversation history", () => {
  assert.equal(hasConversationHistory(placeholder), false);
  assert.equal(hasConversationHistory([null, 1, {}, { content: 42 }, { id: "assistant-welcome", role: "assistant", content: "Welcome" }]), false);
  assert.equal(hasConversationHistory([{ id: "project-context:project-1", role: "assistant", content: "Gear" }]), false);
  assert.equal(hasConversationHistory(realMessages), true);
});

test("loads the saved transcript without overwriting it or querying the archive", async (t) => {
  const calls: string[] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    calls.push(url);
    assert.equal(init.method, undefined);
    assert.deepEqual(init.headers, options.headers);
    assert.equal(init.cache, "no-store");
    return Response.json({ messages: realMessages });
  });
  assert.deepEqual(await loadProjectChatHistory(options), realMessages);
  assert.deepEqual(calls, ["https://forma.test/api/chats/chat-1"]);
});

for (const saved of [null, [], placeholder]) {
  test(`recovers recorded agent turns when the saved transcript is ${saved === null ? "missing" : saved.length ? "synthetic" : "empty"}`, async (t) => {
    const calls: string[] = [];
    t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
      calls.push(url);
      assert.equal(init.method, undefined);
      assert.deepEqual(init.headers, options.headers);
      return calls.length === 1
        ? saved === null ? new Response(null, { status: 404 }) : Response.json({ messages: saved })
        : Response.json({ messages: realMessages });
    });
    assert.deepEqual(await loadProjectChatHistory(options), realMessages);
    assert.deepEqual(calls, ["https://forma.test/api/chats/chat-1", "https://forma.test/api/opencode/projects/project-1/history"]);
  });
}

test("a denied or unavailable transcript is not treated as an empty conversation", async (t) => {
  for (const status of [401, 403, 503]) {
    const mock = t.mock.method(globalThis, "fetch", async () => new Response(null, { status }));
    await assert.rejects(loadProjectChatHistory(options), /Saved chat history could not be loaded/);
    assert.equal(mock.mock.callCount(), 1);
    mock.mock.restore();
  }
});

test("legacy chat does not query OpenCode and cancellation propagates to both reads", async (t) => {
  const controller = new AbortController();
  const mock = t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    assert.equal(init.signal, controller.signal);
    return new Response(null, { status: 404 });
  });
  assert.deepEqual(await loadProjectChatHistory({ ...options, recoverOpenCode: false, signal: controller.signal }), []);
  assert.equal(mock.mock.callCount(), 1);
  await assert.rejects(loadProjectChatHistory({ ...options, signal: controller.signal }), /Recorded Forma Agent history/);
  assert.equal(mock.mock.callCount(), 3);
});
