import type { ReactNode } from "react";
import Image from "next/image";
import Link from "next/link";
import {
  ArrowUpRight,
  ChevronDown,
  Cpu,
  Handshake,
  Mail,
  ShieldCheck,
} from "lucide-react";
import CopyButton from "../../components/copy-button";
import { ProviderMark } from "../../components/provider-mark";
import {
  legalContactEmail,
  legalDocuments,
  legalEntityName,
} from "../../lib/legal-docs";
import { aboutMarqueePartners } from "../../lib/partners";

const CARD_SURFACE_CLASS = "rounded-xl border border-[#2c2f37] bg-[#181b22]";
const BUTTON_OUTLINE_CLASS =
  "inline-flex h-8 items-center gap-1.5 rounded-lg border border-[#2c2f37] px-3 text-xs font-medium text-slate-300 transition hover:bg-white/5 hover:text-white";
const SOCIAL_ICON_CLASS =
  "inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-300 transition hover:bg-white/5 hover:text-white";

function LinkedInMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path
        fill="currentColor"
        d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"
      />
    </svg>
  );
}

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path
        fill="currentColor"
        d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 10.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"
      />
    </svg>
  );
}

const companyLinks = [
  {
    href: "https://www.linkedin.com/company/ca-id",
    label: "CAID Technologies on LinkedIn",
    icon: <LinkedInMark className="h-3.5 w-3.5" />,
  },
  {
    href: "https://huggingface.co/caid-technologies",
    label: "CAID Technologies on Hugging Face",
    icon: <ProviderMark id="huggingface" className="h-4 w-4" />,
  },
  {
    href: "https://github.com/caid-technologies",
    label: "CAID Technologies on GitHub",
    icon: <GitHubMark className="h-3.5 w-3.5" />,
  },
];

function AboutPaneHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4 px-5 py-4 xl:flex-row xl:items-start xl:justify-between">
      <div className="min-w-0">
        {eyebrow ? <p className="text-xs text-slate-500">{eyebrow}</p> : null}
        <h2 className={`${eyebrow ? "mt-1" : ""} text-lg font-semibold tracking-tight text-white`}>{title}</h2>
        {description ? <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}

export default function AboutView() {
  return (
    <div className="mx-auto w-full max-w-7xl font-sans text-zinc-100">
      <div className="mb-6 hidden min-w-0 items-center gap-2 md:flex">
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-400">
          <Handshake className="h-3 w-3" />
          General
        </span>
        <h1 className="truncate text-sm font-semibold tracking-tight text-zinc-100">About</h1>
      </div>

      <div className="space-y-5 pb-4">
        <article className={CARD_SURFACE_CLASS}>
          <AboutPaneHeader
            eyebrow={legalEntityName}
            title="Forma"
            description="Forma helps builders turn early hardware ideas into project plans with parts, wiring, validation, build notes, and generated artifacts."
            actions={
              <>
                <Link href="/install/opencode" className={BUTTON_OUTLINE_CLASS}>
                  OpenCode setup
                  <ArrowUpRight className="h-3.5 w-3.5" />
                </Link>
                <span className={BUTTON_OUTLINE_CLASS}>
                  <Mail className="h-3.5 w-3.5" />
                  <span className="select-all">{legalContactEmail}</span>
                  <CopyButton value={legalContactEmail} label="Copy email address" />
                </span>
                {companyLinks.map((link) => (
                  <a
                    key={link.href}
                    href={link.href}
                    target="_blank"
                    rel="noreferrer"
                    className={SOCIAL_ICON_CLASS}
                    aria-label={link.label}
                    title={link.label}
                  >
                    {link.icon}
                  </a>
                ))}
              </>
            }
          />

          <div className="grid gap-3 p-5 sm:grid-cols-2">
            <div className="flex items-start gap-3 rounded-xl border border-[#2c2f37] bg-[#101115] p-3.5">
              <div className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
                <Cpu className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <h3 className="text-sm font-medium text-zinc-100">Hardware planning</h3>
                <p className="mt-1 text-xs leading-5 text-slate-500">
                  Project generation for low-voltage maker hardware, structured around traceable artifacts.
                </p>
              </div>
            </div>
            <div className="flex items-start gap-3 rounded-xl border border-[#2c2f37] bg-[#101115] p-3.5">
              <div className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
                <ShieldCheck className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <h3 className="text-sm font-medium text-zinc-100">Safety first</h3>
                <p className="mt-1 text-xs leading-5 text-slate-500">
                  Forma keeps safety constraints, validation notes, and known unknowns visible in the workflow.
                </p>
              </div>
            </div>
          </div>
        </article>

        <article className={CARD_SURFACE_CLASS}>
          <AboutPaneHeader
            title="Partners"
            description="Infrastructure partners support model, media, and deployment workflows across the product."
          />

          <div className="grid gap-3 p-5 sm:grid-cols-2 xl:grid-cols-4">
            {aboutMarqueePartners.map((partner) => {
              const lightSurface = "logoSurface" in partner && partner.logoSurface === "light";
              return (
                <div
                  key={partner.slug}
                  className={`flex h-20 items-center justify-center rounded-xl border border-[#2c2f37] px-6 ${
                    lightSurface ? "bg-white" : "bg-[#002b36]"
                  }`}
                  title={partner.name}
                >
                  <Image
                    src={partner.logo}
                    alt={`${partner.name} logo`}
                    width={800}
                    height={176}
                    className="h-auto max-h-10 w-full object-contain"
                    unoptimized
                  />
                </div>
              );
            })}
          </div>
        </article>

        <article className={CARD_SURFACE_CLASS}>
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-4 marker:content-none [&::-webkit-details-marker]:hidden">
              <ChevronDown className="h-4 w-4 shrink-0 -rotate-90 text-slate-500 transition-transform group-open:rotate-0" />
              <div className="min-w-0 flex-1">
                <h2 className="text-lg font-semibold tracking-tight text-white">Legal</h2>
                <p className="mt-1 text-sm leading-6 text-slate-400">
                  Terms, privacy, safety, and acceptable-use documents for Forma.
                </p>
              </div>
            </summary>
            <nav className="grid gap-1 border-t border-[#2c2f37] p-3 sm:grid-cols-2" aria-label="Legal">
              {legalDocuments.map((document) => (
                <Link
                  key={document.slug}
                  href={`/legal/${document.slug}`}
                  className="flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm text-zinc-300 transition hover:bg-zinc-800/40 hover:text-zinc-100"
                >
                  <span className="min-w-0 truncate">{document.title}</span>
                  <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-zinc-600" aria-hidden="true" />
                </Link>
              ))}
            </nav>
          </details>
        </article>
      </div>
    </div>
  );
}
