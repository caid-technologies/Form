"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import PublicAppShell from "../../../components/public-app-shell";
import { useFormaAuth } from "../../../lib/forma-auth";
import { webConfig } from "../../../lib/config/environment";

export default function CliAuthorizePage() {
  const searchParams = useSearchParams();
  const code = searchParams.get("code") || "";
  const auth = useFormaAuth();
  const [state, setState] = useState<"idle" | "approving" | "approved" | "error">("idle");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!code || !auth.isLoaded || (auth.authRequired && !auth.isSignedIn)) return;
    let cancelled = false;
    void (async () => {
      setState("approving");
      const token = await auth.getToken();
      const response = await fetch(`${webConfig.apiBaseUrl}/cli/device/approve`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ user_code: code }),
      });
      if (cancelled) return;
      if (!response.ok) {
        setState("error");
        setMessage("This CLI authorization code is invalid or expired.");
        return;
      }
      setState("approved");
      setMessage("Return to your terminal. Forma OSS has approved the CLI session.");
    })().catch(() => {
      if (!cancelled) {
        setState("error");
        setMessage("Forma could not approve this CLI session. Try again from the terminal.");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [auth, code]);

  return (
    <PublicAppShell badge="Forma OSS CLI" title="Authorize this terminal">
      <div className="mx-auto max-w-xl space-y-5 py-8 text-center">
        {!code && <p>Open this page from the URL printed by `forma-oss login`.</p>}
        {code && auth.authRequired && !auth.isSignedIn && (
          <>
            <p>Sign in to approve the CLI session for your Forma account.</p>
            <button
              className="rounded-md bg-slate-100 px-4 py-2 font-medium text-slate-950"
              onClick={() => auth.openSignIn({ redirectUrl: window.location.href })}
              type="button"
            >
              Sign in to continue
            </button>
          </>
        )}
        {state === "approving" && <p>Approving CLI session...</p>}
        {state === "approved" && <p>{message}</p>}
        {state === "error" && <p>{message}</p>}
      </div>
    </PublicAppShell>
  );
}
