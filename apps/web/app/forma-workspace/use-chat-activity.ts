"use client";

import { useEffect, useRef, useState } from "react";
import {
  readChatActivity,
  type ChatActivityObservations,
  type ChatOperation,
} from "../../lib/chat-activity";

/** Bounded, account-scoped status reads for every pending sidebar operation. */
export function useChatActivity({
  apiUrl, scope, enabled, operations, getHeaders,
}: {
  apiUrl: string;
  scope: string;
  enabled: boolean;
  operations: ChatOperation[];
  getHeaders: () => Promise<Record<string, string>>;
}): ChatActivityObservations {
  const headersRef = useRef(getHeaders);
  headersRef.current = getHeaders;
  const [snapshot, setSnapshot] = useState<{ scope: string; values: ChatActivityObservations }>({ scope, values: {} });
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;
  const key = JSON.stringify(operations);

  useEffect(() => {
    const pending: ChatOperation[] = JSON.parse(key);
    const keys = new Set(pending.map((operation) => operation.key));
    const values = Object.fromEntries(Object.entries(snapshotRef.current.scope === scope ? snapshotRef.current.values : {})
      .filter(([operationKey]) => keys.has(operationKey)));
    setSnapshot({ scope, values: { ...values } });
    if (!enabled || !pending.length) return;
    const controller = new AbortController();
    const retryAfter = new Map<string, number>();
    let polling = false;
    let offset = 0;
    const poll = async () => {
      if (polling || controller.signal.aborted || document.visibilityState !== "visible") return;
      const eligible = pending.filter((operation) => values[operation.key]?.state !== "settled"
        && (retryAfter.get(operation.key) || 0) <= Date.now());
      if (!eligible.length) return;
      const batch = Array.from({ length: Math.min(3, eligible.length) }, (_, index) => eligible[(offset + index) % eligible.length]);
      offset = (offset + batch.length) % eligible.length;
      polling = true;
      try {
        const headers = await headersRef.current();
        await Promise.all(batch.map(async (operation) => {
          const requestController = new AbortController();
          const abort = () => requestController.abort();
          controller.signal.addEventListener("abort", abort, { once: true });
          const timeout = window.setTimeout(abort, 10_000);
          try {
            if (controller.signal.aborted) return;
            const result = await readChatActivity(apiUrl, operation, headers, requestController.signal);
            if (controller.signal.aborted) return;
            values[operation.key] = result;
            retryAfter.set(operation.key, Date.now() + (result.state === "interrupted" ? 60_000 : result.state === "reconnecting" ? 15_000 : 5_000));
          } finally {
            window.clearTimeout(timeout);
            controller.signal.removeEventListener("abort", abort);
          }
        }));
      } catch {
        if (!controller.signal.aborted) batch.forEach((operation) => {
          values[operation.key] = { state: "reconnecting", label: "Could not authenticate the progress check. Checking again.", observedAt: Date.now() };
          retryAfter.set(operation.key, Date.now() + 15_000);
        });
      } finally {
        polling = false;
        if (!controller.signal.aborted) setSnapshot({ scope, values: { ...values } });
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5_000);
    const visible = () => { void poll(); };
    document.addEventListener("visibilitychange", visible);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [apiUrl, enabled, key, scope]);

  return snapshot.scope === scope ? snapshot.values : {};
}
