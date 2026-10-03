import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  AlertTriangle,
  Check,
  ExternalLink,
  Monitor,
  ShieldCheck,
  Terminal,
} from "lucide-react";

import PublicAppShell, {
  PUBLIC_BUTTON_OUTLINE_CLASS,
  PUBLIC_BUTTON_PRIMARY_CLASS,
  PUBLIC_CARD_CLASS,
} from "../../../components/public-app-shell";
import CopyButton from "../../../components/copy-button";

export const metadata: Metadata = {
  title: "Install Forma for OpenCode | Forma",
  description: "Run Forma locally through OpenCode and upload projects only when you choose.",
};

const UNIX_INSTALL_COMMAND =
  "curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/caid-technologies/Forma-OSS/main/scripts/development/install-opencode.sh | bash";
const WINDOWS_INSTALL_COMMAND =
  "irm https://raw.githubusercontent.com/caid-technologies/Forma-OSS/main/scripts/development/install-opencode.ps1 | iex";
const UNIX_OPEN_COMMAND = "cd ~/forma-workspace\nopencode mcp list\nopencode";
const WINDOWS_OPEN_COMMAND = 'Set-Location "$HOME\\forma-workspace"\nopencode mcp list\nopencode';
const EXAMPLE_PROMPT =
  "Use the Forma hardware skill to build a real 3.3V plant-watering monitor with an ESP32, capacitive soil sensor, OLED status display, and a low-voltage pump driver. Ask only essential clarification questions. Author a complete Hardware Intermediate Representation, call the local Forma compiler, fix every CRITICAL validation finding, and save the final compiled project manifest in the local Forma workspace. Do not use simulation.";
const UNIX_UPLOAD_COMMAND =
  "forma-oss login\nforma-oss projects push --path ~/forma-workspace/<project-id>";
const WINDOWS_UPLOAD_COMMAND =
  'forma-oss login\nforma-oss projects push --path "$HOME\\forma-workspace\\<project-id>"';

function CommandBlock({
  label,
  value,
  description,
}: {
  label: string;
  value: string;
  description?: string;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-[#303640] bg-[#0c0f14]">
      <div className="flex items-center justify-between gap-3 border-b border-[#252a32] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Terminal className="h-3.5 w-3.5 shrink-0 text-cyan-300" aria-hidden="true" />
          <span className="truncate text-[11px] font-medium uppercase tracking-[0.14em] text-slate-400">{label}</span>
        </div>
        <CopyButton value={value} label={`Copy ${label}`} />
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-xs leading-6 text-cyan-100">
        <code>{value}</code>
      </pre>
      {description ? <p className="border-t border-[#252a32] px-4 py-2.5 text-xs leading-5 text-slate-500">{description}</p> : null}
    </div>
  );
}

function Requirement({ children }: { children: ReactNode }) {
  return (
    <li className="flex items-start gap-2.5 text-sm leading-6 text-slate-300">
      <Check className="mt-1 h-3.5 w-3.5 shrink-0 text-emerald-300" aria-hidden="true" />
      <span>{children}</span>
    </li>
  );
}

function StepLabel({ number, children }: { number: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.16em] text-emerald-300">
      <span className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-emerald-400/40 bg-emerald-400/10 text-[10px] text-emerald-200">
        {number}
      </span>
      {children}
    </div>
  );
}

