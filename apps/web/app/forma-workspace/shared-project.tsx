"use client";

import { useCallback, useEffect, useState } from "react";
import { webConfig } from "../../lib/config";
import { parseRevisionSnapshot, revisionId, type ProjectRevisionSnapshot } from "../../lib/project-history";
import { SnapshotContent } from "./project-revision-preview";

const base = webConfig.apiBaseUrl.trim().replace(/\/+$/, "");
const apiUrl = !base ? "/api" : base.endsWith("/api") ? base : `${base}/api`;

/** Fetch just one capability-authorized snapshot, with no chat or latest fallback. */
export default function SharedProject({ projectId }: { projectId: string }) {
  const [snapshot, setSnapshot] = useState<ProjectRevisionSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const getHeaders = useCallback(async (): Promise<Record<string, string>> => ({
    "X-Project-Share": new URLSearchParams(window.location.hash.slice(1)).get("share") || "",
  }), []);

  useEffect(() => {
    // Different links for the same revision change only the fragment. Browsers
    // keep the document mounted, so reauthorize instead of keeping a stale view.
    const reloadLink = () => setAttempt((value) => value + 1);
    window.addEventListener("hashchange", reloadLink);
    window.addEventListener("popstate", reloadLink);
    return () => {
      window.removeEventListener("hashchange", reloadLink);
      window.removeEventListener("popstate", reloadLink);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setSnapshot(null);
    setError(null);
    void (async () => {
      try {
        const id = revisionId(new URLSearchParams(window.location.search).get("revision"));
        const headers = await getHeaders();
        if (!id || !headers["X-Project-Share"]) throw new Error("This shared version link is incomplete or invalid.");
        const response = await fetch(`${apiUrl}/projects/${encodeURIComponent(projectId)}/shared/${id}`, {
          headers, signal: controller.signal, cache: "no-store", credentials: "omit",
        });
        if (!response.ok) throw new Error("This shared version is unavailable. Ask the owner for a new link.");
        const next = parseRevisionSnapshot(await response.json(), projectId, id);
        if (!controller.signal.aborted) setSnapshot(next);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load this shared version.");
      }
    })();
    return () => controller.abort();
  }, [projectId, getHeaders, attempt]);

  return <main className="mx-auto min-h-screen max-w-7xl p-4 text-[var(--forma-text-body)]">
    <header className="mb-4 border-b border-[var(--forma-border)] pb-4">
      <p className="text-sm">Forma · Shared project{snapshot ? ` · v${snapshot.revision}` : ""} · Read only</p>
      <h1 className="text-2xl font-semibold">{snapshot?.title || "Shared project"}</h1>
    </header>
    {error ? <div role="alert"><p>{error}</p><button type="button" onClick={() => setAttempt((value) => value + 1)}>Try again</button></div>
      : snapshot ? <SnapshotContent key={snapshot.revision_id} snapshot={snapshot} apiUrl={apiUrl} getHeaders={getHeaders} shared />
        : <p role="status">Loading shared version…</p>}
  </main>;
}
