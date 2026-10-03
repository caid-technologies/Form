import assert from "node:assert/strict";
import { test } from "node:test";
import {
  clampChatFraction, completedProjectReference, initialChatProjectLayout,
  linkedProjectPath, projectPaneVisible,
} from "../lib/chat-project-layout.ts";

test("desktop defaults to split and narrow screens default to chat", () => {
  const state = initialChatProjectLayout();
  assert.equal(projectPaneVisible(state, true, true), true);
  assert.equal(projectPaneVisible(state, true, false), false);
});
test("no viewer exists without a project, even in full screen", () => {
  const state = { ...initialChatProjectLayout(), fullScreen: true };
  assert.equal(projectPaneVisible(state, false, true), false);
  assert.equal(projectPaneVisible(state, false, false), false);
});
test("closed panes are not visible; mobile selection does not open a closed desktop pane", () => {
  const state = { ...initialChatProjectLayout(), desktopOpen: false, narrowPane: "project" as const };
  assert.equal(projectPaneVisible(state, true, true), false);
  assert.equal(projectPaneVisible(state, true, false), true);
});
test("full screen keeps the one selected viewer visible across breakpoints", () => {
  const state = { ...initialChatProjectLayout(), desktopOpen: false, fullScreen: true };
  assert.equal(projectPaneVisible(state, true, true), true);
  assert.equal(projectPaneVisible(state, true, false), true);
});
test("split widths are finite and bounded", () => {
  assert.equal(clampChatFraction(-1), 0.32);
  assert.equal(clampChatFraction(2), 0.60);
  assert.equal(clampChatFraction(0.5), 0.5);
  assert.equal(clampChatFraction(NaN), 0.42);
  assert.equal(clampChatFraction(Infinity), 0.42);
});
test("only completed assistant project references produce cards", () => {
  for (const status of ["loading", "error", "cancelled", "handed-off"]) {
    assert.equal(completedProjectReference({ role: "assistant", status, projectId: "p1" }), null);
  }
  assert.equal(completedProjectReference({ role: "user", status: "success", projectId: "p1" }), null);
  assert.equal(completedProjectReference({ role: "system", status: "success", projectId: "p1" }), null);
  assert.equal(completedProjectReference({ role: "assistant", status: "success", projectId: "  " }), null);
  assert.equal(completedProjectReference({ role: "assistant", status: "success", projectId: " p1 " }), "p1");
  assert.equal(completedProjectReference({ role: "assistant", projectId: "p1" }), "p1");
});
test("legacy project IDs are encoded, not treated as paths or executable URLs", () => {
  assert.equal(linkedProjectPath("a/b?c#d"), "/project/a%2Fb%3Fc%23d");
  assert.equal(linkedProjectPath("javascript:alert(1)"), "/project/javascript%3Aalert(1)");
});
test("new conversations receive independent primitive-only state", () => {
  const first = initialChatProjectLayout();
  first.fullScreen = true;
  first.chatFraction = 0.6;
  assert.equal(initialChatProjectLayout().fullScreen, false);
  assert.equal(initialChatProjectLayout().chatFraction, 0.42);
  assert.ok(Object.values(first).every((value) => typeof value !== "object"));
});
