"use client";

import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Image from "next/image";
import { usePathname, useRouter } from "next/navigation";
import {
  generationLlmImageSupport,
  generationLlmKey,
  type GenerationLlmOption,
  shouldShowProductImageSection,
} from "../lib/active-llms";
import { buildProjectDocsMarkdown, docsExportFilename } from "../lib/docs-export";
import { normalizeContextSuggestions } from "../lib/context-suggestions";
import { chatActivity, collectChatOperations, settleChatActivityMessages, type ChatActivity } from "../lib/chat-activity";
import { useChatActivity } from "./forma-workspace/use-chat-activity";
import {
  cancelOpenCodeSession,
  createOpenCodeSession,
  listOpenCodeEvents,
  reduceOpenCodeTurn,
  openCodeDesignNotice,
  OpenCodePollingError,
  OPENCODE_POLL_MAX_FAILURES,
  OPENCODE_STATUS_UNKNOWN,
  parseOpenCodePendingTurn,
  withOpenCodeDeadline,
  type OpenCodePendingTurn,
  submitOpenCodeCommand,
  type OpenCodeSession,
  type OpenCodeTurnState,
} from "../lib/opencode";
import { authoringModeEnabled, usableRuntimeLlmOptions, webConfig, type RuntimeConfigContract } from "../lib/config";
import { hasConversationHistory, loadProjectChatHistory } from "../lib/project-chat-history";
import ChatAccessStatus, { type ChatAccessLoadState } from "./forma-workspace/chat-access-status";
import { calculateProjectCostMetrics, resolveProjectComponentInstances } from "../lib/project-cost-metrics";
import { useFormaAuth } from "../lib/forma-auth";
import { GalleryImageRequests, GalleryPageCache, galleryPageKey } from "../lib/gallery-loading";
import {
  isAuthOrSecurityHttpStatus,
  workspaceStatusBadge,
  WORKSPACE_STATUS_STALE_AFTER_MS,
} from "../lib/connection-status";
import {
  humanContextSkipChatSummary,
  humanContextSkipPromptSection,
} from "../lib/human-context-defaults";
import {
  contextBuildControls,
  latestRetryableContextBuildMessage,
  shouldOfferFailedBuildRetry,
} from "../lib/conversation-build-state";
import CopyButton from "../components/copy-button";
import {
  useAdminSession,
  useBackendLogs,
  useJobs,
  type A2AJob,
} from "./forma-workspace/use-admin-data";
import { useDeferredTask } from "./forma-workspace/use-deferred-task";
import {
  useVideoModels,
  type VideoGenerationMode,
  type VideoModelOption,
} from "./forma-workspace/use-video-models";
import {
  VIDEO_PROMPT_MAX_CHARS,
  useProjectVideo,
  videoIdentity,
  videoLabel,
  videoPromptText,
  videoSourceUrl,
  type StoredVideoInfo,
} from "./forma-workspace/use-project-video";
import {
  JobsPanel,
  LogsPanel,
  formatBytes,
  isFinalVideoStatus,
} from "./forma-workspace/admin-panels";
import { ChatAttachmentButton, ChatAttachmentSelection, type ChatAttachmentControls } from "./forma-workspace/chat-attachments";
import HomeChatView, { type GenerationMode } from "./forma-workspace/home-chat-view";
import ChatProjectLayout, { ChatProjectSurface } from "./forma-workspace/chat-project-layout";
import { ProjectHistoryProvider, useProjectHistory, type ProjectHistoryConfig } from "./forma-workspace/project-history";
import ProjectRevisionPreview from "./forma-workspace/project-revision-preview";
import { revisionId, revisionNumber, revisionFromProject } from "../lib/project-history";
import ConversationMessageList, {
  type ConversationMessage,
} from "./forma-workspace/conversation-message-list";
import HostedChatMaintenance, {
  AUTHORING_MODE_ACTIVE_MESSAGE,
  AuthoringModeBanner,
  HOSTED_CHAT_MAINTENANCE_MESSAGE,
} from "./forma-workspace/hosted-chat-maintenance";
import useChatAutoScroll from "./forma-workspace/use-chat-auto-scroll";
import useChromeHeaderScroll from "./forma-workspace/use-chrome-header-scroll";
import {
  hardwareReferenceSrcFromChatMessages,
  isHardwareReferenceCandidate,
  resolveProjectImageCandidates,
  withHardwareReferenceMetadata,
  type ProjectImageCandidate,
} from "../lib/project-images";
import {
  PROJECT_GALLERY_PAGE_SIZE,
  buildProjectGalleryItems,
  previewableImageSrc,
  type ProjectGalleryItem,
} from "./forma-workspace/project-gallery";
import CadModelPanel from "./forma-workspace/cad-model-panel";
import ProjectExportsPanel from "./forma-workspace/project-exports-panel";
import { projectCadModel, resolveCadModel } from "../lib/cad-model";
import { FormaProjectBrowser, type FormaProjectSummary } from "@isayahc/forma-gui";
import {
  AssemblyPanel,
  BomPanel,
  MechanicalPanel,
  OverviewPanel,
} from "./forma-workspace/project-detail-panels";
import {
  ChatSidebar,
  EditableWorkspaceTitle,
  MobileSidebarButton,
  MobileSidebarDrawer,
  MobileWorkspaceBar,
  type ChatListItem,
} from "./forma-workspace/sidebar";
import WorkspaceFrame from "./forma-workspace/workspace-frame";
import UserIntegrationsPage from "./user/user-integrations-page";
import AboutView from "./forma-workspace/about-view";
import {
  Sparkles,
  Cpu,
  ShieldCheck,
  AlertTriangle,
  CheckCircle,
  History,
  RefreshCw,
  Eye,
  Film,
  ArrowRight,
  ArrowLeft,
  Layers,
  Paperclip,
  ExternalLink,
  KeyRound,
  Terminal,
  MessageSquare,
  Square,
  Trash2,
  Settings,
  Handshake,
  Database,
  ChevronDown,
  LayoutDashboard,
  ClipboardList,
  Cuboid,
  Box,
  CircuitBoard,
  BookOpen,
  Download,
  Clapperboard,
} from "lucide-react";

const SchematicCanvas = dynamic(() => import("../components/schematic-canvas"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full min-h-[620px] items-center justify-center bg-[var(--forma-page)] text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
      Loading wiring diagram...
    </div>
  ),
});

const API_URL = normalizeApiUrl(webConfig.apiBaseUrl);
const DEFAULT_SHOW_DEVELOPER_TOOLS = webConfig.publicDeveloperTools;
const DEFAULT_HOSTED_CHAT_ENABLED = webConfig.hostedChatEnabled;
const DEFAULT_WORKFLOW_ID = "default";
const WEB_RESEARCH_WORKFLOW_ID = "web_research";
const JOB_POLL_INTERVAL_MS = 5000;
const ACTIVE_JOB_PROGRESS_POLL_INTERVAL_MS = 1200;
const PIPELINE_UI_HEARTBEAT_MS = 5000;
const PIPELINE_STALE_AFTER_MS = WORKSPACE_STATUS_STALE_AFTER_MS;
const RECOVERY_JOB_BATCH_SIZE = 3;
const RECOVERY_JOB_MAX_BACKOFF_MS = 60000;
const LOG_POLL_INTERVAL_MS = 5000;
const AUTHORING_DELIVERY_POLL_MS = 5000;
const AUTHORING_DELIVERED_SIGNAL_MS = 8000;
const CHAT_THREAD_STORAGE_PREFIX = "forma.chat.";
const CHAT_INDEX_STORAGE_KEY = "forma.chatIndex";
const PINNED_CHATS_STORAGE_KEY = "forma.pinnedChats";
const LEGACY_PROJECT_CHAT_STORAGE_PREFIX = "forma.projectChat.";
const MAX_PROJECT_CHAT_MESSAGES = 80;
const MAX_CHAT_INDEX_ITEMS = 200;
const NEW_PROJECT_TITLE = "New project";
const EMPTY_GALLERY_IMAGES: Record<string, ProjectImageCandidate | null> = {};
const EMPTY_PROJECT_HISTORY: any[] = [];

let lastKnownServerStatus: "connected" | "disconnected" | null = null;

type GenerationWorkflowOption = {
  id: string;
  label: string;
  description?: string;
  uses_catalog?: boolean;
  uses_web_research?: boolean;
  uses_firecrawl_mcp?: boolean;
  uses_external_sources?: boolean;
};

type AgentPipelineStep = {
  id: string;
  agent: string;
  label: string;
  description: string;
  duration_ms?: number;
  optional?: boolean;
};

type AgentPipelineEvent = {
  workflow?: string;
  step_id: string;
  status: "started" | "completed" | "failed" | "skipped" | string;
  agent?: string;
  label?: string;
  description?: string;
  observed_at?: string;
  details?: Record<string, any>;
};

type AgentPipelineProgress = {
  startedAt: string;
  steps: AgentPipelineStep[];
  currentStepIndex: number;
  estimated: boolean;
  synced?: boolean;
  jobId?: string | null;
  events?: AgentPipelineEvent[];
  uiUpdatedAt?: string;
};

type ChatMessage = {
  id: string;
  role: "assistant" | "user" | "system";
  content: string;
  status?: "idle" | "loading" | "success" | "error" | "cancelled" | "handed-off" | "interrupted";
  openCodeTurn?: OpenCodePendingTurn | null;
  timestamp: string;
  projectId?: string | null;
  revisionId?: string | null;
  pipelineProgress?: AgentPipelineProgress | null;
  imagePreview?: string | null;
  contextProjectId?: string | null;
  workflowState?: string | null;
  contextQuestions?: string[];
  contextSuggestions?: string[];
  buildPlanId?: string | null;
  buildJobId?: string | null;
};

type ActiveGenerationRun = {
  kind: "chat" | "project-chat" | "context-build";
  controller: AbortController;
  jobId: string | null;
  planId?: string | null;
  projectId?: string | null;
  chatId: string;
  assistantMessageId: string | null;
  openCodeSessionId?: string | null;
  cancelled: boolean;
};

type ActiveGenerationState = Pick<ActiveGenerationRun, "kind" | "jobId">;

type HumanContextQuestion = {
  id: string;
  label: string;
  question: string;
  placeholder: string;
  suggestions: string[];
};

type PendingHumanContext = {
  basePrompt: string;
  questions: HumanContextQuestion[];
  answers: Record<string, string>;
};

type PendingProjectDeletion = {
  projectId: string;
  title: string;
};

const defaultGenerationWorkflows: GenerationWorkflowOption[] = [
  { id: DEFAULT_WORKFLOW_ID, label: "Catalog", description: "Catalog workflow", uses_catalog: true },
  { id: WEB_RESEARCH_WORKFLOW_ID, label: "Web Research", description: "Live web research workflow", uses_web_research: true, uses_firecrawl_mcp: true, uses_external_sources: true },
];

const RUNPOD_PARTI_BASE_MODEL = "caid-technologies/parti-base";
const BASETEN_GLM_MODEL = "zai-org/GLM-5.2";
const BASETEN_DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Pro";
const ANTHROPIC_OPUS_MODEL = "claude-opus-5";
const ANTHROPIC_SONNET_MODEL = "claude-sonnet-5";
const GEMINI_FLASH_MODEL = "gemini-3.7-flash";
const CLOUDFLARE_GEMMA_MODEL = "@cf/google/gemma-4-26b-a4b-it";
const NVIDIA_GLM_MODEL = "nvidia/z-ai/glm-5.2";
const NVIDIA_QWEN_CODER_32B_MODEL = "qwen/qwen2.5-coder-32b-instruct";
const NVIDIA_LLAMA_8B_MODEL = "meta/llama-3.1-8b-instruct";

const defaultAgentPipelineSteps: AgentPipelineStep[] = [
  {
    id: "safety_guardrail",
    agent: "Safety Guardrail",
    label: "Checking safe build scope",
    description: "Screening the request for low-voltage maker hardware constraints.",
    duration_ms: 3500,
  },
  {
    id: "context_clarifier",
    agent: "Context Clarifier Agent",
    label: "Clarifying build context",
    description: "Checking whether user-provided answers should be folded into generation.",
    duration_ms: 2500,
  },
  {
    id: "intent_parser",
    agent: "Intent Parser Agent",
    label: "Parsing the hardware idea",
    description: "Converting the prompt into project intent and category.",
    duration_ms: 5500,
  },
  {
    id: "requirements",
    agent: "Requirements Agent",
    label: "Extracting requirements",
    description: "Capturing functions, voltage, constraints, and safety notes.",
    duration_ms: 5500,
  },
  {
    id: "system_architecture",
    agent: "System Architecture Agent",
    label: "Decomposing the complete system",
    description: "Building a purpose-driven tree of electrical, mechanical, firmware, and nested subsystems.",
    duration_ms: 5500,
  },
  {
    id: "component_selection",
    agent: "Component Selection Agent",
    label: "Selecting compatible parts",
    description: "Choosing parts by system role before exact catalog pins are hydrated.",
    duration_ms: 6500,
  },
  {
    id: "wiring_netlist",
    agent: "Wiring/Netlist Agent",
    label: "Drafting nets and pin mappings",
    description: "Connecting power, ground, buses, controller pins, and peripherals.",
    duration_ms: 6500,
  },
  {
    id: "validation_repair",
    agent: "Validation + Auto-Correction Agent",
    label: "Validating and repairing wiring",
    description: "Checking shorts, voltage mismatches, unpowered parts, and pin conflicts.",
    duration_ms: 5500,
  },
  {
    id: "bom",
    agent: "BOM Agent",
    label: "Calculating BOM and cost",
    description: "Summing selected components and updating the project estimate.",
    duration_ms: 3000,
  },
  {
    id: "mechanical_fabrication",
    agent: "Mechanical/Fabrication Agent",
    label: "Designing enclosure and placement",
    description: "Generating mounting, fabrication, CAD, and 3D placement details.",
    duration_ms: 6500,
  },
  {
    id: "assembly",
    agent: "Assembly Instruction Agent",
    label: "Writing build steps",
    description: "Producing sequential assembly instructions and safety flags.",
    duration_ms: 5500,
  },
  {
    id: "package_project",
    agent: "Project Packager",
    label: "Packaging project artifacts",
    description: "Building the Hardware Intermediate Representation, diagrams, validation summary, and saved record.",
    duration_ms: 3500,
  },
];

const optionalImagePipelineStep: AgentPipelineStep = {
  id: "image_generation",
  agent: "Product Image Agent",
  label: "Generating product visuals",
  description: "Creating optional concept images from the completed Hardware Intermediate Representation visual spec.",
  duration_ms: 8000,
  optional: true,
};

const CHAT_DIAGNOSTIC_CHARACTER_LIMIT = 420;

function generationLlmLabel(provider: string, model: string) {
  if (provider === "runpod-serverless" && model === RUNPOD_PARTI_BASE_MODEL) return RUNPOD_PARTI_BASE_MODEL;
  if (provider === "runpod" && model === RUNPOD_PARTI_BASE_MODEL) return "Runpod Parti Base";
  if (provider === "baseten" && model === BASETEN_GLM_MODEL) return "GLM 5.2";
  if (provider === "baseten" && model === BASETEN_DEEPSEEK_MODEL) return "Baseten DeepSeek V4 Pro";
  if (provider === "anthropic" && model === ANTHROPIC_OPUS_MODEL) return "Claude Opus 5";
  if (provider === "anthropic" && model === ANTHROPIC_SONNET_MODEL) return "Claude Sonnet 5";
  if (provider === "gemini" && model === GEMINI_FLASH_MODEL) return "Gemini 3.7 Flash";
  if (provider === "vertex" && model === GEMINI_FLASH_MODEL) return "Vertex Gemini 3.7 Flash";
  if (provider === "huggingface") return `Hugging Face ${model}`;
  if (provider === "gmi" && model === "anthropic/claude-fable-5") return "GMI Claude Fable 5";
  if (provider === "cloudflare" && model === CLOUDFLARE_GEMMA_MODEL) return "Cloudflare Gemma 4 26B A4B";
  if (provider === "nvidia" && model === NVIDIA_GLM_MODEL) return "NVIDIA GLM 5.2";
  if (provider === "nvidia" && model === NVIDIA_QWEN_CODER_32B_MODEL) return "NVIDIA Qwen2.5 Coder 32B";
  if (provider === "nvidia" && model === NVIDIA_LLAMA_8B_MODEL) return "NVIDIA Llama 3.1 8B";
  if (provider === "simulation") return "Local Simulation";
  return `${provider} ${model}`.trim();
}

function normalizeApiUrl(value: string) {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) return "/api";
  return trimmed.endsWith("/api") ? trimmed : `${trimmed}/api`;
}

function formaBrowserProjectFromGalleryItem(item: ProjectGalleryItem): FormaProjectSummary {
  return {
    project_id: item.projectId,
    title: item.title,
    created_at: item.createdAt,
    creator_display: item.creatorDisplay,
    creator_image_url: item.creatorImageUrl,
    parts_count: item.partsCount,
    save_count: item.saveCount,
    remix_count: item.remixCount,
    saved: item.saved,
    can_chat: item.canChat,
    image_url: item.image ? previewableImageSrc(item.image.src) : null,
  };
}

function downloadBrowserFile(contents: string, filename: string, mimeType: string) {
  const blob = new Blob([contents], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

const samplePrompts = [
  "Compact handheld device with display, controls, USB-C power, and enclosure",
  "Environmental monitor with sensor feedback, display, and battery power",
  "Small controller for a low-voltage actuator or relay",
];

function newChatMessageId() {
  return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function newBuildChatId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function newFrontendJobId() {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `job_frontend_${suffix.replace(/[^A-Za-z0-9_.:-]/g, "_")}`;
}

function chatTimestamp() {
  return new Date().toISOString();
}

function validChatStatus(value: any): ChatMessage["status"] {
  return ["idle", "loading", "success", "error", "cancelled", "interrupted"].includes(value) ? value : "idle";
}

function validChatRole(value: any): ChatMessage["role"] {
  return ["assistant", "user", "system"].includes(value) ? value : "assistant";
}

function normalizeChatMessage(value: any): ChatMessage | null {
  if (!value || typeof value !== "object" || typeof value.content !== "string") return null;
  // Older clients saved onboarding copy as an assistant reply. Keep it out of
  // restored conversations and subsequent writes; real replies keep their IDs.
  if (value.id === "assistant-welcome") return null;
  const buildExecutionStatus = typeof value.buildExecution?.status === "string"
    ? value.buildExecution.status
    : "";
  const normalizedStatus = buildExecutionStatus === "planned" || buildExecutionStatus === "running"
    ? "loading"
    : validChatStatus(value.status);
  return {
    id: typeof value.id === "string" && value.id ? value.id : newChatMessageId(),
    role: validChatRole(value.role),
    content: value.content,
    status: normalizedStatus,
    openCodeTurn: parseOpenCodePendingTurn(value.openCodeTurn),
    timestamp: typeof value.timestamp === "string" && value.timestamp ? value.timestamp : chatTimestamp(),
    projectId: typeof value.projectId === "string" ? value.projectId : null,
    revisionId: revisionId(value.revisionId ?? value.revision_id),
    pipelineProgress: normalizeAgentPipelineProgress(value.pipelineProgress),
    imagePreview: typeof value.imagePreview === "string" ? value.imagePreview : null,
    contextProjectId: typeof value.contextProjectId === "string"
      ? value.contextProjectId
      : typeof value.context_project_id === "string"
        ? value.context_project_id
        : null,
    workflowState: typeof value.workflowState === "string"
      ? value.workflowState
      : typeof value.workflow_state === "string"
        ? value.workflow_state
        : null,
    contextQuestions: (Array.isArray(value.contextQuestions) ? value.contextQuestions : value.questions)
      ?.filter((question: unknown): question is string => typeof question === "string" && Boolean(question.trim()))
      .map((question: string) => question.trim()) || [],
    contextSuggestions: normalizeContextSuggestions(value.contextSuggestions ?? value.suggestions),
    buildPlanId: typeof value.buildPlanId === "string"
      ? value.buildPlanId
      : typeof value.build_plan_id === "string"
        ? value.build_plan_id
        : typeof value.buildExecution?.plan_id === "string"
          ? value.buildExecution.plan_id
          : null,
    buildJobId: typeof value.buildJobId === "string"
      ? value.buildJobId
      : typeof value.build_job_id === "string"
        ? value.build_job_id
        : typeof value.buildExecution?.job_id === "string"
          ? value.buildExecution.job_id
          : null,
  };
}

function chatHasStarted(messages: ChatMessage[]) {
  return messages.some((message) => message.role === "user" || Boolean(message.projectId));
}

function chatTitleFromMessages(messages: ChatMessage[], fallback = NEW_PROJECT_TITLE) {
  const firstUserMessage = messages.find((message) => message.role === "user" && message.content.trim());
  const title = firstUserMessage?.content.trim().replace(/\s+/g, " ");
  if (!title) return fallback;
  return title.length > 80 ? `${title.slice(0, 77)}...` : title;
}

function persistableChatMessages(messages: unknown[]): ChatMessage[] {
  return messages
    .map(normalizeChatMessage)
    .filter((message: ChatMessage | null): message is ChatMessage => Boolean(message))
    .filter((message) => !message.id.startsWith("project-context:"))
    .slice(-MAX_PROJECT_CHAT_MESSAGES);
}

function mergeFetchedChatMessages(remoteMessages: ChatMessage[], localMessages: ChatMessage[], preferLocal = false): ChatMessage[] {
  const localById = new Map(localMessages.map((message) => [message.id, message]));
  const seen = new Set<string>();
  const merged: ChatMessage[] = [];

  remoteMessages.forEach((remote) => {
    if (seen.has(remote.id)) return;
    seen.add(remote.id);
    const local = localById.get(remote.id);
    if (!local) {
      merged.push(remote);
      return;
    }
    if (preferLocal) {
      merged.push(local);
      return;
    }

    const localIsTerminal = ["success", "error", "cancelled", "interrupted"].includes(local.status || "");
    const remoteRegressed = localIsTerminal && remote.status === "loading";
    merged.push({
      ...local,
      ...remote,
      content: remoteRegressed ? local.content : remote.content,
      status: remoteRegressed ? local.status : remote.status,
      openCodeTurn: remoteRegressed ? local.openCodeTurn : remote.openCodeTurn ?? local.openCodeTurn,
      timestamp: remoteRegressed ? local.timestamp : remote.timestamp,
      projectId: remote.projectId || local.projectId || null,
      pipelineProgress: remote.pipelineProgress || local.pipelineProgress || null,
      contextProjectId: remote.contextProjectId || local.contextProjectId || null,
      buildPlanId: remote.buildPlanId || local.buildPlanId || null,
      buildJobId: remote.buildJobId || local.buildJobId || null,
      imagePreview: remote.imagePreview || local.imagePreview || null,
    });
  });

  localMessages.forEach((local) => {
    if (!seen.has(local.id)) merged.push(local);
  });
  return merged.slice(-MAX_PROJECT_CHAT_MESSAGES);
}

function chatMessageIdentityKey(messages: ChatMessage[]) {
  return messages.map((message) => message.id).join("|");
}

function normalizeProjectHistoryRecord(value: any): any | null {
  if (!value || typeof value !== "object") return null;
  const projectId = typeof value.project_id === "string" ? value.project_id.trim() : "";
  if (!projectId) return null;
  const creatorDisplay =
    typeof value.creator_username === "string" && value.creator_username.trim()
      ? value.creator_username.trim()
      : typeof value.creator_display === "string" && value.creator_display.trim()
        ? value.creator_display.trim()
        : "unknown";
  const creatorImageUrl =
    typeof value.creator_image_url === "string" && value.creator_image_url.trim()
      ? value.creator_image_url.trim()
      : typeof value.creatorImageUrl === "string" && value.creatorImageUrl.trim()
        ? value.creatorImageUrl.trim()
        : null;
  return {
    ...value,
    project_id: projectId,
    chat_id: typeof value.chat_id === "string" ? value.chat_id.trim() : "",
    title: typeof value.title === "string" && value.title.trim() ? value.title.trim() : "Untitled project",
    prompt: typeof value.prompt === "string" ? value.prompt : "",
    created_at: typeof value.created_at === "string" && value.created_at ? value.created_at : chatTimestamp(),
    visibility: value.visibility === "private" ? "private" : "public",
    can_chat: Boolean(value.can_chat ?? value.canChat),
    creator_display: creatorDisplay,
    creator_username: creatorDisplay,
    creator_image_url: creatorImageUrl,
    parts_count: Math.max(0, Number(value.parts_count || value.partsCount || 0)),
    save_count: Math.max(0, Number(value.save_count || value.saveCount || 0)),
    remix_count: Math.max(0, Number(value.remix_count || value.remixCount || 0)),
    saved: Boolean(value.saved),
  };
}

function normalizeAgentPipelineStep(value: any): AgentPipelineStep | null {
  if (!value || typeof value !== "object") return null;
  const id = typeof value.id === "string" && value.id.trim() ? value.id.trim() : "";
  const label = typeof value.label === "string" && value.label.trim() ? value.label.trim() : "";
  if (!id || !label) return null;
  return {
    id,
    label,
    agent: typeof value.agent === "string" && value.agent.trim() ? value.agent.trim() : label,
    description: typeof value.description === "string" ? value.description : "",
    duration_ms: Number.isFinite(Number(value.duration_ms)) ? Math.max(1000, Number(value.duration_ms)) : undefined,
    optional: Boolean(value.optional),
  };
}

function normalizeAgentPipelineSteps(value: any): AgentPipelineStep[] {
  const rawSteps = Array.isArray(value?.steps) ? value.steps : Array.isArray(value) ? value : [];
  const steps = rawSteps.map(normalizeAgentPipelineStep).filter(Boolean) as AgentPipelineStep[];
  return steps.length ? steps : defaultAgentPipelineSteps;
}

function normalizeAgentPipelineProgress(value: any): AgentPipelineProgress | null {
  if (!value || typeof value !== "object") return null;
  const steps = normalizeAgentPipelineSteps(value.steps);
  const events = normalizeAgentPipelineEvents(value.events);
  return {
    startedAt: typeof value.startedAt === "string" && value.startedAt ? value.startedAt : chatTimestamp(),
    steps,
    currentStepIndex: Math.min(Math.max(Number(value.currentStepIndex || 0), 0), Math.max(steps.length - 1, 0)),
    estimated: value.estimated !== false,
    synced: Boolean(value.synced),
    jobId: typeof value.jobId === "string" ? value.jobId : null,
    events,
    uiUpdatedAt: typeof value.uiUpdatedAt === "string" && value.uiUpdatedAt ? value.uiUpdatedAt : chatTimestamp(),
  };
}

function normalizeAgentPipelineEvents(value: any): AgentPipelineEvent[] {
  const rawEvents = Array.isArray(value) ? value : [];
  return rawEvents
    .map((event) => {
      if (!event || typeof event !== "object" || typeof event.step_id !== "string") return null;
      return {
        workflow: typeof event.workflow === "string" ? event.workflow : undefined,
        step_id: event.step_id,
        status: typeof event.status === "string" ? event.status : "started",
        agent: typeof event.agent === "string" ? event.agent : undefined,
        label: typeof event.label === "string" ? event.label : undefined,
        description: typeof event.description === "string" ? event.description : undefined,
        observed_at: typeof event.observed_at === "string" ? event.observed_at : undefined,
        details: event.details && typeof event.details === "object" ? event.details : undefined,
      };
    })
    .filter(Boolean) as AgentPipelineEvent[];
}

function stepsForPipelineRun(steps: AgentPipelineStep[], includeImage: boolean) {
  const normalized = steps.length ? steps : defaultAgentPipelineSteps;
  const baseSteps = normalized.filter((step) => !step.optional || includeImage);
  if (includeImage && !baseSteps.some((step) => step.id === optionalImagePipelineStep.id)) {
    return [...baseSteps, optionalImagePipelineStep];
  }
  return baseSteps;
}

function createAgentPipelineProgress(
  steps: AgentPipelineStep[],
  includeImage: boolean,
  startedAt: string = chatTimestamp(),
  jobId: string | null = null
): AgentPipelineProgress {
  return {
    startedAt,
    steps: stepsForPipelineRun(steps, includeImage),
    currentStepIndex: 0,
    estimated: true,
    synced: false,
    jobId,
    events: [],
    uiUpdatedAt: startedAt,
  };
}

function shouldPulsePipelineUi(progress: AgentPipelineProgress, nowMs: number) {
  const lastUiMs = timestampMs(progress.uiUpdatedAt);
  return lastUiMs === null || nowMs - lastUiMs >= PIPELINE_UI_HEARTBEAT_MS;
}

function agentPipelineStepIndex(progress: AgentPipelineProgress, nowMs: number) {
  const startedMs = Date.parse(progress.startedAt);
  const elapsedMs = Math.max(0, nowMs - (Number.isNaN(startedMs) ? nowMs : startedMs));
  let accumulatedMs = 0;
  for (let index = 0; index < progress.steps.length; index += 1) {
    accumulatedMs += progress.steps[index].duration_ms || 5500;
    if (elapsedMs < accumulatedMs) return index;
  }
  return Math.max(0, progress.steps.length - 1);
}

function advanceAgentPipelineProgress(progress: AgentPipelineProgress, nowMs: number): AgentPipelineProgress {
  if (progress.synced || progress.estimated === false) {
    return shouldPulsePipelineUi(progress, nowMs)
      ? { ...progress, uiUpdatedAt: new Date(nowMs).toISOString() }
      : progress;
  }
  const currentStepIndex = agentPipelineStepIndex(progress, nowMs);
  if (currentStepIndex !== progress.currentStepIndex) {
    return { ...progress, currentStepIndex, uiUpdatedAt: new Date(nowMs).toISOString() };
  }
  return shouldPulsePipelineUi(progress, nowMs)
    ? { ...progress, uiUpdatedAt: new Date(nowMs).toISOString() }
    : progress;
}

function pipelineEventCursor(events: AgentPipelineEvent[] | undefined) {
  const normalizedEvents = normalizeAgentPipelineEvents(events);
  const lastEvent = normalizedEvents[normalizedEvents.length - 1];
  return [
    normalizedEvents.length,
    lastEvent?.observed_at || "",
    lastEvent?.step_id || "",
    lastEvent?.status || "",
  ].join(":");
}

function isFailedPipelineStatus(status: any) {
  return String(status || "").toLowerCase().includes("failed");
}

function isCompletedPipelineStatus(status: any) {
  const normalized = String(status || "").toLowerCase();
  return normalized === "completed" || normalized === "provider_response_received";
}

function failedPipelineEvent(events: AgentPipelineEvent[] | undefined) {
  const normalizedEvents = normalizeAgentPipelineEvents(events);
  return [...normalizedEvents].reverse().find((event) => isFailedPipelineStatus(event.status)) || null;
}

function pipelineEventsFromWorkerTask(task: any): AgentPipelineEvent[] {
  if (!Array.isArray(task?.progress)) return [];
  return normalizeAgentPipelineEvents(
    task.progress.map((item: any) => item?.metadata?.pipeline_event).filter(Boolean),
  );
}

function compactDiagnosticText(value: any, limit: number = CHAT_DIAGNOSTIC_CHARACTER_LIMIT) {
  const original = String(value || "").trim();
  if (!original) return "";

  const normalized = original
    .replace(/\r\n/g, "\n")
    .replace(/https:\/\/errors\.pydantic\.dev\/\S+/g, "")
    .replace(/,\s*input_value=(?:'[^']*'|"[^"]*"|[^\]\n]*)/g, "")
    .replace(/,\s*input_type=[^\]\n]+/g, "")
    .replace(/\[type=([^,\]\s]+)[^\]]*\]/g, "[type=$1]");

  const lines = normalized
    .split("\n")
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .filter(Boolean)
    .filter((line) => !/^for further information visit/i.test(line))
    .filter((line) => !/^input_(value|type)=/i.test(line));
  const text = lines.join("\n") || original;

  if (text.length <= limit) return text;
  const clipped = text.slice(0, limit).replace(/\s+\S*$/, "").trimEnd();
  return `${clipped || text.slice(0, limit).trimEnd()}...`;
}

function generationFailureChatMessage(message: string, includeJobsHint = false) {
  const compact = compactDiagnosticText(message);
  const content = compact
    ? /^generation failed\b/i.test(compact)
      ? compact
      : `Generation failed: ${compact}`
    : "Generation failed.";
  return includeJobsHint ? `${content}\nFull diagnostics are available in Jobs.` : content;
}

function jobFailureMessage(job: A2AJob) {
  const event = failedPipelineEvent(job.progress_events);
  const eventDetails = event?.details || {};
  const reason = job.error || eventDetails.error || eventDetails.reason || eventDetails.message;
  if (reason) return String(reason);
  if (event?.label) return `${event.label} failed.`;
  return "Generation failed.";
}

function terminalJobMessagePatch(job: A2AJob, message: ChatMessage): Partial<Omit<ChatMessage, "id">> | null {
  if (message.status !== "loading") return null;
  if (["cancelled", "canceled"].includes(String(job.status || "").toLowerCase())) {
    return {
      content: "Generation stopped by you.",
      status: "cancelled",
    };
  }
  if (job.status === "failed") {
    return {
      content: generationFailureChatMessage(jobFailureMessage(job), true),
      status: "error",
    };
  }
  if (job.status === "succeeded") {
    const title = job.result_summary?.title || "Project";
    const projectId = typeof job.result_summary?.project_id === "string" ? job.result_summary.project_id : null;
    return {
      content: `${title} is ready.`,
      status: "success",
      projectId,
    };
  }
  return null;
}

function patchChangesMessage(message: ChatMessage, patch: Partial<Omit<ChatMessage, "id">> | null) {
  if (!patch) return false;
  return Object.entries(patch).some(([key, value]) => (message as any)[key] !== value);
}

function sameAgentPipelineProgress(left: AgentPipelineProgress | null | undefined, right: AgentPipelineProgress | null | undefined) {
  if (left === right) return true;
  if (!left || !right) return false;
  return (
    left.currentStepIndex === right.currentStepIndex &&
    left.estimated === right.estimated &&
    Boolean(left.synced) === Boolean(right.synced) &&
    (left.jobId || null) === (right.jobId || null) &&
    pipelineEventCursor(left.events) === pipelineEventCursor(right.events)
  );
}

function progressFromJobEvents(
  job: A2AJob | null,
  fallback: AgentPipelineProgress,
  includeImage: boolean
): AgentPipelineProgress | null {
  const events = normalizeAgentPipelineEvents(job?.progress_events);
  if (!events.length) return null;
  const previousEvents = normalizeAgentPipelineEvents(fallback.events);
  if (fallback.synced && previousEvents.length > events.length) return fallback;
  const steps = stepsForPipelineRun(fallback.steps, includeImage);
  const indexByStep = new Map(steps.map((step, index) => [step.id, index]));
  let currentStepIndex = fallback.currentStepIndex;
  for (const event of events) {
    const eventIndex = indexByStep.get(event.step_id);
    if (eventIndex === undefined) continue;
    if (isCompletedPipelineStatus(event.status) || event.status === "skipped") {
      currentStepIndex = Math.min(eventIndex + 1, Math.max(steps.length - 1, 0));
    } else {
      currentStepIndex = eventIndex;
    }
  }
  if (fallback.synced && previousEvents.length >= events.length && currentStepIndex < fallback.currentStepIndex) {
    return fallback;
  }
  return {
    ...fallback,
    startedAt: job?.started_at || fallback.startedAt,
    steps,
    currentStepIndex,
    estimated: false,
    synced: true,
    jobId: job?.job_id || fallback.jobId || null,
    events,
    uiUpdatedAt: chatTimestamp(),
  };
}

function mergeMessagePipelineProgressFromJob(
  message: ChatMessage,
  job: A2AJob,
  seedProgress: AgentPipelineProgress,
  includeImage: boolean
) {
  const nextProgress = progressFromJobEvents(job, message.pipelineProgress || seedProgress, includeImage);
  const terminalPatch = terminalJobMessagePatch(job, message);
  const progressChanged = Boolean(nextProgress && !sameAgentPipelineProgress(message.pipelineProgress, nextProgress));
  const patchChanged = patchChangesMessage(message, terminalPatch);
  if (!progressChanged && !patchChanged) return message;
  return {
    ...message,
    ...(terminalPatch || {}),
    pipelineProgress: nextProgress || message.pipelineProgress || null,
    timestamp: chatTimestamp(),
  };
}

function progressIncludesImageStep(progress: AgentPipelineProgress | null | undefined) {
  return Boolean(progress?.steps?.some((step) => step.id === optionalImagePipelineStep.id || step.optional));
}

function mergeMessagesWithJobs(
  messages: ChatMessage[],
  jobsById: Map<string, A2AJob>,
  includeImageDefault: boolean
) {
  let changed = false;
  const nextMessages = messages.map((message) => {
    const jobId = message.pipelineProgress?.jobId;
    if (!jobId) return message;
    const job = jobsById.get(jobId);
    if (!job || !message.pipelineProgress) return message;
    const includeImage = includeImageDefault || progressIncludesImageStep(message.pipelineProgress);
    const nextMessage = mergeMessagePipelineProgressFromJob(message, job, message.pipelineProgress, includeImage);
    if (nextMessage !== message) changed = true;
    return nextMessage;
  });
  return changed ? nextMessages : messages;
}

function advancePipelineMessages(messages: ChatMessage[], nowMs: number) {
  let changed = false;
  const nextMessages = messages.map((message) => {
    if (message.status !== "loading" || !message.pipelineProgress) return message;
    const nextProgress = advanceAgentPipelineProgress(message.pipelineProgress, nowMs);
    if (nextProgress === message.pipelineProgress) return message;
    changed = true;
    return { ...message, pipelineProgress: nextProgress };
  });
  return changed ? nextMessages : messages;
}

function pipelineEventTimestampMs(event: AgentPipelineEvent | null | undefined): number | null {
  return timestampMs(event?.observed_at);
}

function latestPipelineEvent(events: AgentPipelineEvent[]) {
  const normalizedEvents = normalizeAgentPipelineEvents(events);
  return normalizedEvents[normalizedEvents.length - 1] || null;
}

function pipelineStepForEvent(progress: AgentPipelineProgress, event: AgentPipelineEvent | null | undefined) {
  if (!event) return null;
  return progress.steps.find((step) => step.id === event.step_id) || null;
}

function activePipelineStep(progress: AgentPipelineProgress) {
  const events = normalizeAgentPipelineEvents(progress.events);
  const lastEvent = latestPipelineEvent(events);
  const stepFromEvent = pipelineStepForEvent(progress, lastEvent);
  if (lastEvent && !isCompletedPipelineStatus(lastEvent.status) && lastEvent.status !== "skipped") {
    return stepFromEvent || progress.steps[progress.currentStepIndex] || progress.steps[0] || null;
  }
  return progress.steps[progress.currentStepIndex] || stepFromEvent || progress.steps[0] || null;
}

function completedPipelineStepCount(progress: AgentPipelineProgress) {
  const completed = new Set<string>();
  normalizeAgentPipelineEvents(progress.events).forEach((event) => {
    if (isCompletedPipelineStatus(event.status) || event.status === "skipped") completed.add(event.step_id);
    if (isFailedPipelineStatus(event.status)) completed.delete(event.step_id);
  });
  if (completed.size) return completed.size;
  return progress.estimated ? Math.max(0, progress.currentStepIndex) : 0;
}

function pipelineStepStatus(
  progress: AgentPipelineProgress,
  step: AgentPipelineStep,
  activeStepId: string | null,
  isRunning: boolean = false
) {
  const events = normalizeAgentPipelineEvents(progress.events);
  const stepEvents = events.filter((event) => event.step_id === step.id);
  const lastStepEvent = stepEvents[stepEvents.length - 1];
  // A failed attempt may be followed by a retry while the job remains active.
  // Keep the current step neutral until the job itself reaches a failed state.
  if (isRunning && activeStepId === step.id) return "active";
  if (isFailedPipelineStatus(lastStepEvent?.status)) return "failed";
  if (lastStepEvent?.status === "skipped") return "skipped";
  if (isCompletedPipelineStatus(lastStepEvent?.status)) return "completed";
  if (activeStepId === step.id) return "active";
  return "pending";
}

function formatPipelineAge(value?: string | null, nowMs: number = Date.now()) {
  const ms = timestampMs(value);
  if (ms === null) return "-";
  return formatDurationSeconds(Math.max(1, Math.round((nowMs - ms) / 1000)));
}

function formatPipelineDetails(details: Record<string, any> | undefined) {
  if (!details || typeof details !== "object") return "";
  return Object.entries(details)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .slice(0, 3)
    .map(([key, value]) => {
      const rawText = typeof value === "string" ? value : JSON.stringify(value);
      const text = typeof rawText === "string" ? rawText : String(value);
      return `${key.replace(/_/g, " ")}: ${text.length > 80 ? `${text.slice(0, 77)}...` : text}`;
    })
    .join(" / ");
}

function PipelineStepDot({ status }: { status: string }) {
  const tone =
    status === "completed"
      ? "border-emerald-400 bg-emerald-400"
      : status === "failed"
        ? "border-rose-400 bg-rose-400"
        : status === "skipped"
          ? "border-slate-600 bg-slate-800"
          : status === "active"
            ? "border-cyan-300 bg-cyan-300"
            : "border-slate-700 bg-black";
  return <span className={`h-2.5 w-2.5 shrink-0 border ${tone}`} />;
}

function chatThreadStorageKey(chatId: string, scope = "local") {
  return scope === "local"
    ? `${CHAT_THREAD_STORAGE_PREFIX}${chatId}`
    : `${CHAT_THREAD_STORAGE_PREFIX}${encodeURIComponent(scope)}.${chatId}`;
}

function legacyProjectChatStorageKey(projectId: string) {
  return `${LEGACY_PROJECT_CHAT_STORAGE_PREFIX}${projectId}`;
}

function readStoredChatThread(chatId: string, legacyProjectId?: string | null, scope = "local"): ChatMessage[] {
  if (typeof window === "undefined" || !chatId) return [];
  try {
    const raw = window.localStorage.getItem(chatThreadStorageKey(chatId, scope))
      || (scope === "local" && legacyProjectId ? window.localStorage.getItem(legacyProjectChatStorageKey(legacyProjectId)) : null);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(normalizeChatMessage).filter(Boolean) as ChatMessage[] : [];
  } catch {
    return [];
  }
}

function writeStoredChatThread(chatId: string, messages: ChatMessage[], scope = "local") {
  if (typeof window === "undefined" || !chatId) return;
  try {
    window.localStorage.setItem(chatThreadStorageKey(chatId, scope), JSON.stringify(messages.slice(-MAX_PROJECT_CHAT_MESSAGES)));
  } catch {
    // Local chat history is best-effort.
  }
}

function normalizeChatListItem(value: any): ChatListItem | null {
  if (!value || typeof value !== "object") return null;
  const chatId = typeof value.chatId === "string" ? value.chatId.trim() : "";
  if (!chatId) return null;
  return {
    chatId,
    title: typeof value.title === "string" && value.title.trim() ? value.title.trim() : NEW_PROJECT_TITLE,
    projectId: typeof value.projectId === "string" ? value.projectId : "",
    createdAt: typeof value.createdAt === "string" && value.createdAt ? value.createdAt : chatTimestamp(),
    projectCount: Math.max(0, Number(value.projectCount || 0)),
  };
}

function chatIndexStorageKey(scope = "local") {
  return scope === "local"
    ? CHAT_INDEX_STORAGE_KEY
    : `${CHAT_INDEX_STORAGE_KEY}.${encodeURIComponent(scope)}`;
}

function readStoredChatIndex(scope = "local"): ChatListItem[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(chatIndexStorageKey(scope));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(normalizeChatListItem).filter(Boolean) as ChatListItem[] : [];
  } catch {
    return [];
  }
}

function writeStoredChatIndex(items: ChatListItem[], scope = "local") {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(chatIndexStorageKey(scope), JSON.stringify(items.slice(0, MAX_CHAT_INDEX_ITEMS)));
  } catch {
    // Local chat index is best-effort.
  }
}

function pinnedChatsStorageKey(scope = "local") {
  return scope === "local"
    ? PINNED_CHATS_STORAGE_KEY
    : `${PINNED_CHATS_STORAGE_KEY}.${encodeURIComponent(scope)}`;
}

function readPinnedChatIds(scope = "local"): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(pinnedChatsStorageKey(scope));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((value: unknown): value is string => typeof value === "string" && Boolean(value.trim()))
      : [];
  } catch {
    return [];
  }
}

function writePinnedChatIds(ids: Iterable<string>, scope = "local") {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(pinnedChatsStorageKey(scope), JSON.stringify(Array.from(ids)));
  } catch {
    // Pinned chat ids are best-effort.
  }
}

function removeStoredChatThread(chatId: string, scope = "local") {
  if (typeof window === "undefined" || !chatId) return;
  try {
    window.localStorage.removeItem(chatThreadStorageKey(chatId, scope));
  } catch {
    // Local chat history is best-effort.
  }
}

function chatListItemTime(value: string | null | undefined): number {
  const normalizedValue = value?.trim() || "";
  // Project creation timestamps have historically been emitted as UTC without a
  // timezone suffix. Treat them as UTC so they compare correctly with chat
  // updated_at values, which include "Z".
  const timestamp = Date.parse(
    normalizedValue && !/(?:z|[+-]\d{2}:?\d{2})$/i.test(normalizedValue)
      ? `${normalizedValue}Z`
      : normalizedValue
  );
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function latestChatListItemDate(
  left: string | null | undefined,
  right: string | null | undefined
): string | null {
  return chatListItemTime(right) > chatListItemTime(left) ? right || null : left || right || null;
}

function sortChatListItems(items: ChatListItem[]): ChatListItem[] {
  return [...items].sort((left, right) => {
    if (Boolean(left.pinned) !== Boolean(right.pinned)) return left.pinned ? -1 : 1;
    return chatListItemTime(right.createdAt) - chatListItemTime(left.createdAt);
  });
}

function upsertChatListItem(items: ChatListItem[], item: Partial<ChatListItem> & { chatId: string }): ChatListItem[] {
  const existing = items.find((current) => current.chatId === item.chatId);
  const incomingTitle = item.title?.trim() || "";
  const existingTitle = existing?.title?.trim() || "";
  const keepExistingTitle =
    incomingTitle === NEW_PROJECT_TITLE && Boolean(existingTitle) && existingTitle !== NEW_PROJECT_TITLE;
  const incomingProjectId = typeof item.projectId === "string" ? item.projectId : undefined;
  const nextItem: ChatListItem = {
    chatId: item.chatId,
    title: keepExistingTitle ? existingTitle : incomingTitle || existingTitle || NEW_PROJECT_TITLE,
    projectId: incomingProjectId === undefined ? existing?.projectId || "" : incomingProjectId || existing?.projectId || "",
    createdAt: item.createdAt || existing?.createdAt || chatTimestamp(),
    projectCount: Math.max(item.projectCount ?? existing?.projectCount ?? 0, 0),
  };
  return sortChatListItems([nextItem, ...items.filter((current) => current.chatId !== item.chatId)])
    .slice(0, MAX_CHAT_INDEX_ITEMS);
}

function initialProjectChatMessages(projectId: string, title: string): ChatMessage[] {
  const messages: ChatMessage[] = [];
  messages.push({
    id: `project-context:${projectId}`,
    role: "assistant",
    content: `${title || "Project"} is the active project for this chat.`,
    status: "success",
    timestamp: chatTimestamp(),
    projectId,
  });
  return messages;
}

function missingProjectNotice(projectId: string) {
  return `This chat pointed at project ${projectId}, but that project is no longer available in the project database. The chat history is still here; generate again to create a new project.`;
}

function messagesWithoutMissingProject(messages: ChatMessage[], projectId: string): ChatMessage[] {
  const notice = missingProjectNotice(projectId);
  const normalizedMessages = messages.map((message) =>
    message.projectId === projectId ? { ...message, projectId: null } : message
  );
  if (normalizedMessages.some((message) => message.role === "assistant" && message.content === notice)) {
    return normalizedMessages;
  }
  const noticeMessage: ChatMessage = {
    id: newChatMessageId(),
    role: "assistant",
    content: notice,
    status: "error",
    timestamp: chatTimestamp(),
    projectId: null,
  };
  return [...normalizedMessages, noticeMessage].slice(-MAX_PROJECT_CHAT_MESSAGES);
}

function humanContextQuestionsForPrompt(promptText: string, hasImage = false): HumanContextQuestion[] {
  const lower = promptText.toLowerCase();
  const physicalFormQuestion: HumanContextQuestion = {
    id: "physical_form",
    label: "Shape / Form Factor",
    question: "What overall shape, silhouette, or form factor should the system have?",
    placeholder: hasImage
      ? "Example: preserve the reference silhouette, but make it handheld with a curved grip..."
      : "Example: curved handheld pod, cylindrical wearable, folded frame, or exposed open assembly...",
    suggestions: ["Curved handheld", "Cylindrical / radial", "Open frame"],
  };
  if (/(lab[-\s]?on[-\s]?a[-\s]?chip|microfluid|assay|cartridge|diagnostic|reagent|sample)/.test(lower)) {
    return [
      {
        id: "sample_assay",
        label: "Sample / Assay",
        question: "What sample, analyte, or assay workflow should this support?",
        placeholder: "Example: water sample, colorimetric nitrate assay, 3 reagent chambers...",
        suggestions: ["Water quality", "Colorimetric assay", "Fluorescence readout"],
      },
      physicalFormQuestion,
      {
        id: "instrumentation",
        label: "Reader / Detection",
        question: "What detection and control method should the reader use?",
        placeholder: "Example: LED + photodiode absorbance, heater, pressure sensor, peristaltic pump...",
        suggestions: ["Optical absorbance", "Fluorescence", "Pressure-driven flow"],
      },
    ];
  }

  if (/(tent|deploy|self[-\s]?assembl|fold|frame|shelter|weatherproof|structure)/.test(lower)) {
    return [
      {
        id: "environment",
        label: "Environment",
        question: "Where will this operate, and what weather or load should it survive?",
        placeholder: "Example: camping rain/wind, sandy soil, one-person field setup, 35 mph gust target...",
        suggestions: ["Rain and wind", "Field work", "Portable camping"],
      },
      physicalFormQuestion,
      {
        id: "motion_power",
        label: "Motion / Power",
        question: "How should deployment be powered and limited for safety?",
        placeholder: "Example: 12V battery, low-force servos, clutch release, manual crank fallback...",
        suggestions: ["12V battery", "Low-force actuators", "Manual release"],
      },
    ];
  }

  if (/(wire|wiring|schematic|pcb|sensor|relay|motor|driver|esp32|arduino|pin|gpio)/.test(lower)) {
    return [
      {
        id: "controller_modules",
        label: "Controller / Modules",
        question: "Which controller and major modules should be treated as fixed?",
        placeholder: "Example: ESP32-S3, SSD1306 OLED, SHT41, 5V relay module...",
        suggestions: ["ESP32", "Arduino", "Use generated choice"],
      },
      physicalFormQuestion,
      {
        id: "power",
        label: "Power",
        question: "What power rails, battery, or adapter constraints matter?",
        placeholder: "Example: USB-C 5V only, 3S LiPo, no mains, separate motor rail...",
        suggestions: ["USB-C 5V", "Battery powered", "No mains"],
      },
    ];
  }

  return [
    {
      id: "use_case",
      label: "Use Case",
      question: "Who uses it, and where does it operate?",
      placeholder: "Example: bench prototype, outdoor field tool, wearable, classroom demo...",
      suggestions: ["Bench prototype", "Field tool", "Consumer device"],
    },
    physicalFormQuestion,
    {
      id: "constraints",
      label: "Constraints",
      question: "What hard constraints should the design preserve?",
      placeholder: "Example: USB-C only, under $100, waterproof, no enclosure, safe low voltage...",
      suggestions: ["Low voltage", "Low cost", "Weatherproof"],
    },
  ];
}

function normalizeHumanContextQuestions(value: any): HumanContextQuestion[] {
  const rawQuestions = Array.isArray(value?.questions) ? value.questions : Array.isArray(value) ? value : [];
  return rawQuestions
    .map((question: any): HumanContextQuestion | null => {
      if (!question || typeof question !== "object") return null;
      const id = typeof question.id === "string" && question.id.trim() ? question.id.trim() : "";
      const label = typeof question.label === "string" && question.label.trim() ? question.label.trim() : id;
      const text = typeof question.question === "string" && question.question.trim() ? question.question.trim() : "";
      if (!id || !label || !text) return null;
      return {
        id,
        label,
        question: text,
        placeholder: typeof question.placeholder === "string" ? question.placeholder : "",
        suggestions: Array.isArray(question.suggestions)
          ? question.suggestions.filter((suggestion: any) => typeof suggestion === "string" && suggestion.trim()).slice(0, 4)
          : [],
      };
    })
    .filter((question: HumanContextQuestion | null): question is HumanContextQuestion => Boolean(question));
}

async function requestHumanContextQuestions(promptText: string, workflow: string, hasImage: boolean, signal?: AbortSignal) {
  try {
    const res = await fetch(`${API_URL}/clarifying-questions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal,
      body: JSON.stringify({
        prompt: promptText,
        workflow,
        has_image: hasImage,
        max_questions: 3,
        force: true,
      }),
    });
    if (!res.ok) throw new Error(await readApiErrorMessage(res));
    const data = await res.json();
    const questions = normalizeHumanContextQuestions(data);
    return {
      shouldAsk: Boolean(data?.should_ask) && questions.length > 0,
      reason: typeof data?.reason === "string" ? data.reason : "",
      questions,
    };
  } catch (error) {
    if (signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw error;
    console.warn("Context Clarifier Agent unavailable; using local fallback questions.", error);
    const questions = humanContextQuestionsForPrompt(promptText, hasImage);
    return {
      shouldAsk: questions.length > 0,
      reason: "Context Clarifier Agent is using local fallback questions.",
      questions,
    };
  }
}

function humanContextPromptSection(context: PendingHumanContext, finalNotes: string) {
  const lines = context.questions.map((question) => {
    const answer = (context.answers[question.id] || "").trim() || "not specified";
    return `- ${question.label}: ${answer}`;
  });
  if (finalNotes.trim()) {
    lines.push(`- Additional human notes: ${finalNotes.trim()}`);
  }
  return [
    context.basePrompt,
    "",
    "HUMAN-IN-THE-LOOP CONTEXT:",
    ...lines,
    "",
    "Treat this human context as explicit project requirements. If something is still unspecified, say so in project docs instead of inventing hidden constraints.",
  ].join("\n");
}

function humanContextChatSummary(context: PendingHumanContext, finalNotes: string) {
  const answered = context.questions
    .map((question) => {
      const answer = (context.answers[question.id] || "").trim();
      return answer ? `${question.label}: ${answer}` : null;
    })
    .filter(Boolean);
  if (finalNotes.trim()) answered.push(`Additional notes: ${finalNotes.trim()}`);
  return answered.length ? `Context added:\n${answered.map((item) => `- ${item}`).join("\n")}` : "Build with no extra human context.";
}

function validateGenerationInput(value: string, hasImage: boolean) {
  const promptText = value.trim();
  if (!promptText) {
    return {
      isValid: hasImage,
      message: hasImage ? null : "Provide a prompt or reference image.",
    };
  }

  return {
    isValid: true,
    message: null,
  };
}

type ApiErrorDetails = {
  message: string;
  code?: string;
  reason?: string;
  provider?: string;
  model?: string;
  job_id?: string;
  debug?: Record<string, any>;
};

function normalizeApiErrorDetails(value: any, fallback: string): ApiErrorDetails {
  if (typeof value === "string" && value.trim()) {
    return { message: value.trim() };
  }

  if (Array.isArray(value)) {
    const messages = value
      .map((item: any) => item?.msg || item?.message || item?.detail)
      .filter(Boolean);
    if (messages.length) return { message: messages.join("; ") };
  }

  if (value && typeof value === "object") {
    const message =
      typeof value.message === "string"
        ? value.message
        : typeof value.detail === "string"
          ? value.detail
          : fallback;
    const reason = typeof value.reason === "string" ? value.reason : undefined;
    const provider = typeof value.provider === "string" ? value.provider : undefined;
    const model = typeof value.model === "string" ? value.model : undefined;
    return {
      message,
      code: typeof value.code === "string" ? value.code : undefined,
      reason,
      provider,
      model,
      job_id: typeof value.job_id === "string" ? value.job_id : undefined,
      debug: value.debug && typeof value.debug === "object" ? value.debug : undefined,
    };
  }

  return { message: fallback };
}

async function readApiError(response: Response): Promise<ApiErrorDetails> {
  const fallback = `Server returned ${response.status}`;
  try {
    const body = await response.json();
    if (body?.detail !== undefined) return normalizeApiErrorDetails(body.detail, fallback);
    if (body?.message !== undefined) return normalizeApiErrorDetails(body.message, fallback);
    if (body?.error !== undefined) return normalizeApiErrorDetails(body.error, fallback);
  } catch {
    // Fall through to a generic message.
  }

  return { message: fallback };
}

async function readApiErrorMessage(response: Response) {
  return (await readApiError(response)).message;
}

const communityProjects = [
  {
    title: "Portable device",
    description: "Reference design for a compact handheld product with display, controls, and enclosure notes.",
    file: "pocket_mp3_player.json",
  },
  {
    title: "Monitoring kit",
    description: "General-purpose sensing and control example with power, wiring, and enclosure guidance.",
    file: "plant_watering.json",
  },
  {
    title: "Control module",
    description: "Compact controller example with display, sensor, and validated power rails.",
    file: "smart_thermostat.json",
  },
  {
    title: "Two meshing gears",
    description: "20/40-tooth OpenCAD gears with real meshes and synchronized 2:1 rotation.",
    file: "spur_gear_pair.json",
  },
  {
    title: "Print-in-place hinge",
    description: "Two-body captive hinge benchmark with explicit radial/axial clearance and OpenCAD motion.",
    file: "print_in_place_hinge.json",
  },
  {
    title: "Monolithic flexure",
    description: "Single-solid compliant hinge benchmark with an explicitly approximate deformation preview.",
    file: "monolithic_flexure_hinge.json",
  },
];

type ChatRouteTransition = {
  chatId: string;
  title: string;
  projectId: string;
  error?: string | null;
};

type VideoGenerationConfig = {
  configured: boolean | null;
  reason: string | null;
};

type ImageGenerationConfig = {
  configured: boolean | null;
  requestCapable: boolean | null;
  provider: string | null;
  reason: string | null;
};

type ProviderSetupState = {
  llmRequired: boolean;
  imageRequired: boolean;
};

const workspaceTabs = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "bom", label: "Billing Materials", icon: ClipboardList },
  { id: "mechanical", label: "Mechanical", icon: Cuboid },
  { id: "cad", label: "CAD", icon: Box },
  { id: "schematic", label: "Electrical", icon: CircuitBoard },
  { id: "assembly", label: "Documentation", icon: BookOpen },
  { id: "exports", label: "Exports", icon: Download },
  { id: "video", label: "Media", icon: Clapperboard },
];

const workspaceTabNamespaces: Record<string, string> = {
  overview: "product.overview",
  bom: "product.bom",
  mechanical: "product.mech",
  cad: "product.mech",
  schematic: "product.electrical",
  assembly: "project.docs",
  video: "product.visuals.video",
  jobs: "project.history.jobs",
  logs: "project.runtime.logs",
};

function normalizeTab(tab: string | null) {
  if (!tab) return null;
  const aliases: Record<string, string> = {
    chat: "overview",
    concept: "overview",
    info: "overview",
    image: "overview",
    mech: "mechanical",
    opencad: "cad",
    model: "cad",
    wire: "schematic",
    electrical: "schematic",
    docs: "assembly",
    documentation: "assembly",
    billing: "bom",
    materials: "bom",
    media: "video",
  };
  const normalized = aliases[tab] || tab;
  return workspaceTabs.some((item) => item.id === normalized) ? normalized : null;
}

function workspaceTabMeta(tab: string | null) {
  const normalized = normalizeTab(tab);
  return workspaceTabs.find((item) => item.id === normalized) || workspaceTabs[0];
}

function workspaceNamespaceForTab(tab: string | null) {
  const meta = workspaceTabMeta(tab);
  return workspaceTabNamespaces[meta.id] || meta.id;
}

function withProjectResponseMetadata(ir: any, response: any) {
  if (!ir) return ir;
  const timingMetadata = generationTimingMetadataFromJob(response?.job);
  return {
    ...ir,
    assembly_metadata: {
      ...(ir.assembly_metadata || {}),
      project_id: ir.assembly_metadata?.project_id || response?.project_id,
      chat_id: ir.assembly_metadata?.chat_id || response?.chat_id,
      can_chat: Boolean(response?.can_chat ?? response?.canChat ?? ir.assembly_metadata?.can_chat ?? ir.assembly_metadata?.canChat),
      frontend_job_id: ir.assembly_metadata?.frontend_job_id || response?.job_id,
      source_prompt: ir.assembly_metadata?.source_prompt || response?.prompt,
      canonical_revision_id: response?.revision_id || ir.assembly_metadata?.canonical_revision_id,
      ...timingMetadata,
    },
  };
}

function timestampMs(value: any): number | null {
  if (typeof value !== "string" || !value.trim()) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function durationSecondsBetween(startValue: any, endValue: any): number | null {
  const start = timestampMs(startValue);
  const end = timestampMs(endValue);
  if (start === null || end === null || end < start) return null;
  return Math.max(1, Math.round((end - start) / 1000));
}

function generationTimingMetadataFromJob(job: A2AJob | null | undefined): Record<string, any> {
  const seconds = durationSecondsBetween(job?.started_at, job?.completed_at);
  if (seconds === null) return {};
  return {
    total_generation_time_seconds: seconds,
    total_generation_started_at: job?.started_at || null,
    total_generation_completed_at: job?.completed_at || null,
  };
}

function formatDurationSeconds(value: any) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "-";
  const totalSeconds = Math.max(1, Math.round(seconds));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours) return `${hours}h ${minutes}m ${remainingSeconds}s`;
  if (minutes) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

function projectIdFromIR(ir: any) {
  return ir?.assembly_metadata?.project_id || null;
}

function chatIdFromIR(ir: any) {
  return ir?.assembly_metadata?.chat_id || null;
}

function canChatWithProjectIR(ir: any) {
  return Boolean(ir?.assembly_metadata?.can_chat ?? ir?.assembly_metadata?.canChat);
}

function chatIdFromJob(job: A2AJob) {
  const rawChatId = job.payload?.chat_id || job.result_summary?.chat_id;
  return typeof rawChatId === "string" ? rawChatId.trim() : "";
}

function projectRoute(projectId: string) {
  return `/project/${encodeURIComponent(projectId)}`;
}

function chatRoute(chatId: string) {
  return `/chat/${encodeURIComponent(chatId)}`;
}

function safeDecodeProjectId(projectId: string) {
  try {
    return decodeURIComponent(projectId);
  } catch {
    return projectId;
  }
}

function safeDecodeChatId(chatId: string) {
  try {
    return decodeURIComponent(chatId);
  } catch {
    return chatId;
  }
}

type HomeProps = {
  routeProjectId?: string | null;
  routeChatId?: string | null;
  showDeveloperTools?: boolean;
  homeView?: "chat" | "projects" | "my-projects" | "jobs" | "logs" | "settings" | "about";
};

export function FormaWorkspace({
  routeProjectId = null,
  routeChatId = null,
  showDeveloperTools = DEFAULT_SHOW_DEVELOPER_TOOLS,
  homeView = "chat",
}: HomeProps = {}) {
  const router = useRouter();
  const pathname = usePathname();
  const pathnameChatId = pathname.match(/^\/chat\/([^/]+)\/?$/)?.[1] || null;
  const pathnameProjectId = pathname.match(/^\/project\/([^/]+)\/?$/)?.[1] || null;
  const currentRouteChatId = pathnameChatId || (pathname === null ? routeChatId : null);
  const currentRouteProjectId = pathnameProjectId || (pathname === null ? routeProjectId : null);
  const {
    authRequired,
    getToken,
    identityKey: authIdentityKey,
    isLoaded: authLoaded,
    isSignedIn,
    openSignIn,
    userImageUrl,
  } = useFormaAuth();
  const chatStorageScope = authRequired ? `identity:${authIdentityKey}` : "local";
  const chatStorageScopeRef = useRef(chatStorageScope);
  useLayoutEffect(() => { chatStorageScopeRef.current = chatStorageScope; }, [chatStorageScope]);
  const galleryIdentityKey = JSON.stringify([API_URL, authRequired, authIdentityKey, Boolean(isSignedIn)]);
  const [prompt, setPrompt] = useState("");
  const [activeChatId, setActiveChatId] = useState(() => currentRouteChatId ? safeDecodeChatId(currentRouteChatId) : newBuildChatId());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [pendingHumanContext, setPendingHumanContext] = useState<PendingHumanContext | null>(null);
  const contextProjectIdsRef = useRef<Record<string, string>>({});
  const contextBuildWatchersRef = useRef<Set<string>>(new Set());
  const [contextWorkflowStates, setContextWorkflowStates] = useState<Record<string, string>>({});
  const [contextBuildStarting, setContextBuildStarting] = useState(false);
  const [resettingBuildMessageId, setResettingBuildMessageId] = useState<string | null>(null);
  const [contextSubmitting, setContextSubmitting] = useState(false);
  const [chatThreads, setChatThreads] = useState<Record<string, ChatMessage[]>>({});
  const [liveChatMessageIds, setLiveChatMessageIds] = useState<Set<string>>(() => new Set());
  useEffect(() => setLiveChatMessageIds(new Set()), [authIdentityKey]);
  const [projectChatInput, setProjectChatInput] = useState("");
  const [workspaceLoading, setIsLoading] = useState(false);
  const [generationRuns, setGenerationRuns] = useState<Record<string, ActiveGenerationState>>({});
  const activeGeneration = generationRuns[activeChatId] || null;
  const isLoading = workspaceLoading || Boolean(activeGeneration);
  const hasGenerationRuns = Object.keys(generationRuns).length > 0;
  const [activeTab, setActiveTab] = useState("overview");
  const [projectIR, setProjectIR] = useState<any>(null);
  const currentCadModel = projectCadModel(projectIR);
  const currentCadDescriptor = resolveCadModel(currentCadModel);
  const hasRenderableCadModel = Boolean(currentCadDescriptor && currentCadDescriptor.kind !== "unsupported");
  const [projectHistory, setProjectHistory] = useState<any[]>([]);
  const [myProjectHistory, setMyProjectHistory] = useState<any[]>([]);
  const [projectHistoryPage, setProjectHistoryPage] = useState(0);
  const [myProjectHistoryPage, setMyProjectHistoryPage] = useState(0);
  const [projectHistoryTotal, setProjectHistoryTotal] = useState(0);
  const [myProjectHistoryTotal, setMyProjectHistoryTotal] = useState(0);
  const [projectHistoryLoaded, setProjectHistoryLoaded] = useState(false);
  const [projectHistoryKey, setProjectHistoryKey] = useState<string | null>(null);
  const [projectHistoryError, setProjectHistoryError] = useState<Error | null>(null);
  const projectPageCacheRef = useRef(new GalleryPageCache<{ items: any[]; total: number }>());
  const projectHistoryAbortRef = useRef<AbortController | null>(null);
  const galleryImageRequestsRef = useRef(new GalleryImageRequests<ProjectImageCandidate | null>());
  const [myProjectHistoryLoaded, setMyProjectHistoryLoaded] = useState(false);
  const [myProjectHistoryError, setMyProjectHistoryError] = useState<Error | null>(null);
  const [projectSearchInput, setProjectSearchInput] = useState("");
  const [projectSearchQuery, setProjectSearchQuery] = useState("");
  const [myProjectSearchInput, setMyProjectSearchInput] = useState("");
  const [myProjectSearchQuery, setMyProjectSearchQuery] = useState("");
  const [localChatItems, setLocalChatItems] = useState<ChatListItem[]>([]);
  const [privateChatItems, setPrivateChatItems] = useState<ChatListItem[]>([]);
  const [privateChatsLoaded, setPrivateChatsLoaded] = useState(false);
  const [chatIndexLoaded, setChatIndexLoaded] = useState(false);
  const [sessionChatItems, setSessionChatItems] = useState<ChatListItem[]>([]);
  const [pinnedChatIds, setPinnedChatIds] = useState<Set<string>>(new Set());
  const [galleryImageState, setGalleryImageState] = useState<{
    scope: string; images: Record<string, ProjectImageCandidate | null>;
  }>({ scope: "", images: {} });
  const [visibleProjectGalleryIds, setVisibleProjectGalleryIds] = useState<string[]>([]);
  const [routeProjectError, setRouteProjectError] = useState<string | null>(null);
  const [pendingProjectDeletion, setPendingProjectDeletion] = useState<PendingProjectDeletion | null>(null);
  const [deletionAcknowledged, setDeletionAcknowledged] = useState(false);
  const [contributeDeletedProject, setContributeDeletedProject] = useState(false);
  const [projectDeletionBusy, setProjectDeletionBusy] = useState(false);
  const [projectDeletionError, setProjectDeletionError] = useState<string | null>(null);
  const [chatRouteTransition, setChatRouteTransition] = useState<ChatRouteTransition | null>(() => (
    currentRouteChatId && !currentRouteProjectId
      ? {
          chatId: safeDecodeChatId(currentRouteChatId),
          title: "Opening chat",
          projectId: "",
          error: null,
        }
      : null
  ));
  const [serverStatus, setServerStatus] = useState<"connected" | "disconnected">(
    () => lastKnownServerStatus || "disconnected"
  );
  const [authSecurityError, setAuthSecurityError] = useState(false);
  const [statusClockMs, setStatusClockMs] = useState(() => Date.now());
  const [deliveredSignal, setDeliveredSignal] = useState(false);
  const [selectedImage, setSelectedImage] = useState<string | null>(null);
  const [selectedImageSource, setSelectedImageSource] = useState<"upload" | "clipboard">("upload");
  const [selectedDocument, setSelectedDocument] = useState<{
    name: string;
    mediaType: "application/pdf";
    dataUrl: string;
  } | null>(null);
  const [generationInputNotice, setGenerationInputNotice] = useState<string | null>(null);
  const [hostedChatEnabled, setHostedChatEnabled] = useState(DEFAULT_HOSTED_CHAT_ENABLED);
  const [runtimeConfigState, setRuntimeConfigState] = useState<{ identityKey: string; status: ChatAccessLoadState }>({
    identityKey: authIdentityKey,
    status: "loading",
  });
  const chatAccessState = (authRequired && !authLoaded) || runtimeConfigState.identityKey !== authIdentityKey
    ? "loading"
    : runtimeConfigState.status;
  const [videoGenerationConfig, setVideoGenerationConfig] = useState<VideoGenerationConfig>({
    configured: null,
    reason: null,
  });
  const [videoSelfCorrectionConfig, setVideoSelfCorrectionConfig] = useState<VideoGenerationConfig>({
    configured: null,
    reason: null,
  });
  const [imageGenerationConfig, setImageGenerationConfig] = useState<ImageGenerationConfig>({
    configured: null,
    requestCapable: null,
    provider: null,
    reason: null,
  });
  const [imageGenerationConfigLoaded, setImageGenerationConfigLoaded] = useState(false);
  const [providerSetup, setProviderSetup] = useState<ProviderSetupState>({
    llmRequired: false,
    imageRequired: false,
  });
  const [formaDevMode, setFormaDevMode] = useState(false);
  const [authoringMode, setAuthoringMode] = useState(false);
  const [openCodeConnectorId, setOpenCodeConnectorId] = useState<string | null>(null);
  const [generateProductImage, setGenerateProductImage] = useState(false);
  const [generationMode, setGenerationMode] = useState<GenerationMode>("regular");
  const [visualDecisionBusy, setVisualDecisionBusy] = useState<"approve" | "revise" | "continue_to_cad" | null>(null);
  const [visualDecisionError, setVisualDecisionError] = useState<string | null>(null);
  const [generationWorkflow, setGenerationWorkflow] = useState(DEFAULT_WORKFLOW_ID);
  const [generationWorkflows, setGenerationWorkflows] = useState<GenerationWorkflowOption[]>(defaultGenerationWorkflows);
  const [agentPipelineSteps, setAgentPipelineSteps] = useState<AgentPipelineStep[]>(defaultAgentPipelineSteps);
  const [generationLlms, setGenerationLlms] = useState<GenerationLlmOption[]>([]);
  const [generationLlmKeyValue, setGenerationLlmKeyValue] = useState("");
  const [generationLlmsLoaded, setGenerationLlmsLoaded] = useState(false);
  const [mechElectricalActive, setMechElectricalActive] = useState(true);
  const [mechToggles, setMechToggles] = useState({
    structural: true,
    enclosure: true,
    mechanism: true,
    misc: false,
    print: true,
    bodyRotation: false,
  });

  const fileInputRefSidebar = useRef<HTMLInputElement>(null);
  const fileInputRefCenter = useRef<HTMLInputElement>(null);
  const projectsSectionRef = useRef<HTMLElement>(null);
  const chatPersistenceTimersRef = useRef<Record<string, number>>({});
  const chatPersistenceVersionsRef = useRef<Record<string, number>>({});
  const pendingChatWritesRef = useRef(new Set<string>());
  const projectHistoryRequestIdRef = useRef(0);
  const myProjectHistoryRequestIdRef = useRef(0);
  const generationLlmRequestIdRef = useRef(0);
  const pipelineStepsRequestIdRef = useRef(0);
  const pipelineStepsAbortRef = useRef<AbortController | null>(null);
  const pipelineStepsRequestStartedRef = useRef(false);
  const pipelineStepsLastRequestedWorkflowRef = useRef<string | null>(null);
  const recoveryJobMissesRef = useRef(new Map<string, { misses: number; retryAfter: number }>());
  const generationRunsRef = useRef(new Map<string, ActiveGenerationRun>());
  const openCodeSessionsRef = useRef<Record<string, OpenCodeSession>>({});
  const [openCodeRetries, setOpenCodeRetries] = useState<Record<string, {
    message: string; assistantMessageId: string; projectId?: string | null;
  }>>({});
  const openCodeCursorsRef = useRef<Record<string, number>>({});
  const openCodePollTimersRef = useRef<Record<string, number>>({});
  const activeChatIdRef = useRef(activeChatId);
  useLayoutEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);
  const visibleChatSourceProjects = myProjectHistory;
  const visibleChatSourceItems = useMemo(
    () => authRequired
      ? mergeChatListItems(sessionChatItems, privateChatItems)
      : mergeChatListItems(privateChatItems, localChatItems),
    [authRequired, localChatItems, privateChatItems, sessionChatItems]
  );
  const chatListItems = useMemo(
    () => sortChatListItems(
      buildChatListItems(visibleChatSourceProjects, visibleChatSourceItems).map((item) => ({
        ...item,
        pinned: pinnedChatIds.has(item.chatId),
      }))
    ),
    [pinnedChatIds, visibleChatSourceProjects, visibleChatSourceItems]
  );
  const imageScopeKey = `${galleryIdentityKey}:${formaDevMode}`;
  const projectGalleryImages = galleryImageState.scope === imageScopeKey
    ? galleryImageState.images : EMPTY_GALLERY_IMAGES;
  const currentGalleryPageKey = galleryPageKey(
    galleryIdentityKey, PROJECT_GALLERY_PAGE_SIZE, projectHistoryPage, projectSearchQuery,
  );
  const cachedGalleryPage = projectPageCacheRef.current.get(currentGalleryPageKey);
  const hasCurrentGalleryPage = projectHistoryKey === currentGalleryPageKey;
  const visibleProjectHistory = hasCurrentGalleryPage
    ? projectHistory : cachedGalleryPage?.items || EMPTY_PROJECT_HISTORY;
  const visibleProjectTotal = hasCurrentGalleryPage
    ? projectHistoryTotal : cachedGalleryPage?.total || 0;
  const projectGalleryItems = useMemo(
    () => buildProjectGalleryItems(
      visibleProjectHistory,
      projectGalleryImages,
      formaDevMode,
    ).map((item) => ({
      ...item,
      canChat: item.canChat && (!authRequired || Boolean(isSignedIn)),
    })),
    [authRequired, formaDevMode, isSignedIn, visibleProjectHistory, projectGalleryImages]
  );
  const projectBrowserItems = useMemo(
    () => projectGalleryItems.map(formaBrowserProjectFromGalleryItem),
    [projectGalleryItems]
  );
  const myProjectGalleryItems = useMemo(
    () => buildProjectGalleryItems(
      myProjectHistory,
      projectGalleryImages,
      formaDevMode,
    ).map((item) => ({
      ...item,
      canChat: item.canChat && (!authRequired || Boolean(isSignedIn)),
    })),
    [authRequired, formaDevMode, isSignedIn, myProjectHistory, projectGalleryImages]
  );
  const myProjectBrowserItems = useMemo(
    () => myProjectGalleryItems.map(formaBrowserProjectFromGalleryItem),
    [myProjectGalleryItems]
  );
  const chatHistoryLoaded = myProjectHistoryLoaded && privateChatsLoaded;
  const projectsPageLoading = !(hasCurrentGalleryPage && projectHistoryLoaded) && !cachedGalleryPage;
  const myProjectsPageLoading = (authRequired && !authLoaded)
    || !myProjectHistoryLoaded;
  const handleVisibleProjectGalleryIdsChange = useCallback((projectIds: string[]) => {
    setVisibleProjectGalleryIds((current) => (
      sameStringList(current, projectIds) ? current : projectIds
    ));
  }, []);
  const handleProjectHistoryPageChange = useCallback((page: number) => {
    setVisibleProjectGalleryIds([]);
    setProjectHistoryPage(page);
  }, []);
  const handleMyProjectHistoryPageChange = useCallback((page: number) => {
    setMyProjectHistoryLoaded(false);
    setVisibleProjectGalleryIds([]);
    setMyProjectHistoryPage(page);
  }, []);
  const inlineChatProjectId = useMemo(() => {
    const activeThread = activeChatId ? chatThreads[activeChatId] || [] : [];
    const messages = activeThread.length ? activeThread : chatMessages;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const projectId = messages[index]?.projectId;
      if (projectId) return projectId;
    }
    const completedWorkerBuild = messages.some((message) => (
      message.pipelineProgress?.jobId?.startsWith("generation-")
      && normalizeAgentPipelineEvents(message.pipelineProgress.events).some((event) => (
        event.step_id === "package_project" && isCompletedPipelineStatus(event.status)
      ))
    ));
    if (completedWorkerBuild && activeChatId) return activeChatId;
    return null;
  }, [activeChatId, chatMessages, chatThreads]);
  const generationInputValidation = useMemo(
    () => validateGenerationInput(
      pendingHumanContext?.basePrompt || prompt,
      Boolean(selectedImage || selectedDocument),
    ),
    [pendingHumanContext, prompt, selectedDocument, selectedImage]
  );
  const hasGenerationInput = Boolean(prompt.trim() || selectedImage || selectedDocument || pendingHumanContext);
  const selectedGenerationWorkflow = useMemo(
    () => generationWorkflows.find((workflow) => workflow.id === generationWorkflow) || generationWorkflows[0] || defaultGenerationWorkflows[0],
    [generationWorkflow, generationWorkflows]
  );
  const webResearchEnabled = generationWorkflow === WEB_RESEARCH_WORKFLOW_ID;
  const selectedWorkflowUsesExternalSources = Boolean(
    webResearchEnabled &&
    (
      selectedGenerationWorkflow?.uses_external_sources ||
      selectedGenerationWorkflow?.uses_web_research ||
      selectedGenerationWorkflow?.uses_firecrawl_mcp
    )
  );
  const selectedGenerationLlm = useMemo(
    () => generationLlms.find((option) => generationLlmKey(option) === generationLlmKeyValue) || generationLlms[0] || null,
    [generationLlmKeyValue, generationLlms]
  );
  const needsGenerationProvider = generationLlmsLoaded && providerSetup.llmRequired && (!authRequired || authLoaded);
  const needsImageProvider = imageGenerationConfigLoaded && providerSetup.imageRequired && (!authRequired || authLoaded);
  const visibleContextInputNotice =
    generationInputNotice || ((prompt.trim() || selectedImage || selectedDocument) && !generationInputValidation.isValid
      ? generationInputValidation.message
      : null);
  const hostedChatReadOnly = !hostedChatEnabled;
  const chatReadOnly = chatAccessState !== "ready" || (hostedChatReadOnly && !authoringMode);
  const requireHostedChatEnabled = () => {
    if (hostedChatEnabled) return true;
    setGenerationInputNotice(HOSTED_CHAT_MAINTENANCE_MESSAGE);
    return false;
  };
  const trackLiveChatMessage = (id: string, status: ChatMessage["status"]) => {
    if (!status) return;
    setLiveChatMessageIds((current) => {
      const live = status === "loading";
      if (current.has(id) === live) return current;
      const next = new Set(current);
      if (live) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  const appendChatMessage = (message: Omit<ChatMessage, "id" | "timestamp"> & { id?: string }) => {
    const nextMessage: ChatMessage = {
      id: message.id || newChatMessageId(),
      role: message.role,
      content: message.content,
      status: message.status || "idle",
      projectId: message.projectId,
      pipelineProgress: message.pipelineProgress || null,
      imagePreview: message.imagePreview || null,
      contextProjectId: message.contextProjectId || null,
      workflowState: message.workflowState || null,
      contextQuestions: message.contextQuestions || [],
      contextSuggestions: message.contextSuggestions || [],
      buildPlanId: message.buildPlanId || null,
      buildJobId: message.buildJobId || null,
      timestamp: chatTimestamp(),
    };
    trackLiveChatMessage(nextMessage.id, nextMessage.status);
    setChatMessages((current) => [...current, nextMessage]);
    return nextMessage.id;
  };
  const updateChatMessage = (id: string, patch: Partial<Omit<ChatMessage, "id">>) => {
    trackLiveChatMessage(id, patch.status);
    setChatMessages((current) =>
      current.map((message) =>
        message.id === id
          ? {
              ...message,
              ...patch,
              timestamp: patch.timestamp || chatTimestamp(),
            }
          : message
      )
    );
  };

  const ensureChatThread = async (projectId: string | null, ir: any, routedChatId?: string, signal?: AbortSignal) => {
    if (!projectId) return;
    const chatId = routedChatId || chatIdFromIR(ir) || projectId;
    setActiveChatId(chatId);
    const cached = chatThreads[chatId] || readStoredChatThread(chatId, projectId, chatStorageScope);
    let messages = cached;
    if (!hasConversationHistory(cached)) {
      try {
        messages = persistableChatMessages(await loadProjectChatHistory({
          apiUrl: API_URL, chatId, projectId, headers: await generationRequestHeaders(),
          recoverOpenCode: authoringMode || ir?.assembly_metadata?.authoring_agent === "opencode", signal,
        }));
      } catch (error) {
        if (!signal?.aborted && chatStorageScopeRef.current === chatStorageScope) {
          setGenerationInputNotice("Previous messages could not be loaded. Reopen this chat to retry.");
        }
        return;
      }
    }
    if (signal?.aborted || chatStorageScopeRef.current !== chatStorageScope) return;
    setChatThreads((current) => {
      const nextMessages = messages.length
        ? mergeFetchedChatMessages(messages, hasConversationHistory(current[chatId] || []) ? current[chatId] : [], true)
        : hasConversationHistory(current[chatId] || []) ? current[chatId]
        : initialProjectChatMessages(projectId, ir?.overview?.title || "Project");
      if (hasConversationHistory(nextMessages)) writeStoredChatThread(chatId, nextMessages, chatStorageScope);
      return {
        ...current,
        [chatId]: nextMessages,
      };
    });
  };

  const appendThreadMessage = (chatId: string | null, message: Omit<ChatMessage, "id" | "timestamp"> & { id?: string }) => {
    if (!chatId) return "";
    const nextMessage: ChatMessage = {
      id: message.id || newChatMessageId(),
      role: message.role,
      content: message.content,
      status: message.status || "idle",
      projectId: message.projectId,
      pipelineProgress: message.pipelineProgress || null,
      imagePreview: message.imagePreview || null,
      contextProjectId: message.contextProjectId || null,
      workflowState: message.workflowState || null,
      contextQuestions: message.contextQuestions || [],
      contextSuggestions: message.contextSuggestions || [],
      buildPlanId: message.buildPlanId || null,
      buildJobId: message.buildJobId || null,
      timestamp: chatTimestamp(),
    };
    trackLiveChatMessage(nextMessage.id, nextMessage.status);
    setChatThreads((current) => {
      const nextMessages = [...(current[chatId] || []), nextMessage].slice(-MAX_PROJECT_CHAT_MESSAGES);
      writeStoredChatThread(chatId, nextMessages, chatStorageScope);
      persistChatThread(chatId, nextMessages);
      return {
        ...current,
        [chatId]: nextMessages,
      };
    });
    return nextMessage.id;
  };

  const updateThreadMessage = (chatId: string | null, messageId: string, patch: Partial<Omit<ChatMessage, "id">>) => {
    if (!chatId || !messageId) return;
    trackLiveChatMessage(messageId, patch.status);
    setChatThreads((current) => {
      const currentMessages = current[chatId] || [];
      const nextMessages = currentMessages.map((message) =>
        message.id === messageId
          ? {
              ...message,
              ...patch,
              timestamp: patch.timestamp || chatTimestamp(),
            }
          : message
      );
      writeStoredChatThread(chatId, nextMessages, chatStorageScope);
      persistChatThread(chatId, nextMessages);
      return {
        ...current,
        [chatId]: nextMessages,
      };
    });
  };

  const applyChatPipelineProgressFromJob = (
    messageId: string,
    job: A2AJob | null,
    seedProgress: AgentPipelineProgress,
    includeImage: boolean
  ) => {
    if (!messageId || !job) return;
    setChatMessages((current) => {
      let changed = false;
      const nextMessages = current.map((message) => {
        if (message.id !== messageId) return message;
        const nextMessage = mergeMessagePipelineProgressFromJob(message, job, seedProgress, includeImage);
        if (nextMessage !== message) changed = true;
        return nextMessage;
      });
      return changed ? nextMessages : current;
    });
  };

  const applyThreadPipelineProgressFromJob = (
    chatId: string | null,
    messageId: string,
    job: A2AJob | null,
    seedProgress: AgentPipelineProgress,
    includeImage: boolean
  ) => {
    if (!chatId || !messageId || !job) return;
    setChatThreads((current) => {
      const currentMessages = current[chatId] || [];
      let changed = false;
      const nextMessages = currentMessages.map((message) => {
        if (message.id !== messageId) return message;
        const nextMessage = mergeMessagePipelineProgressFromJob(message, job, seedProgress, includeImage);
        if (nextMessage !== message) changed = true;
        return nextMessage;
      });
      if (!changed) return current;
      writeStoredChatThread(chatId, nextMessages, chatStorageScope);
      persistChatThread(chatId, nextMessages);
      return {
        ...current,
        [chatId]: nextMessages,
      };
    });
  };

  const rememberChatItem = (item: Partial<ChatListItem> & { chatId: string }) => {
    const normalizedItem = normalizeChatListItem(item);
    if (authRequired) {
      if (normalizedItem) {
        setSessionChatItems((current) => mergeChatListItems([normalizedItem], current));
      }
    }
    setLocalChatItems((current) => {
      const nextItems = upsertChatListItem(current, item);
      writeStoredChatIndex(nextItems, chatStorageScope);
      return nextItems;
    });
    if (normalizedItem && (!authRequired || isSignedIn)) {
      const messages = chatThreads[item.chatId]
        || (activeChatId === item.chatId ? chatMessages : readStoredChatThread(item.chatId, null, chatStorageScope));
      persistChatThread(item.chatId, messages, normalizedItem.title);
    }
  };

  const rememberProjectRecord = (record: any) => {
    const normalizedRecord = normalizeProjectHistoryRecord(record);
    if (!normalizedRecord) return;
    const mergeProject = (projects: any[]) => (
      [normalizedRecord, ...projects.filter((project: any) => project?.project_id !== normalizedRecord.project_id)]
        .sort((left: any, right: any) => {
          const leftTime = Date.parse(left.created_at || "");
          const rightTime = Date.parse(right.created_at || "");
          return (Number.isNaN(rightTime) ? 0 : rightTime) - (Number.isNaN(leftTime) ? 0 : leftTime);
        })
    );
    projectPageCacheRef.current.clear();
    setProjectHistory((projects) => (
      normalizedRecord.visibility === "public"
        ? mergeProject(projects)
        : projects.filter((project: any) => project?.project_id !== normalizedRecord.project_id)
    ));
    setMyProjectHistory(mergeProject);
    setMyProjectHistoryLoaded(true);
  };

  const detachMissingProjectFromChat = (chatId: string, projectId: string, title?: string | null) => {
    if (!chatId || !projectId) return;
    const clearProjectReference = (items: ChatListItem[]) => items.map((item) => (
      item.chatId === chatId ? { ...item, projectId: "", projectCount: 0 } : item
    ));
    setSessionChatItems(clearProjectReference);
    setPrivateChatItems(clearProjectReference);
    setLocalChatItems((current) => {
      const existing = current.find((item) => item.chatId === chatId);
      const nextItem: ChatListItem = {
        chatId,
        title: existing?.title?.trim() || title?.trim() || NEW_PROJECT_TITLE,
        projectId: "",
        createdAt: existing?.createdAt || chatTimestamp(),
        projectCount: 0,
      };
      const nextItems = [nextItem, ...current.filter((item) => item.chatId !== chatId)]
        .sort((left, right) => {
          const leftTime = Date.parse(left.createdAt || "");
          const rightTime = Date.parse(right.createdAt || "");
          return (Number.isNaN(rightTime) ? 0 : rightTime) - (Number.isNaN(leftTime) ? 0 : leftTime);
        })
        .slice(0, MAX_CHAT_INDEX_ITEMS);
      writeStoredChatIndex(nextItems, chatStorageScope);
      return nextItems;
    });
  };

  const updateHumanContextAnswer = (questionId: string, value: string) => {
    setPendingHumanContext((current) =>
      current
        ? {
            ...current,
            answers: {
              ...current.answers,
              [questionId]: value,
            },
          }
        : current
    );
  };
  const clearHumanContextCheckpoint = () => {
    if (pendingHumanContext) setPrompt(pendingHumanContext.basePrompt);
    setPendingHumanContext(null);
  };

  const requireSignedInForGeneration = async () => {
    if (!authRequired || isSignedIn) return true;
    setGenerationInputNotice("Sign in to talk in chat and make projects.");
    openSignIn({ redirectUrl: typeof window !== "undefined" ? window.location.href : "/" });
    return false;
  };

  const generationRequestHeaders = useCallback(async () => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (!authRequired) return headers;
    const token = await getToken();
    if (!token) {
      openSignIn({ redirectUrl: typeof window !== "undefined" ? window.location.href : "/" });
      if (isSignedIn) setAuthSecurityError(true);
      throw new Error("Sign in to talk in chat and make projects.");
    }
    headers.Authorization = `Bearer ${token}`;
    return headers;
  }, [authRequired, getToken, isSignedIn, openSignIn]);

  const noteAuthResponseStatus = useCallback((status: number) => {
    if (isAuthOrSecurityHttpStatus(status)) setAuthSecurityError(true);
  }, []);

  const optionalAuthHeaders = useCallback(async (): Promise<Record<string, string>> => {
    if (!isSignedIn) return {};
    try {
      const token = await getToken();
      return token ? { Authorization: `Bearer ${token}` } : {};
    } catch {
      return {};
    }
  }, [getToken, isSignedIn]);

  const canInteractWithGallery = !authRequired || Boolean(isSignedIn);

  const patchProjectEngagement = useCallback((
    projectId: string,
    updates: { saved?: boolean; save_count?: number; remix_count?: number },
  ) => {
    const apply = (projects: any[]) => projects.map((project) => (
      project?.project_id === projectId ? { ...project, ...updates } : project
    ));
    projectPageCacheRef.current.clear();
    setProjectHistory(apply);
    setMyProjectHistory(apply);
  }, []);

  const handleToggleProjectSave = useCallback(async (item: ProjectGalleryItem) => {
    if (!canInteractWithGallery) return;
    const nextSaved = !item.saved;
    const nextCount = Math.max(0, item.saveCount + (nextSaved ? 1 : -1));
    patchProjectEngagement(item.projectId, { saved: nextSaved, save_count: nextCount });
    try {
      const response = await fetch(`${API_URL}/projects/${encodeURIComponent(item.projectId)}/save`, {
        method: nextSaved ? "POST" : "DELETE",
        headers: await generationRequestHeaders(),
      });
      if (!response.ok) {
        noteAuthResponseStatus(response.status);
        throw new Error(await readApiErrorMessage(response));
      }
      const data = await response.json();
      patchProjectEngagement(item.projectId, {
        saved: Boolean(data.saved),
        save_count: Math.max(0, Number(data.save_count ?? nextCount)),
      });
    } catch (error) {
      patchProjectEngagement(item.projectId, { saved: item.saved, save_count: item.saveCount });
      console.error("Could not update project save", error);
    }
  }, [canInteractWithGallery, generationRequestHeaders, noteAuthResponseStatus, patchProjectEngagement]);

  const handleRemixProject = useCallback(async (item: ProjectGalleryItem) => {
    if (!canInteractWithGallery || !hostedChatEnabled) return;
    try {
      const response = await fetch(`${API_URL}/projects/${encodeURIComponent(item.projectId)}/remix`, {
        method: "POST",
        headers: await generationRequestHeaders(),
      });
      if (!response.ok) {
        noteAuthResponseStatus(response.status);
        throw new Error(await readApiErrorMessage(response));
      }
      const data = await response.json();
      patchProjectEngagement(item.projectId, {
        remix_count: Math.max(0, Number(data.remix_count ?? item.remixCount + 1)),
      });
      if (data.project_id) {
        rememberProjectRecord({
          project_id: data.project_id,
          chat_id: data.chat_id || "",
          title: data.title || `${item.title} remix`,
          prompt: data.prompt || item.title,
          created_at: data.created_at || chatTimestamp(),
          can_chat: true,
          creator_display: "you",
          creator_image_url: userImageUrl,
          parts_count: item.partsCount,
          save_count: 0,
          remix_count: 0,
          saved: false,
        });
        router.push(projectRoute(String(data.project_id)));
      }
    } catch (error) {
      console.error("Could not remix project", error);
    }
  }, [canInteractWithGallery, generationRequestHeaders, hostedChatEnabled, noteAuthResponseStatus, patchProjectEngagement, router, userImageUrl]);

  const openProjectDeletion = useCallback((project: PendingProjectDeletion) => {
    setPendingProjectDeletion(project);
    setDeletionAcknowledged(false);
    setContributeDeletedProject(false);
    setProjectDeletionError(null);
  }, []);

  const closeProjectDeletion = useCallback(() => {
    if (projectDeletionBusy) return;
    setPendingProjectDeletion(null);
    setProjectDeletionError(null);
  }, [projectDeletionBusy]);

  const forgetChatRecords = (chatIds: string[]) => {
    if (!chatIds.length) return;
    const idSet = new Set(chatIds);
    chatIds.forEach((chatId) => {
      const existingTimer = chatPersistenceTimersRef.current[chatId];
      if (existingTimer) window.clearTimeout(existingTimer);
      delete chatPersistenceTimersRef.current[chatId];
      removeStoredChatThread(chatId, chatStorageScope);
    });
    setLocalChatItems((current) => {
      const nextItems = current.filter((item) => !idSet.has(item.chatId));
      writeStoredChatIndex(nextItems, chatStorageScope);
      return nextItems;
    });
    setPrivateChatItems((current) => current.filter((item) => !idSet.has(item.chatId)));
    setSessionChatItems((current) => current.filter((item) => !idSet.has(item.chatId)));
    setChatThreads((current) => {
      const next = { ...current };
      chatIds.forEach((chatId) => {
        delete next[chatId];
      });
      return next;
    });
    setPinnedChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      chatIds.forEach((chatId) => {
        if (next.delete(chatId)) changed = true;
      });
      if (changed) writePinnedChatIds(next, chatStorageScope);
      return changed ? next : current;
    });
  };

  const confirmProjectDeletion = async () => {
    if (!pendingProjectDeletion || !deletionAcknowledged || projectDeletionBusy) return;
    if (!hostedChatEnabled) {
      setProjectDeletionError(HOSTED_CHAT_MAINTENANCE_MESSAGE);
      return;
    }
    setProjectDeletionBusy(true);
    setProjectDeletionError(null);
    const projectId = pendingProjectDeletion.projectId;
    try {
      const headers = await generationRequestHeaders();
      if (contributeDeletedProject) {
        const consentResponse = await fetch(
          `${API_URL}/projects/${encodeURIComponent(projectId)}/data-contribution-consent`,
          {
            method: "PUT",
            headers,
            body: JSON.stringify({
              granted: true,
              consent_version: "2026-07-31",
              permitted_purposes: ["product_research", "evaluation", "ai_system_improvement"],
            }),
          },
        );
        if (!consentResponse.ok) throw new Error(await readApiErrorMessage(consentResponse));
      }
      const response = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}`, {
        method: "DELETE",
        headers,
      });
      if (!response.ok) throw new Error(await readApiErrorMessage(response));
      const relatedChatIds = chatListItems
        .filter((item) => item.projectId === projectId)
        .map((item) => item.chatId);
      projectPageCacheRef.current.clear();
      setProjectHistory((projects) => projects.filter((project: any) => project?.project_id !== projectId));
      setMyProjectHistory((projects) => projects.filter((project: any) => project?.project_id !== projectId));
      setGalleryImageState((current) => {
        const images = { ...current.images };
        delete images[projectId];
        return { ...current, images };
      });
      forgetChatRecords(relatedChatIds);
      relatedChatIds.forEach((chatId) => {
        void fetch(`${API_URL}/chats/${encodeURIComponent(chatId)}`, {
          method: "DELETE",
          headers,
        }).catch(() => {});
      });
      setPendingProjectDeletion(null);
      if (
        (currentRouteProjectId && safeDecodeProjectId(currentRouteProjectId) === projectId) ||
        (activeChatId && relatedChatIds.includes(activeChatId))
      ) {
        goHome();
      }
      void fetchProjectHistory();
      void fetchMyProjectHistory();
      void fetchPrivateChats();
    } catch (error) {
      setProjectDeletionError(error instanceof Error ? error.message : "Project deletion failed.");
    } finally {
      setProjectDeletionBusy(false);
    }
  };

  const { isAdmin, loaded: adminSessionLoaded } = useAdminSession({
    apiUrl: API_URL,
    getHeaders: optionalAuthHeaders,
    readError: readApiErrorMessage,
    enabled: authRequired,
    authRequired,
    authReady: !authRequired || authLoaded,
    signedIn: isSignedIn,
    requestScopeKey: authIdentityKey,
  });
  const canViewJobs = !authRequired || isAdmin;
  const canViewAdminTools = !authRequired || showDeveloperTools || isAdmin;
  const sidebarChatsLoading = !chatIndexLoaded || !chatHistoryLoaded;
  const sidebarJobsPending = authRequired && !adminSessionLoaded;
  const jobsViewActive = canViewJobs && (homeView === "jobs" || Boolean(projectIR && activeTab === "jobs"));
  const logsViewActive = canViewAdminTools && (homeView === "logs" || Boolean(projectIR && activeTab === "logs"));
  const {
    jobs: a2aJobs,
    metrics: jobMetrics,
    metricsError: jobMetricsError,
    metricsWindow: jobMetricsWindow,
    setMetricsWindow: setJobMetricsWindow,
    loading: jobsLoading,
    error: jobsError,
    statusFilter: jobStatusFilter,
    setStatusFilter: setJobStatusFilter,
    lastUpdatedAt: jobsLastUpdatedAt,
    refresh: fetchA2aJobs,
    fetchJob: fetchA2aJob,
  } = useJobs({
    apiUrl: API_URL,
    getHeaders: generationRequestHeaders,
    readError: readApiErrorMessage,
    enabled: jobsViewActive,
    requestScopeKey: authIdentityKey,
    pollIntervalMs: JOB_POLL_INTERVAL_MS,
  });
  const {
    logs: backendLogs,
    loading: logsLoading,
    error: logsError,
    lastUpdatedAt: logsLastUpdatedAt,
    refresh: fetchBackendLogs,
  } = useBackendLogs({
    apiUrl: API_URL,
    getHeaders: generationRequestHeaders,
    readError: readApiErrorMessage,
    enabled: logsViewActive,
    pollIntervalMs: LOG_POLL_INTERVAL_MS,
  });
  const {
    models: videoModels,
    loading: videoModelsLoading,
    error: videoModelsError,
    selectedModel: selectedVideoModel,
    setSelectedModel: setSelectedVideoModel,
    aspectRatios: videoAspectRatios,
    aspectRatio: videoAspectRatio,
    setAspectRatio: setVideoAspectRatio,
  } = useVideoModels({
    apiUrl: API_URL,
    enabled: Boolean(projectIR && activeTab === "video"),
    onAvailabilityChange: setVideoGenerationConfig,
  });
  const waitingGenerationJobKey = useMemo(() => {
    const jobIds = new Set<string>();
    const liveJobIds = new Set(Object.values(generationRuns).map((run) => run.jobId));
    const collect = (messages: ChatMessage[]) => {
      messages.forEach((message) => {
        const jobId = message.status === "loading" && !message.buildPlanId ? message.pipelineProgress?.jobId : null;
        if (jobId && !jobId.startsWith("generation-") && !liveJobIds.has(jobId)) jobIds.add(jobId);
      });
    };
    collect(chatMessages);
    Object.values(chatThreads).forEach(collect);
    return Array.from(jobIds).join("\n");
  }, [generationRuns, chatMessages, chatThreads]);
  const pendingContextBuildMessage = useMemo(
    () => [...chatMessages].reverse().find((message) => (
      message.status === "loading"
      && Boolean(message.buildPlanId)
      && Boolean(message.contextProjectId)
    )) || null,
    [chatMessages],
  );
  const retryableContextBuildMessage = useMemo(() => {
    return latestRetryableContextBuildMessage(chatMessages);
  }, [chatMessages]);

  const latestAgentOperation = useMemo(() => {
    return [...chatMessages].reverse().find((message) => (
      message.role === "assistant"
      && (message.status === "loading" || message.status === "success" || message.status === "error" || message.status === "cancelled")
    )) || null;
  }, [chatMessages]);

  useEffect(() => {
    if (latestAgentOperation?.status !== "loading") return;
    const intervalId = window.setInterval(() => setStatusClockMs(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, [latestAgentOperation?.status]);

  const workspaceStatus = useMemo(() => {
    const progress = latestAgentOperation?.pipelineProgress;
    const events = progress?.events;
    const lastEvent = Array.isArray(events) && events.length ? events[events.length - 1] : null;
    return workspaceStatusBadge({
      connection: serverStatus,
      authError: authSecurityError,
      agent: latestAgentOperation
        ? {
            status: latestAgentOperation.status,
            content: latestAgentOperation.content,
            startedAt: progress?.startedAt || latestAgentOperation.timestamp,
            lastEventAt: lastEvent?.observed_at || progress?.uiUpdatedAt || null,
          }
        : null,
      authoring: authoringMode,
      delivered: deliveredSignal,
      nowMs: statusClockMs,
    });
  }, [authSecurityError, authoringMode, deliveredSignal, latestAgentOperation, serverStatus, statusClockMs]);


  const persistChatThread = (chatId: string | null, messages: ChatMessage[], explicitTitle?: string | null) => {
    if (!(hostedChatEnabled || authoringMode) || (authRequired && !isSignedIn) || !chatId || typeof window === "undefined") return;
    const nextMessages = persistableChatMessages(messages);
    if (!chatHasStarted(nextMessages) || !hasConversationHistory(nextMessages)) return;
    const listedTitle = chatListItems.find((item) => item.chatId === chatId)?.title?.trim() || "";
    const title = explicitTitle?.trim()
      || (listedTitle && listedTitle !== NEW_PROJECT_TITLE ? listedTitle : chatTitleFromMessages(nextMessages));
    const existingTimer = chatPersistenceTimersRef.current[chatId];
    if (existingTimer) window.clearTimeout(existingTimer);
    const version = (chatPersistenceVersionsRef.current[chatId] || 0) + 1;
    chatPersistenceVersionsRef.current[chatId] = version;
    pendingChatWritesRef.current.add(chatId);
    chatPersistenceTimersRef.current[chatId] = window.setTimeout(async () => {
      delete chatPersistenceTimersRef.current[chatId];
      if (!(hostedChatEnabled || authoringMode) || chatStorageScopeRef.current !== chatStorageScope) return;
      try {
        const res = await fetch(`${API_URL}/chats/${encodeURIComponent(chatId)}`, {
          method: "PUT",
          headers: await generationRequestHeaders(),
          body: JSON.stringify({
            chat_id: chatId,
            title,
            messages: nextMessages,
          }),
        });
        if (!res.ok) throw new Error(await readApiErrorMessage(res));
        const savedChat = await res.json();
        if (chatStorageScopeRef.current !== chatStorageScope || chatPersistenceVersionsRef.current[chatId] !== version) return;
        pendingChatWritesRef.current.delete(chatId);
        setPrivateChatItems((current) => mergeChatListItems(normalizePrivateChatItems([savedChat]), current));
      } catch (error) {
        console.error("Error saving private chat", error);
      }
    }, 300);
  };

  const currentProjectChatHasStarted = () => {
    if (currentRouteProjectId) return true;
    const guardChatId = projectIR ? (chatIdFromIR(projectIR) || projectIdFromIR(projectIR) || activeChatId) : activeChatId;
    const guardMessages = projectIR && guardChatId ? chatThreads[guardChatId] || [] : chatMessages;
    const guardItem = chatListItems.find((item) => item.chatId === guardChatId);
    return Boolean(
      projectIR ||
      chatHasStarted(guardMessages) ||
      guardItem?.projectId ||
      guardItem?.projectCount
    );
  };

  const resetToNewProjectChat = () => {
    const nextChatId = newBuildChatId();
    setActiveChatId(nextChatId);
    rememberChatItem({
      chatId: nextChatId,
      title: NEW_PROJECT_TITLE,
      projectId: "",
      createdAt: chatTimestamp(),
      projectCount: 0,
    });
    setChatMessages([]);
    setPrompt("");
    setProjectChatInput("");
    setPendingHumanContext(null);
    setGenerationInputNotice(null);
    setSelectedImage(null);
    setSelectedImageSource("upload");
    setSelectedDocument(null);
    setChatRouteTransition(null);
    setProjectIR(null);
    setActiveTab("overview");
    return nextChatId;
  };

  const goHome = () => {
    if (chatReadOnly) {
      setChatRouteTransition(null);
      setProjectIR(null);
      setActiveTab("overview");
      router.push("/");
      return;
    }
    if (currentProjectChatHasStarted()) {
      resetToNewProjectChat();
    } else {
      setChatRouteTransition(null);
      setProjectIR(null);
      setActiveTab("overview");
    }
    router.push("/");
  };

  const startNewProjectChat = () => {
    if (homeView === "chat" && !currentRouteProjectId && !currentProjectChatHasStarted()) return;
    const nextChatId = resetToNewProjectChat();
    router.push(chatRoute(nextChatId));
  };

  const openChatItem = (item: ChatListItem) => {
    if (authRequired && !isSignedIn) {
      openSignIn({ redirectUrl: typeof window !== "undefined" ? chatRoute(item.chatId) : "/" });
      return;
    }
    setActiveChatId(item.chatId);
    setGenerationInputNotice(null);
    setActiveTab("overview");
    const storedMessages = readStoredChatThread(item.chatId, null, chatStorageScope);
    if (storedMessages.length) {
      setChatThreads((current) => ({ ...current, [item.chatId]: storedMessages }));
      setChatMessages(storedMessages);
    } else {
      setChatMessages([]);
    }
    const projectAlreadyLoaded = Boolean(
      item.projectId && projectIdFromIR(projectIR) === item.projectId
    );
    setChatRouteTransition(
      item.projectId && !projectAlreadyLoaded
        ? { chatId: item.chatId, title: item.title || "Opening chat", projectId: item.projectId, error: null }
        : null
    );
    if (!item.projectId) setProjectIR(null);
    syncChatRoute(item.chatId);
  };

  const openChatById = (chatId: string) => {
    const item = chatListItems.find((candidate) => candidate.chatId === chatId);
    if (item) {
      openChatItem(item);
      return;
    }
    setActiveChatId(chatId);
    setGenerationInputNotice(null);
    setActiveTab("overview");
    setChatRouteTransition({
      chatId,
      title: "Opening chat",
      projectId: "",
      error: null,
    });
    router.push(chatRoute(chatId));
  };

  const syncProjectRoute = (projectId: string, mode: "push" | "replace" = "push") => {
    const nextPath = projectRoute(projectId);
    if (window.location.pathname === nextPath) return;
    if (mode === "replace") {
      router.replace(nextPath);
    } else {
      router.push(nextPath);
    }
  };

  const syncChatRoute = (chatId: string, mode: "push" | "replace" = "push") => {
    if (!chatId) return;
    const nextPath = chatRoute(chatId);
    if (typeof window !== "undefined" && window.location.pathname === nextPath) return;
    if (mode === "replace") {
      router.replace(nextPath);
    } else {
      router.push(nextPath);
    }
  };

  useLayoutEffect(() => {
    setLocalChatItems(authRequired ? [] : readStoredChatIndex(chatStorageScope));
    setPinnedChatIds(new Set(readPinnedChatIds(chatStorageScope)));
    setChatIndexLoaded(true);
  }, [authRequired, chatStorageScope]);

  useLayoutEffect(() => {
    projectPageCacheRef.current.clear();
    projectHistoryRequestIdRef.current += 1;
    projectHistoryAbortRef.current?.abort();
    return () => {
      projectHistoryRequestIdRef.current += 1;
      projectHistoryAbortRef.current?.abort();
    };
  }, [galleryIdentityKey]);

  useEffect(() => {
    const requests = galleryImageRequestsRef.current;
    return () => requests.clear();
  }, []);

  useEffect(() => {
    if (homeView !== "projects") return;
    void fetchProjectHistory(projectHistoryPage, projectSearchQuery);
    // Public gallery data becomes critical only when its route is active.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [homeView, projectHistoryPage, projectSearchQuery, galleryIdentityKey, authLoaded]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const nextQuery = projectSearchInput.trim();
      if (nextQuery === projectSearchQuery) return;
      setVisibleProjectGalleryIds([]);
      setProjectHistoryPage(0);
      setProjectSearchQuery(nextQuery);
    }, 300);
    return () => window.clearTimeout(timeout);
  }, [projectSearchInput, projectSearchQuery]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const nextQuery = myProjectSearchInput.trim();
      if (nextQuery === myProjectSearchQuery) return;
      setMyProjectHistoryLoaded(false);
      setVisibleProjectGalleryIds([]);
      setMyProjectHistoryPage(0);
      setMyProjectSearchQuery(nextQuery);
    }, 300);
    return () => window.clearTimeout(timeout);
  }, [myProjectSearchInput, myProjectSearchQuery]);

  useDeferredTask(() => {
    if (!projectHistoryLoaded) void fetchProjectHistory(projectHistoryPage, projectSearchQuery);
  }, {
    delayMs: 1200,
    enabled: homeView !== "projects" && !projectHistoryLoaded,
    taskKey: `${homeView}:${projectHistoryPage}:${projectSearchQuery}`,
    timeoutMs: 1800,
  });

  useDeferredTask(() => {
    void checkServerStatus();
  }, { delayMs: 150, timeoutMs: 700 });

  useDeferredTask(() => {
    if (!authRequired) void fetchRuntimeConfig();
  }, { delayMs: 500, enabled: !authRequired, timeoutMs: 1100 });

  useEffect(() => {
    if (!authRequired || !authLoaded) return;
    generationLlmRequestIdRef.current += 1;
    setRuntimeConfigState({ identityKey: authIdentityKey, status: "loading" });
    setGenerationLlmsLoaded(false);
    setGenerationLlms([]);
    setGenerationLlmKeyValue("");
    setImageGenerationConfig({ configured: null, requestCapable: null, provider: null, reason: null });
    setImageGenerationConfigLoaded(false);
    setProviderSetup({ llmRequired: false, imageRequired: false });
    setAuthSecurityError(false);
  }, [authIdentityKey, authLoaded, authRequired, isSignedIn]);

  useDeferredTask(() => {
    // Reload BYOK providers once Clerk has resolved the current user. The
    // initial production render cannot safely query user integrations yet.
    void fetchRuntimeConfig();
  }, {
    delayMs: 250,
    enabled: authRequired && authLoaded,
    taskKey: authIdentityKey,
    timeoutMs: 1000,
  });

  useEffect(() => {
    if (authRequired && !authLoaded) {
      setPrivateChatsLoaded(false);
      return;
    }
    setMyProjectHistoryPage(0);
    setPrivateChatsLoaded(false);
    void fetchPrivateChats();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authIdentityKey, authLoaded, authRequired, isSignedIn]);

  useEffect(() => {
    if (authRequired && !authLoaded) {
      setMyProjectHistoryLoaded(false);
      return;
    }
    void fetchMyProjectHistory(myProjectHistoryPage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authIdentityKey, authLoaded, authRequired, isSignedIn, myProjectHistoryPage, myProjectSearchQuery]);

  useDeferredTask(() => {
    void fetchAgentPipelineSteps(generationWorkflow);
  }, { delayMs: 1100, timeoutMs: 1400 });

  useEffect(() => {
    if (!pipelineStepsRequestStartedRef.current) return;
    if (pipelineStepsLastRequestedWorkflowRef.current === generationWorkflow) return;
    void fetchAgentPipelineSteps(generationWorkflow);
    // The first request is staged by useDeferredTask; later workflow changes
    // load immediately and cancel any response for the previous workflow.
  }, [generationWorkflow]);

  useEffect(() => {
    if (!isLoading && !hasGenerationRuns) return;

    const intervalId = window.setInterval(() => {
      const nowMs = Date.now();
      setChatMessages((current) => advancePipelineMessages(current, nowMs));
      setChatThreads((current) => {
        let changed = false;
        const nextThreads: Record<string, ChatMessage[]> = {};
        Object.entries(current).forEach(([chatId, messages]) => {
          const nextMessages = advancePipelineMessages(messages, nowMs);
          if (nextMessages !== messages) changed = true;
          nextThreads[chatId] = nextMessages;
        });
        return changed ? nextThreads : current;
      });
    }, 1000);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [isLoading, hasGenerationRuns]);

  const checkServerStatus = async () => {
    try {
      const res = await fetch(`${API_URL}/`);
      const nextStatus = res.ok ? "connected" : "disconnected";
      lastKnownServerStatus = nextStatus;
      setServerStatus(nextStatus);
    } catch {
      lastKnownServerStatus = "disconnected";
      setServerStatus("disconnected");
    }
  };

  const fetchRuntimeConfig = async () => {
    const requestId = ++generationLlmRequestIdRef.current;
    const requestIsCurrent = () => generationLlmRequestIdRef.current === requestId;
    setRuntimeConfigState({ identityKey: authIdentityKey, status: "loading" });
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20_000);
    try {
      const res = await fetch(`${API_URL}/runtime/config`, {
        cache: "no-store",
        headers: await optionalAuthHeaders(),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`Runtime config request failed (${res.status})`);

      const config = (await res.json()) as RuntimeConfigContract;
      if (!requestIsCurrent()) return;
      if (typeof config.deployment?.hosted_chat_enabled === "boolean") {
        setHostedChatEnabled(config.deployment.hosted_chat_enabled);
      }
      setAuthoringMode(authoringModeEnabled(config));
      setOpenCodeConnectorId(
        typeof config.deployment?.opencode_connector_id === "string" && config.deployment.opencode_connector_id.trim()
          ? config.deployment.opencode_connector_id.trim()
          : null,
      );
      setFormaDevMode(config.forma_dev_mode === true);
      const activeLlms = usableRuntimeLlmOptions(config);
      const selectedLlm = config.generation.selected_llm;
      const selectedLlmKey = selectedLlm ? generationLlmKey(selectedLlm) : "";
      setGenerationLlms(activeLlms);
      setGenerationLlmKeyValue(
        activeLlms.some((option) => generationLlmKey(option) === selectedLlmKey)
          ? selectedLlmKey
          : activeLlms[0]
            ? generationLlmKey(activeLlms[0])
            : "",
      );
      setProviderSetup({
        llmRequired: config.provider_setup.llm_required,
        imageRequired: config.provider_setup.image_required,
      });

      if (config.video?.generation) {
        setVideoGenerationConfig({
          configured: Boolean(config.video.generation.configured),
          reason: typeof config.video.generation.reason === "string" ? config.video.generation.reason : null,
        });
      }
      if (config.video?.self_correction) {
        setVideoSelfCorrectionConfig({
          configured: Boolean(config.video.self_correction.configured),
          reason: typeof config.video.self_correction.reason === "string" ? config.video.self_correction.reason : null,
        });
      }
      setImageGenerationConfig({
        configured: config.images.configured,
        requestCapable: config.images.request_capable,
        provider: config.images.provider,
        reason: config.images.reason,
      });
      setGenerateProductImage(config.images.generate_by_default);

      const workflows = Array.isArray(config.workflow.options) ? config.workflow.options : [];
      if (workflows.length > 0) {
        setGenerationWorkflows(workflows);
        setGenerationWorkflow(
          workflows.some((workflow) => workflow.id === config.workflow.default_id)
            ? config.workflow.default_id
            : workflows[0].id,
        );
      }
      setRuntimeConfigState({ identityKey: authIdentityKey, status: "ready" });
    } catch (e) {
      if (requestIsCurrent()) {
        console.error("Error fetching runtime config", e);
        setRuntimeConfigState({ identityKey: authIdentityKey, status: "error" });
      }
    } finally {
      window.clearTimeout(timeout);
      if (requestIsCurrent()) {
        setGenerationLlmsLoaded(true);
        setImageGenerationConfigLoaded(true);
      }
    }
  };

  const fetchAgentPipelineSteps = async (workflowId: string) => {
    pipelineStepsRequestStartedRef.current = true;
    pipelineStepsLastRequestedWorkflowRef.current = workflowId;
    pipelineStepsAbortRef.current?.abort();
    const controller = new AbortController();
    const requestId = pipelineStepsRequestIdRef.current + 1;
    pipelineStepsAbortRef.current = controller;
    pipelineStepsRequestIdRef.current = requestId;
    try {
      const params = new URLSearchParams({ workflow: workflowId || "default", include_image: "true" });
      const res = await fetch(`${API_URL}/pipeline/steps?${params.toString()}`, { signal: controller.signal });
      if (!res.ok) return;
      const data = await res.json();
      if (controller.signal.aborted || pipelineStepsRequestIdRef.current !== requestId) return;
      setAgentPipelineSteps(normalizeAgentPipelineSteps(data));
    } catch (e) {
      if (controller.signal.aborted) return;
      console.error("Error fetching agent pipeline steps", e);
    } finally {
      if (pipelineStepsAbortRef.current === controller) pipelineStepsAbortRef.current = null;
    }
  };


  const fetchProjectHistory = async (
    page: number = projectHistoryPage,
    search: string = projectSearchQuery,
  ) => {
    const requestId = ++projectHistoryRequestIdRef.current;
    projectHistoryAbortRef.current?.abort();
    const controller = new AbortController();
    projectHistoryAbortRef.current = controller;
    const isCurrent = () => !controller.signal.aborted && projectHistoryRequestIdRef.current === requestId;
    const key = galleryPageKey(galleryIdentityKey, PROJECT_GALLERY_PAGE_SIZE, page, search);
    const cached = projectPageCacheRef.current.get(key);
    setProjectHistoryError(null);
    if (cached) {
      setProjectHistory(cached.items);
      setProjectHistoryTotal(cached.total);
      setProjectHistoryKey(key);
      setProjectHistoryLoaded(true);
    } else if (projectHistoryKey !== key) {
      setProjectHistoryLoaded(false);
    }
    // A same-page refresh keeps the current cards mounted. A cached page can
    // also render immediately, but every visit still revalidates permissions.
    try {
      const params = new URLSearchParams({
        limit: String(PROJECT_GALLERY_PAGE_SIZE),
        offset: String(Math.max(0, page) * PROJECT_GALLERY_PAGE_SIZE),
      });
      const normalizedSearch = search.trim();
      if (normalizedSearch) params.set("q", normalizedSearch);
      const headers = await optionalAuthHeaders();
      if (!isCurrent()) return;
      const res = await fetch(`${API_URL}/projects?${params.toString()}`, {
        signal: controller.signal,
        headers,
      });
      if (!isCurrent()) return;
      if (!res.ok) {
        if (isAuthOrSecurityHttpStatus(res.status)) {
          projectPageCacheRef.current.clear();
          setProjectHistory([]);
          setProjectHistoryTotal(0);
        }
        throw new Error(await readApiErrorMessage(res));
      }
      const result = normalizeProjectListPage(await res.json());
      if (!isCurrent()) return;
      projectPageCacheRef.current.set(key, result);
      setProjectHistory(result.items);
      setProjectHistoryTotal(result.total);
      setProjectHistoryKey(key);
      if (!authRequired) {
        setLocalChatItems((current) => {
          const repairedItems = buildChatListItems(result.items, current);
          writeStoredChatIndex(repairedItems, chatStorageScope);
          return repairedItems;
        });
      }
    } catch (error) {
      if (!isCurrent()) return;
      if (!cached && projectHistoryKey !== key) {
        setProjectHistory([]);
        setProjectHistoryTotal(0);
      }
      setProjectHistoryKey(key);
      setProjectHistoryError(error instanceof Error ? error : new Error("Projects could not be loaded."));
      console.error("Error fetching project history", error);
    } finally {
      if (isCurrent()) {
        setProjectHistoryLoaded(true);
        projectHistoryAbortRef.current = null;
      }
    }
  };

  const fetchMyProjectHistory = async (
    page: number = myProjectHistoryPage,
    search: string = myProjectSearchQuery,
  ) => {
    const requestId = myProjectHistoryRequestIdRef.current + 1;
    myProjectHistoryRequestIdRef.current = requestId;
    if (authRequired && !authLoaded) {
      setMyProjectHistoryLoaded(false);
      return;
    }
    if (authRequired && !isSignedIn) {
      setMyProjectHistory([]);
      setMyProjectHistoryTotal(0);
      setMyProjectHistoryError(new Error("Sign in to view your projects."));
      setMyProjectHistoryLoaded(true);
      return;
    }

    setMyProjectHistoryLoaded(false);
    setMyProjectHistoryError(null);
    try {
      const params = new URLSearchParams({
        limit: String(PROJECT_GALLERY_PAGE_SIZE),
        offset: String(Math.max(0, page) * PROJECT_GALLERY_PAGE_SIZE),
      });
      const normalizedSearch = search.trim();
      if (normalizedSearch) params.set("q", normalizedSearch);
      const res = await fetch(`${API_URL}/my/projects?${params.toString()}`, {
        headers: await generationRequestHeaders(),
      });
      if (myProjectHistoryRequestIdRef.current !== requestId) return;
      if (res.ok) {
        const result = normalizeProjectListPage(await res.json());
        if (myProjectHistoryRequestIdRef.current !== requestId) return;
        setMyProjectHistory(result.items);
        setMyProjectHistoryTotal(result.total);
        setMyProjectHistoryError(null);
        setAuthSecurityError(false);
      } else if (isAuthOrSecurityHttpStatus(res.status)) {
        if (isSignedIn) setAuthSecurityError(true);
        setMyProjectHistory([]);
        setMyProjectHistoryTotal(0);
        setMyProjectHistoryError(new Error("Sign in to view your projects."));
      } else {
        throw new Error(await readApiErrorMessage(res));
      }
    } catch (e) {
      if (myProjectHistoryRequestIdRef.current !== requestId) return;
      setMyProjectHistoryError(e instanceof Error ? e : new Error("Projects could not be loaded."));
      console.error("Error fetching my project history", e);
    } finally {
      if (myProjectHistoryRequestIdRef.current === requestId) setMyProjectHistoryLoaded(true);
    }
  };

  const fetchPrivateChats = async () => {
    if (authRequired && !authLoaded) {
      setPrivateChatsLoaded(false);
      return;
    }
    if (authRequired && !isSignedIn) {
      setPrivateChatItems([]);
      setSessionChatItems([]);
      setPrivateChatsLoaded(true);
      return;
    }

    const versions = { ...chatPersistenceVersionsRef.current };
    const pending = new Set(pendingChatWritesRef.current);
    const keepLocal = (chatId: string) => pending.has(chatId) || pendingChatWritesRef.current.has(chatId)
      || versions[chatId] !== chatPersistenceVersionsRef.current[chatId];
    try {
      const res = await fetch(`${API_URL}/chats`, {
        headers: await generationRequestHeaders(),
      });
      if (res.ok) {
        setAuthSecurityError(false);
        const chats = await res.json();
        if (chatStorageScopeRef.current !== chatStorageScope) return;
        setPrivateChatItems(normalizePrivateChatItems(chats));
        const threadUpdates: Record<string, ChatMessage[]> = {};
        if (Array.isArray(chats)) {
          chats.forEach((chat: any) => {
            const chatId = typeof chat?.chat_id === "string" ? chat.chat_id.trim() : "";
            const messages = persistableChatMessages(Array.isArray(chat?.messages) ? chat.messages : []);
            if (!chatId || !messages.length) return;
            threadUpdates[chatId] = messages;
          });
        }
        if (Object.keys(threadUpdates).length) {
          setChatThreads((current) => {
            const next = { ...current };
            Object.entries(threadUpdates).forEach(([chatId, remoteMessages]) => {
              const mergedMessages = mergeFetchedChatMessages(remoteMessages, current[chatId] || [], keepLocal(chatId));
              next[chatId] = mergedMessages;
              writeStoredChatThread(chatId, mergedMessages, chatStorageScope);
            });
            return next;
          });
          if (activeChatId && threadUpdates[activeChatId]) {
            setChatMessages((current) => mergeFetchedChatMessages(threadUpdates[activeChatId], current, keepLocal(activeChatId)));
          }
        }
      } else if (isAuthOrSecurityHttpStatus(res.status)) {
        if (isSignedIn) setAuthSecurityError(true);
        setPrivateChatItems([]);
        setSessionChatItems([]);
      } else {
        throw new Error(await readApiErrorMessage(res));
      }
    } catch (e) {
      console.error("Error fetching private chats", e);
    } finally {
      setPrivateChatsLoaded(true);
    }
  };

  const refreshProjectAndChatLists = () => {
    fetchProjectHistory();
    if (!authRequired || isSignedIn) {
      void fetchMyProjectHistory();
      void fetchPrivateChats();
    }
  };

  const changeJobStatusFilter = (status: string) => {
    setJobStatusFilter(status);
  };


  useEffect(() => {
    if (!normalizeTab(activeTab) || (activeTab === "cad" && !hasRenderableCadModel)) {
      setActiveTab("overview");
    }
  }, [activeTab, hasRenderableCadModel]);


  useEffect(() => {
    if (!a2aJobs.length) return;
    const jobsById = new Map(a2aJobs.map((job) => [job.job_id, job]));

    setChatMessages((current) => mergeMessagesWithJobs(current, jobsById, generateProductImage));
    setChatThreads((current) => {
      let changed = false;
      const nextThreads: Record<string, ChatMessage[]> = {};
      Object.entries(current).forEach(([chatId, messages]) => {
        const nextMessages = mergeMessagesWithJobs(messages, jobsById, generateProductImage);
        if (nextMessages !== messages) {
          changed = true;
          writeStoredChatThread(chatId, nextMessages, chatStorageScope);
          persistChatThread(chatId, nextMessages);
        }
        nextThreads[chatId] = nextMessages;
      });
      return changed ? nextThreads : current;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a2aJobs, generateProductImage]);

  useEffect(() => {
    if (!waitingGenerationJobKey || jobsViewActive || (authRequired && !isSignedIn)) return;
    const jobIds = waitingGenerationJobKey.split("\n").filter(Boolean);
    let cancelled = false;
    let polling = false;
    let nextJobIndex = 0;

    const reconcileWaitingJobs = async () => {
      if (polling) return;
      polling = true;
      try {
        const now = Date.now();
        const eligibleJobIds = jobIds.filter(
          (jobId) => (recoveryJobMissesRef.current.get(jobId)?.retryAfter || 0) <= now
        );
        if (!eligibleJobIds.length) return;
        const batchSize = Math.min(RECOVERY_JOB_BATCH_SIZE, eligibleJobIds.length);
        const batch = Array.from({ length: batchSize }, (_, offset) => (
          eligibleJobIds[(nextJobIndex + offset) % eligibleJobIds.length]
        ));
        nextJobIndex = (nextJobIndex + batchSize) % eligibleJobIds.length;
        const jobs: A2AJob[] = [];
        for (const jobId of batch) {
          if (cancelled) return;
          const job = await fetchA2aJob(jobId);
          if (job) {
            recoveryJobMissesRef.current.delete(jobId);
            jobs.push(job);
          } else {
            const previousMisses = recoveryJobMissesRef.current.get(jobId)?.misses || 0;
            const misses = previousMisses + 1;
            const backoffMs = Math.min(
              JOB_POLL_INTERVAL_MS * (2 ** Math.min(misses, 4)),
              RECOVERY_JOB_MAX_BACKOFF_MS
            );
            recoveryJobMissesRef.current.set(
              jobId,
              { misses, retryAfter: Date.now() + backoffMs }
            );
          }
        }
        if (cancelled || !jobs.length) return;
        const jobsById = new Map(jobs.map((job) => [job.job_id, job]));
        setChatMessages((current) => mergeMessagesWithJobs(current, jobsById, generateProductImage));
        setChatThreads((current) => {
          let changed = false;
          const nextThreads: Record<string, ChatMessage[]> = {};
          Object.entries(current).forEach(([chatId, messages]) => {
            const nextMessages = mergeMessagesWithJobs(messages, jobsById, generateProductImage);
            if (nextMessages !== messages) {
              changed = true;
              writeStoredChatThread(chatId, nextMessages, chatStorageScope);
              persistChatThread(chatId, nextMessages);
            }
            nextThreads[chatId] = nextMessages;
          });
          return changed ? nextThreads : current;
        });
      } finally {
        polling = false;
      }
    };

    const pollWhenVisible = () => {
      if (typeof document === "undefined" || document.visibilityState === "visible") {
        void reconcileWaitingJobs();
      }
    };
    pollWhenVisible();
    const intervalId = window.setInterval(pollWhenVisible, JOB_POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", pollWhenVisible);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", pollWhenVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authRequired, fetchA2aJob, generateProductImage, isSignedIn, jobsViewActive, waitingGenerationJobKey]);


  useEffect(() => {
    const galleryActive = (homeView === "projects" || homeView === "my-projects")
      && !currentRouteProjectId && !projectIR;
    const visibleProjectIds = new Set(galleryActive ? visibleProjectGalleryIds : []);
    const imageProjects = homeView === "my-projects" ? myProjectHistory : visibleProjectHistory;
    const missingIds = imageProjects.filter((project: any) => {
      const projectId = project?.project_id ? String(project.project_id) : "";
      if (!visibleProjectIds.has(projectId) || projectGalleryImages[projectId] !== undefined) return false;
      return !resolveProjectImageCandidates({
        product_visual_sequence: project.product_visual_sequence,
        product_image_url: project.product_image_url,
        product_image_data: project.product_image_data,
        product_image_content_type: project.product_image_content_type,
        product_image_model: project.product_image_model,
        image_output_model: project.image_output_model,
      }, formaDevMode)[0];
    }).map((project: any) => String(project.project_id));
    const storeImage = (projectId: string, image: ProjectImageCandidate | null) => {
      setGalleryImageState((current) => ({
        scope: imageScopeKey,
        images: { ...(current.scope === imageScopeKey ? current.images : {}), [projectId]: image },
      }));
    };
    galleryImageRequestsRef.current.sync(
      imageScopeKey,
      missingIds,
      async (projectId, signal) => {
        const headers = await optionalAuthHeaders();
        signal.throwIfAborted();
        const res = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}/image-summary`, {
          signal, headers,
        });
        if (!res.ok) return null;
        return resolveProjectImageCandidates(await res.json(), formaDevMode)[0] || null;
      },
      storeImage,
      (projectId, error) => {
        console.error("Error fetching project image", error);
        storeImage(projectId, null);
      },
    );
    // No per-render cleanup: settling one image must not abort its siblings.
    // sync cancels obsolete IDs/scope; the separate cleanup handles unmount.
  }, [formaDevMode, homeView, imageScopeKey, currentRouteProjectId, myProjectHistory, optionalAuthHeaders, visibleProjectHistory, projectGalleryImages, projectIR, visibleProjectGalleryIds]);

  const attachReferenceFile = (file: File, source: "upload" | "clipboard" = "upload") => {
    const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
    if (isPdf) {
      const maxPdfBytes = 2 * 1024 * 1024;
      if (file.size > maxPdfBytes) {
        setGenerationInputNotice("PDF context files are limited to 2 MB. Split or compress the PDF and try again.");
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        if (typeof reader.result !== "string") {
          setGenerationInputNotice("Forma could not read that PDF. Try exporting it again.");
          return;
        }
        setSelectedImage(null);
        setSelectedDocument({
          name: file.name || "reference.pdf",
          mediaType: "application/pdf",
          dataUrl: reader.result,
        });
        setGenerationMode("progressive");
        setGenerationInputNotice("PDF attached. Forma will extract its text into project context.");
      };
      reader.onerror = () => {
        setGenerationInputNotice("Forma could not read that PDF. Try uploading it again.");
      };
      reader.readAsDataURL(file);
      return;
    }

    if (!file.type.startsWith("image/")) {
      setGenerationInputNotice("Attach an image or PDF as a hardware reference.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        setGenerationInputNotice("Forma could not read that image. Try another image format.");
        return;
      }
      setGenerationInputNotice(null);
      setSelectedDocument(null);
      setSelectedImage(reader.result);
      setSelectedImageSource(source);
    };
    reader.onerror = () => {
      setGenerationInputNotice("Forma could not read that image. Try copying or uploading it again.");
    };
    reader.readAsDataURL(file);
  };

  const handleImageChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    // Permit selecting the same file again on a later turn or after validation fails.
    event.target.value = "";
    if (file) attachReferenceFile(file, "upload");
  };

  const handleImagePaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const imageFile = Array.from(event.clipboardData.files).find((file) => file.type.startsWith("image/"))
      || Array.from(event.clipboardData.items)
        .find((item) => item.type.startsWith("image/"))
        ?.getAsFile();
    if (imageFile) attachReferenceFile(imageFile, "clipboard");
  };

  const removeSelectedImage = () => {
    setGenerationInputNotice(null);
    setSelectedImage(null);
    setSelectedImageSource("upload");
    if (fileInputRefSidebar.current) fileInputRefSidebar.current.value = "";
    if (fileInputRefCenter.current) fileInputRefCenter.current.value = "";
  };

  const removeSelectedDocument = () => {
    setGenerationInputNotice(null);
    setSelectedDocument(null);
    if (fileInputRefSidebar.current) fileInputRefSidebar.current.value = "";
    if (fileInputRefCenter.current) fileInputRefCenter.current.value = "";
  };

  const beginGenerationRun = (kind: ActiveGenerationRun["kind"], chatId: string): ActiveGenerationRun => {
    const run: ActiveGenerationRun = {
      kind,
      controller: new AbortController(),
      jobId: null,
      chatId,
      assistantMessageId: null,
      openCodeSessionId: null,
      cancelled: false,
    };
    generationRunsRef.current.set(chatId, run);
    setGenerationRuns((current) => ({ ...current, [chatId]: { kind, jobId: null } }));
    return run;
  };

  const setGenerationRunJob = (run: ActiveGenerationRun, jobId: string, assistantMessageId: string) => {
    run.jobId = jobId;
    run.assistantMessageId = assistantMessageId;
    if (generationRunsRef.current.get(run.chatId) === run) {
      setGenerationRuns((current) => ({ ...current, [run.chatId]: { kind: run.kind, jobId } }));
    }
  };

  const beginContextBuildRun = (
    projectId: string,
    planId: string,
    jobId: string,
    chatId: string,
    assistantMessageId: string,
  ) => {
    const active = generationRunsRef.current.get(chatId);
    if (active?.kind === "context-build" && active.planId === planId) return active;
    const run = beginGenerationRun("context-build", chatId);
    run.projectId = projectId;
    run.planId = planId;
    setGenerationRunJob(run, jobId, assistantMessageId);
    return run;
  };

  const finishGenerationRun = (run: ActiveGenerationRun) => {
    if (generationRunsRef.current.get(run.chatId) !== run) return;
    if (run.assistantMessageId) trackLiveChatMessage(run.assistantMessageId, "idle");
    generationRunsRef.current.delete(run.chatId);
    setGenerationRuns((current) => {
      const next = { ...current };
      delete next[run.chatId];
      return next;
    });
  };

  const cancelGenerationJob = async (jobId: string) => {
    // The stop click can win the race with the server creating the job, so retry
    // a short-lived 404 before giving up.
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        const res = await fetch(`${API_URL}/a2a/jobs/${encodeURIComponent(jobId)}/cancel`, {
          method: "POST",
          headers: await generationRequestHeaders(),
        });
        if (res.ok || res.status !== 404) return;
      } catch (error) {
        console.warn("Could not notify the backend that generation was stopped.", error);
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
  };

  const cancelContextBuild = async (projectId: string, planId: string) => {
    try {
      const response = await fetch(
        `${API_URL}/projects/${encodeURIComponent(projectId)}/build/plans/${encodeURIComponent(planId)}/cancel`,
        { method: "POST", headers: await generationRequestHeaders() },
      );
      if (!response.ok) throw new Error(await readApiErrorMessage(response));
    } catch (error) {
      console.warn("Could not notify the backend that the build was stopped.", error);
      setGenerationInputNotice(error instanceof Error ? error.message : "Could not stop the build.");
    }
  };

  const executeContextBuild = async (
    projectId: string,
    planId: string,
    run?: ActiveGenerationRun,
  ) => {
    if (!hostedChatEnabled) return;
    for (let attempt = 1; attempt <= 4; attempt += 1) {
      try {
        const response = await fetch(
          `${API_URL}/projects/${encodeURIComponent(projectId)}/build/plans/${encodeURIComponent(planId)}/execute`,
          {
            method: "POST",
            headers: await generationRequestHeaders(),
            signal: run?.controller.signal,
          },
        );
        if (!response.ok) throw new Error(await readApiErrorMessage(response));
        return;
      } catch (error) {
        if (run?.cancelled || run?.controller.signal.aborted) return;
        console.warn("The build execution request ended before the plan reached a terminal state.", error);
        if (attempt === 4) {
          setGenerationInputNotice(
            error instanceof Error ? error.message : "The build execution request ended unexpectedly.",
          );
          return;
        }
        setGenerationInputNotice("Build connection interrupted. Resuming from the latest saved stage…");
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      }
    }
  };

  const stopContextBuildMessage = (message: ChatMessage) => {
    const projectId = message.contextProjectId;
    const planId = message.buildPlanId;
    if (!projectId || !planId) return;
    const active = generationRunsRef.current.get(activeChatId);
    if (active?.kind === "context-build" && active.planId === planId) {
      stopActiveGeneration();
      return;
    }
    const stoppedMessage = "Build stopped by you. Your project brief is preserved.";
    updateChatMessage(message.id, { content: stoppedMessage, status: "cancelled" });
    updateThreadMessage(activeChatId, message.id, { content: stoppedMessage, status: "cancelled" });
    setGenerationInputNotice("Build stopped. Your project brief is preserved.");
    setIsLoading(false);
    void cancelContextBuild(projectId, planId);
  };

  const stopActiveGeneration = (chatId = activeChatId) => {
    const run = generationRunsRef.current.get(chatId);
    if (!run) return;

    run.cancelled = true;
    run.controller.abort();
    if (run.assistantMessageId) {
      const patch: Partial<Omit<ChatMessage, "id">> = {
        content: run.kind === "context-build"
          ? "Build stopped by you. Your project brief is preserved."
          : "Generation stopped by you.",
        status: "cancelled",
      };
      if (run.kind !== "project-chat") updateChatMessage(run.assistantMessageId, patch);
      updateThreadMessage(run.chatId, run.assistantMessageId, patch);
    }
    setGenerationInputNotice(
      run.kind === "context-build"
        ? "Build stopped. Your project brief is preserved."
        : "Generation stopped. You can send another message whenever you're ready.",
    );
    if (authoringMode && run.openCodeSessionId) {
      window.clearTimeout(openCodePollTimersRef.current[run.openCodeSessionId]);
      delete openCodePollTimersRef.current[run.openCodeSessionId];
      delete openCodeSessionsRef.current[run.chatId];
      void generationRequestHeaders()
        .then((headers) => cancelOpenCodeSession(API_URL, headers, run.openCodeSessionId || ""))
        .catch(() => undefined);
    } else if (run.kind === "context-build" && run.projectId && run.planId) {
      void cancelContextBuild(run.projectId, run.planId);
    } else if (run.jobId) {
      void cancelGenerationJob(run.jobId);
    }
    finishGenerationRun(run);
  };

  const watchContextBuild = (
    projectId: string,
    planId: string,
    jobId: string,
    chatId: string,
    assistantMessageId: string,
    run?: ActiveGenerationRun,
  ) => {
    if (!hostedChatEnabled) return;
    const watcherKey = `${projectId}:${planId}`;
    if (contextBuildWatchersRef.current.has(watcherKey)) return;
    contextBuildWatchersRef.current.add(watcherKey);
    void executeContextBuild(projectId, planId, run);
    let attempts = 0;
    const poll = async () => {
      if (run?.cancelled || run?.controller.signal.aborted) {
        contextBuildWatchersRef.current.delete(watcherKey);
        return;
      }
      attempts += 1;
      try {
        const response = await fetch(
          `${API_URL}/projects/${encodeURIComponent(projectId)}/build/plans/${encodeURIComponent(planId)}`,
          { headers: await generationRequestHeaders(), signal: run?.controller.signal },
        );
        if (!response.ok) throw new Error(await readApiErrorMessage(response));
        const plan = await response.json();
        const planStatus = typeof plan?.status === "string" ? plan.status : "";
        const task = plan?.jobs?.[jobId];
        const progressEvents = pipelineEventsFromWorkerTask(task);
        let synchronizedProgress: AgentPipelineProgress | null = null;
        if (progressEvents.length) {
          const seedProgress = createAgentPipelineProgress(
            defaultAgentPipelineSteps,
            false,
            typeof task?.started_at === "string" ? task.started_at : chatTimestamp(),
            jobId,
          );
          const progressJob: A2AJob = {
            job_id: jobId,
            action: "forma.generate_project",
            sender: "worker-orchestrator",
            recipient: "forma",
            status: "running",
            started_at: typeof task?.started_at === "string" ? task.started_at : null,
            progress_events: progressEvents,
          };
          synchronizedProgress = progressFromJobEvents(progressJob, seedProgress, false);
          applyChatPipelineProgressFromJob(assistantMessageId, progressJob, seedProgress, false);
          applyThreadPipelineProgressFromJob(chatId, assistantMessageId, progressJob, seedProgress, false);
        }
        if (planStatus === "succeeded") {
          const projectResponse = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}`, {
            headers: await optionalAuthHeaders(),
          });
          if (!projectResponse.ok) throw new Error(await readApiErrorMessage(projectResponse));
          const projectData = await projectResponse.json();
          const ir = withProjectResponseMetadata(projectData.project_ir, projectData);
          const title = ir?.overview?.title || "Project";
          setProjectIR(ir);
          setContextWorkflowStates((current) => ({ ...current, [chatId]: "awaiting_feedback" }));
          rememberProjectRecord({
            project_id: projectId,
            chat_id: chatId,
            title,
            prompt: projectData.prompt || title,
            created_at: projectData.created_at || chatTimestamp(),
            can_chat: true,
            creator_display: "you",
            creator_image_url: userImageUrl,
            parts_count: Array.isArray(ir?.components) ? ir.components.length : 0,
            save_count: 0,
            remix_count: 0,
            saved: false,
          });
          rememberChatItem({
            chatId,
            title,
            projectId,
            createdAt: chatTimestamp(),
            projectCount: 1,
          });
          const readyMessage = `${title} is ready. The first structured design revision is available for review.`;
          updateChatMessage(assistantMessageId, {
            content: readyMessage,
            status: "success",
            ...revisionFromProject(ir),
            pipelineProgress: synchronizedProgress,
            projectId,
            contextProjectId: projectId,
            workflowState: "awaiting_feedback",
          });
          updateThreadMessage(chatId, assistantMessageId, {
            content: readyMessage,
            status: "success",
            ...revisionFromProject(ir),
            pipelineProgress: synchronizedProgress,
            projectId,
            contextProjectId: projectId,
            workflowState: "awaiting_feedback",
          });
          setGenerationInputNotice("Design ready for review.");
          void fetchProjectHistory();
          if (!authRequired || isSignedIn) void fetchMyProjectHistory();
          contextBuildWatchersRef.current.delete(watcherKey);
          if (run) finishGenerationRun(run);
          return;
        }
        if (planStatus === "cancelled" || planStatus === "canceled") {
          const stoppedMessage = "Build stopped by you. Your project brief is preserved.";
          updateChatMessage(assistantMessageId, { content: stoppedMessage, status: "cancelled" });
          updateThreadMessage(chatId, assistantMessageId, { content: stoppedMessage, status: "cancelled" });
          setGenerationInputNotice("Build stopped. Your project brief is preserved.");
          contextBuildWatchersRef.current.delete(watcherKey);
          if (run) finishGenerationRun(run);
          return;
        }
        if (planStatus === "partial") {
          try {
            const projectResponse = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}`, {
              headers: await optionalAuthHeaders(),
            });
            if (projectResponse.ok) {
              const projectData = await projectResponse.json();
              setProjectIR(withProjectResponseMetadata(projectData.project_ir, projectData));
            }
          } catch (error) {
            console.warn("Could not load the preserved partial project.", error);
          }
          const failedStage = task?.result?.metadata?.generation_retry?.retry_stage;
          const partialMessage = typeof failedStage === "string" && failedStage
            ? `The ${failedStage.replaceAll("_", " ")} stage failed. Earlier and independent work was preserved.`
            : "One build stage failed. Earlier and independent work was preserved.";
          updateChatMessage(assistantMessageId, {
            content: partialMessage,
            status: "error",
            pipelineProgress: synchronizedProgress,
            projectId,
            workflowState: "awaiting_feedback",
          });
          updateThreadMessage(chatId, assistantMessageId, {
            content: partialMessage,
            status: "error",
            pipelineProgress: synchronizedProgress,
            projectId,
            workflowState: "awaiting_feedback",
          });
          setGenerationInputNotice("Partial design saved. Retry will resume from the failed stage.");
          contextBuildWatchersRef.current.delete(watcherKey);
          if (run) finishGenerationRun(run);
          return;
        }
        if (planStatus === "failed") {
          const failureMessage = typeof task?.error?.message === "string"
            ? task.error.message
            : "The design build stopped after an agent failure.";
          updateChatMessage(assistantMessageId, { content: failureMessage, status: "error", workflowState: "awaiting_feedback" });
          updateThreadMessage(chatId, assistantMessageId, { content: failureMessage, status: "error", workflowState: "awaiting_feedback" });
          setGenerationInputNotice(failureMessage);
          contextBuildWatchersRef.current.delete(watcherKey);
          if (run) finishGenerationRun(run);
          return;
        }
      } catch (error) {
        if (run?.controller.signal.aborted) {
          contextBuildWatchersRef.current.delete(watcherKey);
          return;
        }
        if (attempts >= 600) {
          setGenerationInputNotice("Live build updates were interrupted. Checking the saved build status.");
        }
      }
      if (attempts >= 600) {
        contextBuildWatchersRef.current.delete(watcherKey);
        trackLiveChatMessage(assistantMessageId, "idle");
        if (run) finishGenerationRun(run);
        return;
      }
      window.setTimeout(poll, 2000);
    };
    window.setTimeout(poll, 750);
  };

  const resetFailedContextBuild = async (message: ChatMessage) => {
    if (!requireHostedChatEnabled()) return;
    const projectId = message.contextProjectId;
    const planId = message.buildPlanId;
    const jobId = message.buildJobId;
    const chatId = activeChatId;
    if (!projectId || !planId || !jobId || !chatId || generationRunsRef.current.has(chatId)) return;

    setResettingBuildMessageId(message.id);
    setGenerationInputNotice(null);
    try {
      const response = await fetch(
        `${API_URL}/projects/${encodeURIComponent(projectId)}/build/plans/${encodeURIComponent(planId)}/reset`,
        { method: "POST", headers: await generationRequestHeaders() },
      );
      if (!response.ok) throw new Error(await readApiErrorMessage(response));

      const resetPlan = await response.json();
      const resetJobId = typeof resetPlan?.jobs?.[jobId]?.request?.job_id === "string"
        ? resetPlan.jobs[jobId].request.job_id
        : jobId;
      const previousProgress = message.pipelineProgress;
      const resetTask = resetPlan?.jobs?.[jobId];
      const resetEvents = pipelineEventsFromWorkerTask(resetTask);
      const seedProgress = createAgentPipelineProgress(
        previousProgress?.steps || defaultAgentPipelineSteps,
        progressIncludesImageStep(previousProgress),
        chatTimestamp(),
        resetJobId,
      );
      const progress = resetEvents.length
        ? progressFromJobEvents({
            job_id: resetJobId,
            action: "forma.generate_project",
            sender: "worker-orchestrator",
            recipient: "forma",
            status: "running",
            progress_events: resetEvents,
          }, seedProgress, false)
        : seedProgress;
      const patch: Partial<Omit<ChatMessage, "id">> = {
        content: "Trying the design build again with the preserved project brief.",
        status: "loading",
        pipelineProgress: progress,
        workflowState: "building",
      };
      updateChatMessage(message.id, patch);
      updateThreadMessage(chatId, message.id, patch);
      setContextWorkflowStates((current) => ({ ...current, [chatId]: "building" }));
      setGenerationInputNotice("Job reset. The build agents are trying again.");

      const run = beginContextBuildRun(projectId, planId, resetJobId, chatId, message.id);
      watchContextBuild(projectId, planId, resetJobId, chatId, message.id, run);
    } catch (error) {
      setGenerationInputNotice(error instanceof Error ? error.message : "Could not reset the failed job.");
    } finally {
      setResettingBuildMessageId(null);
    }
  };

  const renderConversationPipelineProgress = (message: ConversationMessage) => {
    const buildMessage = message as ChatMessage;
    const buildControls = contextBuildControls(
      buildMessage,
      Boolean(activeGeneration || pendingContextBuildMessage),
    );
    const controls = {
      canStop: buildControls.canStop,
      canReset: hostedChatEnabled && buildControls.canReset,
    };
    return (
      <AgentPipelineProgressView
        progress={buildMessage.pipelineProgress}
        status={buildMessage.status}
        compact
        onStop={controls.canStop ? () => stopContextBuildMessage(buildMessage) : undefined}
        onReset={controls.canReset ? () => void resetFailedContextBuild(buildMessage) : undefined}
        resetting={resettingBuildMessageId === buildMessage.id}
      />
    );
  };

  // Saved builds are observed by useChatActivity. Opening history must not
  // call executeContextBuild; execution belongs to explicit submit/retry actions.

  const submitGatherContext = async (answer?: string, target?: { chatId: string; projectId: string }) => {
    if (generationRunsRef.current.has(target?.chatId || activeChatId)) return;
    if (authoringMode) {
      if (selectedImage || selectedDocument) {
        setGenerationInputNotice("Image and PDF attachments are not available in FormaAgent authoring yet.");
        return;
      }
      const text = (answer ?? prompt).trim();
      if (!text || !openCodeConnectorId) {
        setGenerationInputNotice(openCodeConnectorId ? "Describe the hardware project you want OpenCode to author." : "FormaAgent authoring is not configured for this deployment.");
        return;
      }
      const requestChatId = activeChatId || newBuildChatId();
      setActiveChatId(requestChatId);
      rememberChatItem({ chatId: requestChatId, title: text, projectId: "", createdAt: chatTimestamp(), projectCount: 0 });
      syncChatRoute(requestChatId);
      const userMessageId = appendChatMessage({ id: newChatMessageId(), role: "user", content: text, status: "idle" });
      appendThreadMessage(requestChatId, { id: userMessageId, role: "user", content: text, status: "idle" });
      const assistantMessageId = appendChatMessage({ id: newChatMessageId(), role: "assistant", content: "Sending to OpenCode…", status: "loading" });
      appendThreadMessage(requestChatId, { id: assistantMessageId, role: "assistant", content: "Sending to OpenCode…", status: "loading" });
      setPrompt("");
      setGenerationInputNotice(null);
      void submitOpenCodeTurn({ chatId: requestChatId, message: text, assistantMessageId });
      return;
    }
    if (contextSubmitting) return;
    if (!(await requireSignedInForGeneration())) return;
    if (!requireHostedChatEnabled()) return;

    const submittedPrompt = answer ?? prompt;
    const validation = validateGenerationInput(submittedPrompt, Boolean(selectedImage || selectedDocument));
    if (!validation.isValid) {
      setGenerationInputNotice(validation.message);
      return;
    }

    const requestChatId = target?.chatId || activeChatId || newBuildChatId();
    const requestProjectId = target?.projectId || contextProjectIdsRef.current[requestChatId] || (
      /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(requestChatId)
        ? requestChatId
        : newBuildChatId()
    );
    contextProjectIdsRef.current[requestChatId] = requestProjectId;
    const text = submittedPrompt.trim();
    const imageData = selectedImage;
    const documentData = selectedDocument;
    const userMessageId = newChatMessageId();
    const assistantMessageId = newChatMessageId();
    const userContent = text
      || (documentData ? `Shared ${documentData.name} as project context.` : "Shared a hardware reference image.");

    setActiveChatId(requestChatId);
    rememberChatItem({
      chatId: requestChatId,
      title: target ? projectTitle : text || documentData?.name || "Hardware reference",
      projectId: target?.projectId || "",
      createdAt: chatTimestamp(),
      projectCount: target ? 1 : 0,
    });
    if (!target) syncChatRoute(requestChatId);
    appendChatMessage({ id: userMessageId, role: "user", content: userContent, imagePreview: imageData, status: "idle" });
    appendThreadMessage(requestChatId, { id: userMessageId, role: "user", content: userContent, imagePreview: imageData, status: "idle" });
    appendChatMessage({ id: assistantMessageId, role: "assistant", content: "Thinking…", status: "loading" });
    appendThreadMessage(requestChatId, { id: assistantMessageId, role: "assistant", content: "Thinking…", status: "loading" });
    if (target) setProjectChatInput("");
    else setPrompt("");
    setSelectedImage(null);
    setSelectedImageSource("upload");
    setSelectedDocument(null);
    setGenerationInputNotice(null);
    setContextSubmitting(true);

    try {
      const res = await fetch(`${API_URL}/projects/${encodeURIComponent(requestProjectId)}/context/messages`, {
        method: "POST",
        headers: await generationRequestHeaders(),
        body: JSON.stringify({
          conversation_id: requestChatId,
          text,
          generation_mode: generationMode,
          attachments: [
            ...(imageData ? [{
              attachment_id: `context-image-${userMessageId}`,
              kind: "image",
              name: "hardware-reference.png",
              media_type: imageData.match(/^data:([^;,]+)/)?.[1] || "image/png",
              data_url: imageData,
              source: selectedImageSource,
            }] : []),
            ...(documentData ? [{
              attachment_id: `context-document-${userMessageId}`,
              kind: "document",
              name: documentData.name,
              media_type: documentData.mediaType,
              data_url: documentData.dataUrl,
              source: "upload",
            }] : []),
          ],
        }),
      });
      if (!res.ok) {
        noteAuthResponseStatus(res.status);
        throw new Error(await readApiErrorMessage(res));
      }
      const data = await res.json();
      const turnKind = typeof data?.turn_kind === "string" ? data.turn_kind : "context";
      const persistedProjectId = typeof data?.design_brief?.project_id === "string"
        ? data.design_brief.project_id
        : typeof data?.workflow?.project_id === "string"
          ? data.workflow.project_id
          : "";
      const workflowState = typeof data?.workflow?.state === "string" ? data.workflow.state : "";
      const buildPlanId = typeof data?.build_execution?.plan_id === "string"
        ? data.build_execution.plan_id
        : "";
      const buildJobId = typeof data?.build_execution?.job_id === "string"
        ? data.build_execution.job_id
        : "";
      const buildExecutionStatus = typeof data?.build_execution?.status === "string"
        ? data.build_execution.status
        : "";
      const buildIsActive = buildExecutionStatus === "planned" || buildExecutionStatus === "running";
      const buildPipelineProgress = buildPlanId
        ? createAgentPipelineProgress(defaultAgentPipelineSteps, generateProductImage, chatTimestamp(), buildJobId || null)
        : null;
      if (persistedProjectId) contextProjectIdsRef.current[requestChatId] = persistedProjectId;
      if (workflowState) {
        setContextWorkflowStates((current) => ({ ...current, [requestChatId]: workflowState }));
      }
      const assistantContent = typeof data?.assistant_message === "string"
        ? data.assistant_message
        : "How can I help with your hardware idea?";
      updateChatMessage(assistantMessageId, {
        content: assistantContent,
        status: buildIsActive ? "loading" : buildExecutionStatus === "failed" ? "error" : "success",
        pipelineProgress: buildPipelineProgress,
        contextProjectId: persistedProjectId || null,
        workflowState: workflowState || null,
        contextQuestions: Array.isArray(data?.questions) ? data.questions : [],
        contextSuggestions: normalizeContextSuggestions(data?.suggestions),
        buildPlanId: buildPlanId || null,
        buildJobId: buildJobId || null,
      });
      updateThreadMessage(requestChatId, assistantMessageId, {
        content: assistantContent,
        status: buildIsActive ? "loading" : buildExecutionStatus === "failed" ? "error" : "success",
        pipelineProgress: buildPipelineProgress,
        contextProjectId: persistedProjectId || null,
        workflowState: workflowState || null,
        contextQuestions: Array.isArray(data?.questions) ? data.questions : [],
        contextSuggestions: normalizeContextSuggestions(data?.suggestions),
        buildPlanId: buildPlanId || null,
        buildJobId: buildJobId || null,
      });
      setGenerationInputNotice(
        buildPlanId
          ? "Build started. Live agent progress is shown above."
          : turnKind === "context"
          ? "Project context updated."
          : turnKind === "proceed"
            ? "Project handed to the next agent stage."
            : null,
      );
      if (buildPlanId && buildJobId && persistedProjectId) {
        const run = beginContextBuildRun(
          persistedProjectId,
          buildPlanId,
          buildJobId,
          requestChatId,
          assistantMessageId,
        );
        watchContextBuild(
          persistedProjectId,
          buildPlanId,
          buildJobId,
          requestChatId,
          assistantMessageId,
          run,
        );
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not save project context.";
      updateChatMessage(assistantMessageId, { content: message, status: "error" });
      updateThreadMessage(requestChatId, assistantMessageId, { content: message, status: "error" });
      setGenerationInputNotice(message);
    } finally {
      setContextSubmitting(false);
    }
  };

  const handleGatherContext = (event: React.FormEvent) => {
    event.preventDefault();
    void submitGatherContext();
  };

  const handleBuildNow = async () => {
    if (!requireHostedChatEnabled()) return;
    if (authoringMode) {
      setGenerationInputNotice(AUTHORING_MODE_ACTIVE_MESSAGE);
      return;
    }
    if (contextBuildStarting || contextSubmitting || generationRunsRef.current.has(activeChatId)) return;
    const requestChatId = activeChatId;
    const availableMessages = requestChatId
      ? chatThreads[requestChatId] || chatMessages
      : chatMessages;
    const persistedContextMessage = [...availableMessages]
      .reverse()
      .find((message) => Boolean(message.contextProjectId));
    const projectId = requestChatId
      ? contextProjectIdsRef.current[requestChatId] || persistedContextMessage?.contextProjectId || ""
      : "";
    if (!requestChatId || !projectId) {
      setGenerationInputNotice("Share the initial project context before starting the build.");
      return;
    }

    setContextBuildStarting(true);
    setGenerationInputNotice(null);
    try {
      const response = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}/context/messages`, {
        method: "POST",
        headers: await generationRequestHeaders(),
        body: JSON.stringify({
          conversation_id: requestChatId,
          generation_mode: generationMode,
          requested_tool: "build_project",
        }),
      });
      if (!response.ok) {
        noteAuthResponseStatus(response.status);
        throw new Error(await readApiErrorMessage(response));
      }
      const outcome = await response.json();
      const workflowState = typeof outcome?.workflow?.state === "string"
        ? outcome.workflow.state
        : "building";
      const buildPlanId = typeof outcome?.build_execution?.plan_id === "string"
        ? outcome.build_execution.plan_id
        : "";
      const buildJobId = typeof outcome?.build_execution?.job_id === "string"
        ? outcome.build_execution.job_id
        : "";
      const buildStatus = typeof outcome?.build_execution?.status === "string"
        ? outcome.build_execution.status
        : "planned";
      const buildIsActive = buildStatus === "planned" || buildStatus === "running";
      const pipelineProgress = buildPlanId
        ? createAgentPipelineProgress(defaultAgentPipelineSteps, generateProductImage, chatTimestamp(), buildJobId || null)
        : null;
      contextProjectIdsRef.current[requestChatId] = projectId;
      setContextWorkflowStates((current) => ({ ...current, [requestChatId]: workflowState }));
      const message: ChatMessage = {
        id: newChatMessageId(),
        role: "assistant",
        content: typeof outcome?.assistant_message === "string"
          ? outcome.assistant_message
          : buildIsActive
            ? "I’ve started the design build."
            : "The first design revision is ready for review.",
        status: buildIsActive ? "loading" : buildStatus === "failed" ? "error" : "success",
        timestamp: chatTimestamp(),
        contextProjectId: projectId,
        workflowState,
        pipelineProgress,
        buildPlanId: buildPlanId || null,
        buildJobId: buildJobId || null,
      };
      appendChatMessage(message);
      appendThreadMessage(requestChatId, message);
      setGenerationInputNotice(
        buildIsActive ? "Build started. Live agent progress is shown above." : "Design ready for review.",
      );
      if (buildPlanId && buildJobId) {
        const run = beginContextBuildRun(projectId, buildPlanId, buildJobId, requestChatId, message.id);
        watchContextBuild(projectId, buildPlanId, buildJobId, requestChatId, message.id, run);
      }
    } catch (error) {
      setGenerationInputNotice(error instanceof Error ? error.message : "Could not start the build.");
    } finally {
      setContextBuildStarting(false);
    }
  };

  const handleGenerate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (generationRunsRef.current.has(activeChatId)) return;
    if (!(await requireSignedInForGeneration())) return;
    if (!requireHostedChatEnabled()) return;
    if (!selectedGenerationLlm) {
      setGenerationInputNotice("Turn on at least one model provider in Settings before building.");
      return;
    }
    if (
      selectedImage &&
      (selectedGenerationLlm.supports_image_input ?? generationLlmImageSupport(selectedGenerationLlm)) === false
    ) {
      const message = `${selectedGenerationLlm.label} cannot read reference images. Choose a vision-capable model or remove the image.`;
      setGenerationInputNotice(message);
      appendChatMessage({
        role: "assistant",
        content: message,
        status: "error",
      });
      return;
    }
    const contextCheckpoint = pendingHumanContext;
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    const skipHumanContext = Boolean(
      contextCheckpoint && submitter?.name === "humanContextAction" && submitter.value === "skip"
    );
    const validationSubject = contextCheckpoint ? contextCheckpoint.basePrompt : prompt;
    const validation = validateGenerationInput(validationSubject, Boolean(selectedImage));
    if (!validation.isValid) {
      setGenerationInputNotice(validation.message);
      if (validation.message) {
        appendChatMessage({
          role: "assistant",
          content: validation.message,
          status: "error",
        });
      }
      return;
    }

    const rawPromptText = contextCheckpoint
      ? contextCheckpoint.basePrompt
      : validationSubject.trim() || "Infer a buildable hardware project from the uploaded reference image.";
    const imageData = selectedImage;
    const requestChatId = activeChatId || newBuildChatId();
    const generationRun = beginGenerationRun("chat", requestChatId);

    if (!contextCheckpoint && generationMode !== "regular") {
      setGenerationInputNotice(null);
      try {
        const clarification = await requestHumanContextQuestions(
          validationSubject.trim(),
          generationWorkflow,
          Boolean(imageData),
          generationRun.controller.signal
        );
        if (generationRun.cancelled) return;
        if (clarification.shouldAsk) {
          const answers = Object.fromEntries(clarification.questions.map((question) => [question.id, ""]));
          setActiveChatId(requestChatId);
          rememberChatItem({
            chatId: requestChatId,
            title: rawPromptText,
            projectId: "",
            createdAt: chatTimestamp(),
            projectCount: 0,
          });
          syncChatRoute(requestChatId);
          appendChatMessage({
            role: "user",
            content: rawPromptText,
            status: "idle",
          });
          appendThreadMessage(requestChatId, {
            role: "user",
            content: rawPromptText,
            status: "idle",
          });
          appendChatMessage({
            role: "assistant",
            content: [
              "A few quick questions before I build.",
              "",
              ...clarification.questions.map((question) => `- ${question.label}: ${question.question}`),
            ].join("\n"),
            status: "idle",
          });
          appendThreadMessage(requestChatId, {
            role: "assistant",
            content: [
              "A few quick questions before I build.",
              "",
              ...clarification.questions.map((question) => `- ${question.label}: ${question.question}`),
            ].join("\n"),
            status: "idle",
          });
          setPendingHumanContext({
            basePrompt: rawPromptText,
            questions: clarification.questions,
            answers,
          });
          setPrompt("");
          setGenerationInputNotice(clarification.reason || "Answer the context questions, then build.");
          finishGenerationRun(generationRun);
          return;
        }
      } catch (error) {
        if (generationRun.cancelled || (error instanceof Error && error.name === "AbortError")) return;
        finishGenerationRun(generationRun);
        throw error;
      }
    }

    if (generationRun.cancelled) return;

    const finalContextNotes = contextCheckpoint ? prompt.trim() : "";
    const promptText = contextCheckpoint
      ? skipHumanContext
        ? humanContextSkipPromptSection(contextCheckpoint.basePrompt, contextCheckpoint.questions, finalContextNotes)
        : humanContextPromptSection(contextCheckpoint, finalContextNotes)
      : rawPromptText;
    const userMessageContent = contextCheckpoint
      ? skipHumanContext
        ? humanContextSkipChatSummary(contextCheckpoint.questions, finalContextNotes)
        : humanContextChatSummary(contextCheckpoint, finalContextNotes)
      : rawPromptText;
    let generatedProject = false;
    let generatedProjectId: string | null = null;
    const frontendJobId = newFrontendJobId();
    const userMessageId = newChatMessageId();
    const assistantMessageId = newChatMessageId();
    const pipelineProgress = createAgentPipelineProgress(agentPipelineSteps, generateProductImage, chatTimestamp(), frontendJobId);
    const workflowLabel = selectedGenerationWorkflow?.label || generationWorkflow;
    const providerSuffix = selectedWorkflowUsesExternalSources ? " via live web sources" : "";
    const loadingMessage = formaDevMode
      ? `Running ${workflowLabel}${providerSuffix} with ${selectedGenerationLlm.label}.`
      : `Running ${workflowLabel}${providerSuffix}.`;
    let progressPollId: number | null = null;
    const syncProgressFromJob = async () => {
      const job = await fetchA2aJob(frontendJobId);
      if (!job) return;
      applyChatPipelineProgressFromJob(assistantMessageId, job, pipelineProgress, generateProductImage);
      applyThreadPipelineProgressFromJob(requestChatId, assistantMessageId, job, pipelineProgress, generateProductImage);
    };
    setActiveChatId(requestChatId);
    rememberChatItem({
      chatId: requestChatId,
      title: rawPromptText,
      projectId: "",
      createdAt: chatTimestamp(),
      projectCount: 0,
    });
    syncChatRoute(requestChatId);
    appendChatMessage({
      id: userMessageId,
      role: "user",
      content: userMessageContent,
      status: "idle",
    });
    appendThreadMessage(requestChatId, {
      id: userMessageId,
      role: "user",
      content: userMessageContent,
      status: "idle",
    });
    appendChatMessage({
      id: assistantMessageId,
      role: "assistant",
      content: loadingMessage,
      status: "loading",
      pipelineProgress,
    });
    appendThreadMessage(requestChatId, {
      id: assistantMessageId,
      role: "assistant",
      content: loadingMessage,
      status: "loading",
      pipelineProgress,
    });
    setGenerationRunJob(generationRun, frontendJobId, assistantMessageId);

    setGenerationInputNotice(null);
    setPendingHumanContext(null);
    setPrompt("");
    checkServerStatus();
    progressPollId = window.setInterval(() => {
      void syncProgressFromJob();
    }, ACTIVE_JOB_PROGRESS_POLL_INTERVAL_MS);
    void syncProgressFromJob();

    try {
      const res = await fetch(`${API_URL}/generate`, {
        method: "POST",
        headers: await generationRequestHeaders(),
        signal: generationRun.controller.signal,
        body: JSON.stringify({
          prompt: promptText,
          workflow: generationWorkflow,
          provider: selectedGenerationLlm.provider,
          model: selectedGenerationLlm.model,
          chat_id: requestChatId,
          client_job_id: frontendJobId,
          image_data: imageData || null,
          generate_image: generateProductImage,
          generation_mode: generationMode,
        }),
      });

      if (!res.ok) {
        noteAuthResponseStatus(res.status);
        const apiError = await readApiError(res);
        if (apiError.debug) {
          console.error("Forma API debug trace", apiError);
        }
        const errorMessage = apiError.message;
        const displayErrorMessage = compactDiagnosticText(errorMessage) || errorMessage;
        if (res.status === 400) {
          setGenerationInputNotice(displayErrorMessage);
          updateChatMessage(assistantMessageId, {
            content: displayErrorMessage,
            status: "error",
          });
          updateThreadMessage(requestChatId, assistantMessageId, {
            content: displayErrorMessage,
            status: "error",
          });
          return;
        }
        if (apiError.code === "llm_output_invalid" || apiError.code === "llm_generation_unavailable") {
          setGenerationInputNotice(displayErrorMessage);
          updateChatMessage(assistantMessageId, {
            content: displayErrorMessage,
            status: "error",
          });
          updateThreadMessage(requestChatId, assistantMessageId, {
            content: displayErrorMessage,
            status: "error",
          });
          return;
        }
        if (res.status === 503) {
          setGenerationInputNotice(displayErrorMessage);
          updateChatMessage(assistantMessageId, {
            content: displayErrorMessage,
            status: "error",
          });
          updateThreadMessage(requestChatId, assistantMessageId, {
            content: displayErrorMessage,
            status: "error",
          });
          return;
        }
        throw new Error(errorMessage);
      }

      const data = await res.json();
      if (data.job) {
        applyChatPipelineProgressFromJob(assistantMessageId, data.job, pipelineProgress, generateProductImage);
        applyThreadPipelineProgressFromJob(requestChatId, assistantMessageId, data.job, pipelineProgress, generateProductImage);
      }
      const ir = withProjectResponseMetadata(data.project_ir, data);
      setProjectIR(ir);
      const projectId = projectIdFromIR(ir);
      const responseChatId = chatIdFromIR(ir) || data.chat_id || requestChatId;
      generatedProjectId = projectId;
      setActiveChatId(responseChatId);
      rememberProjectRecord({
        project_id: projectId,
        chat_id: responseChatId,
        title: ir?.overview?.title || rawPromptText,
        prompt: promptText,
        created_at: data.created_at || chatTimestamp(),
        can_chat: true,
        creator_display: "you",
        creator_image_url: userImageUrl,
        parts_count: Array.isArray(ir?.components) ? ir.components.length : 0,
        save_count: 0,
        remix_count: 0,
        saved: false,
      });
      const successMessage = `${ir?.overview?.title || "Project"} is ready. I generated the project object, wiring view, BOM, docs, and validation metadata.`;
      rememberChatItem({
        chatId: responseChatId,
        title: ir?.overview?.title || rawPromptText,
        projectId: projectId || "",
        createdAt: chatTimestamp(),
        projectCount: projectId ? 1 : 0,
      });
      updateChatMessage(assistantMessageId, {
        content: successMessage,
        status: "success",
        ...revisionFromProject(ir),
        projectId,
      });
      if (projectId) {
        updateThreadMessage(requestChatId, userMessageId, {
          projectId,
        });
        updateThreadMessage(requestChatId, assistantMessageId, {
          content: successMessage,
          status: "success",
          ...revisionFromProject(ir),
          projectId,
        });
      }
      refreshProjectAndChatLists();
      fetchA2aJobs(jobStatusFilter, { silent: true });
      generatedProject = true;
    } catch (error) {
      if (generationRun.cancelled || (error instanceof Error && error.name === "AbortError")) {
        updateChatMessage(assistantMessageId, {
          content: "Generation stopped by you.",
          status: "cancelled",
        });
        updateThreadMessage(requestChatId, assistantMessageId, {
          content: "Generation stopped by you.",
          status: "cancelled",
        });
        return;
      }
      console.warn("Using local simulation fallback", error);
      try {
        const mockRes = await runMockCompilation(promptText, imageData);
        mockRes.project_ir.assembly_metadata = {
          ...(mockRes.project_ir.assembly_metadata || {}),
          chat_id: requestChatId,
          can_chat: true,
        };
        setProjectIR(mockRes.project_ir);
        const fallbackProjectId = projectIdFromIR(mockRes.project_ir);
        generatedProjectId = fallbackProjectId;
        const fallbackMessage = `${mockRes.project_ir?.overview?.title || "Local example"} is loaded from local fallback because live generation failed.`;
        rememberProjectRecord({
          project_id: fallbackProjectId,
          chat_id: requestChatId,
          title: mockRes.project_ir?.overview?.title || rawPromptText,
          prompt: promptText,
          created_at: chatTimestamp(),
          can_chat: true,
          creator_display: "you",
          creator_image_url: userImageUrl,
          parts_count: Array.isArray(mockRes.project_ir?.components) ? mockRes.project_ir.components.length : 0,
          save_count: 0,
          remix_count: 0,
          saved: false,
        });
        rememberChatItem({
          chatId: requestChatId,
          title: mockRes.project_ir?.overview?.title || rawPromptText,
          projectId: fallbackProjectId || "",
          createdAt: chatTimestamp(),
          projectCount: fallbackProjectId ? 1 : 0,
        });
        updateChatMessage(assistantMessageId, {
          content: fallbackMessage,
          status: "success",
          projectId: fallbackProjectId,
        });
        if (fallbackProjectId) {
          updateThreadMessage(requestChatId, userMessageId, {
            projectId: fallbackProjectId,
          });
          updateThreadMessage(requestChatId, assistantMessageId, {
            content: fallbackMessage,
            status: "success",
            projectId: fallbackProjectId,
          });
        }
        generatedProject = true;
      } catch (fallbackError) {
        const message = fallbackError instanceof Error ? fallbackError.message : "Local example fallback failed.";
        const errorMessage = generationFailureChatMessage(`Generation failed and local fallback was unavailable: ${message}`);
        setGenerationInputNotice(errorMessage);
        updateChatMessage(assistantMessageId, {
          content: errorMessage,
          status: "error",
        });
        updateThreadMessage(requestChatId, assistantMessageId, {
          content: errorMessage,
          status: "error",
        });
      }
    } finally {
      if (progressPollId !== null) window.clearInterval(progressPollId);
      if (generatedProject) {
        setSelectedImage(null);
        setActiveTab("overview");
      }
      if (generatedProjectId) {
        refreshProjectAndChatLists();
      }
      finishGenerationRun(generationRun);
    }
  };

  const handleProjectChatGenerate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (generationRunsRef.current.has(currentProjectChatId || activeChatId)) return;
    if (selectedImage || selectedDocument) {
      if (!currentProjectId || !currentUserOwnsProject) return;
      await submitGatherContext(projectChatInput, {
        chatId: currentProjectChatId || activeChatId || newBuildChatId(),
        projectId: currentProjectId,
      });
      return;
    }
    if (authoringMode) {
      const handoffProjectId = currentProjectId;
      const handoffMessage = projectChatInput.trim();
      if (!handoffProjectId || !projectIR || !handoffMessage || !openCodeConnectorId) {
        if (!openCodeConnectorId) setGenerationInputNotice("FormaAgent authoring is not configured for this deployment.");
        return;
      }
      const sourceChatId = currentProjectChatId || activeChatId || newBuildChatId();
      setActiveChatId(sourceChatId);
      const userMessageId = appendThreadMessage(sourceChatId, {
        role: "user",
        content: handoffMessage,
        status: "idle",
        projectId: handoffProjectId,
      });
      const assistantMessageId = appendThreadMessage(sourceChatId, {
        role: "assistant",
        content: "Sending to OpenCode…",
        status: "loading",
        projectId: handoffProjectId,
      });
      setProjectChatInput("");
      setGenerationInputNotice(null);
      if (sourceChatId === activeChatId) {
        setChatMessages((current) => [
          ...current,
          {
            id: userMessageId,
            role: "user",
            content: handoffMessage,
            status: "idle",
            projectId: handoffProjectId,
            timestamp: chatTimestamp(),
          },
          {
            id: assistantMessageId,
            role: "assistant",
            content: "Sending to OpenCode…",
            status: "loading",
            projectId: handoffProjectId,
            timestamp: chatTimestamp(),
          },
        ]);
      }
      void submitOpenCodeTurn({
        chatId: sourceChatId,
        message: handoffMessage,
        projectId: handoffProjectId,
        assistantMessageId,
      });
      return;
    }
    if (!requireHostedChatEnabled()) return;
    if (!(await requireSignedInForGeneration())) return;
    if (!currentUserOwnsProject) {
      setGenerationInputNotice("You can only chat with projects you own.");
      return;
    }
    if (!selectedGenerationLlm) {
      setGenerationInputNotice("Turn on at least one model provider in Settings before changing this project.");
      return;
    }
    if (!currentProjectId || !projectIR) return;

    const userMessage = projectChatInput.trim();
    if (!userMessage) return;

    const sourceProjectId = currentProjectId;
    const sourceChatId = currentProjectChatId || activeChatId || newBuildChatId();
    const targetNamespace = activeTab === "overview" || activeTab === "exports" ? null : workspaceNamespaceForTab(activeTab);
    const generationRun = beginGenerationRun("project-chat", sourceChatId);
    setActiveChatId(sourceChatId);
    rememberChatItem({
      chatId: sourceChatId,
      title: projectTitle || userMessage,
      projectId: sourceProjectId,
      createdAt: chatTimestamp(),
      projectCount: 1,
    });
    appendThreadMessage(sourceChatId, {
      role: "user",
      content: userMessage,
      status: "idle",
      projectId: sourceProjectId,
    });
    const assistantMessageId = appendThreadMessage(sourceChatId, {
      role: "assistant",
      content: `Applying your change to ${projectTitle}.`,
      status: "loading",
      projectId: sourceProjectId,
    });
    generationRun.assistantMessageId = assistantMessageId;

    setProjectChatInput("");
    setGenerationInputNotice(null);
    checkServerStatus();

    try {
      const res = await fetch(`${API_URL}/projects/${encodeURIComponent(sourceProjectId)}/iterate`, {
        method: "POST",
        headers: await generationRequestHeaders(),
        signal: generationRun.controller.signal,
        body: JSON.stringify({
          instruction: userMessage,
          namespace: targetNamespace,
          provider: selectedGenerationLlm.provider,
          model: selectedGenerationLlm.model,
          save: true,
        }),
      });

      if (!res.ok) {
        noteAuthResponseStatus(res.status);
        const apiError = await readApiError(res);
        if (apiError.debug) {
          console.error("Forma API debug trace", apiError);
        }
        throw new Error(compactDiagnosticText(apiError.message) || apiError.message);
      }

      const data = await res.json();
      const ir = withProjectResponseMetadata(data.project_ir, {
        ...data,
        project_id: sourceProjectId,
        chat_id: sourceChatId,
        can_chat: true,
      });
      const responseProjectId = projectIdFromIR(ir);
      if (responseProjectId !== sourceProjectId) {
        throw new Error("Project iteration returned a different project ID.");
      }
      setProjectIR(ir);
      setActiveChatId(sourceChatId);
      rememberProjectRecord({
        project_id: sourceProjectId,
        chat_id: sourceChatId,
        title: ir?.overview?.title || projectTitle || userMessage,
        prompt: data.prompt || ir?.assembly_metadata?.source_prompt || projectTitle,
        created_at: data.created_at || chatTimestamp(),
        can_chat: true,
        creator_display: "you",
        creator_image_url: userImageUrl,
        parts_count: Array.isArray(ir?.components) ? ir.components.length : 0,
        save_count: 0,
        remix_count: 0,
        saved: false,
      });
      const revision = data?.iteration?.revision || ir?.assembly_metadata?.revision;
      const successMessage = `${ir?.overview?.title || "Project"} was updated${revision ? ` to revision ${revision}` : ""}.`;
      rememberChatItem({
        chatId: sourceChatId,
        title: ir?.overview?.title || projectTitle || userMessage,
        projectId: sourceProjectId,
        createdAt: chatTimestamp(),
        projectCount: 1,
      });

      updateThreadMessage(sourceChatId, assistantMessageId, {
        content: successMessage,
        status: "success",
        ...revisionFromProject(ir),
        projectId: sourceProjectId,
      });

      refreshProjectAndChatLists();
    } catch (error) {
      if (generationRun.cancelled || (error instanceof Error && error.name === "AbortError")) {
        updateThreadMessage(sourceChatId, assistantMessageId, {
          content: "Project update stopped by you.",
          status: "cancelled",
        });
        return;
      }
      const message = error instanceof Error ? error.message : "Project update failed.";
      updateThreadMessage(sourceChatId, assistantMessageId, {
        content: message,
        status: "error",
      });
    } finally {
      finishGenerationRun(generationRun);
    }
  };

  const loadExample = async (filename: string) => {
    setIsLoading(true);
    try {
      const res = await fetch(`/examples/${filename}`);
      if (!res.ok) return;

      const ir = await res.json();
      setProjectIR(ir);
      setActiveTab("overview");
    } catch (error) {
      console.error("Error loading example", error);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const example = params.get("example");
    const tab = normalizeTab(params.get("tab"));
    if (!example) {
      if (tab) setActiveTab(tab);
      return;
    }

    const filename = example.endsWith(".json") ? example : `${example}.json`;
    loadExample(filename).then(() => {
      if (tab) setActiveTab(tab);
    });
  }, []);


  const runMockCompilation = async (userPrompt: string, imageData?: string | null): Promise<any> => {
    const promptLower = userPrompt.toLowerCase();
    let file = "biometric_deadbolt.json";

    if (
      imageData ||
      promptLower.includes("mp3") ||
      promptLower.includes("audio") ||
      promptLower.includes("music") ||
      promptLower.includes("player") ||
      promptLower.includes("pocket")
    ) {
      file = "pocket_mp3_player.json";
    } else if (promptLower.includes("water") || promptLower.includes("plant") || promptLower.includes("soil") || promptLower.includes("garden")) {
      file = "plant_watering.json";
    } else if (promptLower.includes("thermostat") || promptLower.includes("temperature") || promptLower.includes("weather")) {
      file = "smart_thermostat.json";
    }

    const res = await fetch(`/examples/${file}`);
    if (!res.ok) {
      throw new Error(`Could not load local example ${file}.`);
    }
    const ir = await res.json();
    ir.assembly_metadata = {
      ...(ir.assembly_metadata || {}),
      reference_image_data: imageData || ir.assembly_metadata?.reference_image_data || null,
      input_mode: imageData ? "prompt_image" : "prompt",
      image_features: ir.assembly_metadata?.image_features || ir.constraints || [],
    };
    return {
      project_ir: ir,
    };
  };

  const loadOldProject = async (
    projectId: string,
    options: { syncRoute?: boolean; signal?: AbortSignal; tab?: string | null; hydrateChat?: boolean; chatId?: string; retryTransient?: boolean; openCodeResult?: boolean; onReadiness?: (readiness: unknown) => void } = {}
  ): Promise<boolean> => {
    if (options.signal?.aborted) return false;

    const shouldSyncRoute = options.syncRoute ?? true;
    const signal = options.signal;
    const isVisibleChat = () => !options.chatId || activeChatIdRef.current === options.chatId;
    if (isVisibleChat()) setIsLoading(true);
    try {
      let res: Response | undefined;
      const attempts = options.retryTransient ? 3 : 1;
      for (let attempt = 0; attempt < attempts; attempt += 1) {
        try {
          res = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}`, {
            signal,
            headers: await (options.openCodeResult ? generationRequestHeaders() : optionalAuthHeaders()),
            cache: "no-store",
          });
        } catch (error) {
          if (signal?.aborted || attempt === attempts - 1) throw error;
        }
        if (res && res.status !== 429 && res.status < 500) break;
        if (attempt < attempts - 1) await new Promise((resolve) => window.setTimeout(resolve, 500 * (attempt + 1)));
      }
      if (!res?.ok) {
        if (options.openCodeResult && res?.status !== 404) {
          throw new Error(`OpenCode finished, but its project could not be loaded${res ? ` (HTTP ${res.status})` : ""}. Try opening the saved project again.`);
        }
        return false;
      }

      const data = await res.json();
      if (signal?.aborted) return false;

      if (!data?.project_ir || typeof data.project_ir !== "object" || !Array.isArray(data.project_ir.components)) {
        throw new Error("OpenCode finished, but the project response contains no usable Hardware Intermediate Representation. Try opening the saved project again.");
      }

      const ir = withProjectResponseMetadata(data.project_ir, data);
      options.onReadiness?.(data.project_readiness);
      if (isVisibleChat()) {
        if (options.hydrateChat && canChatWithProjectIR(ir)) {
          await ensureChatThread(projectId, ir, options.chatId, signal);
        }
        if (signal?.aborted || !isVisibleChat()) return false;
        setProjectIR(ir);
        setActiveTab(normalizeTab(options.tab || "") || "overview");
        if (shouldSyncRoute) syncProjectRoute(projectId);
      }
      return true;
    } catch (error) {
      const errorName = error instanceof Error ? error.name : "";
      if (errorName !== "AbortError") {
        console.error(error);
        if (options.openCodeResult) throw error;
      }
      return false;
    } finally {
      if (!signal?.aborted && isVisibleChat()) {
        setIsLoading(false);
      }
    }
  };

  const pollOpenCodeTurn = (turn: {
    chatId: string;
    sessionId: string;
    commandId: string;
    message: string;
    projectId?: string | null;
    assistantMessageId: string;
    run: ActiveGenerationRun;
    recovery: OpenCodePendingTurn;
  }) => {
    let state: OpenCodeTurnState = {
      assistantMessage: turn.recovery.assistantMessage,
      content: turn.recovery.assistantMessage || "Waiting for OpenCode.",
      status: "loading",
      terminalEvent: null,
    };
    let failures = 0;
    const isCurrent = () => generationRunsRef.current.get(turn.chatId) === turn.run
      && !turn.run.cancelled && !turn.run.controller.signal.aborted;
    const poll = async () => {
      if (!isCurrent()) return;
      try {
        const page = await listOpenCodeEvents(
          API_URL,
          generationRequestHeaders,
          turn.sessionId,
          turn.recovery.cursor,
          { signal: turn.run.controller.signal },
        );
        if (!isCurrent()) return;
        failures = 0;
        openCodeCursorsRef.current[turn.sessionId] = page.next_cursor;
        for (const event of page.events) {
          if (event.session_id !== turn.sessionId) continue;
          state = reduceOpenCodeTurn(state, event, turn.commandId);
        }
        turn.recovery = { ...turn.recovery, cursor: page.next_cursor, assistantMessage: state.assistantMessage };
        const patch = { content: state.content, status: state.status, openCodeTurn: state.terminalEvent ? null : turn.recovery };
        updateThreadMessage(turn.chatId, turn.assistantMessageId, patch);
        if (activeChatIdRef.current === turn.chatId) updateChatMessage(turn.assistantMessageId, patch);
        const terminalEvent = state.terminalEvent;
        if (terminalEvent) {
          delete openCodePollTimersRef.current[turn.sessionId];
          const timedOut = terminalEvent.error?.code === "connector_timeout";
          if (terminalEvent.kind === "cancelled" || timedOut) delete openCodeSessionsRef.current[turn.chatId];
          if (terminalEvent.kind === "failed") {
            setOpenCodeRetries((current) => ({ ...current, [turn.chatId]: {
              message: turn.message, projectId: turn.recovery.targetProjectId, assistantMessageId: turn.assistantMessageId,
            } }));
          }
          let resultLoadError: string | null = null;
          let resultLoadNotice: string | null = null;
          if (terminalEvent.kind === "completed") {
            // A successful conversation need not create a project. Probe before linking it.
            let projectLoaded = false;
            try {
              projectLoaded = await withOpenCodeDeadline((signal) => loadOldProject(terminalEvent.project_id, {
                syncRoute: false,
                tab: "chat",
                signal,
                chatId: turn.chatId,
                retryTransient: true,
                openCodeResult: true,
                onReadiness: (readiness) => { resultLoadNotice = openCodeDesignNotice(readiness); },
              }), turn.run.controller.signal, 20_000);
              if (!projectLoaded) {
                resultLoadNotice = "OpenCode finished responding, but no saved project is available to this account. If you expected a design, try opening it from your projects or ask OpenCode to check its saved result.";
              }
              if (!projectLoaded && terminalEvent.revision_id) {
                resultLoadError = "OpenCode saved a revision, but the project is not available to this account. Try opening the saved project again.";
              }
            } catch (error) {
              resultLoadError = error instanceof Error && error.message.startsWith("OpenCode")
                ? error.message
                : "OpenCode finished, but its project could not be loaded. Check your connection and sign-in, then try opening the saved project again.";
            }
            if (!isCurrent()) return;
            if (projectLoaded) {
              contextProjectIdsRef.current[turn.chatId] = terminalEvent.project_id;
              rememberChatItem({
                chatId: turn.chatId,
                title: projectTitle || "OpenCode project",
                projectId: terminalEvent.project_id,
                createdAt: chatTimestamp(),
                projectCount: 1,
              });
              const projectPatch = { ...patch, projectId: terminalEvent.project_id, revisionId: revisionId(terminalEvent.revision_id) };
              updateThreadMessage(turn.chatId, turn.assistantMessageId, projectPatch);
              if (activeChatIdRef.current === turn.chatId) updateChatMessage(turn.assistantMessageId, projectPatch);
              refreshProjectAndChatLists();
            }
          }
          if (resultLoadError) {
            const errorPatch = { content: `${state.content}\n\n${resultLoadError}`, status: "error" as const };
            updateThreadMessage(turn.chatId, turn.assistantMessageId, errorPatch);
            if (activeChatIdRef.current === turn.chatId) updateChatMessage(turn.assistantMessageId, errorPatch);
          }
          if (resultLoadNotice && !resultLoadError && !state.content.includes(resultLoadNotice)) {
            const noticePatch = { content: `${state.content}\n\n${resultLoadNotice}`, status: state.status };
            updateThreadMessage(turn.chatId, turn.assistantMessageId, noticePatch);
            if (activeChatIdRef.current === turn.chatId) updateChatMessage(turn.assistantMessageId, noticePatch);
          }
          if (activeChatIdRef.current === turn.chatId) setGenerationInputNotice(resultLoadError || resultLoadNotice);
          if (activeChatIdRef.current === turn.chatId) setIsLoading(false);
          finishGenerationRun(turn.run);
          return;
        }
        openCodePollTimersRef.current[turn.sessionId] = window.setTimeout(poll, 1500);
      } catch (error) {
        if (!isCurrent()) return;
        failures += 1;
        // Codes and counts only: never log credentials, prompts, or response bodies.
        console.warn("opencode_poll_failed", {
          code: error instanceof OpenCodePollingError ? error.code : "transport_error",
          status: error instanceof OpenCodePollingError ? error.httpStatus : undefined,
          consecutiveFailures: failures,
        });
        if (failures >= OPENCODE_POLL_MAX_FAILURES) {
          delete openCodePollTimersRef.current[turn.sessionId];
          const patch = {
            content: [state.assistantMessage, OPENCODE_STATUS_UNKNOWN].filter(Boolean).join("\n\n"),
            status: "interrupted" as const, openCodeTurn: turn.recovery,
          };
          updateThreadMessage(turn.chatId, turn.assistantMessageId, patch);
          if (activeChatIdRef.current === turn.chatId) {
            updateChatMessage(turn.assistantMessageId, patch);
            setGenerationInputNotice(OPENCODE_STATUS_UNKNOWN);
            setIsLoading(false);
          }
          finishGenerationRun(turn.run);
          return;
        }
        if (activeChatIdRef.current === turn.chatId) setGenerationInputNotice("Connection interrupted. Checking request status again…");
        openCodePollTimersRef.current[turn.sessionId] = window.setTimeout(poll, 3000);
      }
    };
    void poll();
  };

  const submitOpenCodeTurn = async ({
    chatId,
    message,
    projectId,
    assistantMessageId,
  }: {
    chatId: string;
    message: string;
    projectId?: string | null;
    assistantMessageId: string;
  }) => {
    if (generationRunsRef.current.has(chatId)) return;
    if (!openCodeConnectorId) {
      const error = "FormaAgent authoring is not configured for this deployment.";
      updateChatMessage(assistantMessageId, { content: error, status: "error" });
      updateThreadMessage(chatId, assistantMessageId, { content: error, status: "error" });
      setGenerationInputNotice(error);
      return;
    }
    setOpenCodeRetries((current) => {
      const next = { ...current };
      delete next[chatId];
      return next;
    });
    const pending = { content: "Waiting for Forma Agent.", status: "loading" as const };
    updateThreadMessage(chatId, assistantMessageId, pending);
    if (activeChatIdRef.current === chatId) updateChatMessage(assistantMessageId, pending);
    const run = beginGenerationRun(projectId ? "project-chat" : "chat", chatId);
    run.assistantMessageId = assistantMessageId;
    try {
      const headers = await generationRequestHeaders();
      if (run.cancelled) return;
      let session = openCodeSessionsRef.current[chatId];
      if (!session) {
        session = await createOpenCodeSession(API_URL, headers, openCodeConnectorId, projectId);
        // Stop may be clicked before session creation responds. Do not enqueue
        // a command or replace a newer run's session after that cancellation.
        if (run.cancelled) {
          await cancelOpenCodeSession(API_URL, headers, session.session_id);
          return;
        }
        openCodeSessionsRef.current[chatId] = session;
        openCodeCursorsRef.current[session.session_id] = 0;
      }
      run.projectId = session.project_id;
      run.openCodeSessionId = session.session_id;
      const command = await submitOpenCodeCommand(API_URL, headers, session.session_id, message);
      if (run.cancelled) return;
      setGenerationRunJob(run, command.command_id, assistantMessageId);
      if (activeChatIdRef.current === chatId) {
        setGenerationInputNotice("Sent to OpenCode. Live authoring status will appear here.");
      }
      const recovery: OpenCodePendingTurn = {
        sessionId: session.session_id, commandId: command.command_id, projectId: session.project_id,
        message, targetProjectId: projectId || null, cursor: openCodeCursorsRef.current[session.session_id] || 0, assistantMessage: null,
      };
      updateThreadMessage(chatId, assistantMessageId, { openCodeTurn: recovery });
      if (activeChatIdRef.current === chatId) updateChatMessage(assistantMessageId, { openCodeTurn: recovery });
      pollOpenCodeTurn({ chatId, sessionId: session.session_id, commandId: command.command_id, message, projectId, assistantMessageId, run, recovery });
    } catch (error) {
      if (run.cancelled) return;
      const messageText = error instanceof Error ? error.message : "OpenCode could not accept this request.";
      updateChatMessage(assistantMessageId, { content: messageText, status: "error" });
      updateThreadMessage(chatId, assistantMessageId, { content: messageText, status: "error" });
      finishGenerationRun(run);
      if (activeChatIdRef.current === chatId) setGenerationInputNotice(messageText);
    }
  };

  const pendingOpenCodeMessage = (chatId: string) => {
    if (generationRunsRef.current.has(chatId)) return undefined;
    const messages = chatThreads[chatId]?.length ? chatThreads[chatId] : chatId === activeChatId ? chatMessages : [];
    return [...messages].reverse().find((message) => message.openCodeTurn
      && (message.status === "interrupted" || message.status === "loading"));
  };

  const checkOpenCodeStatus = (chatId: string) => {
    const message = pendingOpenCodeMessage(chatId);
    const recovery = message?.openCodeTurn;
    if (!message || !recovery) return;
    const run = beginGenerationRun("project-chat", chatId);
    run.openCodeSessionId = recovery.sessionId;
    run.projectId = recovery.projectId;
    setGenerationRunJob(run, recovery.commandId, message.id);
    const patch = { status: "loading" as const, content: recovery.assistantMessage || "Checking request status…" };
    updateThreadMessage(chatId, message.id, patch);
    if (activeChatIdRef.current === chatId) {
      updateChatMessage(message.id, patch);
      setGenerationInputNotice(null);
    }
    pollOpenCodeTurn({ ...recovery, chatId, assistantMessageId: message.id, run, recovery });
  };

  // A saved loading turn has no live browser poll after reload. Expose a GET-only
  // recovery action instead of rendering its historical spinner indefinitely.
  useEffect(() => {
    const messages = chatThreads[activeChatId] || chatMessages;
    if (generationRunsRef.current.has(activeChatId)) return;
    for (const message of messages) {
      if (message.status !== "loading" || !message.openCodeTurn) continue;
      const patch = {
        status: "interrupted" as const,
        content: [message.openCodeTurn.assistantMessage, OPENCODE_STATUS_UNKNOWN].filter(Boolean).join("\n\n"),
      };
      updateThreadMessage(activeChatId, message.id, patch);
      updateChatMessage(message.id, patch);
    }
    // Updates are scoped to the selected chat and only change orphaned turns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChatId, chatMessages, chatThreads]);

  useEffect(() => {
    setGenerationRuns({});
    setOpenCodeRetries({});
    const runs = generationRunsRef.current;
    return () => {
      runs.forEach((run) => { run.cancelled = true; run.controller.abort(); });
      runs.clear();
      Object.values(openCodePollTimersRef.current).forEach(window.clearTimeout);
      openCodePollTimersRef.current = {};
      openCodeSessionsRef.current = {};
      openCodeCursorsRef.current = {};
    };
  }, [authIdentityKey]);

  const loadedProjectId = projectIdFromIR(projectIR);

  useEffect(() => {
    if (currentRouteProjectId || homeView !== "chat" || !inlineChatProjectId || loadedProjectId === inlineChatProjectId) return;
    if (currentRouteChatId && activeChatId !== safeDecodeChatId(currentRouteChatId)) return;

    const controller = new AbortController();
    let retryTimer: number | null = null;
    let attempt = 0;
    const maxAttempts = 10;

    const hydrateInlineProject = async () => {
      attempt += 1;
      try {
        const res = await fetch(`${API_URL}/projects/${encodeURIComponent(inlineChatProjectId)}`, {
          signal: controller.signal,
          headers: await optionalAuthHeaders(),
        });
        if (!res.ok) throw new Error(`Project output is not available yet (${res.status}).`);

        const data = await res.json();
        if (controller.signal.aborted) return;
        const ir = withProjectResponseMetadata(data.project_ir, data);
        if (canChatWithProjectIR(ir)) {
          await ensureChatThread(inlineChatProjectId, ir, currentRouteChatId ? safeDecodeChatId(currentRouteChatId) : undefined, controller.signal);
        }
        if (controller.signal.aborted) return;
        setProjectIR(ir);
        setActiveTab("overview");
      } catch (error) {
        if (controller.signal.aborted) return;
        if (attempt < maxAttempts) {
          const retryDelayMs = Math.min(500 * (2 ** (attempt - 1)), 5000);
          retryTimer = window.setTimeout(hydrateInlineProject, retryDelayMs);
          return;
        }
        console.error("Could not hydrate inline project output", error);
      }
    };

    void hydrateInlineProject();
    return () => {
      controller.abort();
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
    // The project id and loaded project identity fully define this hydration request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChatId, currentRouteChatId, currentRouteProjectId, homeView, inlineChatProjectId, loadedProjectId]);

  const routedProjectId = currentRouteProjectId ? safeDecodeProjectId(currentRouteProjectId) : "";

  useEffect(() => {
    if (!routedProjectId) {
      setRouteProjectError(null);
      return;
    }
    if (authRequired && !authLoaded) return;

    const controller = new AbortController();
    const projectId = routedProjectId;
    const tab = normalizeTab(new URLSearchParams(window.location.search).get("tab"));
    setChatRouteTransition(null);
    setRouteProjectError(null);

    setProjectIR(null);

    loadOldProject(projectId, { syncRoute: false, signal: controller.signal, tab }).then((loaded) => {
      if (controller.signal.aborted) return;
      if (!loaded) {
        setRouteProjectError("Could not load that saved project.");
        return;
      }
    });

    return () => {
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routedProjectId, authIdentityKey, authLoaded, authRequired, isSignedIn]);

  const routedChatId = currentRouteChatId && !currentRouteProjectId ? safeDecodeChatId(currentRouteChatId) : "";
  const routedChatItem = routedChatId
    ? chatListItems.find((item) => item.chatId === routedChatId) || null
    : null;
  const routedChatFound = Boolean(routedChatItem);
  const routedChatProjectId = routedChatItem?.projectId || "";
  const routedChatTitle = routedChatItem?.title || "Opening chat";

  useEffect(() => {
    if (!routedChatId || currentRouteProjectId) return;
    if (authRequired && !isSignedIn) return;

    const controller = new AbortController();
    const chatId = routedChatId;
    const chatSourcesReady = chatIndexLoaded && chatHistoryLoaded;
    const storedMessages = !authRequired || chatSourcesReady
      ? readStoredChatThread(chatId, null, chatStorageScope)
      : [];
    setActiveChatId(chatId);
    setActiveTab("overview");
    setRouteProjectError(null);
    if (storedMessages.length) {
      // The route transition can finish alongside a reply. Merge with the
      // latest state so a saved "Thinking…" snapshot cannot replace that reply.
      setChatThreads((current) => ({
        ...current,
        [chatId]: mergeFetchedChatMessages(storedMessages, current[chatId] || [], true),
      }));
      setChatMessages((current) => activeChatId === chatId
        ? mergeFetchedChatMessages(storedMessages, current, true)
        : storedMessages);
    } else {
      setChatMessages((current) => activeChatId === chatId ? current : []);
    }

    if (!chatSourcesReady) {
      setChatRouteTransition({ chatId, title: "Opening chat", projectId: "", error: null });
      return () => {
        controller.abort();
      };
    }

    if (!routedChatFound && chatSourcesReady && authRequired && chatHasStarted(storedMessages)) {
      const recoveredTitle = chatTitleFromMessages(storedMessages);
      const recoveredItem = {
        chatId,
        title: recoveredTitle,
        projectId: "",
        createdAt: chatTimestamp(),
        projectCount: 0,
      };
      setSessionChatItems((current) => mergeChatListItems([recoveredItem], current));
      persistChatThread(chatId, storedMessages, recoveredTitle);
      setChatRouteTransition(null);
      return () => {
        controller.abort();
      };
    }

    if (!routedChatFound && chatSourcesReady && authRequired) {
      setProjectIR(null);
      setChatRouteTransition({
        chatId,
        title: "Chat unavailable",
        projectId: "",
        error: "This chat does not exist or is not available to this account.",
      });
      return () => {
        controller.abort();
      };
    }

    if (!routedChatFound && chatSourcesReady) {
      rememberChatItem({
        chatId,
        title: NEW_PROJECT_TITLE,
        projectId: "",
        createdAt: chatTimestamp(),
        projectCount: 0,
      });
    }

    if (!routedChatProjectId) {
      setChatRouteTransition(null);
      if (!inlineChatProjectId) setProjectIR(null);
      return () => {
        controller.abort();
      };
    }

    if (projectIdFromIR(projectIR) === routedChatProjectId) {
      setChatRouteTransition(null);
      return () => {
        controller.abort();
      };
    }

    setChatRouteTransition({
      chatId,
      title: routedChatTitle,
      projectId: routedChatProjectId,
      error: null,
    });
    loadOldProject(routedChatProjectId, {
      syncRoute: false,
      signal: controller.signal,
      tab: "chat",
      hydrateChat: true,
      chatId,
    }).then((loaded) => {
      if (controller.signal.aborted) return;
      if (loaded) {
        setChatRouteTransition(null);
        return;
      }
      setProjectIR(null);
      setActiveTab("overview");
      const nextMessages = messagesWithoutMissingProject(
        storedMessages,
        routedChatProjectId
      );
      setChatThreads((current) => ({
        ...current,
        [chatId]: nextMessages,
      }));
      setChatMessages(nextMessages);
      writeStoredChatThread(chatId, nextMessages, chatStorageScope);
      persistChatThread(chatId, nextMessages, routedChatTitle);
      detachMissingProjectFromChat(chatId, routedChatProjectId, routedChatTitle);
      setChatRouteTransition(null);
    });

    return () => {
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routedChatId, currentRouteProjectId, routedChatFound, routedChatProjectId, inlineChatProjectId, chatIndexLoaded, chatHistoryLoaded, authRequired, isSignedIn, chatStorageScope]);

  useEffect(() => {
    if (!routedChatId || currentRouteProjectId || chatHistoryLoaded) return;
    const chatId = routedChatId;
    const timeoutId = window.setTimeout(() => {
      setChatRouteTransition((current) => (
        current?.chatId === chatId && !current.error
          ? {
              ...current,
              title: "Chat unavailable",
              error: "The chat workspace could not finish loading. Please return home and try again.",
            }
          : current
      ));
    }, 10000);
    return () => window.clearTimeout(timeoutId);
  }, [routedChatId, currentRouteProjectId, chatHistoryLoaded]);

  const findProjectForJob = (job: A2AJob) => {
    const projectId = job.result_summary?.project_id;
    if (projectId) {
      const directMatch = projectHistory.find((project: any) => project.project_id === projectId);
      return directMatch || { project_id: projectId };
    }

    const prompt = job.payload?.prompt;
    const title = job.result_summary?.title;
    if (!prompt && !title) return null;

    return projectHistory.find((project: any) => {
      const promptMatches = prompt ? project.prompt === prompt : true;
      const titleMatches = title ? project.title === title : true;
      return promptMatches && titleMatches;
    }) || null;
  };

  const chatItemForJob = (job: A2AJob, project: any = findProjectForJob(job)): ChatListItem | null => {
    const chatId = chatIdFromJob(job);
    if (!chatId) return null;

    const existing = chatListItems.find((item) => item.chatId === chatId);
    const projectId = String(project?.project_id || job.result_summary?.project_id || existing?.projectId || "").trim();
    return {
      chatId,
      title: existing?.title || job.result_summary?.title || job.payload?.prompt || job.action || NEW_PROJECT_TITLE,
      projectId,
      createdAt: existing?.createdAt || job.created_at || chatTimestamp(),
      projectCount: projectId ? 1 : existing?.projectCount || 0,
    };
  };

  const loadProjectForJob = async (job: A2AJob) => {
    const project = findProjectForJob(job);
    const chatItem = chatItemForJob(job, project);
    if (chatItem) {
      openChatItem(chatItem);
      return;
    }
    if (!project?.project_id) return;
    await loadOldProject(project.project_id);
  };

  const downloadJSONIR = () => {
    if (!projectIR) return;
    if (!currentUserOwnsProject) {
      if (authRequired && !isSignedIn) {
        openSignIn({ redirectUrl: typeof window !== "undefined" ? window.location.href : "/" });
      }
      return;
    }
    const title = projectIR.overview?.title || "forma_project";
    downloadBrowserFile(
      JSON.stringify(projectIR, null, 2),
      `${title.toLowerCase().replace(/\s+/g, "_")}_forma.json`,
      "application/json"
    );
  };

  const downloadMarkdownDocs = () => {
    if (!projectIR) return;
    if (!currentUserOwnsProject) {
      if (authRequired && !isSignedIn) {
        openSignIn({ redirectUrl: typeof window !== "undefined" ? window.location.href : "/" });
      }
      return;
    }

    const title = projectIR.overview?.title || "Untitled Hardware Project";
    const markdown = buildProjectDocsMarkdown({
      title,
      description: projectIR.overview?.description,
      assembly: projectIR.assembly || [],
      issues: [
        ...(projectIR.validation?.critical || []),
        ...(projectIR.validation?.warning || []),
        ...(projectIR.validation?.info || []),
        ...(projectIR.validation_issues || []),
      ],
    });

    downloadBrowserFile(markdown, docsExportFilename(title), "text/markdown;charset=utf-8");
  };

  const metrics = calculateProjectCostMetrics(projectIR);
  const components = useMemo(() => resolveProjectComponentInstances(projectIR), [projectIR]);
  const bomLineItems = projectIR?.bom?.length ? projectIR.bom : components;
  const schematicProject = useMemo(
    () => projectIR ? { ...projectIR, components } : projectIR,
    [components, projectIR]
  );
  const assembly = projectIR?.assembly || [];
  const constraints = projectIR?.constraints || [];
  const imageFeatures = projectIR?.assembly_metadata?.image_features?.length
    ? projectIR.assembly_metadata.image_features
    : constraints;
  const issues = [
    ...(projectIR?.validation?.critical || []),
    ...(projectIR?.validation?.warning || []),
    ...(projectIR?.validation?.info || []),
    ...(projectIR?.validation_issues || []),
  ];
  const projectTitle = projectIR?.overview?.title || "Untitled Hardware Project";
  const projectDescription = projectIR?.overview?.description || "Generated hardware package";
  const currentProjectId = projectIR?.assembly_metadata?.project_id || null;
  const currentProjectPrivate = (
    myProjectHistory.find((project: any) => project.project_id === currentProjectId)?.visibility
    || projectIR?.assembly_metadata?.visibility
  ) === "private";
  const isPublicExample = projectIR?.assembly_metadata?.status === "example" && !currentProjectId;
  const currentUserOwnsProject = Boolean(projectIR && canChatWithProjectIR(projectIR) && (!authRequired || isSignedIn));
  useEffect(() => {
    const persistedMode = projectIR?.assembly_metadata?.generation_mode;
    if (persistedMode === "regular" || persistedMode === "progressive") {
      setGenerationMode(persistedMode);
    }
  }, [currentProjectId, projectIR?.assembly_metadata?.generation_mode]);
  const currentProjectCanDownloadAssets = currentUserOwnsProject;
  const handleProgressiveVisualDecision = async (
    decision: "approve" | "revise" | "continue_to_cad",
    feedback?: string,
  ) => {
    if (!currentProjectId || !currentUserOwnsProject || visualDecisionBusy) return;
    setVisualDecisionBusy(decision);
    setVisualDecisionError(null);
    try {
      const response = await fetch(
        `${API_URL}/projects/${encodeURIComponent(currentProjectId)}/visual-decision`,
        {
          method: "POST",
          headers: await generationRequestHeaders(),
          body: JSON.stringify({ decision, feedback: feedback || null }),
        },
      );
      if (!response.ok) throw new Error(await readApiErrorMessage(response));
      const payload = await response.json();
      if (payload?.project_ir) {
        setProjectIR(withProjectResponseMetadata(payload.project_ir, payload));
      }
      if (decision === "revise" && feedback?.trim()) {
        setGenerationMode("progressive");
        setPrompt(`Revise the current concept using this feedback: ${feedback.trim()}`);
      }
      if (decision === "continue_to_cad" && payload?.cad_generated) {
        setActiveTab("cad");
      }
      void refreshProjectAndChatLists();
    } catch (error) {
      setVisualDecisionError(error instanceof Error ? error.message : "Could not update the Progressive concept review.");
    } finally {
      setVisualDecisionBusy(null);
    }
  };
  const ownerProjectChatId = projectIR && currentUserOwnsProject
    ? (chatIdFromIR(projectIR) || currentProjectId)
    : null;
  const currentProjectChatId = projectIR
    ? routedProjectId ? null : (routedChatId || ownerProjectChatId || activeChatId)
    : activeChatId;
  const currentProjectChatMessages = useMemo(
    () => currentProjectChatId ? chatThreads[currentProjectChatId] || [] : [],
    [chatThreads, currentProjectChatId]
  );
  const projectVersionHistory: ProjectHistoryConfig = {
    projectId: currentProjectId || "",
    identityKey: authIdentityKey,
    enabled: currentUserOwnsProject,
    apiUrl: API_URL,
    latestRevisionId: revisionFromProject(projectIR).revisionId,
    latestRevision: revisionNumber(projectIR?.assembly_metadata?.project_revision) || revisionNumber(projectIR?.assembly_metadata?.revision),
    getHeaders: generationRequestHeaders,
    loadLatest: (signal) => loadOldProject(currentProjectId, {
      syncRoute: false, signal, tab: activeTab, chatId: currentProjectChatId || undefined,
    }),
  };
  const retryableProjectBuildMessage = useMemo(
    () => latestRetryableContextBuildMessage(currentProjectChatMessages),
    [currentProjectChatMessages],
  );
  const projectImageCandidates = useMemo(() => {
    const chatReference =
      hardwareReferenceSrcFromChatMessages(currentProjectChatMessages) ||
      hardwareReferenceSrcFromChatMessages(chatMessages);
    return resolveProjectImageCandidates(
      withHardwareReferenceMetadata(projectIR?.assembly_metadata || {}, chatReference),
      formaDevMode,
    );
  }, [chatMessages, currentProjectChatMessages, formaDevMode, projectIR]);
  const showProductImageSection = shouldShowProductImageSection({
    imageCandidates: projectImageCandidates,
    llms: generationLlms,
    imageGeneration: imageGenerationConfigLoaded
      ? {
          configured: imageGenerationConfig.configured,
          requestCapable: imageGenerationConfig.requestCapable,
          provider: imageGenerationConfig.provider,
        }
      : null,
    metadata: projectIR?.assembly_metadata || {},
  });
  const videoImageOptions = useMemo(
    () => projectImageCandidates.filter((candidate) => !isHardwareReferenceCandidate(candidate)),
    [projectImageCandidates]
  );
  const defaultVideoImage = videoImageOptions[0]?.src || "";
  const projectVideo = useProjectVideo({
    apiUrl: API_URL,
    enabled: Boolean(projectIR && activeTab === "video"),
    projectId: currentProjectId,
    authIdentityKey,
    canManageProject: hostedChatEnabled && currentUserOwnsProject,
    canLoadProjectVideos: currentProjectCanDownloadAssets,
    imageOptions: videoImageOptions,
    defaultImage: defaultVideoImage,
    authorizeGeneration: requireSignedInForGeneration,
    getRequestHeaders: generationRequestHeaders,
    readError: readApiErrorMessage,
    modelControls: {
      models: videoModels,
      loading: videoModelsLoading,
      error: videoModelsError,
      selectedModel: selectedVideoModel,
      setSelectedModel: setSelectedVideoModel,
      aspectRatios: videoAspectRatios,
      aspectRatio: videoAspectRatio,
      setAspectRatio: setVideoAspectRatio,
    },
    generationAvailability: videoGenerationConfig,
    reviewAvailability: videoSelfCorrectionConfig,
    globalBusy: isLoading,
    setGlobalBusy: setIsLoading,
    updateProject: (nextProjectIR, response) => {
      setProjectIR(withProjectResponseMetadata(nextProjectIR, response));
    },
    refreshProjectAndChatLists,
    refreshJobs: () => {
      void fetchA2aJobs(jobStatusFilter, { silent: true });
    },
  });
  const currentProjectJobId = projectIR?.assembly_metadata?.frontend_job_id || null;
  const activeSidebarChatId = routedProjectId ? null : (currentProjectChatId || activeChatId);
  const activeSidebarChatItem = chatListItems.find((item) => item.chatId === activeSidebarChatId);
  const activeSidebarChatStarted = Boolean(
    projectIR ||
    chatHasStarted(projectIR ? currentProjectChatMessages : chatMessages) ||
    activeSidebarChatItem?.projectId ||
    activeSidebarChatItem?.projectCount
  );
  const commitOwnedWorkspaceTitle = async (nextTitle: string, options?: { chatId?: string | null; projectId?: string | null }) => {
    if (!hostedChatEnabled) return;
    const title = nextTitle.trim() || "Untitled Hardware Project";
    const chatId = options && "chatId" in options
      ? options.chatId
      : (currentProjectChatId || chatIdFromIR(projectIR) || activeChatId);
    const projectId = ((options && "projectId" in options ? options.projectId : currentProjectId) || "");
    if (projectId && projectId === currentProjectId && !currentUserOwnsProject) return;
    if (projectIR && projectId && projectId === currentProjectId) {
      setProjectIR((current: any) => {
        if (!current) return current;
        return {
          ...current,
          overview: {
            ...(current.overview || {}),
            title,
          },
        };
      });
    }
    if (chatId) {
      rememberChatItem({
        chatId,
        title,
        ...(projectId ? { projectId } : {}),
      });
    }
    if (!projectId || (authRequired && !isSignedIn)) return;
    const existingProject = myProjectHistory.find((project: any) => project?.project_id === projectId)
      || projectHistory.find((project: any) => project?.project_id === projectId);
    if (existingProject) rememberProjectRecord({ ...existingProject, title });
    try {
      const res = await fetch(`${API_URL}/projects/${encodeURIComponent(projectId)}`, {
        method: "PATCH",
        headers: await generationRequestHeaders(),
        body: JSON.stringify({ title }),
      });
      if (!res.ok) throw new Error(await readApiErrorMessage(res));
    } catch (error) {
      console.error("Error renaming project", error);
    }
  };
  const renameSidebarChat = (item: ChatListItem, title: string) => {
    if (!hostedChatEnabled) return;
    void commitOwnedWorkspaceTitle(title, { chatId: item.chatId, projectId: item.projectId || null });
  };
  const togglePinnedChat = (item: ChatListItem) => {
    if (!hostedChatEnabled) return;
    setPinnedChatIds((current) => {
      const next = new Set(current);
      if (next.has(item.chatId)) next.delete(item.chatId);
      else next.add(item.chatId);
      writePinnedChatIds(next, chatStorageScope);
      return next;
    });
  };
  const deleteSidebarChat = (item: ChatListItem) => {
    if (!hostedChatEnabled) return;
    if (item.projectId) {
      openProjectDeletion({ projectId: item.projectId, title: item.title });
      return;
    }
    forgetChatRecords([item.chatId]);
    void (async () => {
      try {
        const res = await fetch(`${API_URL}/chats/${encodeURIComponent(item.chatId)}`, {
          method: "DELETE",
          headers: await generationRequestHeaders(),
        });
        if (!res.ok && res.status !== 404) throw new Error(await readApiErrorMessage(res));
      } catch (error) {
        console.error("Error deleting chat", error);
      }
    })();
    if (activeChatId === item.chatId || (typeof window !== "undefined" && window.location.pathname === chatRoute(item.chatId))) {
      goHome();
    }
  };
  const newChatDisabled = chatAccessState !== "ready" || (homeView === "chat" && !routedProjectId && !activeSidebarChatStarted);
  const newChatDisabledReason = chatAccessState !== "ready" ? "Chat is still loading." : undefined;
  const homeChromeRef = useRef<HTMLDivElement>(null);
  const { headerAway: homeHeaderAway, bindCapture: bindHomeChromeScroll } = useChromeHeaderScroll(
    `${homeView}:${activeChatId || ""}:${activeSidebarChatStarted ? "started" : "new"}`
  );
  useEffect(() => bindHomeChromeScroll(homeChromeRef.current), [bindHomeChromeScroll, homeView, projectIR]);
  const activityThreads = useMemo(() => {
    const threads = { ...chatThreads };
    if (activeChatId) {
      const byId = new Map((threads[activeChatId] || []).map((message) => [message.id, message]));
      chatMessages.forEach((message) => byId.set(message.id, message));
      threads[activeChatId] = [...byId.values()];
    }
    return threads;
  }, [activeChatId, chatMessages, chatThreads]);
  const activityOperations = useMemo(() => collectChatOperations(activityThreads), [activityThreads]);
  const activityObservations = useChatActivity({
    apiUrl: API_URL,
    scope: chatStorageScope,
    enabled: !authRequired || (authLoaded && isSignedIn),
    operations: activityOperations,
    getHeaders: generationRequestHeaders,
  });
  useEffect(() => {
    // This is a local projection of confirmed outcomes. Do not overwrite server
    // chat history merely because its status was read, including in maintenance.
    setChatMessages((current) => settleChatActivityMessages(current, activityObservations, liveChatMessageIds));
    setChatThreads((current) => {
      let changed = false;
      const next = { ...current };
      Object.entries(current).forEach(([chatId, messages]) => {
        const settled = settleChatActivityMessages(messages, activityObservations, liveChatMessageIds);
        if (settled !== messages) {
          changed = true;
          next[chatId] = settled;
          writeStoredChatThread(chatId, settled, chatStorageScope);
        }
      });
      return changed ? next : current;
    });
  }, [activityObservations, chatStorageScope, liveChatMessageIds]);
  const chatActivityById = useMemo(() => {
    const activities: Record<string, ChatActivity> = {};
    Object.entries(activityThreads).forEach(([chatId, messages]) => {
      const activity = chatActivity(messages, activityObservations, liveChatMessageIds);
      if (activity) activities[chatId] = activity;
    });
    return activities;
  }, [activityThreads, activityObservations, liveChatMessageIds]);
  const waitingChatIds = useMemo(() => new Set(Object.entries(chatActivityById)
    .filter(([, activity]) => activity.state === "running")
    .map(([chatId]) => chatId)), [chatActivityById]);
  const projectJobs = a2aJobs.filter((job) => {
    if (currentProjectJobId && job.job_id === currentProjectJobId) return true;
    if (currentProjectId && job.result_summary?.project_id === currentProjectId) return true;
    return false;
  });
  const visibleWorkspaceTabs = useMemo(
    () => hasRenderableCadModel ? workspaceTabs : workspaceTabs.filter((item) => item.id !== "cad"),
    [hasRenderableCadModel]
  );
  const effectiveActiveTab = activeTab === "cad" && !hasRenderableCadModel ? "overview" : activeTab;
  const activeWorkspaceTab = workspaceTabMeta(effectiveActiveTab);
  const activeWorkspaceNamespace = workspaceNamespaceForTab(effectiveActiveTab);
  const displayedWorkspaceNamespace = activeWorkspaceNamespace;
  const projectNamespaceContent = (() => {
    switch (activeWorkspaceTab.id) {
      case "overview":
        return (
          <OverviewPanel
            title={projectTitle}
            description={projectDescription}
            imageCandidates={projectImageCandidates}
            features={imageFeatures}
            metrics={metrics}
            metadata={projectIR?.assembly_metadata || {}}
            systemArchitecture={projectIR?.system_architecture || null}
            showModelName={formaDevMode}
            showImageSection={showProductImageSection}
            canManageProgressiveReview={hostedChatEnabled && currentUserOwnsProject}
            visualDecisionBusy={visualDecisionBusy}
            visualDecisionError={visualDecisionError}
            onVisualDecision={handleProgressiveVisualDecision}
          />
        );
      case "bom":
        return (
          <BomPanel
            components={bomLineItems}
            metrics={metrics}
            cadSources={(projectIR?.mechanical && Array.isArray(projectIR.mechanical.cad_sources)) ? projectIR.mechanical.cad_sources : []}
            fabricationCost={Number(projectIR?.mechanical?.fabrication_cost_estimate_usd || 0)}
            canDownloadAssets={currentProjectCanDownloadAssets}
          />
        );
      case "mechanical":
        return (
          <MechanicalPanel
            systemArchitecture={projectIR?.system_architecture}
            toggles={mechToggles}
            setToggles={setMechToggles}
            electricalActive={mechElectricalActive}
            setElectricalActive={setMechElectricalActive}
            components={components}
            features={imageFeatures}
            metadata={projectIR?.assembly_metadata || {}}
            mechanical={projectIR?.mechanical || {}}
            cadModel={currentCadModel && typeof currentCadModel === "object" ? currentCadModel as Record<string, any> : null}
          />
        );
      case "cad": {
        return <CadModelPanel cadModel={currentCadModel} apiUrl={API_URL} getHeaders={generationRequestHeaders} />;
      }
      case "schematic":
        return <SchematicCanvas project={schematicProject} />;
      case "assembly":
        return (
          <AssemblyPanel
            assembly={assembly}
            issues={issues}
          />
        );
      case "exports":
        return currentProjectId ? (
          <ProjectExportsPanel
            projectId={currentProjectId}
            canDownloadAssets={currentProjectCanDownloadAssets}
            onDownloadJSON={downloadJSONIR}
            onDownloadMarkdown={downloadMarkdownDocs}
          />
        ) : null;
      case "video":
        return (
          <VideoPanel {...projectVideo} />
        );
      case "jobs":
        return (
          <JobsPanel
            jobs={projectJobs}
            loading={jobsLoading}
            error={jobsError}
            statusFilter={jobStatusFilter}
            onStatusFilterChange={changeJobStatusFilter}
            onRefresh={() => fetchA2aJobs(jobStatusFilter)}
            onOpenProject={loadProjectForJob}
            findProjectForJob={findProjectForJob}
            lastUpdatedAt={jobsLastUpdatedAt}
            pollIntervalMs={JOB_POLL_INTERVAL_MS}
            title="Project Jobs"
            description="Only jobs tied to this project are shown here."
            emptyMessage="No jobs recorded for this project and filter."
            formatLlmLabel={generationLlmLabel}
          />
        );
      case "logs":
        return canViewAdminTools ? (
          <LogsPanel
            logs={backendLogs}
            loading={logsLoading}
            error={logsError}
            lastUpdatedAt={logsLastUpdatedAt}
            onRefresh={() => fetchBackendLogs()}
            pollIntervalMs={LOG_POLL_INTERVAL_MS}
          />
        ) : null;
      default:
        return null;
    }
  })();

  useEffect(() => {
    // Routed chats are hydrated by the route loader. A project's backend chat
    // identity may differ from the browser's OpenCode conversation identity.
    if (routedProjectId || routedChatId) return;
    if (!currentUserOwnsProject) return;
    if (!currentProjectId || currentProjectChatMessages.length) return;
    void ensureChatThread(currentProjectId, projectIR);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routedProjectId, routedChatId, currentUserOwnsProject, currentProjectId, currentProjectChatMessages.length, projectIR]);

  const deliverySignatureRef = useRef<string | null>(null);
  useEffect(() => {
    if (!authoringMode || !currentProjectId) {
      setDeliveredSignal(false);
      return;
    }
    const token = currentProjectId;
    let cancelled = false;
    let pending = false;
    deliverySignatureRef.current = null;

    const signatureOf = (record: Record<string, unknown>) => {
      const updatedAt = typeof record?.updated_at === "string" ? record.updated_at : "";
      const contentUpdatedAt = typeof record?.content_updated_at === "string" ? record.content_updated_at : "";
      return `${contentUpdatedAt || updatedAt}`;
    };

    const poll = async () => {
      if (cancelled || pending) return;
      pending = true;
      try {
        const response = await fetch(`${API_URL}/projects/${encodeURIComponent(token)}`, {
          cache: "no-store",
          headers: await optionalAuthHeaders(),
        });
        if (cancelled) return;
        if (!response.ok) return;
        const data = await response.json();
        const signature = signatureOf(data);
        if (!deliverySignatureRef.current) {
          deliverySignatureRef.current = signature;
          return;
        }
        if (signature && signature !== deliverySignatureRef.current) {
          deliverySignatureRef.current = signature;
          refreshProjectAndChatLists();
          setDeliveredSignal(true);
          window.setTimeout(() => {
            if (!cancelled) setDeliveredSignal(false);
          }, AUTHORING_DELIVERED_SIGNAL_MS);
        }
      } catch {
        // Transient polling errors must not clear the tracked delivery signature.
      } finally {
        pending = false;
      }
    };

    void poll();
    const intervalId = window.setInterval(poll, AUTHORING_DELIVERY_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refreshProjectAndChatLists is intentionally excluded for parity with the other polling effects.
  }, [authoringMode, currentProjectId, optionalAuthHeaders]);

  const implicitChatRouteTransition: ChatRouteTransition | null = routedChatId && (
    activeChatId !== routedChatId ||
    !chatIndexLoaded ||
    !chatHistoryLoaded ||
    Boolean(routedChatProjectId && loadedProjectId !== routedChatProjectId)
  )
    ? {
        chatId: routedChatId,
        title: routedChatTitle,
        projectId: routedChatProjectId,
        error: null,
      }
    : null;
  const visibleChatRouteTransition = chatRouteTransition?.chatId === routedChatId
    ? chatRouteTransition
    : implicitChatRouteTransition;
  const chatTransitionProjectId = visibleChatRouteTransition?.projectId || "";
  const showChatRouteFallback = Boolean(
    routedChatId &&
      visibleChatRouteTransition &&
      (visibleChatRouteTransition.error || !chatTransitionProjectId || loadedProjectId !== chatTransitionProjectId)
  );
  const privateChatRouteRequested = Boolean(authRequired && currentRouteChatId && !currentRouteProjectId);
  const privateChatRouteDenied = privateChatRouteRequested && authLoaded && !isSignedIn;

  if (privateChatRouteDenied) {
    return (
      <AuthRequiredRouteScreen
        loading={false}
        title="Private chat"
        message="Sign in to open this chat."
        onHome={goHome}
      />
    );
  }

  if (showChatRouteFallback && visibleChatRouteTransition) {
    return (
      <WorkspaceFrame
        collapsed={sidebarCollapsed}
        workspaceStatus={workspaceStatus}
        mobileSidebar={(
          <MobileSidebarDrawer
            open={mobileSidebarOpen}
            onClose={() => setMobileSidebarOpen(false)}
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
            onHome={goHome}
            chats={chatListItems}
            activeChatId={visibleChatRouteTransition.chatId}
            onNewChat={startNewProjectChat}
            newChatDisabled={newChatDisabled}
            newChatDisabledReason={newChatDisabledReason}
             readOnly={chatReadOnly || authoringMode}
            onOpenChat={openChatItem}
            onRenameChat={renameSidebarChat}
            onPinChat={togglePinnedChat}
            onDeleteChat={deleteSidebarChat}
            waitingChatIds={waitingChatIds}
            chatActivityById={chatActivityById}
            chatsLoading={sidebarChatsLoading}
            showJobs={canViewJobs}
            jobsPending={sidebarJobsPending}
            showDeveloperTools={showDeveloperTools}
            authRequired={authRequired}
          />
        )}
        desktopSidebar={(
          <ChatSidebar
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
            onHome={goHome}
            chats={chatListItems}
            activeChatId={visibleChatRouteTransition.chatId}
            onNewChat={startNewProjectChat}
            newChatDisabled={newChatDisabled}
            newChatDisabledReason={newChatDisabledReason}
             readOnly={chatReadOnly || authoringMode}
            onOpenChat={openChatItem}
            onRenameChat={renameSidebarChat}
            onPinChat={togglePinnedChat}
            onDeleteChat={deleteSidebarChat}
            waitingChatIds={waitingChatIds}
            chatActivityById={chatActivityById}
            chatsLoading={sidebarChatsLoading}
            showJobs={canViewJobs}
            jobsPending={sidebarJobsPending}
            showDeveloperTools={showDeveloperTools}
            authRequired={authRequired}
          />
        )}
      >
        <ChatRouteFallbackPanel
          transition={visibleChatRouteTransition}
          onHome={goHome}
          onOpenSidebar={() => setMobileSidebarOpen(true)}
        />
        <ProjectDeletionDialog
          project={pendingProjectDeletion}
          acknowledged={deletionAcknowledged}
          contribute={contributeDeletedProject}
          busy={projectDeletionBusy}
          error={projectDeletionError}
          onAcknowledgedChange={setDeletionAcknowledged}
          onContributeChange={setContributeDeletedProject}
          onCancel={closeProjectDeletion}
          onConfirm={confirmProjectDeletion}
        />
      </WorkspaceFrame>
    );
  }

  if (routedProjectId && loadedProjectId !== routedProjectId) {
    return (
      <WorkspaceFrame
        collapsed={sidebarCollapsed}
        workspaceStatus={workspaceStatus}
        mobileSidebar={(
          <MobileSidebarDrawer
            open={mobileSidebarOpen}
            onClose={() => setMobileSidebarOpen(false)}
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
            onHome={goHome}
            chats={chatListItems}
            activeChatId={null}
            onNewChat={startNewProjectChat}
             newChatDisabled={newChatDisabled}
             newChatDisabledReason={newChatDisabledReason}
             readOnly={chatReadOnly || authoringMode}
            onOpenChat={openChatItem}
            onRenameChat={renameSidebarChat}
            onPinChat={togglePinnedChat}
            onDeleteChat={deleteSidebarChat}
            waitingChatIds={waitingChatIds}
            chatActivityById={chatActivityById}
            chatsLoading={sidebarChatsLoading}
            showJobs={canViewJobs}
            jobsPending={sidebarJobsPending}
            showDeveloperTools={showDeveloperTools}
            authRequired={authRequired}
          />
        )}
        desktopSidebar={(
          <ChatSidebar
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
            onHome={goHome}
            chats={chatListItems}
            activeChatId={null}
            onNewChat={startNewProjectChat}
             newChatDisabled={newChatDisabled}
             newChatDisabledReason={newChatDisabledReason}
             readOnly={chatReadOnly || authoringMode}
            onOpenChat={openChatItem}
            onRenameChat={renameSidebarChat}
            onPinChat={togglePinnedChat}
            onDeleteChat={deleteSidebarChat}
            waitingChatIds={waitingChatIds}
            chatActivityById={chatActivityById}
            chatsLoading={sidebarChatsLoading}
            showJobs={canViewJobs}
            jobsPending={sidebarJobsPending}
            showDeveloperTools={showDeveloperTools}
            authRequired={authRequired}
          />
        )}
      >
        <ProjectRouteFallbackPanel
          projectId={routedProjectId}
          error={routeProjectError}
          onHome={goHome}
          onOpenSidebar={() => setMobileSidebarOpen(true)}
        />
        <ProjectDeletionDialog
          project={pendingProjectDeletion}
          acknowledged={deletionAcknowledged}
          contribute={contributeDeletedProject}
          busy={projectDeletionBusy}
          error={projectDeletionError}
          onAcknowledgedChange={setDeletionAcknowledged}
          onContributeChange={setContributeDeletedProject}
          onCancel={closeProjectDeletion}
          onConfirm={confirmProjectDeletion}
        />
      </WorkspaceFrame>
    );
  }

  if (homeView !== "chat" || !projectIR) {
    return (
      <WorkspaceFrame
        collapsed={sidebarCollapsed}
        workspaceStatus={workspaceStatus}
        homeMobileTopPadding
        mobileSidebar={(
          <MobileSidebarDrawer
            open={mobileSidebarOpen}
            onClose={() => setMobileSidebarOpen(false)}
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
            onHome={goHome}
            chats={chatListItems}
             activeChatId={activeChatId}
            onNewChat={startNewProjectChat}
            newChatDisabled={newChatDisabled}
            newChatDisabledReason={newChatDisabledReason}
             readOnly={chatReadOnly || authoringMode}
            onOpenChat={openChatItem}
            onRenameChat={renameSidebarChat}
            onPinChat={togglePinnedChat}
            onDeleteChat={deleteSidebarChat}
            waitingChatIds={waitingChatIds}
            chatActivityById={chatActivityById}
            chatsLoading={sidebarChatsLoading}
            showJobs={canViewJobs}
            jobsPending={sidebarJobsPending}
            showDeveloperTools={showDeveloperTools}
            authRequired={authRequired}
          />
        )}
        desktopSidebar={(
          <ChatSidebar
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
            onHome={goHome}
            chats={chatListItems}
             activeChatId={activeChatId}
            onNewChat={startNewProjectChat}
            newChatDisabled={newChatDisabled}
            newChatDisabledReason={newChatDisabledReason}
             readOnly={chatReadOnly || authoringMode}
            onOpenChat={openChatItem}
            onRenameChat={renameSidebarChat}
            onPinChat={togglePinnedChat}
            onDeleteChat={deleteSidebarChat}
            waitingChatIds={waitingChatIds}
            chatActivityById={chatActivityById}
            chatsLoading={sidebarChatsLoading}
            showJobs={canViewJobs}
            jobsPending={sidebarJobsPending}
            showDeveloperTools={showDeveloperTools}
            authRequired={authRequired}
          />
        )}
      >
        <div ref={homeChromeRef} className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <MobileWorkspaceBar onOpenSidebar={() => setMobileSidebarOpen(true)} headerAway={homeHeaderAway}>
          {homeView === "settings" ? (
            <WorkspaceChromeIdentity icon={Settings} badge="General" title="Settings" />
          ) : homeView === "about" ? (
            <WorkspaceChromeIdentity icon={Handshake} badge="General" title="About" />
          ) : homeView === "projects" ? (
            <WorkspaceChromeIdentity icon={Layers} badge="Workspace" title="Community" />
          ) : homeView === "my-projects" ? (
            <WorkspaceChromeIdentity icon={Database} badge="Workspace" title="My projects" />
          ) : homeView === "jobs" ? (
            <WorkspaceChromeIdentity icon={History} badge="Workspace" title="Jobs" />
          ) : homeView === "logs" ? (
            <WorkspaceChromeIdentity icon={Terminal} badge="Workspace" title="Backend logs" />
          ) : activeSidebarChatStarted ? (
            <WorkspaceChromeIdentity
              icon={MessageSquare}
              badge="Chat"
              title={(
                <EditableWorkspaceTitle
                  value={activeSidebarChatItem?.title || NEW_PROJECT_TITLE}
                   canEdit={hostedChatEnabled && !authoringMode}
                  label="Chat title"
                  onCommit={(title) => {
                    if (activeChatId) {
                      void commitOwnedWorkspaceTitle(title, {
                        chatId: activeChatId,
                        projectId: activeSidebarChatItem?.projectId || null,
                      });
                    }
                  }}
                />
              )}
            />
          ) : null}
        </MobileWorkspaceBar>
	        <main className={`mx-auto w-full ${homeView === "chat" || homeView === "settings" || homeView === "about" ? "max-w-none" : "max-w-6xl"} ${
	          homeView === "chat"
	            ? "flex min-h-0 flex-1 flex-col overflow-hidden px-0 pb-0 pt-0 md:pt-4"
            : "min-h-0 flex-1 overflow-y-auto px-4 pb-6 pt-16 sm:px-5 md:py-8"
        }`}>
          {homeView === "projects" ? (
              <FormaProjectBrowser
                sectionRef={projectsSectionRef}
                projects={projectBrowserItems}
                title="Community"
                pageSize={PROJECT_GALLERY_PAGE_SIZE}
                loading={projectsPageLoading}
                onOpenProject={(projectId) => router.push(projectRoute(projectId))}
                onToggleSave={canInteractWithGallery ? (project) => {
                  const item = projectGalleryItems.find((candidate) => candidate.projectId === project.project_id);
                  return item ? handleToggleProjectSave(item) : undefined;
                } : undefined}
                onRemixProject={canInteractWithGallery && hostedChatEnabled ? (project) => {
                  const item = projectGalleryItems.find((candidate) => candidate.projectId === project.project_id);
                  return item ? handleRemixProject(item) : undefined;
                } : undefined}
                onVisibleProjectIdsChange={handleVisibleProjectGalleryIdsChange}
                totalItems={visibleProjectTotal}
                error={hasCurrentGalleryPage && !visibleProjectHistory.length ? projectHistoryError : null}
                currentPage={projectHistoryPage}
                onPageChange={handleProjectHistoryPageChange}
                searchValue={projectSearchInput}
                onSearchValueChange={setProjectSearchInput}
              />
	          ) : homeView === "my-projects" ? (
              <FormaProjectBrowser
                sectionRef={projectsSectionRef}
                projects={myProjectBrowserItems}
                title="My projects"
                pageSize={PROJECT_GALLERY_PAGE_SIZE}
                loading={myProjectsPageLoading}
                onOpenProject={(projectId) => router.push(projectRoute(projectId))}
                onToggleSave={canInteractWithGallery ? (project) => {
                  const item = myProjectGalleryItems.find((candidate) => candidate.projectId === project.project_id);
                  return item ? handleToggleProjectSave(item) : undefined;
                } : undefined}
                onRemixProject={canInteractWithGallery && hostedChatEnabled ? (project) => {
                  const item = myProjectGalleryItems.find((candidate) => candidate.projectId === project.project_id);
                  return item ? handleRemixProject(item) : undefined;
                } : undefined}
                onVisibleProjectIdsChange={handleVisibleProjectGalleryIdsChange}
                totalItems={myProjectHistoryTotal}
                currentPage={myProjectHistoryPage}
                onPageChange={handleMyProjectHistoryPageChange}
                searchValue={myProjectSearchInput}
                onSearchValueChange={setMyProjectSearchInput}
                error={myProjectHistoryError}
              />
          ) : homeView === "jobs" ? (
            <>
              <WorkspacePageHeading
                icon={History}
                title="Jobs"
                description="Generated-project jobs, pipeline events, image status, and operation errors."
              />
              {canViewJobs ? (
                <JobsPanel
                  jobs={a2aJobs}
                  metrics={jobMetrics}
                  metricsError={jobMetricsError}
                  metricsWindow={jobMetricsWindow}
                  onMetricsWindowChange={setJobMetricsWindow}
                  loading={jobsLoading}
                  error={jobsError}
                  statusFilter={jobStatusFilter}
                  onStatusFilterChange={changeJobStatusFilter}
                  onRefresh={() => fetchA2aJobs(jobStatusFilter)}
                  onOpenProject={loadProjectForJob}
                  findProjectForJob={findProjectForJob}
                  lastUpdatedAt={jobsLastUpdatedAt}
                  pollIntervalMs={JOB_POLL_INTERVAL_MS}
                  title="Jobs"
                  description="Generation and example project job metadata. Polling stays active while this page is open."
                  emptyMessage="No jobs recorded for this filter."
                  formatLlmLabel={generationLlmLabel}
                />
              ) : (
                <div className="rounded-xl border border-white/5 bg-[#181b22] p-6 text-sm leading-6 text-zinc-400">
                  {adminSessionLoaded ? "Admin access is required to view jobs." : "Checking admin access..."}
                </div>
              )}
            </>
          ) : homeView === "logs" ? (
            <>
              <WorkspacePageHeading
                icon={Terminal}
                title="Backend logs"
                description="Recent backend log lines for local debugging and package observability."
              />
              {canViewAdminTools ? (
                <LogsPanel
                  logs={backendLogs}
                  loading={logsLoading}
                  error={logsError}
                  lastUpdatedAt={logsLastUpdatedAt}
                  onRefresh={() => fetchBackendLogs()}
                  pollIntervalMs={LOG_POLL_INTERVAL_MS}
                />
              ) : (
                <div className="rounded-xl border border-white/5 bg-[#181b22] p-6 text-sm leading-6 text-zinc-400">
                  {adminSessionLoaded ? "Admin access is required to view backend logs." : "Checking admin access..."}
                </div>
              )}
            </>
          ) : homeView === "settings" ? (
            <UserIntegrationsPage embedded />
          ) : homeView === "about" ? (
            <AboutView />
          ) : chatAccessState !== "ready" ? (
            <ChatAccessStatus status={chatAccessState} onRetry={() => { void fetchRuntimeConfig(); }} />
          ) : (
            <HomeChatView
              started={activeSidebarChatStarted}
              readOnly={chatReadOnly}
              authoringActive={authoringMode}
              conversationKey={activeChatId || "new-chat"}
              workspaceTitle={
                activeSidebarChatStarted ? (
                  <EditableWorkspaceTitle
                    value={activeSidebarChatItem?.title || NEW_PROJECT_TITLE}
                     canEdit={hostedChatEnabled && !authoringMode}
                    label="Chat title"
                    onCommit={(title) => {
                      if (activeChatId) {
                        void commitOwnedWorkspaceTitle(title, {
                          chatId: activeChatId,
                          projectId: activeSidebarChatItem?.projectId || null,
                        });
                      }
                    }}
                  />
                ) : null
              }
              messages={chatMessages}
              renderPipelineProgress={renderConversationPipelineProgress}
              projectArtifactId={inlineChatProjectId}
              history={projectVersionHistory}
              projectArtifact={
                projectIR && inlineChatProjectId && currentProjectId === inlineChatProjectId
                  ? (
                    <ChatProjectArtifact
                      projectId={currentProjectId}
                      projectTitle={projectTitle}
                       canEdit={hostedChatEnabled && !authoringMode && currentUserOwnsProject}
                       onRenameTitle={hostedChatEnabled && !authoringMode && currentUserOwnsProject ? (title) => { void commitOwnedWorkspaceTitle(title); } : undefined}
                      namespaceTabs={visibleWorkspaceTabs}
                      activeNamespace={activeWorkspaceTab.id}
                      onNamespaceChange={setActiveTab}
                      projectContent={projectNamespaceContent}
                    />
                  )
                  : null
              }
              examples={samplePrompts}
              onSelectExample={(example) => {
                setGenerationInputNotice(null);
                setPendingHumanContext(null);
                setPrompt(example);
              }}
              generationMode={generationMode}
              onGenerationModeChange={setGenerationMode}
              onSubmit={authoringMode || selectedDocument ? handleGatherContext : generationMode === "regular" ? handleGenerate : handleGatherContext}
              canBuildNow={generationMode === "progressive" && hostedChatEnabled && (() => {
                const messages = activeChatId ? chatThreads[activeChatId] || chatMessages : chatMessages;
                const contextMessage = [...messages].reverse().find((message) => Boolean(message.contextProjectId));
                const state = contextWorkflowStates[activeChatId]
                  || contextMessage?.workflowState
                  || (contextMessage?.contextProjectId ? "gathering_context" : "");
                return state === "gathering_context";
              })()}
              buildNowLoading={contextBuildStarting}
              onBuildNow={handleBuildNow}
              onSelectContextSuggestion={(suggestion) => {
                void submitGatherContext(suggestion);
              }}
               isLoading={(hostedChatEnabled || authoringMode) && (contextSubmitting || Boolean(activeGeneration || pendingContextBuildMessage || resettingBuildMessageId))}
              generationReady
              needsGenerationProvider={false}
              needsImageProvider={false}
              selectedImage={selectedImage}
              selectedDocumentName={selectedDocument?.name || null}
              onRemoveImage={removeSelectedImage}
              onRemoveDocument={removeSelectedDocument}
              notice={visibleContextInputNotice}
              prompt={prompt}
              onPromptChange={(value) => {
                setGenerationInputNotice(null);
                setPrompt(value);
              }}
               generationActive={(hostedChatEnabled || authoringMode) && Boolean(activeGeneration || pendingContextBuildMessage)}
              onStop={() => {
                if (generationRunsRef.current.has(activeChatId)) stopActiveGeneration();
                else if (pendingContextBuildMessage) stopContextBuildMessage(pendingContextBuildMessage);
              }}
              retryLabel={pendingOpenCodeMessage(activeChatId) ? "Check request status" : undefined}
                canRetryFailedBuild={Boolean(pendingOpenCodeMessage(activeChatId)) || Boolean(openCodeRetries[activeChatId]) || (hostedChatEnabled && Boolean(retryableContextBuildMessage))}
              retryingFailedBuild={hostedChatEnabled && resettingBuildMessageId === retryableContextBuildMessage?.id}
              onRetryFailedBuild={() => {
                if (pendingOpenCodeMessage(activeChatId)) checkOpenCodeStatus(activeChatId);
                else if (openCodeRetries[activeChatId]) void submitOpenCodeTurn({ chatId: activeChatId, ...openCodeRetries[activeChatId] });
                else if (retryableContextBuildMessage) void resetFailedContextBuild(retryableContextBuildMessage);
              }}
              hasGenerationInput={hasGenerationInput}
              inputValid={generationInputValidation.isValid}
              imageInputRef={fileInputRefCenter}
              onImageChange={handleImageChange}
              onImagePaste={handleImagePaste}
            />
          )}
        </main>
        </div>
        <ProjectDeletionDialog
          project={pendingProjectDeletion}
          acknowledged={deletionAcknowledged}
          contribute={contributeDeletedProject}
          busy={projectDeletionBusy}
          error={projectDeletionError}
          onAcknowledgedChange={setDeletionAcknowledged}
          onContributeChange={setContributeDeletedProject}
          onCancel={closeProjectDeletion}
          onConfirm={confirmProjectDeletion}
        />
      </WorkspaceFrame>
    );
  }

  return (
    <WorkspaceFrame
      collapsed={sidebarCollapsed}
      workspaceStatus={workspaceStatus}
      mobileSidebar={(
        <MobileSidebarDrawer
          open={mobileSidebarOpen}
          onClose={() => setMobileSidebarOpen(false)}
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed((value) => !value)}
          onHome={goHome}
          chats={chatListItems}
           activeChatId={activeSidebarChatId}
          onNewChat={startNewProjectChat}
          newChatDisabled={newChatDisabled}
          newChatDisabledReason={newChatDisabledReason}
                 readOnly={hostedChatReadOnly || authoringMode}
          onOpenChat={openChatItem}
          onRenameChat={renameSidebarChat}
          onPinChat={togglePinnedChat}
          onDeleteChat={deleteSidebarChat}
          waitingChatIds={waitingChatIds}
          chatActivityById={chatActivityById}
          chatsLoading={sidebarChatsLoading}
          showJobs={canViewJobs}
          jobsPending={sidebarJobsPending}
          showDeveloperTools={showDeveloperTools}
          authRequired={authRequired}
        />
      )}
      desktopSidebar={(
        <ChatSidebar
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed((value) => !value)}
          onHome={goHome}
          chats={chatListItems}
          activeChatId={activeSidebarChatId}
          onNewChat={startNewProjectChat}
          newChatDisabled={newChatDisabled}
          newChatDisabledReason={newChatDisabledReason}
           readOnly={chatReadOnly || authoringMode}
          onOpenChat={openChatItem}
          onRenameChat={renameSidebarChat}
          onPinChat={togglePinnedChat}
          onDeleteChat={deleteSidebarChat}
          waitingChatIds={waitingChatIds}
          chatActivityById={chatActivityById}
          chatsLoading={sidebarChatsLoading}
          showJobs={canViewJobs}
          jobsPending={sidebarJobsPending}
          showDeveloperTools={showDeveloperTools}
          authRequired={authRequired}
        />
      )}
    >
      <main className="flex min-h-0 min-w-0 flex-col">
        <input
          ref={projectVideo.fileInputRef}
          type="file"
          accept="image/*"
          onChange={projectVideo.onImageFileChange}
          className="hidden"
        />

          <section className="min-h-0 min-w-0 flex-1 overflow-hidden">
            {routedProjectId || isPublicExample ? (
              <ProjectDetailWorkspace
                history={projectVersionHistory}
                onOpenSidebar={() => setMobileSidebarOpen(true)}
                projectId={currentProjectId}
                projectTitle={projectTitle}
                isPrivate={currentProjectPrivate}
                owned={currentUserOwnsProject}
                readOnly={hostedChatReadOnly || authoringMode}
                 onRenameTitle={hostedChatEnabled && !authoringMode && currentUserOwnsProject ? (title) => { void commitOwnedWorkspaceTitle(title); } : undefined}
                namespaceTabs={visibleWorkspaceTabs}
                activeNamespace={activeWorkspaceTab.id}
                onNamespaceChange={setActiveTab}
                projectContent={projectNamespaceContent}
              />
            ) : chatAccessState !== "ready" ? (
              <ChatAccessStatus status={chatAccessState} onRetry={() => { void fetchRuntimeConfig(); }} />
            ) : (
              <ChatWorkspace
                history={projectVersionHistory}
                onOpenSidebar={() => setMobileSidebarOpen(true)}
                projectId={currentProjectId}
                chatId={currentProjectChatId}
                projectTitle={projectTitle}
                isPrivate={currentProjectPrivate}
                 onRenameTitle={hostedChatEnabled && !authoringMode && currentUserOwnsProject ? (title) => { void commitOwnedWorkspaceTitle(title); } : undefined}
                messages={currentProjectChatMessages}
                renderPipelineProgress={renderConversationPipelineProgress}
                input={projectChatInput}
                setInput={setProjectChatInput}
                onSubmit={handleProjectChatGenerate}
                attachmentControls={{
                  imageInputRef: fileInputRefSidebar,
                  onImageChange: handleImageChange,
                  selectedImage,
                  selectedDocumentName: selectedDocument?.name || null,
                  onRemoveImage: removeSelectedImage,
                  onRemoveDocument: removeSelectedDocument,
                }}
                onImagePaste={handleImagePaste}
                notice={generationInputNotice}
                 isLoading={(hostedChatEnabled || authoringMode) && (isLoading || contextSubmitting || Boolean(generationRuns[currentProjectChatId]))}
                 canStop={(hostedChatEnabled || authoringMode) && generationRuns[currentProjectChatId]?.kind === "project-chat"}
                onStop={() => stopActiveGeneration(currentProjectChatId)}
                retryLabel={pendingOpenCodeMessage(currentProjectChatId) ? "Check request status" : undefined}
                canRetryFailedBuild={Boolean(pendingOpenCodeMessage(currentProjectChatId)) || Boolean(openCodeRetries[currentProjectChatId]) || (hostedChatEnabled && Boolean(retryableProjectBuildMessage))}
                retryingFailedBuild={hostedChatEnabled && resettingBuildMessageId === retryableProjectBuildMessage?.id}
                onRetryFailedBuild={() => {
                  if (pendingOpenCodeMessage(currentProjectChatId)) checkOpenCodeStatus(currentProjectChatId);
                else if (openCodeRetries[currentProjectChatId]) void submitOpenCodeTurn({ chatId: currentProjectChatId, ...openCodeRetries[currentProjectChatId] });
                  else if (retryableProjectBuildMessage) void resetFailedContextBuild(retryableProjectBuildMessage);
                }}
                 canChat={(hostedChatEnabled || authoringMode) && currentUserOwnsProject}
                 readOnly={chatReadOnly}
                authoringActive={authoringMode}
                namespaceTabs={visibleWorkspaceTabs}
                activeNamespace={activeWorkspaceTab.id}
                activeNamespaceLabel={activeWorkspaceTab.label}
                activeNamespaceName={activeWorkspaceNamespace}
                onNamespaceChange={setActiveTab}
                projectContent={projectNamespaceContent}
              />
            )}
          </section>
      </main>
      <ProjectDeletionDialog
        project={pendingProjectDeletion}
        acknowledged={deletionAcknowledged}
        contribute={contributeDeletedProject}
        busy={projectDeletionBusy}
        error={projectDeletionError}
        onAcknowledgedChange={setDeletionAcknowledged}
        onContributeChange={setContributeDeletedProject}
        onCancel={closeProjectDeletion}
        onConfirm={confirmProjectDeletion}
      />
    </WorkspaceFrame>
  );
}

export default FormaWorkspace;

function ProjectDeletionDialog({
  project,
  acknowledged,
  contribute,
  busy,
  error,
  onAcknowledgedChange,
  onContributeChange,
  onCancel,
  onConfirm,
}: {
  project: PendingProjectDeletion | null;
  acknowledged: boolean;
  contribute: boolean;
  busy: boolean;
  error: string | null;
  onAcknowledgedChange: (value: boolean) => void;
  onContributeChange: (value: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  if (!project) return null;
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" role="presentation">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-project-title"
        className="w-full max-w-xl rounded-2xl border border-white/5 bg-[#181b22] p-6 text-zinc-100 shadow-2xl"
      >
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-red-500/10 text-red-300">
            <Trash2 className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <h2 id="delete-project-title" className="text-lg font-semibold tracking-tight text-zinc-100">
              Delete this project?
            </h2>
            <p className="mt-1 break-words text-sm text-zinc-400">{project.title}</p>
          </div>
        </div>
        <p className="mt-5 text-sm leading-6 text-zinc-300">
          Removed from your workspace now. Permanently deleted after 30 days.
        </p>
        <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-lg border border-white/10 bg-[#0f1117] p-4 text-sm leading-5 text-zinc-300">
          <input
            type="checkbox"
            checked={acknowledged}
            onChange={(event) => onAcknowledgedChange(event.target.checked)}
            disabled={busy}
            className="mt-0.5 h-4 w-4 accent-red-400"
          />
          <span>I understand I will lose access to this project.</span>
        </label>
        <div className="mt-4 rounded-lg border border-emerald-500/20 bg-emerald-500/[0.06] p-4">
          <label className="flex cursor-pointer items-start gap-3 text-sm font-medium leading-5 text-zinc-200">
            <input
              type="checkbox"
              checked={contribute}
              onChange={(event) => onContributeChange(event.target.checked)}
              disabled={busy}
              className="mt-0.5 h-4 w-4 accent-emerald-400"
            />
            <span>Keep a sanitized copy for product research.</span>
          </label>
          <p className="mt-3 text-xs leading-5 text-zinc-500">
            Personal details are removed. You can withdraw until the copy is anonymized.
          </p>
          <div className="mt-3 flex flex-wrap gap-4 text-xs font-medium">
            <a href="/legal/privacy-policy" target="_blank" rel="noreferrer" className="text-emerald-400 transition-colors hover:text-emerald-300">Privacy policy</a>
            <a href="/legal/data-contribution-terms" target="_blank" rel="noreferrer" className="text-emerald-400 transition-colors hover:text-emerald-300">Data contribution terms</a>
          </div>
        </div>
        {error && <p className="mt-4 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-200">{error}</p>}
        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="inline-flex h-9 items-center justify-center rounded-lg border border-white/10 px-4 text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!acknowledged || busy}
            className="inline-flex h-9 items-center justify-center rounded-lg bg-red-500 px-4 text-xs font-medium text-white transition-colors hover:bg-red-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Deleting..." : "Delete project"}
          </button>
        </div>
      </section>
    </div>
  );
}

function buildChatListItems(projectHistory: any[], localChatItems: ChatListItem[] = []): ChatListItem[] {
  const groups = new Map<string, { latest: any; projectCount: number }>();

  projectHistory
    .filter((project: any) => project?.project_id)
    .forEach((project: any) => {
      const projectId = String(project.project_id);
      const conversation = localChatItems.find((item) => item.projectId === projectId);
      const chatId = String(conversation?.chatId || project.chat_id || projectId).trim();
      if (!chatId) return;

      const existing = groups.get(chatId);
      if (!existing) {
        groups.set(chatId, { latest: project, projectCount: 1 });
        return;
      }

      const currentTime = Date.parse(existing.latest?.created_at || "");
      const nextTime = Date.parse(project.created_at || "");
      groups.set(chatId, {
        latest: Number.isNaN(nextTime) || nextTime <= (Number.isNaN(currentTime) ? 0 : currentTime)
          ? existing.latest
          : project,
        projectCount: existing.projectCount + 1,
      });
    });

  const savedItems = Array.from(groups.entries())
    .map(([chatId, group]) => ({
      chatId,
      title: group.latest?.title || "Untitled chat",
      projectId: String(group.latest?.project_id || ""),
      createdAt: typeof group.latest?.created_at === "string" ? group.latest.created_at : null,
      projectCount: group.projectCount,
    }));

  const merged = new Map<string, ChatListItem>();
  localChatItems.forEach((item) => {
    if (item.chatId) merged.set(item.chatId, item);
  });
  savedItems.forEach((item) => {
    const existing = merged.get(item.chatId);
    merged.set(item.chatId, {
      ...existing,
      ...item,
      createdAt: latestChatListItemDate(existing?.createdAt, item.createdAt),
      projectCount: Math.max(existing?.projectCount || 0, item.projectCount),
    });
  });

  return sortChatListItems(Array.from(merged.values()));
}

function normalizePrivateChatItems(value: any): ChatListItem[] {
  const chats = Array.isArray(value) ? value : [];
  return chats
    .map((chat: any): ChatListItem | null => {
      const chatId = typeof chat?.chat_id === "string" ? chat.chat_id.trim() : "";
      if (!chatId) return null;
      const messages = persistableChatMessages(Array.isArray(chat?.messages) ? chat.messages : []);
      const projectId = [...messages].reverse().find((message) => message.projectId)?.projectId || "";
      return {
        chatId,
        title: typeof chat.title === "string" && chat.title.trim() ? chat.title.trim() : NEW_PROJECT_TITLE,
        projectId,
        createdAt: typeof chat.updated_at === "string" ? chat.updated_at : typeof chat.created_at === "string" ? chat.created_at : null,
        projectCount: 0,
      };
    })
    .filter((item: ChatListItem | null): item is ChatListItem => Boolean(item));
}

function normalizeProjectListPage(value: any): { items: any[]; total: number } {
  if (Array.isArray(value)) return { items: value, total: value.length };
  const items = Array.isArray(value?.items) ? value.items : [];
  const parsedTotal = Number(value?.total);
  return {
    items,
    total: Number.isFinite(parsedTotal) ? Math.max(items.length, Math.trunc(parsedTotal)) : items.length,
  };
}

function mergeChatListItems(primary: ChatListItem[], secondary: ChatListItem[]): ChatListItem[] {
  const merged = new Map<string, ChatListItem>();
  secondary.forEach((item) => {
    if (item.chatId) merged.set(item.chatId, item);
  });
  primary.forEach((item) => {
    if (!item.chatId) return;
    const existing = merged.get(item.chatId);
    merged.set(item.chatId, {
      ...existing,
      ...item,
      createdAt: latestChatListItemDate(existing?.createdAt, item.createdAt),
      projectCount: Math.max(existing?.projectCount || 0, item.projectCount),
    });
  });
  return sortChatListItems(Array.from(merged.values()));
}

function mergeProjectRecords(primary: any[], secondary: any[]): any[] {
  const merged = new Map<string, any>();
  primary.forEach((project: any) => {
    const projectId = project?.project_id ? String(project.project_id) : "";
    if (projectId) merged.set(projectId, project);
  });
  secondary.forEach((project: any) => {
    const projectId = project?.project_id ? String(project.project_id) : "";
    if (projectId) merged.set(projectId, project);
  });
  return Array.from(merged.values());
}

function sameStringList(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function WorkspaceChromeIdentity({
  icon: Icon,
  badge,
  title,
}: {
  icon: React.ElementType<{ className?: string }>;
  badge: string;
  title: React.ReactNode;
}) {
  return (
    <div className="min-w-0 flex-1">
      <div className="flex min-w-0 items-center gap-2">
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-400">
          <Icon className="h-3 w-3" />
          {badge}
        </span>
        {typeof title === "string" ? (
          <h2 className="truncate text-sm font-semibold tracking-tight text-zinc-100">{title}</h2>
        ) : title}
      </div>
    </div>
  );
}

function WorkspacePageHeading({
  icon: Icon,
  title,
  description,
}: {
  icon: React.ElementType<{ className?: string }>;
  title: string;
  description: string;
}) {
  return (
    <section className="mb-6 border-b border-white/5 pb-5">
      <div className="hidden min-w-0 items-center gap-3 md:flex">
        <div className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold tracking-tight text-zinc-100">{title}</h1>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-zinc-500">{description}</p>
        </div>
      </div>
      <p className="max-w-2xl text-sm leading-6 text-zinc-500 md:hidden">{description}</p>
    </section>
  );
}

function ProjectRouteFallbackPanel({
  projectId,
  error,
  onHome,
  onOpenSidebar,
}: {
  projectId: string;
  error: string | null;
  onHome: () => void;
  onOpenSidebar: () => void;
}) {
  return (
    <main className="flex min-h-0 min-w-0 flex-col">
      <header className="flex h-12 items-center gap-3 border-b border-white/5 bg-[#0f1117]/80 px-4 backdrop-blur-md">
        <MobileSidebarButton onClick={onOpenSidebar} />
        <div className="flex min-w-0 flex-1 items-baseline gap-2">
          <div className="truncate text-sm font-semibold tracking-tight text-zinc-100">
            {error ? "Project unavailable" : "Opening project"}
          </div>
          <div className="truncate font-mono text-[10px] text-zinc-600">{projectId}</div>
        </div>
      </header>

      <section className="flex min-h-0 flex-1 items-center justify-center p-5">
        <div className="w-full max-w-md rounded-2xl border border-white/5 bg-[#181b22] p-6 text-center shadow-2xl shadow-black/30">
          <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-lg bg-emerald-500/10">
            {error ? <AlertTriangle className="h-5 w-5 text-amber-300" /> : <RefreshCw className="h-5 w-5 animate-spin text-emerald-400" />}
          </div>
          <h1 className="mt-5 text-lg font-semibold tracking-tight text-zinc-100">
            {error ? "Project unavailable" : "Opening project"}
          </h1>
          <p className="mt-3 text-sm leading-6 text-zinc-500">
            {error || "Loading the saved hardware plan."}
          </p>
          {error && (
            <button
              type="button"
              onClick={onHome}
              className="mt-5 inline-flex h-9 items-center gap-2 rounded-lg border border-white/10 px-4 text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100"
            >
              <ArrowLeft className="h-4 w-4" />
              Back home
            </button>
          )}
        </div>
      </section>
    </main>
  );
}

function AuthRequiredRouteScreen({
  loading,
  title,
  message,
  onHome,
}: {
  loading: boolean;
  title: string;
  message: string;
  onHome: () => void;
}) {
  const { openSignIn } = useFormaAuth();
  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-[#0f1117] px-5 font-sans text-zinc-100">
      <div className="w-full max-w-md rounded-2xl border border-white/5 bg-[#181b22] p-6 shadow-2xl">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
            <KeyRound className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-lg font-semibold tracking-tight text-zinc-100">{title}</h1>
            <p className="mt-1 text-sm text-zinc-500">{loading ? "Checking session..." : message}</p>
          </div>
        </div>
        <div className="mt-6 grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={onHome}
            className="inline-flex h-9 items-center justify-center rounded-lg border border-white/10 px-3 text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100"
          >
            Home
          </button>
          <button
            type="button"
            disabled={loading}
            onClick={() => openSignIn({ redirectUrl: typeof window !== "undefined" ? window.location.href : "/" })}
            className="inline-flex h-9 items-center justify-center rounded-lg bg-emerald-500 px-3 text-xs font-semibold text-zinc-950 transition-colors hover:bg-emerald-400 disabled:cursor-wait disabled:opacity-50"
          >
            Sign in
          </button>
        </div>
      </div>
    </div>
  );
}

function ChatRouteFallbackPanel({
  transition,
  onHome,
  onOpenSidebar,
}: {
  transition: ChatRouteTransition;
  onHome: () => void;
  onOpenSidebar: () => void;
}) {
  const hasProjectTarget = Boolean(transition.projectId);
  return (
    <main className="flex min-h-0 min-w-0 flex-col">
      <header className="flex h-12 min-w-0 items-center gap-2 overflow-hidden border-b border-white/5 bg-[#0f1117]/80 px-3 backdrop-blur-md sm:gap-3 sm:px-4">
        <MobileSidebarButton onClick={onOpenSidebar} />
        <div className="flex min-w-0 flex-1 items-baseline gap-2">
          <div className="truncate text-sm font-semibold tracking-tight text-zinc-100">{transition.title || "Opening chat"}</div>
          <span className="truncate font-mono text-[10px] text-zinc-600">{transition.projectId || transition.chatId}</span>
        </div>
      </header>

      <section className="flex min-h-0 flex-1 items-center justify-center p-5">
        <div className="w-full max-w-md rounded-2xl border border-white/5 bg-[#181b22] p-6 text-center shadow-2xl shadow-black/30">
          <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-lg bg-emerald-500/10">
            {transition.error ? <AlertTriangle className="h-5 w-5 text-amber-300" /> : <RefreshCw className="h-5 w-5 animate-spin text-emerald-400" />}
          </div>
          <h1 className="mt-5 text-lg font-semibold tracking-tight text-zinc-100">
            {transition.error ? "Chat unavailable" : hasProjectTarget ? "Opening project chat" : "Opening chat"}
          </h1>
          <p className="mt-3 text-sm leading-6 text-zinc-500">
            {transition.error || (hasProjectTarget ? "Loading the active project for this chat." : "Preparing the chat workspace.")}
          </p>
          <div className="mt-4 space-y-2">
            <div className="truncate rounded-lg border border-white/5 bg-[#0f1117] px-3 py-2 font-mono text-xs text-zinc-500">
              {transition.chatId}
            </div>
            {transition.projectId && (
              <div className="truncate rounded-lg border border-emerald-500/20 bg-emerald-500/[0.06] px-3 py-2 font-mono text-xs text-emerald-100">
                {transition.projectId}
              </div>
            )}
          </div>
          {transition.error && (
            <button
              type="button"
              onClick={onHome}
              className="mt-5 inline-flex h-9 items-center gap-2 rounded-lg border border-white/10 px-4 text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-800/40 hover:text-zinc-100"
            >
              <ArrowLeft className="h-4 w-4" />
              Back home
            </button>
          )}
        </div>
      </section>
    </main>
  );
}


function videoStatusTone(status: string) {
  const normalized = status.toLowerCase();
  if (normalized === "succeeded" || normalized === "success") {
    return "border-[rgb(var(--forma-green-rgb)/0.35)] bg-[rgb(var(--forma-green-rgb)/0.1)] text-[rgb(var(--forma-green-rgb))]";
  }
  if (normalized === "failed" || normalized === "failure" || normalized === "error") {
    return "border-[rgb(var(--forma-red-rgb)/0.35)] bg-[rgb(var(--forma-red-rgb)/0.1)] text-[rgb(var(--forma-red-rgb))]";
  }
  if (normalized === "running" || normalized === "loading" || normalized === "reviewing") {
    return "border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] text-[rgb(var(--forma-cyan-rgb))]";
  }
  if (normalized === "queued") {
    return "border-[rgb(var(--forma-yellow-rgb)/0.35)] bg-[rgb(var(--forma-yellow-rgb)/0.1)] text-[rgb(var(--forma-yellow-rgb))]";
  }
  return "border-[var(--forma-border)] bg-[var(--forma-surface-muted)] text-[var(--forma-text-muted)]";
}

function VideoPanel({
  projectId,
  readOnly,
  models,
  modelsLoading,
  modelsError,
  selectedModel,
  setSelectedModel,
  mode,
  setMode,
  imageInput,
  setImageInput,
  imageOptions,
  selectedImageSources,
  setSelectedImageSources,
  defaultImage,
  sourceVideoUrl,
  setSourceVideoUrl,
  prompt,
  setPrompt,
  duration,
  setDuration,
  aspectRatio,
  setAspectRatio,
  aspectRatios,
  status,
  statusMessage,
  requestId,
  storedVideo,
  gallery,
  galleryLoading,
  galleryError,
  generationAvailable,
  generationUnavailableReason,
  reviewStatus,
  reviewMessage,
  reviewAvailable,
  reviewUnavailableReason,
  selectedReviewVideoKey,
  setSelectedReviewVideoKey,
  makeNewVideo,
  setMakeNewVideo,
  promptGenerating,
  promptMessage,
  onGenerate,
  onGeneratePrompt,
  onReview,
  onReviewVideo,
  onUploadImage,
  onUseProjectImage,
  onRefreshGallery,
  canGenerate,
  canReview,
  canMakeNewVideo,
  canGeneratePrompt,
  canOpenAssets,
}: {
  projectId: string | null;
  readOnly: boolean;
  canOpenAssets: boolean;
  models: VideoModelOption[];
  modelsLoading: boolean;
  modelsError: string | null;
  selectedModel: string;
  setSelectedModel: (value: string) => void;
  mode: VideoGenerationMode;
  setMode: (value: VideoGenerationMode) => void;
  imageInput: string;
  setImageInput: (value: string) => void;
  imageOptions: ProjectImageCandidate[];
  selectedImageSources: string[];
  setSelectedImageSources: (value: string[]) => void;
  defaultImage: string;
  sourceVideoUrl: string;
  setSourceVideoUrl: (value: string) => void;
  prompt: string;
  setPrompt: (value: string) => void;
  duration: string;
  setDuration: (value: string) => void;
  aspectRatio: string;
  setAspectRatio: (value: string) => void;
  aspectRatios: string[];
  status: string;
  statusMessage: string | null;
  requestId: string | null;
  storedVideo: StoredVideoInfo | null;
  gallery: StoredVideoInfo[];
  galleryLoading: boolean;
  galleryError: string | null;
  generationAvailable: boolean;
  generationUnavailableReason: string | null;
  reviewStatus: string;
  reviewMessage: string | null;
  reviewAvailable: boolean;
  reviewUnavailableReason: string | null;
  selectedReviewVideoKey: string | null;
  setSelectedReviewVideoKey: (value: string | null) => void;
  makeNewVideo: boolean;
  setMakeNewVideo: (value: boolean) => void;
  promptGenerating: boolean;
  promptMessage: string | null;
  onGenerate: () => void;
  onGeneratePrompt: () => void;
  onReview: () => void;
  onReviewVideo: (video: StoredVideoInfo) => void;
  onUploadImage: () => void;
  onUseProjectImage: () => void;
  onRefreshGallery: () => void;
  canGenerate: boolean;
  canReview: boolean;
  canMakeNewVideo: boolean;
  canGeneratePrompt: boolean;
}) {
  const modeModels = models.filter((model) => model.mode === mode);
  const sourceVideos = gallery
    .map((video, index) => ({
      video,
      url: videoSourceUrl(video),
      label: videoLabel(video, `Video ${index + 1}`),
    }))
    .filter((item) => item.url);
  const videoToVideoAvailable = sourceVideos.length > 0;
  const selectedImagePreviewSource = selectedImageSources[0] || imageInput;
  const imagePreview = mode === "image-to-video" ? previewableImageSrc(selectedImagePreviewSource) : null;
  const sourceVideoPreview = mode === "video-to-video" ? sourceVideoUrl : "";
  const isGenerating = status === "loading" || Boolean(requestId && !storedVideo && !isFinalVideoStatus(status));
  const isReviewing = reviewStatus === "loading";
  const generateDisabled = !canGenerate || isGenerating || !modeModels.length;
  const reviewDisabled = !canReview || isReviewing;
  const savedHref = canOpenAssets ? storedVideo?.publicUrl || null : null;
  const allProjectImagesSelected = imageOptions.length > 0 && imageOptions.every((candidate) => selectedImageSources.includes(candidate.src));
  const toggleImageSource = (source: string) => {
    setSelectedImageSources(
      selectedImageSources.includes(source)
        ? selectedImageSources.filter((item) => item !== source)
        : [...selectedImageSources, source]
    );
  };

  if (!generationAvailable) {
    return (
      <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden bg-[var(--forma-page)] px-4 py-5 text-[var(--forma-text)] sm:px-5 sm:py-6">
        <div className="mx-auto min-w-0 max-w-[890px]">
          <section className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
            <div className="flex flex-col gap-4 border-b border-[var(--forma-border)] pb-4 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex min-w-0 items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] text-[rgb(var(--forma-cyan-rgb))]">
                  <Film className="h-4 w-4" />
                </span>
                <div className="min-w-0">
                  <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Media</div>
                  <h2 className="mt-1 text-sm font-semibold tracking-tight text-[var(--forma-text-strong)]">Video</h2>
                  <div className="mt-1.5 truncate font-mono text-[10px] text-[var(--forma-text-muted)]">{projectId || "No project id"}</div>
                </div>
              </div>
              <button
                type="button"
                onClick={onReview}
                disabled={reviewDisabled}
                className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] px-3 text-xs font-medium text-[rgb(var(--forma-cyan-rgb))] transition-colors hover:border-[rgb(var(--forma-cyan-rgb)/0.55)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isReviewing ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                Review
              </button>
            </div>

            {readOnly && (
              <div className="mt-5 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-3 text-xs leading-5 text-[var(--forma-text-secondary)]">
                {canOpenAssets
                  ? "Video generation and review are unavailable during hosted chat maintenance. Saved videos remain available for viewing."
                  : "Video actions are available only to the owner."}
              </div>
            )}

            <div className="mt-5 rounded-lg border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] p-4">
              <div className="flex items-start gap-3">
                <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-[rgb(var(--forma-cyan-rgb))]" />
                <div className="min-w-0">
                  <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[rgb(var(--forma-cyan-rgb))]">Alpha</div>
                  <p className="mt-2 text-sm leading-6 text-[var(--forma-text-body)]">
                    We are in alpha and video generation is coming soon.
                  </p>
                  {generationUnavailableReason && (
                    <p className="mt-2 break-words text-xs leading-5 text-[var(--forma-text-secondary)]">{generationUnavailableReason}</p>
                  )}
                </div>
              </div>
            </div>

            <VideoReviewStatus
              status={reviewStatus}
              message={reviewMessage}
              available={reviewAvailable}
              unavailableReason={reviewUnavailableReason}
              isReviewing={isReviewing}
              makeNewVideo={makeNewVideo}
              setMakeNewVideo={setMakeNewVideo}
              canMakeNewVideo={canMakeNewVideo}
            />

              <VideoGallery
                videos={gallery}
                loading={galleryLoading}
                error={galleryError}
                onRefresh={onRefreshGallery}
                selectedKey={selectedReviewVideoKey}
                onSelect={setSelectedReviewVideoKey}
                onReview={onReviewVideo}
                canReview={canReview}
                canOpenAssets={canOpenAssets}
                reviewing={isReviewing}
              />
          </section>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden bg-[var(--forma-page)] px-4 py-5 text-[var(--forma-text)] sm:px-5 sm:py-6">
      <div className="mx-auto flex min-w-0 max-w-[890px] flex-col gap-4">
        <section className="min-w-0 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
          <div className="mb-5 flex flex-col gap-4 border-b border-[var(--forma-border)] pb-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex min-w-0 items-start gap-3">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] text-[rgb(var(--forma-cyan-rgb))]">
                <Film className="h-4 w-4" />
              </span>
              <div className="min-w-0">
                <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Media</div>
                <h2 className="mt-1 text-sm font-semibold tracking-tight text-[var(--forma-text-strong)]">Video</h2>
                <div className="mt-1.5 truncate font-mono text-[10px] text-[var(--forma-text-muted)]">{projectId || "No project id"}</div>
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <button
                type="button"
                onClick={onReview}
                disabled={reviewDisabled}
                className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] px-3 text-xs font-medium text-[rgb(var(--forma-cyan-rgb))] transition-colors hover:border-[rgb(var(--forma-cyan-rgb)/0.55)] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isReviewing ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                Review
              </button>
              <button
                type="button"
                onClick={onGenerate}
                disabled={generateDisabled}
                className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-[var(--forma-text-strong)] bg-[var(--forma-text-strong)] px-3 text-xs font-medium text-[var(--forma-page)] transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {isGenerating ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Film className="h-4 w-4" />}
                Generate
              </button>
            </div>
          </div>

          {readOnly && (
            <div className="mb-5 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-3 text-xs leading-5 text-[var(--forma-text-secondary)]">
              {canOpenAssets
                ? "Video generation and review are unavailable during hosted chat maintenance. Saved videos remain available for viewing."
                : "Video actions are available only to the owner."}
            </div>
          )}

          <div className="mb-4 grid grid-cols-2 overflow-hidden rounded-lg border border-[var(--forma-border)]">
            {([
              { value: "image-to-video" as VideoGenerationMode, label: "Image" },
              { value: "video-to-video" as VideoGenerationMode, label: "Video", disabled: !videoToVideoAvailable },
            ]).map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => {
                  if (!item.disabled) setMode(item.value);
                }}
                disabled={readOnly || item.disabled}
                className={`flex h-10 items-center justify-center gap-2 border-r border-[var(--forma-border)] text-xs font-medium last:border-r-0 ${
                  mode === item.value
                    ? "bg-[var(--forma-surface)] text-[var(--forma-text-strong)]"
                    : "bg-[var(--forma-surface-muted)] text-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]"
                } disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-[var(--forma-text-muted)]`}
              >
                {item.value === "video-to-video" ? <Film className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                {item.label}
              </button>
            ))}
          </div>

          <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_170px_180px]">
            <label className="block text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
              Model
              <select
                value={selectedModel}
                onChange={(event) => setSelectedModel(event.target.value)}
                disabled={readOnly || modelsLoading || !modeModels.length}
                className="mt-2 h-10 w-full rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 text-sm font-normal tracking-normal text-[var(--forma-text-body)] outline-none focus:border-[rgb(var(--forma-cyan-rgb))] disabled:opacity-50"
              >
                {!modeModels.length && <option value="">No models</option>}
                {modeModels.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="block text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
              Aspect ratio
              <select
                value={aspectRatio}
                onChange={(event) => setAspectRatio(event.target.value)}
                disabled={readOnly}
                className="mt-2 h-10 w-full rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 text-sm font-normal tracking-normal text-[var(--forma-text-body)] outline-none focus:border-[rgb(var(--forma-cyan-rgb))] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {aspectRatios.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>

            <div>
              <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Duration</div>
              <div className="mt-2 grid grid-cols-2 overflow-hidden rounded-lg border border-[var(--forma-border)]">
                {["5", "10"].map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setDuration(value)}
                    disabled={readOnly}
                    className={`h-10 border-r border-[var(--forma-border)] text-xs font-medium last:border-r-0 ${
                      duration === value
                        ? "bg-[var(--forma-surface)] text-[var(--forma-text-strong)]"
                        : "bg-[var(--forma-surface-muted)] text-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]"
                    } disabled:cursor-not-allowed disabled:opacity-50`}
                  >
                    {value}s
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label htmlFor="video-prompt" className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
                Prompt
              </label>
              <button
                type="button"
                onClick={onGeneratePrompt}
                disabled={!canGeneratePrompt}
                className="inline-flex h-9 items-center gap-2 rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] px-3 text-[11px] font-medium text-[rgb(var(--forma-cyan-rgb))] transition-colors hover:border-[rgb(var(--forma-cyan-rgb)/0.55)] disabled:cursor-not-allowed disabled:opacity-40"
                title="Generate an image-to-video prompt from project namespaces"
              >
                {promptGenerating ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                Generate prompt
              </button>
            </div>
            <textarea
              id="video-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              readOnly={readOnly}
              maxLength={VIDEO_PROMPT_MAX_CHARS}
              placeholder="Slow orbit, reveal ports, show display glow."
              className="mt-2 min-h-[132px] w-full resize-none rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-3 text-sm font-normal leading-6 tracking-normal text-[var(--forma-text-body)] outline-none placeholder:text-[var(--forma-text-muted)] focus:border-[rgb(var(--forma-cyan-rgb))] read-only:cursor-not-allowed read-only:opacity-60"
            />
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              {promptMessage ? (
                <p className="break-words text-[11px] leading-5 text-[var(--forma-text-secondary)]">{promptMessage}</p>
              ) : (
                <span />
              )}
              <span className={`font-mono text-[10px] ${prompt.length > VIDEO_PROMPT_MAX_CHARS - 120 ? "text-[rgb(var(--forma-yellow-rgb))]" : "text-[var(--forma-text-muted)]"}`}>
                {prompt.length}/{VIDEO_PROMPT_MAX_CHARS}
              </span>
            </div>
          </div>

          {mode === "image-to-video" ? (
            <div className="mt-5 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-3">
              <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Image source</div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={onUploadImage}
                    disabled={readOnly}
                    className="inline-flex h-9 items-center gap-2 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 text-xs font-medium text-[var(--forma-text-body)] transition-colors hover:bg-[var(--forma-page)] hover:text-[var(--forma-text-strong)]"
                  >
                    <Paperclip className="h-4 w-4" />
                    Upload
                  </button>
                  <button
                    type="button"
                    onClick={() => setSelectedImageSources(allProjectImagesSelected ? [] : imageOptions.map((candidate) => candidate.src))}
                    disabled={readOnly || !imageOptions.length}
                    className="inline-flex h-9 items-center gap-2 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 text-xs font-medium text-[var(--forma-text-body)] transition-colors hover:bg-[var(--forma-page)] hover:text-[var(--forma-text-strong)] disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Layers className="h-4 w-4" />
                    {allProjectImagesSelected ? "Clear" : "All"}
                  </button>
                  <button
                    type="button"
                    onClick={onUseProjectImage}
                    disabled={readOnly || !defaultImage}
                    className="inline-flex h-9 items-center gap-2 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 text-xs font-medium text-[var(--forma-text-body)] transition-colors hover:bg-[var(--forma-page)] hover:text-[var(--forma-text-strong)] disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Eye className="h-4 w-4" />
                    First
                  </button>
                </div>
              </div>

              {imageOptions.length > 0 && (
                <div className="mb-3 grid gap-2 sm:grid-cols-3">
                  {imageOptions.map((candidate) => {
                    const selected = selectedImageSources.includes(candidate.src);
                    return (
                      <button
                        key={candidate.src}
                        type="button"
                        onClick={() => toggleImageSource(candidate.src)}
                        disabled={readOnly}
                        className={`min-w-0 rounded-lg border p-2 text-left transition ${
                          selected
                            ? "border-[rgb(var(--forma-cyan-rgb)/0.55)] bg-[var(--forma-surface)] text-[rgb(var(--forma-cyan-rgb))]"
                            : "border-[var(--forma-border)] bg-[var(--forma-surface)] text-[var(--forma-text-muted)] hover:border-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]"
                        }`}
                        aria-pressed={selected}
                      >
                        <div className="relative h-20 overflow-hidden rounded-md bg-[var(--forma-surface-muted)]">
                          <Image
                            src={candidate.src}
                            alt={candidate.label}
                            width={1}
                            height={1}
                            unoptimized
                            className="h-full w-full object-cover"
                          />
                          <span className={`absolute right-2 top-2 flex h-5 w-5 items-center justify-center rounded-md border text-[10px] font-medium ${
                            selected
                              ? "border-[rgb(var(--forma-cyan-rgb))] bg-[rgb(var(--forma-cyan-rgb))] text-[var(--forma-page)]"
                              : "border-[var(--forma-border)] bg-[rgb(var(--forma-chrome-rgb)/0.8)] text-[var(--forma-text-strong)]"
                          }`}>
                            {selected ? <CheckCircle className="h-3.5 w-3.5" /> : null}
                          </span>
                        </div>
                        <div className="mt-2 truncate text-[10px] font-medium">{candidate.label}</div>
                      </button>
                    );
                  })}
                </div>
              )}

              <input
                value={imageInput}
                onChange={(event) => {
                  setImageInput(event.target.value);
                  setSelectedImageSources([]);
                }}
                readOnly={readOnly}
                placeholder="https://... or data:image/..."
                className="h-10 w-full rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 font-mono text-xs text-[var(--forma-text-body)] outline-none placeholder:text-[var(--forma-text-muted)] focus:border-[rgb(var(--forma-cyan-rgb))]"
              />
              <div className="mt-2 text-[11px] leading-5 text-[var(--forma-text-secondary)]">
                {selectedImageSources.length
                  ? `${selectedImageSources.length} project image${selectedImageSources.length === 1 ? "" : "s"} selected.`
                  : "No project images selected; the manual image field will be used."}
              </div>
            </div>
          ) : (
            <label className="mt-5 block text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
              Source video
              <select
                value={sourceVideoUrl}
                onChange={(event) => setSourceVideoUrl(event.target.value)}
                disabled={readOnly || !sourceVideos.length}
                className="mt-2 h-10 w-full rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 text-sm font-normal tracking-normal text-[var(--forma-text-body)] outline-none focus:border-[rgb(var(--forma-cyan-rgb))] disabled:opacity-50"
              >
                {!sourceVideos.length && <option value="">No saved videos</option>}
                {sourceVideos.map((item) => (
                  <option key={item.url} value={item.url}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
          )}

          <VideoGallery
            videos={gallery}
            loading={galleryLoading}
            error={galleryError}
            onRefresh={onRefreshGallery}
            selectedKey={selectedReviewVideoKey}
            onSelect={setSelectedReviewVideoKey}
            onReview={onReviewVideo}
            canReview={canReview}
            canOpenAssets={canOpenAssets}
            reviewing={isReviewing}
          />
        </section>

        <aside className="min-w-0 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
          <div className="aspect-video overflow-hidden rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)]">
            {mode === "video-to-video" && sourceVideoPreview ? (
              <video src={sourceVideoPreview} controls preload="metadata" className="h-full w-full object-contain" />
            ) : imagePreview ? (
              <Image
                src={imagePreview}
                alt="Video source preview"
                width={1}
                height={1}
                unoptimized
                className="h-full w-full object-contain"
              />
            ) : (
              <div className="flex h-full items-center justify-center text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
                No source
              </div>
            )}
          </div>
          {mode === "image-to-video" && selectedImageSources.length > 0 && (
            <div className="mt-2 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-2 text-[11px] leading-5 text-[var(--forma-text-secondary)]">
              Previewing the first selected image. Generate will queue {selectedImageSources.length} image source{selectedImageSources.length === 1 ? "" : "s"}.
            </div>
          )}

          <div className="mt-4 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-4">
            <div className="flex items-center justify-between gap-3">
              <span className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Status</span>
              <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium uppercase tracking-[0.12em] ${videoStatusTone(status)}`}>
                {status === "failed" ? <AlertTriangle className="h-3.5 w-3.5" /> : status === "succeeded" ? <CheckCircle className="h-3.5 w-3.5" /> : <RefreshCw className={`h-3.5 w-3.5 ${isGenerating ? "animate-spin" : ""}`} />}
                {status}
              </span>
            </div>
            {requestId && <div className="mt-3 truncate font-mono text-[10px] text-[var(--forma-text-muted)]">{requestId}</div>}
            {statusMessage && <p className="mt-3 break-words text-xs leading-5 text-[var(--forma-text-secondary)]">{statusMessage}</p>}
            {modelsError && <p className="mt-3 break-words text-xs leading-5 text-[rgb(var(--forma-yellow-rgb))]">{modelsError}</p>}
          </div>

          <VideoReviewStatus
            status={reviewStatus}
            message={reviewMessage}
            available={reviewAvailable}
            unavailableReason={reviewUnavailableReason}
            isReviewing={isReviewing}
            makeNewVideo={makeNewVideo}
            setMakeNewVideo={setMakeNewVideo}
            canMakeNewVideo={canMakeNewVideo}
          />

          {storedVideo && (
            <div className="mt-4 rounded-lg border border-[rgb(var(--forma-green-rgb)/0.35)] bg-[rgb(var(--forma-green-rgb)/0.1)] p-4">
              <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-[rgb(var(--forma-green-rgb))]">
                <CheckCircle className="h-4 w-4" />
                Saved
              </div>
              {savedHref ? (
                <a
                  href={savedHref}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-3 inline-flex max-w-full items-center gap-2 rounded-md border border-[rgb(var(--forma-green-rgb)/0.4)] px-3 py-2 text-xs font-medium text-[rgb(var(--forma-green-rgb))] transition-colors hover:border-[rgb(var(--forma-green-rgb)/0.6)]"
                >
                  <ExternalLink className="h-4 w-4 shrink-0" />
                  Open saved video
                </a>
              ) : (
                <div className="mt-3 break-all font-mono text-xs leading-5 text-[var(--forma-text-body)]">{storedVideo.s3Uri || storedVideo.key}</div>
              )}
              {storedVideo.key && <div className="mt-3 break-all font-mono text-[10px] leading-5 text-[var(--forma-text-muted)]">{storedVideo.key}</div>}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

function VideoReviewStatus({
  status,
  message,
  available,
  unavailableReason,
  isReviewing,
  makeNewVideo,
  setMakeNewVideo,
  canMakeNewVideo,
}: {
  status: string;
  message: string | null;
  available: boolean;
  unavailableReason: string | null;
  isReviewing: boolean;
  makeNewVideo: boolean;
  setMakeNewVideo: (value: boolean) => void;
  canMakeNewVideo: boolean;
}) {
  return (
    <div className="mt-4 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Review</span>
        <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium uppercase tracking-[0.12em] ${videoStatusTone(status)}`}>
          {status === "failed" ? (
            <AlertTriangle className="h-3.5 w-3.5" />
          ) : status === "succeeded" ? (
            <CheckCircle className="h-3.5 w-3.5" />
          ) : isReviewing ? (
            <RefreshCw className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <ShieldCheck className="h-3.5 w-3.5" />
          )}
          {status}
        </span>
      </div>
      <label className={`mt-3 flex min-h-10 items-center gap-3 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 py-2 text-xs font-medium ${
        canMakeNewVideo ? "text-[var(--forma-text-body)]" : "text-[var(--forma-text-muted)]"
      }`}>
        <input
          type="checkbox"
          checked={makeNewVideo}
          onChange={(event) => setMakeNewVideo(event.target.checked)}
          disabled={!canMakeNewVideo || isReviewing}
          className="h-4 w-4 accent-[rgb(var(--forma-cyan-rgb))] disabled:cursor-not-allowed"
        />
        <span>Make new video</span>
      </label>
      {message && <p className="mt-3 break-words text-xs leading-5 text-[var(--forma-text-secondary)]">{message}</p>}
      {!available && unavailableReason && <p className="mt-3 break-words text-xs leading-5 text-[rgb(var(--forma-yellow-rgb))]">{unavailableReason}</p>}
    </div>
  );
}

function VideoGallery({
  videos,
  loading,
  error,
  onRefresh,
  selectedKey,
  onSelect,
  onReview,
  canReview,
  canOpenAssets,
  reviewing,
}: {
  videos: StoredVideoInfo[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  selectedKey: string | null;
  onSelect: (value: string | null) => void;
  onReview: (video: StoredVideoInfo) => void;
  canReview: boolean;
  canOpenAssets: boolean;
  reviewing: boolean;
}) {
  return (
    <div className="mt-5 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Film className="h-4 w-4 text-[rgb(var(--forma-cyan-rgb))]" />
          <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Gallery</div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={!canOpenAssets}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] text-[var(--forma-text-muted)] transition-colors hover:bg-[var(--forma-page)] hover:text-[var(--forma-text-strong)] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-[var(--forma-surface)] disabled:hover:text-[var(--forma-text-muted)]"
          title={canOpenAssets ? "Refresh gallery" : "Videos are available only on projects you generated."}
          aria-label="Refresh gallery"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      {error && (
        <div className="mb-3 break-words rounded-md border border-[rgb(var(--forma-yellow-rgb)/0.35)] bg-[rgb(var(--forma-yellow-rgb)/0.1)] p-3 text-xs leading-5 text-[rgb(var(--forma-yellow-rgb))]">
          {error}
        </div>
      )}

      {loading && !videos.length ? (
        <div className="rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 text-xs leading-5 text-[var(--forma-text-secondary)]">
          Loading gallery…
        </div>
      ) : videos.length ? (
        <div className="grid gap-3 md:grid-cols-2">
          {videos.map((video, index) => {
            const key = videoIdentity(video, `video-${index}`);
            const reviewable = Boolean(videoSourceUrl(video));
            return (
              <VideoGalleryItem
                key={key}
                video={video}
                identity={key}
                selected={selectedKey === key}
                onSelect={() => onSelect(key)}
                onReview={() => onReview(video)}
                canReview={canReview && reviewable}
                canOpenAssets={canOpenAssets}
                reviewing={reviewable && reviewing && selectedKey === key}
                reviewable={reviewable}
              />
            );
          })}
        </div>
      ) : (
        <div className="rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 text-xs leading-5 text-[var(--forma-text-secondary)]">
          No videos yet.
        </div>
      )}
    </div>
  );
}

function VideoGalleryItem({
  video,
  identity,
  selected,
  onSelect,
  onReview,
  canReview,
  canOpenAssets,
  reviewing,
  reviewable,
}: {
  video: StoredVideoInfo;
  identity: string;
  selected: boolean;
  onSelect: () => void;
  onReview: () => void;
  canReview: boolean;
  canOpenAssets: boolean;
  reviewing: boolean;
  reviewable: boolean;
}) {
  const playableUrl = canOpenAssets ? videoSourceUrl(video) || null : null;
  const openUrl = canOpenAssets ? playableUrl || null : null;
  const label = videoLabel(video);
  const prompt = videoPromptText(video);

  return (
    <article className={`min-w-0 overflow-hidden rounded-lg border bg-[var(--forma-surface)] transition ${
      selected
        ? "border-[rgb(var(--forma-cyan-rgb)/0.55)]"
        : "border-[var(--forma-border)]"
    }`}>
      <div className="aspect-video bg-[var(--forma-surface-muted)]">
        {playableUrl ? (
          <video src={playableUrl} controls preload="metadata" className="h-full w-full object-contain" />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
            Video saved
          </div>
        )}
      </div>
      <div className="border-t border-[var(--forma-border)] p-3">
        <div className="flex min-w-0 items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate font-mono text-[11px] text-[var(--forma-text-secondary)]">{label}</div>
            <div className="mt-1 truncate font-mono text-[10px] text-[var(--forma-text-muted)]">{identity}</div>
          </div>
          <span className={`shrink-0 rounded-md border px-2 py-1 text-[10px] font-medium uppercase tracking-[0.12em] ${
            selected
              ? "border-[rgb(var(--forma-cyan-rgb)/0.45)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] text-[rgb(var(--forma-cyan-rgb))]"
              : "border-[var(--forma-border)] text-[var(--forma-text-muted)]"
          }`}>
            {selected ? "Selected" : reviewable ? "Reviewable" : "No URL"}
          </span>
        </div>
        <div className="mt-3 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-3">
          <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Prompt</div>
          <p className="mt-2 max-h-28 overflow-y-auto break-words text-xs leading-5 text-[var(--forma-text-secondary)]">
            {prompt || "No prompt saved for this video."}
          </p>
        </div>
        {video.key && <div className="mt-2 break-all font-mono text-[10px] leading-4 text-[var(--forma-text-muted)]">{video.key}</div>}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">{formatBytes(video.sizeBytes || 0)}</span>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onSelect}
              disabled={!reviewable || !canOpenAssets}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-[var(--forma-border)] px-2 text-[10px] font-medium text-[var(--forma-text-body)] transition-colors hover:bg-[var(--forma-surface-muted)] hover:text-[var(--forma-text-strong)] disabled:cursor-not-allowed disabled:opacity-40"
              title={canOpenAssets ? (reviewable ? "Select video for review" : "This saved video needs an HTTP(S) URL before review") : "Videos are available only on projects you generated."}
            >
              {selected ? <CheckCircle className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              Select
            </button>
            <button
              type="button"
              onClick={() => {
                onSelect();
                onReview();
              }}
              disabled={!canReview || reviewing}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] px-2 text-[10px] font-medium text-[rgb(var(--forma-cyan-rgb))] transition-colors hover:border-[rgb(var(--forma-cyan-rgb)/0.55)] disabled:cursor-not-allowed disabled:opacity-40"
              title={reviewable ? "Review selected video" : "This saved video needs an HTTP(S) URL before review"}
            >
              {reviewing ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}
              Review
            </button>
            {openUrl ? (
              <a
                href={openUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-[var(--forma-border)] px-2 text-[10px] font-medium text-[var(--forma-text-body)] transition-colors hover:bg-[var(--forma-surface-muted)] hover:text-[var(--forma-text-strong)]"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                Open
              </a>
            ) : (
              <span className="truncate font-mono text-[10px] text-[var(--forma-text-muted)]">{video.s3Uri || "-"}</span>
            )}
          </div>
        </div>
      </div>
    </article>
  );
}

function ProjectDetailWorkspace({
  history,
  onOpenSidebar,
  projectId,
  projectTitle,
  isPrivate,
  owned,
  readOnly,
  onRenameTitle,
  namespaceTabs,
  activeNamespace,
  onNamespaceChange,
  projectContent,
}: {
  history: ProjectHistoryConfig;
  onOpenSidebar: () => void;
  projectId: string | null;
  projectTitle: string;
  isPrivate: boolean;
  owned: boolean;
  readOnly: boolean;
  onRenameTitle?: (title: string) => void;
  namespaceTabs: typeof workspaceTabs;
  activeNamespace: string;
  onNamespaceChange: (value: string) => void;
  projectContent: React.ReactNode;
}) {
  return (
    <ProjectHistoryProvider config={history}>
      <ChatProjectArtifact
        projectId={projectId}
        projectTitle={projectTitle}
        isPrivate={isPrivate}
        canEdit={!readOnly && owned}
        onRenameTitle={onRenameTitle}
        namespaceTabs={namespaceTabs}
        activeNamespace={activeNamespace}
        onNamespaceChange={onNamespaceChange}
        projectContent={projectContent}
        leading={<MobileSidebarButton onClick={onOpenSidebar} />}
      />
    </ProjectHistoryProvider>
  );
}

function ChatWorkspace({
  history,
  onOpenSidebar,
  projectId,
  chatId,
  projectTitle,
  isPrivate,
  onRenameTitle,
  messages,
  renderPipelineProgress,
  input,
  setInput,
  onSubmit,
  attachmentControls,
  onImagePaste,
  notice,
  isLoading,
  canStop,
  onStop,
  canRetryFailedBuild,
  retryLabel = "Try failed build again",
  retryingFailedBuild,
  onRetryFailedBuild,
  canChat,
  readOnly,
  authoringActive = false,
  namespaceTabs,
  activeNamespace,
  activeNamespaceLabel,
  activeNamespaceName,
  onNamespaceChange,
  projectContent,
}: {
  history: ProjectHistoryConfig;
  onOpenSidebar: () => void;
  projectId: string | null;
  chatId: string | null;
  projectTitle: string;
  isPrivate: boolean;
  onRenameTitle?: (title: string) => void;
  messages: ChatMessage[];
  renderPipelineProgress: (message: ConversationMessage) => React.ReactNode;
  input: string;
  setInput: (value: string) => void;
  onSubmit: (event: React.FormEvent) => void;
  attachmentControls: ChatAttachmentControls;
  onImagePaste: React.ClipboardEventHandler<HTMLTextAreaElement>;
  notice: string | null;
  isLoading: boolean;
  canStop: boolean;
  onStop: () => void;
  canRetryFailedBuild: boolean;
  retryLabel?: string;
  retryingFailedBuild: boolean;
  onRetryFailedBuild: () => void;
  canChat: boolean;
  readOnly: boolean;
  authoringActive?: boolean;
  namespaceTabs: typeof workspaceTabs;
  activeNamespace: string;
  activeNamespaceLabel: string;
  activeNamespaceName: string;
  onNamespaceChange: (value: string) => void;
  projectContent: React.ReactNode;
}) {
  const { containerRef, endRef, handleScroll } = useChatAutoScroll(chatId || projectId || "project-chat", messages);
  const { headerAway, updateFromContainer } = useChromeHeaderScroll(chatId || projectId || "project-chat");
  const chatAvailable = canChat && !readOnly;
  const hasInput = Boolean(input.trim() || attachmentControls.selectedImage || attachmentControls.selectedDocumentName);
  const retryMode = shouldOfferFailedBuildRetry({
    canRetryFailedBuild: canRetryFailedBuild && !readOnly,
    hasInput,
    generationActive: canStop,
  });
  const primaryActionLabel = readOnly
    ? "Hosted chat is temporarily under maintenance"
    : canStop
      ? "Stop project update"
      : retryMode
        ? retryLabel
        : "Apply change to project, or press Enter";

  const onChatScroll = () => {
    handleScroll();
    updateFromContainer(containerRef.current);
  };

  return (
    <ChatProjectLayout
      history={history}
      conversationKey={chatId || projectId || "project-chat"}
      projectId={projectId}
      project={(chatAvailable || readOnly) && projectId ? (
                <ChatProjectArtifact
                  projectId={projectId}
                  projectTitle={projectTitle}
                  isPrivate={isPrivate}
                  canEdit={chatAvailable && Boolean(onRenameTitle)}
                  onRenameTitle={chatAvailable ? onRenameTitle : undefined}
                  namespaceTabs={namespaceTabs}
                  activeNamespace={activeNamespace}
                  onNamespaceChange={onNamespaceChange}
                  projectContent={projectContent}
                />
      ) : null}
    >
    <div className="relative flex h-full min-h-0 min-w-0 flex-col bg-[var(--forma-page)]">
      <header className={`workspace-chrome-header absolute inset-x-0 top-0 z-20 flex min-h-14 min-w-0 items-center gap-3 px-3 pb-5 pt-2 sm:px-4 ${headerAway ? "is-away" : ""}`}>
        <MobileSidebarButton onClick={onOpenSidebar} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-[rgb(var(--forma-green-rgb)/0.12)] px-2 py-0.5 text-[10px] font-medium text-[rgb(var(--forma-green-rgb))]">
              {chatAvailable ? <MessageSquare className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
              {authoringActive
                ? "FormaAgent authoring"
                : chatAvailable
                  ? "Project chat"
                  : "Project"}
            </span>
            <EditableWorkspaceTitle
              value={projectTitle}
              canEdit={chatAvailable && Boolean(onRenameTitle)}
              label="Project chat title"
              onCommit={(title) => onRenameTitle?.(title)}
            />
          </div>
        </div>
      </header>

      <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
        {(chatAvailable || readOnly) && (
          <div className="flex h-full min-h-0 min-w-0 flex-col">
            <div
              ref={containerRef}
              onScroll={onChatScroll}
              className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-3 pb-5 pt-16 sm:px-5 sm:pb-6 sm:pt-16"
            >
              <div className="mx-auto flex w-full min-w-0 max-w-6xl flex-col gap-3">
                {readOnly && <HostedChatMaintenance compact />}
                {authoringActive && <AuthoringModeBanner compact />}
                <ConversationMessageList
                  messages={messages}
                  renderPipelineProgress={renderPipelineProgress}
                  variant="project"
                   emptyMessage="This chat has no project messages yet."
                   isLoading={readOnly ? false : isLoading}
                   assistantLabel={authoringActive ? "OpenCode" : "Forma"}
                />
                <div ref={endRef} />

              </div>
            </div>

            {chatAvailable && (
              <form onSubmit={onSubmit} className="shrink-0 border-t border-white/5 bg-[#0f1117]/95 p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] backdrop-blur sm:p-4">
              <div className="mx-auto max-w-3xl">
                {notice && <div id="project-input-notice" role="status" className="mb-3 rounded-lg border border-amber-300/30 bg-amber-300/10 px-3 py-2 text-xs leading-5 text-amber-100">{notice.replace(/\bOpenCode\b/g, "Forma Agent")}</div>}
                <div className="w-full rounded-2xl border border-white/5 bg-[#181b22] p-3 shadow-lg transition-all focus-within:border-emerald-500/50 focus-within:ring-1 focus-within:ring-emerald-500/20">
                  <ChatAttachmentSelection {...attachmentControls} />
                  <textarea
                    value={input}
                    onPaste={onImagePaste}
                    aria-describedby={notice ? "project-input-notice" : undefined}
                    onChange={(event) => setInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        if (isLoading) return;
                        if (retryMode) {
                          onRetryFailedBuild();
                          return;
                        }
                        event.currentTarget.form?.requestSubmit();
                      }
                    }}
                    aria-label={`Describe a change to ${activeNamespaceLabel.toLowerCase()}`}
                    placeholder={`Describe a change to ${activeNamespaceLabel.toLowerCase()}...`}
                    className="min-h-[72px] w-full resize-none border-none bg-transparent text-sm leading-6 text-zinc-100 outline-none placeholder:text-zinc-500"
                  />
                  <div className="mt-1 flex items-center justify-between gap-1.5">
                    <ChatAttachmentButton imageInputRef={attachmentControls.imageInputRef} />
                    <div className="flex items-center gap-1.5">
                    {!canStop && !retryMode && !isLoading && hasInput && (
                      <span className="prompt-composer-enter-hint hidden sm:inline" aria-hidden="true">
                        Enter
                      </span>
                    )}
                    <button
                      type={canStop || retryMode ? "button" : "submit"}
                      onClick={canStop || retryMode ? (event) => {
                        event.preventDefault();
                        if (canStop) onStop();
                        else onRetryFailedBuild();
                      } : undefined}
                      disabled={retryMode ? retryingFailedBuild : !canStop && (isLoading || !projectId || !hasInput)}
                      className={`prompt-composer-send inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed ${
                        retryMode || (!canStop && !isLoading && hasInput) ? "is-ready" : ""
                      }`}
                      aria-label={primaryActionLabel}
                      title={retryMode || canStop ? primaryActionLabel : `Apply change to ${activeNamespaceName} · Enter`}
                    >
                      {canStop ? (
                        <Square className="h-3.5 w-3.5 fill-current" />
                      ) : retryMode ? (
                        <RefreshCw className={`h-3.5 w-3.5 ${retryingFailedBuild ? "animate-spin" : ""}`} />
                      ) : isLoading ? (
                        <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <ArrowRight className="h-4 w-4" />
                      )}
                    </button>
                    </div>
                  </div>
                </div>
              </div>
              </form>
            )}
          </div>
        )}

        {!chatAvailable && !readOnly && (
          <section className="absolute inset-0 min-h-0 min-w-0 overflow-hidden bg-[var(--forma-page)] pt-14" aria-label="Project workspace">
            <ProjectWorkspacePanel
              projectId={projectId}
              namespaceTabs={namespaceTabs}
              activeNamespace={activeNamespace}
              onNamespaceChange={onNamespaceChange}
            >
              {projectContent}
            </ProjectWorkspacePanel>
          </section>
        )}
      </div>
    </div>
    </ChatProjectLayout>
  );
}

function ChatProjectArtifact({
  projectId,
  projectTitle,
  isPrivate = false,
  canEdit = false,
  onRenameTitle,
  namespaceTabs,
  activeNamespace,
  onNamespaceChange,
  projectContent,
  leading,
}: {
  projectId: string | null;
  projectTitle: string;
  isPrivate?: boolean;
  canEdit?: boolean;
  onRenameTitle?: (title: string) => void;
  namespaceTabs: typeof workspaceTabs;
  activeNamespace: string;
  onNamespaceChange: (namespaceId: string) => void;
  projectContent: React.ReactNode;
  leading?: React.ReactNode;
}) {
  const history = useProjectHistory();
  const viewingSnapshot = Boolean(history?.selection);
  return (
    <ChatProjectSurface
      leading={leading}
      projectId={projectId}
      shareTitle={projectTitle}
      isPrivate={isPrivate}
      title={(
        <EditableWorkspaceTitle
          value={history?.selection?.snapshot?.title || projectTitle}
          canEdit={!viewingSnapshot && canEdit && Boolean(onRenameTitle)}
          label="Project title"
          element="div"
          className="truncate text-xs font-semibold text-[var(--forma-text-strong)]"
          onCommit={(title) => onRenameTitle?.(title)}
        />
      )}
    >
      {viewingSnapshot ? <ProjectRevisionPreview /> : <ProjectWorkspacePanel
        projectId={projectId}
        namespaceTabs={namespaceTabs}
        activeNamespace={activeNamespace}
        onNamespaceChange={onNamespaceChange}
      >
        {projectContent}
      </ProjectWorkspacePanel>}
    </ChatProjectSurface>
  );
}

function ProjectWorkspacePanel({
  projectId,
  namespaceTabs,
  activeNamespace,
  onNamespaceChange,
  children,
}: {
  projectId?: string | null;
  namespaceTabs: typeof workspaceTabs;
  activeNamespace: string;
  onNamespaceChange: (value: string) => void;
  children: React.ReactNode;
}) {
  const namespaceName = workspaceNamespaceForTab(activeNamespace);
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col">
      <nav
        className="flex min-h-[44px] min-w-0 shrink-0 items-center gap-1 overflow-x-auto border-b border-[var(--forma-border)] bg-[var(--forma-surface)] px-2"
        aria-label="Project workspace"
      >
        {namespaceTabs.map((tab) => {
          const Icon = tab.icon;
          const active = activeNamespace === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => onNamespaceChange(tab.id)}
              className={`inline-flex h-8 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 text-xs font-medium transition-colors ${
                active
                  ? "bg-[rgb(var(--forma-green-rgb)/0.12)] text-[rgb(var(--forma-green-rgb))]"
                  : "text-[var(--forma-text-muted)] hover:bg-[var(--forma-surface-muted)] hover:text-[var(--forma-text-strong)]"
              }`}
              aria-pressed={active}
              title={`${tab.label} / ${workspaceNamespaceForTab(tab.id)}`}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" strokeWidth={1.75} />
              <span className={active ? "inline" : "hidden sm:inline"}>{tab.label}</span>
            </button>
          );
        })}
      </nav>
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden">{children}</div>
      <div className="flex shrink-0 items-center justify-end gap-2 border-t border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 py-1.5 font-mono text-[9px] text-[var(--forma-text-muted)]">
        {projectId && (
          <span className="max-w-[min(100%,14rem)] truncate" title={projectId}>
            {projectId}
          </span>
        )}
        {projectId && <span aria-hidden="true">·</span>}
        <span className="max-w-48 truncate" title={namespaceName}>
          {namespaceName}
        </span>
      </div>
    </div>
  );
}

function AgentPipelineEventsDisclosure({
  eventCount,
  children,
}: {
  eventCount: number;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="group mt-3 border-t border-white/5 pt-3"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 marker:content-none [&::-webkit-details-marker]:hidden">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <ChevronDown className="h-3 w-3 shrink-0 -rotate-90 text-zinc-600 transition-transform group-open:rotate-0" />
          <span className="text-[10px] font-medium text-zinc-600">Recent events</span>
        </span>
        <span className="font-mono text-[10px] text-zinc-600">{eventCount}</span>
      </summary>
      <div className="mt-2">{children}</div>
    </details>
  );
}

function AgentPipelineProgressView({
  progress,
  status,
  compact = false,
  onStop,
  onReset,
  resetting = false,
}: {
  progress?: AgentPipelineProgress | null;
  status?: ChatMessage["status"];
  compact?: boolean;
  onStop?: () => void;
  onReset?: () => void;
  resetting?: boolean;
}) {
  if (!progress) return null;

  const steps = progress.steps.length ? progress.steps : defaultAgentPipelineSteps;
  const events = normalizeAgentPipelineEvents(progress.events);
  const lastEvent = latestPipelineEvent(events);
  const activeStep = activePipelineStep({ ...progress, steps });
  const activeStepId = activeStep?.id || null;
  const nowMs = Date.now();
  const startedMs = timestampMs(progress.startedAt);
  const elapsedSeconds = startedMs === null ? null : Math.max(1, Math.round((nowMs - startedMs) / 1000));
  const lastEventMs = pipelineEventTimestampMs(lastEvent);
  const quietMs = lastEventMs === null ? null : nowMs - lastEventMs;
  const isLoading = status === "loading";
  const isSuccess = status === "success";
  const isCancelled = status === "cancelled";
  // Progress events are an audit trail and may include a failed attempt that
  // the backend subsequently retries. Only the terminal message state means
  // the whole pipeline failed.
  const isError = status === "error";
  const waitingForFirstEvent = isLoading && !events.length && startedMs !== null && nowMs - startedMs >= PIPELINE_STALE_AFTER_MS;
  const backendQuiet = isLoading && quietMs !== null && quietMs >= PIPELINE_STALE_AFTER_MS;
  const completedCount = completedPipelineStepCount({ ...progress, steps });
  const progressPercent = Math.min(100, Math.max(6, Math.round((completedCount / Math.max(steps.length, 1)) * 100)));
  const visibleEvents = events.slice(compact ? -4 : -6);
  const signalLabel = isError
    ? "failed"
    : isCancelled
    ? "stopped"
    : isSuccess
    ? "completed"
    : progress.synced
    ? backendQuiet
      ? "waiting on provider"
      : "backend synced"
    : waitingForFirstEvent
      ? "starting"
      : "estimated";
  const signalTone = isError
    ? "border-rose-400/35 bg-rose-950/25 text-rose-200"
    : isCancelled
    ? "border-amber-300/35 bg-amber-950/20 text-amber-100"
    : isSuccess
    ? "border-emerald-300/35 bg-emerald-950/20 text-emerald-100"
    : backendQuiet || waitingForFirstEvent
    ? "border-emerald-500/25 bg-emerald-950/25 text-emerald-100"
    : progress.synced
      ? "border-emerald-500/25 bg-emerald-950/25 text-emerald-100"
      : "border-slate-500/25 bg-black/25 text-slate-400";

  return (
    <div className="mt-3 rounded-xl border border-white/5 bg-black/25 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-medium text-zinc-500">Agent pipeline</span>
            <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] font-medium ${signalTone}`}>
              {isError ? <AlertTriangle className="h-3 w-3" /> : isCancelled ? <Square className="h-3 w-3 fill-current" /> : isLoading ? <RefreshCw className="h-3 w-3 animate-spin" /> : <CheckCircle className="h-3 w-3" />}
              {signalLabel}
            </span>
          </div>
          {progress.jobId && (
            <div className="mt-1 truncate font-mono text-[10px] text-zinc-600">{progress.jobId}</div>
          )}
        </div>
        <div className="flex shrink-0 items-start gap-2">
          {isLoading && onStop && (
            <button
              type="button"
              onClick={onStop}
              className="inline-flex h-7 items-center gap-1 rounded-lg border border-amber-300/40 px-2 text-[10px] font-medium text-amber-100 transition-colors hover:bg-amber-300/10"
              aria-label="Stop agent pipeline"
              title="Stop agent pipeline"
            >
              <Square className="h-3 w-3 fill-current" />
              Stop
            </button>
          )}
          {isError && onReset && (
            <button
              type="button"
              onClick={onReset}
              disabled={resetting}
              className="inline-flex h-7 items-center gap-1 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2 text-[10px] font-medium text-emerald-400 transition-colors hover:bg-emerald-500/20 disabled:cursor-wait disabled:opacity-60"
              aria-label="Reset failed generation job"
              title="Reset failed generation job and try again"
            >
              <RefreshCw className={`h-3 w-3 ${resetting ? "animate-spin" : ""}`} />
              {resetting ? "Resetting" : "Reset job"}
            </button>
          )}
          <div className="text-right">
            <div className="font-mono text-[11px] font-semibold text-zinc-300">{completedCount}/{steps.length}</div>
            <div className="text-[10px] text-zinc-600">{formatDurationSeconds(elapsedSeconds)}</div>
          </div>
        </div>
      </div>

      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#111216]">
        <div
          className={`h-full rounded-full ${isError ? "bg-rose-300" : isCancelled ? "bg-amber-300" : "bg-emerald-400"} ${isLoading ? "animate-pulse" : ""}`}
          style={{ width: `${progressPercent}%` }}
        />
      </div>

      <div className="mt-3 flex min-w-0 items-start gap-2 rounded-lg border border-white/5 bg-[#111216] p-3">
        {isError ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-300" /> : isCancelled ? <Square className="mt-0.5 h-4 w-4 shrink-0 fill-current text-amber-300" /> : isLoading ? <RefreshCw className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-emerald-400" /> : <Cpu className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" />}
        <div className="min-w-0">
          <div className="truncate text-xs font-semibold text-zinc-100">{activeStep?.label || "Preparing job"}</div>
          <div className="mt-1 truncate text-[11px] font-medium text-emerald-400">{activeStep?.agent || "Forma runtime"}</div>
          {activeStep?.description && !compact && (
            <div className="mt-1 line-clamp-2 text-[11px] leading-4 text-zinc-500">{activeStep.description}</div>
          )}
          {lastEvent && (
            <div className="mt-2 flex flex-wrap gap-2 text-[10px] text-zinc-500">
              <span>Last: {lastEvent.label || lastEvent.step_id}</span>
              <span>{String(lastEvent.status).replace(/_/g, " ")}</span>
              <span>{formatPipelineAge(lastEvent.observed_at, nowMs)} ago</span>
            </div>
          )}
        </div>
      </div>

      {(backendQuiet || waitingForFirstEvent) && (
        <div className="mt-2 flex gap-2 rounded-lg border border-emerald-500/20 bg-emerald-950/20 p-2 text-[11px] leading-4 text-emerald-100">
          <RefreshCw className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
          <span>
            {events.length
              ? `Still working. The active provider or backend call has been running for ${formatDurationSeconds(Math.round((quietMs || 0) / 1000))} since the last progress update.`
              : "Still starting. The job poller is active and waiting for the first backend progress update."}
          </span>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-1.5">
        {steps.map((step) => (
          <span
            key={step.id}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/5 bg-[#111216] px-2 py-1 text-[10px] text-zinc-500"
            title={`${step.agent}: ${step.label}`}
          >
            <PipelineStepDot status={pipelineStepStatus({ ...progress, steps }, step, activeStepId, isLoading)} />
            <span className="max-w-[120px] truncate">{step.label}</span>
          </span>
        ))}
      </div>

      {!isLoading && (
        <AgentPipelineEventsDisclosure eventCount={events.length}>
          {visibleEvents.length ? (
            <div className="space-y-1.5">
              {visibleEvents.map((event, index) => {
                const details = formatPipelineDetails(event.details);
                return (
                  <div key={`${event.step_id}-${event.status}-${event.observed_at || index}`} className="min-w-0 rounded-lg border border-white/5 bg-[#0f1014] px-2 py-1.5">
                    <div className="flex min-w-0 flex-wrap items-center gap-2 text-[10px]">
                      <span className="max-w-[160px] truncate font-medium text-zinc-300">{event.label || event.step_id}</span>
                      <span className={`${isFailedPipelineStatus(event.status) ? "text-rose-300" : isCompletedPipelineStatus(event.status) ? "text-emerald-300" : "text-emerald-400"}`}>
                        {String(event.status).replace(/_/g, " ")}
                      </span>
                      <span className="text-zinc-600">{formatPipelineAge(event.observed_at, nowMs)} ago</span>
                    </div>
                    {details && !compact && <div className="mt-1 line-clamp-2 text-[10px] leading-4 text-zinc-500">{details}</div>}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rounded-lg border border-white/5 bg-[#0f1014] px-2 py-2 text-[11px] leading-4 text-zinc-500">
              Polling job metadata. Backend pipeline events will appear here as agents report progress.
            </div>
          )}
        </AgentPipelineEventsDisclosure>
      )}
    </div>
  );
}
