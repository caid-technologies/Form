"use client";

import { LoaderCircle, RefreshCw } from "lucide-react";

export type ChatAccessLoadState = "loading" | "ready" | "error";

export default function ChatAccessStatus({
  status,
  onRetry,
}: {
  status: Exclude<ChatAccessLoadState, "ready">;
  onRetry: () => void;
}) {
  return (
    <div className="flex h-full min-h-48 flex-1 items-center justify-center p-6">
      <div role={status === "loading" ? "status" : "alert"} className="flex flex-col items-center gap-3 text-center text-sm text-zinc-400">
        {status === "loading" ? (
          <>
            <LoaderCircle className="h-5 w-5 animate-spin text-cyan-300" aria-hidden="true" />
            <p>Loading chat…</p>
          </>
        ) : (
          <>
            <p>Chat could not be loaded. Please try again.</p>
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex items-center gap-2 rounded-lg bg-cyan-300 px-3 py-2 text-xs font-semibold text-slate-950 hover:bg-cyan-200"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Retry loading chat
            </button>
          </>
        )}
      </div>
    </div>
  );
}
