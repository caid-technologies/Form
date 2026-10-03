"use client";

import { useEffect, useState, type ReactNode } from "react";
import Image from "next/image";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle,
  Cpu,
  RefreshCw,
  Settings,
  Square,
} from "lucide-react";

import CopyButton from "../../components/copy-button";
import { webConfig } from "../../lib/config";
import { FORMA_AGENT_RUNTIME_STORAGE_KEY } from "../../lib/opencode";
import { ProjectUpdateCard } from "./chat-project-layout";

export type ConversationMessage = {
  id: string;
  role: "assistant" | "user" | "system";
  content: string;
  status?: "idle" | "loading" | "success" | "error" | "cancelled" | "handed-off" | "interrupted";
  timestamp: string;
  projectId?: string | null;
  revisionId?: string | null;
  pipelineProgress?: unknown;
  imagePreview?: string | null;
  contextProjectId?: string | null;
  workflowState?: string | null;
  contextQuestions?: string[];
  contextSuggestions?: string[];
  buildPlanId?: string | null;
  buildJobId?: string | null;
};

type AgentRuntimeOption = {
  id: string;
  label: string;
};

type RuntimeConfigPayload = {
  deployment?: {
    opencode_connector_id?: string | null;
    authoring_runtimes?: AgentRuntimeOption[];
  };
};

const API_URL = webConfig.apiBaseUrl.replace(/\/+$/, "");

function formatTimestamp(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function isRuntimeOfflineMessage(message: ConversationMessage): boolean {
  if (message.role !== "assistant" || message.status !== "error") return false;
  return /(?:runtime assigned to this workspace|local runtime|runtime request failed)/i.test(message.content);
}

function validRuntimeOptions(value: unknown): AgentRuntimeOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const id = "id" in item && typeof item.id === "string" ? item.id.trim() : "";
    const label = "label" in item && typeof item.label === "string" ? item.label.trim() : "";
    return id ? [{ id, label: label || id }] : [];
  });
}

