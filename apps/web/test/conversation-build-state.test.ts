import assert from "node:assert/strict";
import { test } from "node:test";

import {
  contextBuildControls,
  latestRetryableContextBuildMessage,
  shouldOfferFailedBuildRetry,
} from "../lib/conversation-build-state.ts";

const failedBuild = {
  id: "failed-build",
  status: "error" as const,
  contextProjectId: "project-1",
  buildPlanId: "plan-1",
  buildJobId: "job-1",
};

test("failed build recovery does not depend on whether a project artifact is loaded", () => {
  assert.equal(contextBuildControls(failedBuild).canReset, true);
  assert.equal(contextBuildControls({ ...failedBuild, projectId: "project-1" }).canReset, true);
});

test("active work blocks reset until the terminal plan state settles", () => {
  assert.equal(contextBuildControls(failedBuild, true).canReset, false);
  assert.equal(contextBuildControls({ ...failedBuild, status: "loading" }).canStop, true);
});

test("latest retryable build selection is shared across conversation layouts", () => {
  const messages = [
    failedBuild,
    { ...failedBuild, id: "later-success", status: "success" as const },
  ];
  assert.equal(latestRetryableContextBuildMessage(messages), null);
  assert.equal(latestRetryableContextBuildMessage([messages[1], failedBuild])?.id, "failed-build");
});

test("an empty idle composer offers retry while typed input preserves normal submission", () => {
  assert.equal(shouldOfferFailedBuildRetry({
    canRetryFailedBuild: true,
    hasInput: false,
    generationActive: false,
  }), true);
  assert.equal(shouldOfferFailedBuildRetry({
    canRetryFailedBuild: true,
    hasInput: true,
    generationActive: false,
  }), false);
});
