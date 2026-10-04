import assert from "node:assert/strict";
import { test } from "node:test";
import { sharedProjectUrl } from "../lib/project-share.ts";

test("share links open a project revision and keep the capability out of the request URL", () => {
  const url = new URL(sharedProjectUrl("https://forma.test", "project-1", "revision-1", "secret-token"));
  assert.equal(url.pathname, "/project/project-1/shared");
  assert.equal(url.searchParams.get("revision"), "revision-1");
  assert.equal(new URLSearchParams(url.hash.slice(1)).get("share"), "secret-token");
  assert.equal(`${url.pathname}${url.search}`.includes("secret-token"), false);
});

test("sharing different versions produces distinct links without carrying private chat state", () => {
  const first = sharedProjectUrl("https://forma.test/chat/private?debug=1", "project-1", "v1", "token-1");
  const second = sharedProjectUrl("https://forma.test", "project-1", "v2", "token-2");
  assert.notEqual(first, second);
  assert.equal(first.includes("private"), false);
  assert.equal(first.includes("debug"), false);
  assert.equal(new URL(first).searchParams.get("revision"), "v1");
});
