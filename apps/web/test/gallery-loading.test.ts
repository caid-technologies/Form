import assert from "node:assert/strict";
import { test } from "node:test";
import { GalleryImageRequests, GalleryPageCache, galleryPageKey } from "../lib/gallery-loading.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

test("page snapshots expire, remain bounded, and cannot cross identities or searches", () => {
  const cache = new GalleryPageCache<string>(2, 100);
  const first = galleryPageKey("alice", 6, 0, " fan ");
  assert.equal(first, galleryPageKey("alice", 6, 0, "fan"));
  cache.set(first, "page one", 0);
  assert.equal(cache.get(first, 99), "page one");
  assert.equal(cache.get(first, 100), undefined);
  for (const key of [galleryPageKey("bob", 6, 0, "fan"), galleryPageKey("alice", 6, 1, "fan"), galleryPageKey("alice", 6, 0, "motor"), galleryPageKey("alice", 12, 0, "fan")]) {
    assert.equal(cache.get(key, 10), undefined);
  }
  cache.set("two", "page two", 0);
  cache.set("three", "page three", 0);
  assert.equal(cache.get(first, 1), undefined);
  assert.equal(cache.get("two", 1), "page two");
  cache.clear();
  assert.equal(cache.get("three", 1), undefined);
});

test("a fast thumbnail renders before its slow sibling; rerenders do not restart pending work", async () => {
  const requests = new GalleryImageRequests<string>();
  const fast = deferred<string>();
  const slow = deferred<string>();
  const calls: string[] = [];
  const results: string[] = [];
  const signals = new Map<string, AbortSignal>();
  const load = (id: string, signal: AbortSignal) => {
    calls.push(id); signals.set(id, signal);
    return id === "fast" ? fast.promise : slow.promise;
  };
  const result = (id: string, value: string) => results.push(`${id}:${value}`);
  const error = (_id: string, err: unknown) => assert.fail(String(err));
  requests.sync("alice", ["fast", "slow", "slow"], load, result, error);
  requests.sync("alice", ["fast", "slow"], load, result, error);
  assert.deepEqual(calls, ["fast", "slow"]);
  fast.resolve("ready");
  await flush();
  assert.deepEqual(results, ["fast:ready"]);
  requests.sync("alice", ["slow"], load, result, error);
  assert.equal(signals.get("slow")?.aborted, false);
  assert.deepEqual(calls, ["fast", "slow"]);
  slow.resolve("later");
  await flush();
  assert.deepEqual(results, ["fast:ready", "slow:later"]);
});

test("page changes cancel only removed thumbnails and discard their late responses", async () => {
  const requests = new GalleryImageRequests<string>();
  const pending = new Map<string, ReturnType<typeof deferred<string>>>();
  const signals = new Map<string, AbortSignal>();
  const results: string[] = [];
  const load = (id: string, signal: AbortSignal) => {
    const item = deferred<string>(); pending.set(id, item); signals.set(id, signal);
    return item.promise;
  };
  const result = (id: string) => results.push(id);
  const error = (_id: string, err: unknown) => assert.fail(String(err));
  requests.sync("alice", ["old", "retained"], load, result, error);
  requests.sync("alice", ["retained", "new"], load, result, error);
  assert.equal(signals.get("old")?.aborted, true);
  assert.equal(signals.get("retained")?.aborted, false);
  pending.get("old")!.resolve("stale");
  pending.get("retained")!.resolve("ok");
  pending.get("new")!.resolve("ok");
  await flush();
  assert.deepEqual(results, ["retained", "new"]);
});

test("identity changes and unmount cancel work, including requests whose loaders ignore abort", async () => {
  const requests = new GalleryImageRequests<string>();
  const old = deferred<string>();
  const next = deferred<string>();
  const results: string[] = [];
  const errors: unknown[] = [];
  let oldSignal: AbortSignal | undefined;
  let nextSignal: AbortSignal | undefined;
  requests.sync("alice", ["same-id"], (_id, signal) => { oldSignal = signal; return old.promise; }, (_id, value) => results.push(value), (_id, error) => errors.push(error));
  requests.sync("bob", ["same-id"], (_id, signal) => { nextSignal = signal; return next.promise; }, (_id, value) => results.push(value), (_id, error) => errors.push(error));
  assert.equal(oldSignal?.aborted, true);
  assert.equal(nextSignal?.aborted, false);
  old.resolve("private old result");
  await flush();
  assert.deepEqual(results, []);
  requests.clear();
  assert.equal(nextSignal?.aborted, true);
  next.reject(new Error("cancelled transport"));
  await flush();
  assert.deepEqual(results, []);
  assert.deepEqual(errors, []);
});

test("a failed thumbnail does not block successful siblings or produce unhandled rejections", async () => {
  const requests = new GalleryImageRequests<string>();
  const results: string[] = [];
  const errors: string[] = [];
  requests.sync("public", ["bad", "good"], async (id) => {
    if (id === "bad") throw new Error("offline");
    return "ready";
  }, (id) => results.push(id), (id) => errors.push(id));
  await flush();
  assert.deepEqual(results, ["good"]);
  assert.deepEqual(errors, ["bad"]);
});