export default function ConversationMessageList({
  messages,
  renderPipelineProgress,
  variant = "home",
  emptyMessage,
  onSelectContextSuggestion,
  isLoading = false,
  canBuildNow = false,
  buildNowLoading = false,
  onBuildNow,
  assistantLabel = "Forma",
}: {
  messages: ConversationMessage[];
  renderPipelineProgress: (message: ConversationMessage) => ReactNode;
  variant?: "home" | "project";
  emptyMessage?: string;
  onSelectContextSuggestion?: (suggestion: string) => void;
  isLoading?: boolean;
  canBuildNow?: boolean;
  buildNowLoading?: boolean;
  onBuildNow?: () => void;
  assistantLabel?: string;
}) {
  const [runtimeOptions, setRuntimeOptions] = useState<AgentRuntimeOption[]>([]);
  const [selectedRuntimeId, setSelectedRuntimeId] = useState("");
  const hasRuntimeOfflineMessage = messages.some(isRuntimeOfflineMessage);
  const latestChoiceMessageId = onSelectContextSuggestion
    ? [...messages]
      .reverse()
      .find((message) => message.role === "assistant" && Boolean(message.contextSuggestions?.length))?.id
    : null;
  const projectLayout = variant === "project";
  const displayAssistantLabel = assistantLabel === "OpenCode" ? "Forma Agent" : assistantLabel;

  useEffect(() => {
    if (!hasRuntimeOfflineMessage) return;
    const controller = new AbortController();

    void (async () => {
      try {
        const response = await fetch(`${API_URL}/runtime/config`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) return;
        const payload = await response.json() as RuntimeConfigPayload;
        const configuredOptions = validRuntimeOptions(payload.deployment?.authoring_runtimes);
        const defaultRuntimeId = typeof payload.deployment?.opencode_connector_id === "string"
          ? payload.deployment.opencode_connector_id.trim()
          : "";
        const options = configuredOptions.length > 0
          ? configuredOptions
          : defaultRuntimeId
            ? [{ id: defaultRuntimeId, label: defaultRuntimeId }]
            : [];
        setRuntimeOptions(options);

        const storedRuntimeId = window.localStorage.getItem(FORMA_AGENT_RUNTIME_STORAGE_KEY)?.trim() || "";
        const nextRuntimeId = options.some((option) => option.id === storedRuntimeId)
          ? storedRuntimeId
          : options.some((option) => option.id === defaultRuntimeId)
            ? defaultRuntimeId
            : options[0]?.id || "";
        setSelectedRuntimeId(nextRuntimeId);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
      }
    })();

    return () => controller.abort();
  }, [hasRuntimeOfflineMessage]);

  if (!messages.length && emptyMessage) {
    return (
      <div className="mx-auto w-full max-w-3xl rounded-xl border border-white/5 bg-[#181b22] p-5 text-sm leading-6 text-zinc-500">
        {emptyMessage}
      </div>
    );
  }

  return messages.map((message) => {
    const isUser = message.role === "user";
    const isSystem = message.role === "system";
    const runtimeOffline = isRuntimeOfflineMessage(message);
    const statusTone =
      message.status === "error"
        ? "border-rose-400/40 bg-rose-950/30 text-rose-100"
        : message.status === "success"
          ? "border-emerald-400/35 bg-emerald-950/25 text-emerald-50"
          : message.status === "cancelled" || message.status === "interrupted"
            ? "border-amber-300/35 bg-amber-950/20 text-amber-50"
            : isUser
              ? "border-emerald-500/20 bg-emerald-500/10 text-zinc-100"
              : isSystem
                ? "border-white/5 bg-black/25 text-zinc-400"
                : "border-white/5 bg-[#181b22] text-zinc-100";
    const showPipeline = Boolean(message.pipelineProgress)
      && (!message.projectId || message.status === "error" || message.status === "cancelled");

    return (
      <div
        key={message.id}
        className={`flex min-w-0 ${isUser ? "justify-end" : "justify-start"} ${projectLayout ? "mx-auto w-full max-w-3xl" : ""}`}
      >
        <div className={`min-w-0 max-w-[92%] overflow-hidden rounded-xl border px-3 py-2.5 sm:max-w-[86%] sm:px-4 sm:py-3 ${statusTone}`}>
          <div className="mb-2 flex min-w-0 flex-wrap items-center gap-2 text-[10px] font-medium text-zinc-500">
            {message.status === "loading" ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin text-emerald-400" />
            ) : message.status === "success" ? (
              <CheckCircle className="h-3.5 w-3.5 text-emerald-300" />
            ) : message.status === "error" || message.status === "interrupted" ? (
              <AlertTriangle className="h-3.5 w-3.5 text-rose-300" />
            ) : message.status === "cancelled" ? (
              <Square className="h-3.5 w-3.5 fill-current text-amber-300" />
            ) : isUser ? (
              <ArrowRight className="h-3.5 w-3.5 text-emerald-400" />
            ) : (
              <Cpu className="h-3.5 w-3.5 text-zinc-400" />
            )}
            <span>{isUser ? "You" : isSystem ? "Context" : displayAssistantLabel}</span>
            <span className="text-zinc-700">·</span>
            <span suppressHydrationWarning>{formatTimestamp(message.timestamp)}</span>
            <CopyButton
              value={message.content}
              label={isUser ? "Copy your message" : isSystem ? "Copy context message" : "Copy Forma's message"}
              className="ml-auto"
            />
          </div>
          <p className="break-anywhere whitespace-pre-wrap text-sm leading-6">{message.content}</p>
          {runtimeOffline && (
            <div className="mt-3 border-t border-rose-300/15 pt-3">
              <div className="mb-2 flex items-center gap-2 text-[11px] font-medium text-rose-200/80">
                <span className="h-1.5 w-1.5 rounded-full bg-rose-300" aria-hidden="true" />
                Runtime · Offline
              </div>
              {runtimeOptions.length > 0 && (
                <label className="mb-2 block text-[11px] text-rose-100/80">
                  <span className="mb-1 block">Choose runtime</span>
                  <select
                    value={selectedRuntimeId}
                    onChange={(event) => {
                      const runtimeId = event.target.value;
                      setSelectedRuntimeId(runtimeId);
                      window.localStorage.setItem(FORMA_AGENT_RUNTIME_STORAGE_KEY, runtimeId);
                    }}
                    className="h-8 w-full rounded-lg border border-white/10 bg-[#181b22] px-2 text-xs text-zinc-100 outline-none focus:border-rose-300/40"
                    aria-label="Choose Forma Agent runtime"
                  >
                    {runtimeOptions.map((runtime) => (
                      <option key={runtime.id} value={runtime.id}>{runtime.label}</option>
                    ))}
                  </select>
                </label>
              )}
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => {
                    if (selectedRuntimeId) {
                      window.localStorage.setItem(FORMA_AGENT_RUNTIME_STORAGE_KEY, selectedRuntimeId);
                    }
                    window.location.reload();
                  }}
                  className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-rose-300/25 bg-rose-300/10 px-3 text-xs font-medium text-rose-100 transition-colors hover:bg-rose-300/15"
                >
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  Reconnect
                </button>
                {runtimeOptions.length === 0 && (
                  <Link
                    href="/install/opencode"
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 text-xs font-medium text-zinc-200 transition-colors hover:bg-white/10"
                  >
                    <Settings className="h-3.5 w-3.5" aria-hidden="true" />
                    Runtime setup
                  </Link>
                )}
              </div>
            </div>
          )}
          {!isUser && message.id === latestChoiceMessageId && Boolean(message.contextSuggestions?.length) && (
            <div className="mt-3 grid gap-2 sm:grid-cols-2" aria-label="Suggested answers">
              {message.contextSuggestions?.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => onSelectContextSuggestion?.(suggestion)}
                  disabled={isLoading}
                  className="flex min-h-12 items-center gap-2 rounded-lg border border-white/10 bg-[#0f1117] px-3 py-2 text-left text-xs font-medium text-zinc-300 transition-colors hover:border-emerald-500/30 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <ArrowRight className="h-3.5 w-3.5 shrink-0" />
                  <span className="break-words">{suggestion}</span>
                </button>
              ))}
            </div>
          )}
          {!isUser && canBuildNow && (message.workflowState === "gathering_context" || Boolean(message.contextProjectId)) && (
            <button
              type="button"
              onClick={onBuildNow}
              disabled={buildNowLoading || isLoading}
              className="mt-3 inline-flex h-8 items-center justify-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 text-xs font-medium text-emerald-400 transition-colors hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50"
              aria-label="Default — proceed with safe prototype defaults"
              title="Default — proceed with safe prototype defaults"
            >
              {buildNowLoading ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="h-3.5 w-3.5" />}
              Default
            </button>
          )}
          {message.imagePreview && (
            <Image
              src={message.imagePreview}
              alt="Hardware reference thumbnail"
              width={144}
              height={96}
              unoptimized
              className="mt-3 h-24 w-36 rounded-lg border border-white/10 object-cover"
            />
          )}
          {showPipeline && renderPipelineProgress(message)}
          <ProjectUpdateCard message={message} />
        </div>
      </div>
    );
  });
}
