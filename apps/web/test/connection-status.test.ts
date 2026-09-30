import assert from "node:assert/strict";
import { test } from "node:test";

import {
  connectionStatusPresentation,
  isAuthOrSecurityHttpStatus,
  workspaceStatusBadge,
} from "../lib/connection-status";

test("connected status uses a green dot and accessible label", () => {
  const presentation = connectionStatusPresentation("connected");

  assert.equal(presentation.label, "Connected");
  assert.match(presentation.dotClassName, /emerald/);
});

test("disconnected status uses an orange warning dot and accessible label", () => {
  const presentation = connectionStatusPresentation("disconnected");

  assert.equal(presentation.label, "Disconnected");
  assert.match(presentation.dotClassName, /orange/);
});

test("healthy connection, auth, and agent operations resolve to an illuminated ready badge", () => {
  const badge = workspaceStatusBadge({
    connection: "connected",
    agent: { status: "success", content: "Project is ready." },
  });

  assert.equal(badge.tone, "ok");
  assert.equal(badge.reason, "stable");
  assert.match(badge.label, /Ready/);
  assert.equal(badge.pulse, false);
});

test("a live agent run stays green while it is still healthy", () => {
  const badge = workspaceStatusBadge({
    connection: "connected",
    agent: {
      status: "loading",
      startedAt: "2026-08-18T09:00:00.000Z",
      lastEventAt: "2026-08-18T09:00:10.000Z",
    },
    nowMs: Date.parse("2026-08-18T09:00:20.000Z"),
  });

  assert.equal(badge.tone, "ok");
  assert.equal(badge.reason, "stable");
  assert.equal(badge.pulse, true);
});

test("run failures switch the badge to illuminated red", () => {
  const badge = workspaceStatusBadge({
    connection: "connected",
    agent: { status: "error", content: "The design build stopped after an agent failure." },
  });

  assert.equal(badge.tone, "error");
  assert.equal(badge.reason, "run-failure");
  assert.match(badge.label, /Run failed/);
  assert.equal(badge.pulse, true);
});

test("execution timeouts switch the badge to illuminated red", () => {
  const fromMessage = workspaceStatusBadge({
    connection: "connected",
    agent: { status: "error", content: "Generation failed: the provider timed out." },
  });
  const fromSilence = workspaceStatusBadge({
    connection: "connected",
    agent: {
      status: "loading",
      startedAt: "2026-08-18T09:00:00.000Z",
      lastEventAt: "2026-08-18T09:00:05.000Z",
    },
    nowMs: Date.parse("2026-08-18T09:00:40.000Z"),
  });

  assert.equal(fromMessage.reason, "timeout");
  assert.equal(fromSilence.reason, "timeout");
  assert.equal(fromMessage.tone, "error");
  assert.equal(fromSilence.tone, "error");
});

test("authentication and security errors switch the badge to illuminated red", () => {
  assert.equal(isAuthOrSecurityHttpStatus(401), true);
  assert.equal(isAuthOrSecurityHttpStatus(403), true);
  assert.equal(isAuthOrSecurityHttpStatus(500), false);

  const fromFlag = workspaceStatusBadge({
    connection: "connected",
    authError: true,
  });
  const fromMessage = workspaceStatusBadge({
    connection: "connected",
    agent: { status: "error", content: "Server returned 401" },
  });

  assert.equal(fromFlag.reason, "auth");
  assert.equal(fromMessage.reason, "auth");
  assert.equal(fromFlag.tone, "error");
});

test("a dropped API connection is not treated as healthy", () => {
  const badge = workspaceStatusBadge({ connection: "disconnected" });

  assert.equal(badge.tone, "error");
  assert.equal(badge.reason, "disconnected");
});

test("an active FormaAgent authoring session is distinct from a stable ready badge", () => {
  const badge = workspaceStatusBadge({
    connection: "connected",
    agent: { status: "success", content: "Project is ready." },
    authoring: true,
  });

  assert.equal(badge.tone, "authoring");
  assert.equal(badge.reason, "authoring");
  assert.match(badge.label, /Forma Agent/);
  assert.equal(badge.pulse, true);
});

test("a delivered OpenCode result is distinct from authoring and ready", () => {
  const badge = workspaceStatusBadge({
    connection: "connected",
    authoring: true,
    delivered: true,
  });

  assert.equal(badge.tone, "delivered");
  assert.equal(badge.reason, "delivered");
  assert.match(badge.label, /Delivered/);
  assert.equal(badge.pulse, false);
});

test("delivery is only claimed while authoring is active", () => {
  const badge = workspaceStatusBadge({
    connection: "connected",
    delivered: true,
    authoring: false,
  });

  assert.equal(badge.reason, "stable");
  assert.equal(badge.tone, "ok");
});

test("connection and auth failures still outrank an authoring session", () => {
  const disconnected = workspaceStatusBadge({
    connection: "disconnected",
    authoring: true,
  });
  const authFailed = workspaceStatusBadge({
    connection: "connected",
    authError: true,
    authoring: true,
  });

  assert.equal(disconnected.tone, "error");
  assert.equal(disconnected.reason, "disconnected");
  assert.equal(authFailed.tone, "error");
  assert.equal(authFailed.reason, "auth");
});