export default function OpenCodeInstallPage() {
  return (
    <PublicAppShell badge="Local setup" title="Install Forma for OpenCode">
      <main className="mx-auto w-full max-w-5xl space-y-5 pb-8 font-sans">
        <section className={`${PUBLIC_CARD_CLASS} overflow-hidden`}>
          <div className="grid gap-0 lg:grid-cols-[1.15fr_0.85fr]">
            <div className="border-b border-[#2c2f37] p-6 lg:border-b-0 lg:border-r lg:p-8">
              <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-cyan-300">Forma OSS / OpenCode</p>
              <h2 className="mt-3 max-w-2xl text-3xl font-semibold tracking-[-0.03em] text-white sm:text-4xl">
                Build hardware locally. Upload only when you are ready.
              </h2>
              <p className="mt-4 max-w-2xl text-sm leading-7 text-slate-400">
                OpenCode supplies the model and authors the design. Forma runs locally, validates the wiring,
                renders the project, and keeps the working files on your machine.
              </p>
              <div className="mt-6 flex flex-wrap gap-2">
                <span className="rounded-full border border-emerald-400/25 bg-emerald-400/10 px-2.5 py-1 text-[11px] text-emerald-200">
                  No Forma login for local work
                </span>
                <span className="rounded-full border border-cyan-400/25 bg-cyan-400/10 px-2.5 py-1 text-[11px] text-cyan-200">
                  No simulation path
                </span>
                <span className="rounded-full border border-[#39404b] bg-[#11151c] px-2.5 py-1 text-[11px] text-slate-300">
                  Cloud upload is opt-in
                </span>
              </div>
            </div>
            <div className="bg-[radial-gradient(circle_at_top_right,rgba(34,211,238,0.14),transparent_55%),#11151c] p-6 lg:p-8">
              <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-slate-400">
                <Monitor className="h-4 w-4 text-cyan-300" aria-hidden="true" />
                Local boundary
              </div>
              <div className="mt-6 space-y-3 font-mono text-xs">
                <div className="rounded-lg border border-[#39404b] bg-[#0c0f14] p-3 text-slate-400">OpenCode model</div>
                <div className="flex justify-center text-cyan-300" aria-hidden="true">v</div>
                <div className="rounded-lg border border-cyan-400/30 bg-cyan-400/10 p-3 text-cyan-100">Hardware Intermediate Representation + local MCP</div>
                <div className="flex justify-center text-cyan-300" aria-hidden="true">v</div>
                <div className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-3 text-emerald-100">Forma validation + SQLite</div>
                <div className="flex justify-center text-emerald-300" aria-hidden="true">v</div>
                <div className="rounded-lg border border-[#39404b] bg-[#0c0f14] p-3 text-slate-300">forma-project.json</div>
              </div>
            </div>
          </div>
        </section>

        <section className={`${PUBLIC_CARD_CLASS} p-5 sm:p-6`}>
          <StepLabel number="1">Check the prerequisites</StepLabel>
          <div className="mt-4 grid gap-5 lg:grid-cols-[1fr_1fr]">
            <div>
              <h2 className="text-xl font-semibold tracking-tight text-white">Bring OpenCode and a model</h2>
              <p className="mt-2 text-sm leading-6 text-slate-400">
                Forma does not select a server model or replace failed requests with examples. OpenCode must already
                be installed and authenticated with the provider you want to use.
              </p>
            </div>
            <ul className="space-y-2">
              <Requirement>OpenCode with a configured model provider. A local provider is supported.</Requirement>
              <Requirement>Git, Python 3.11+, Node.js 18+, and npm for the local Forma runtime.</Requirement>
              <Requirement>Nothing from this setup requires Bun.</Requirement>
            </ul>
          </div>
        </section>

        <section className={`${PUBLIC_CARD_CLASS} p-5 sm:p-6`}>
          <StepLabel number="2">Install and start the local runtime</StepLabel>
          <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <h2 className="text-xl font-semibold tracking-tight text-white">One command per platform</h2>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">
                The installer uses a pinned checkout when `FORMA_OSS_REF` is supplied, reports the installed revision,
                preserves your OpenCode settings by using a dedicated local workspace, and starts the backend and UI.
              </p>
            </div>
            <a
              href="https://github.com/caid-technologies/Forma-OSS/tree/main/scripts/development"
              target="_blank"
              rel="noreferrer"
              className={PUBLIC_BUTTON_OUTLINE_CLASS}
            >
              Review installer source
              <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
            </a>
          </div>
          <div className="mt-5 grid gap-3 lg:grid-cols-2">
            <CommandBlock
              label="macOS / Linux"
              value={UNIX_INSTALL_COMMAND}
              description="Keep this terminal open while the local Forma backend and UI are running."
            />
            <CommandBlock
              label="Windows PowerShell"
              value={WINDOWS_INSTALL_COMMAND}
              description="The script uses native PowerShell process and path handling."
            />
          </div>
          <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-300/20 bg-amber-300/5 px-3 py-2.5 text-xs leading-5 text-amber-100/80">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" aria-hidden="true" />
            <p>Review the installer source before running it. It clones the public repository, installs the local CLI, and starts the local services.</p>
          </div>
        </section>

        <section className={`${PUBLIC_CARD_CLASS} p-5 sm:p-6`}>
          <StepLabel number="3">OpenCode and build</StepLabel>
          <div className="mt-4 grid gap-5 lg:grid-cols-[0.8fr_1.2fr]">
            <div>
              <h2 className="text-xl font-semibold tracking-tight text-white">Use the Forma skill</h2>
              <p className="mt-2 text-sm leading-6 text-slate-400">
                Open a second terminal. The installer creates `~/forma-workspace`, installs the complete shared skill,
                and configures its local MCP entry. The project is created by your OpenCode model, not by a demo fixture.
              </p>
              <div className="mt-4">
                <CommandBlock label="Start OpenCode" value={UNIX_OPEN_COMMAND} />
              </div>
              <p className="mt-3 text-xs leading-5 text-slate-500">
                On Windows, use the PowerShell equivalent below. `opencode mcp list` should show `forma` connected before
                you start the prompt.
              </p>
              <div className="mt-3">
                <CommandBlock label="Windows PowerShell" value={WINDOWS_OPEN_COMMAND} />
              </div>
            </div>
            <div className="overflow-hidden rounded-xl border border-[#303640] bg-[#0c0f14]">
              <div className="flex items-center justify-between gap-3 border-b border-[#252a32] px-3 py-2">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-300" aria-hidden="true" />
                  <span className="text-[11px] font-medium uppercase tracking-[0.14em] text-slate-400">Real project prompt</span>
                </div>
                <CopyButton value={EXAMPLE_PROMPT} label="Copy example prompt" />
              </div>
              <p className="p-4 text-sm leading-7 text-slate-200">{EXAMPLE_PROMPT}</p>
              <div className="border-t border-[#252a32] px-4 py-3 text-xs leading-5 text-slate-500">
                The normal path calls `forma.compile_project`, applies deterministic electrical validation, and keeps the
                final project in the local workspace. There is no simulation fallback.
              </div>
            </div>
          </div>
        </section>

        <section className={`${PUBLIC_CARD_CLASS} p-5 sm:p-6`}>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <StepLabel number="4">Keep or upload</StepLabel>
              <h2 className="mt-3 text-xl font-semibold tracking-tight text-white">The cloud boundary is explicit</h2>
              <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
                Local generation, validation, rendering, and status do not require an account. Sign in only when you
                want to send a completed project to Forma Cloud.
              </p>
            </div>
            <div className="inline-flex items-center gap-2 rounded-full border border-emerald-400/25 bg-emerald-400/10 px-2.5 py-1 text-[11px] text-emerald-200">
              <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
              Opt-in upload
            </div>
          </div>
          <div className="mt-5 grid gap-3 lg:grid-cols-2">
            <CommandBlock
              label="macOS / Linux"
              value={UNIX_UPLOAD_COMMAND}
              description="The CLI stores the session in the OS credential store and redacts secrets before upload."
            />
            <CommandBlock
              label="Windows PowerShell"
              value={WINDOWS_UPLOAD_COMMAND}
              description="After a successful push, the CLI reports the cloud project URL and keeps local linkage."
            />
          </div>
          <div className="mt-5 flex flex-wrap gap-2">
            <Link href="/about" className={PUBLIC_BUTTON_OUTLINE_CLASS}>
              About Forma
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
            <a
              href="https://github.com/caid-technologies/Forma-OSS"
              target="_blank"
              rel="noreferrer"
              className={PUBLIC_BUTTON_PRIMARY_CLASS}
            >
              View the source
              <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
            </a>
          </div>
        </section>

        <section className={`${PUBLIC_CARD_CLASS} p-5 sm:p-6`}>
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-300" aria-hidden="true" />
            <h2 className="text-lg font-semibold tracking-tight text-white">Troubleshooting</h2>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <details className="rounded-lg border border-[#2c2f37] bg-[#11151c] p-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-200">Missing Python or Node</summary>
              <p className="mt-2 text-xs leading-5 text-slate-500">Install Python 3.11+ and Node.js 18+, then rerun the same setup command. The installer leaves existing files untouched when a prerequisite check fails.</p>
            </details>
            <details className="rounded-lg border border-[#2c2f37] bg-[#11151c] p-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-200">Forma is not connected</summary>
              <p className="mt-2 text-xs leading-5 text-slate-500">Keep the installer terminal open and run `opencode mcp list` from `forma-workspace`. The local endpoint is `http://127.0.0.1:8000/mcp`.</p>
            </details>
            <details className="rounded-lg border border-[#2c2f37] bg-[#11151c] p-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-200">OpenCode cannot run the prompt</summary>
              <p className="mt-2 text-xs leading-5 text-slate-500">Authenticate a model provider in OpenCode. Forma does not provide a hidden simulation model or silently substitute an example project.</p>
            </details>
            <details className="rounded-lg border border-[#2c2f37] bg-[#11151c] p-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-200">Upload asks you to log in</summary>
              <p className="mt-2 text-xs leading-5 text-slate-500">That is expected. Run `forma-oss login` only for cloud operations, then rerun the push command.</p>
            </details>
          </div>
        </section>
      </main>
    </PublicAppShell>
  );
}
