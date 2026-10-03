"use client";

import Link from "next/link";
import { ArrowRight, Eye, Terminal } from "lucide-react";

export const HOSTED_CHAT_MAINTENANCE_MESSAGE = "Forma hosted chat is temporarily under maintenance.";

export const AUTHORING_MODE_ACTIVE_MESSAGE = "Forma Agent is authoring this workspace.";

export const AUTHORING_MODE_HANDOFF_MESSAGE =
  "Routed to Forma Agent — this change is being authored by the runtime assigned to this workspace and will appear here when delivered.";

const LOCAL_WORKFLOW_COMMANDS = `forma-oss init <local-project-directory>
forma-oss build "<your hardware prompt>" --path <local-project-directory>

# Only cloud operations require authentication
forma-oss login
forma-oss projects push --path <local-project-directory>`;

export default function HostedChatMaintenance({ compact = false }: { compact?: boolean }) {
  return (
    <section
      role="status"
      aria-label="Hosted chat maintenance"
      className={`rounded-xl border border-cyan-400/20 bg-[linear-gradient(135deg,rgba(34,211,238,0.08),rgba(16,185,129,0.05)),#181b22] text-left ${
        compact ? "p-4" : "p-5 sm:p-6"
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 rounded-lg border border-cyan-300/20 bg-cyan-300/10 p-2 text-cyan-200">
          <Eye className="h-4 w-4" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-zinc-100">{HOSTED_CHAT_MAINTENANCE_MESSAGE}</h2>
          <p className="mt-1.5 text-xs leading-5 text-zinc-400">
            You can still view existing chats. Create and validate your project locally with Forma-OSS,
            then upload it to Forma Cloud when you want to view or share it online.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Link
              href="/install/opencode"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-cyan-300 px-3 text-xs font-semibold text-slate-950 transition-colors hover:bg-cyan-200"
            >
              Open CLI quick start
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
            <span className="text-[11px] text-zinc-500">Login is only needed for cloud upload.</span>
          </div>
        </div>
      </div>
      <div className="mt-4 overflow-hidden rounded-lg border border-white/10 bg-[#0f1117]">
        <div className="flex items-center gap-2 border-b border-white/5 px-3 py-2 text-[10px] font-medium uppercase tracking-[0.14em] text-zinc-500">
          <Terminal className="h-3.5 w-3.5 text-cyan-300" aria-hidden="true" />
          Local-first workflow
        </div>
        <pre className="overflow-x-auto whitespace-pre-wrap break-words px-3 py-3 font-mono text-[11px] leading-5 text-cyan-100">
          <code>{LOCAL_WORKFLOW_COMMANDS}</code>
        </pre>
      </div>
    </section>
  );
}

export function AuthoringModeBanner({ compact = false }: { compact?: boolean }) {
  return (
    <section
      role="status"
      aria-label={AUTHORING_MODE_ACTIVE_MESSAGE}
      className={`rounded-xl border border-violet-400/20 bg-[linear-gradient(135deg,rgba(167,139,250,0.09),rgba(34,211,238,0.06)),#181b22] text-left ${
        compact ? "p-4" : "p-5 sm:p-6"
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 rounded-lg border border-violet-300/20 bg-violet-300/10 p-2 text-violet-200">
          <Terminal className="h-4 w-4" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-zinc-100">{AUTHORING_MODE_ACTIVE_MESSAGE}</h2>
          <p className="mt-1.5 text-xs leading-5 text-zinc-400">
            Change requests are routed to your assigned Forma Agent. The updated project will appear here after
            delivery.
          </p>
        </div>
      </div>
    </section>
  );
}
