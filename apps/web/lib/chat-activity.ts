/** Current operation activity is separate from the status saved in chat history. */
export type ChatActivity = {
  state: "checking" | "queued" | "running" | "reconnecting" | "interrupted" | "settled";
  label: string;
};

export type ChatActivityMessage = {
  id: string;
  status?: string;
  content: string;
  contextProjectId?: string | null;
  buildPlanId?: string | null;
  pipelineProgress?: { jobId?: string | null } | null;
};

export type ChatOperation = {
  key: string;
  path: string;
  projectId?: string;
  kind: "build" | "job";
};

export type ChatActivityObservation = ChatActivity & {
  observedAt: number;
  outcome?: "succeeded" | "failed" | "partial" | "cancelled";
  projectId?: string;
};

export type ChatActivityObservations = Record<string, ChatActivityObservation>;
export const CHAT_ACTIVITY_STALE_MS = 60_000;

export function chatOperation(message: ChatActivityMessage): ChatOperation | null {
  if (message.status !== "loading") return null;
  if (message.buildPlanId && message.contextProjectId) {
    return {
      key: JSON.stringify(["build", message.contextProjectId, message.buildPlanId]),
      kind: "build",
      projectId: message.contextProjectId,
      path: `/projects/${encodeURIComponent(message.contextProjectId)}/build/plans/${encodeURIComponent(message.buildPlanId)}`,
    };
  }
  const jobId = message.pipelineProgress?.jobId;
  // Worker-plan jobs are not A2A jobs. A missing plan cannot be recovered there.
  if (jobId && !jobId.startsWith("generation-")) {
    return { key: JSON.stringify(["job", jobId]), kind: "job", path: `/a2a/jobs/${encodeURIComponent(jobId)}` };
  }
  return null;
}

export function collectChatOperations(threads: Record<string, ChatActivityMessage[]>): ChatOperation[] {
  const operations = new Map<string, ChatOperation>();
  Object.values(threads).forEach((messages) => messages.forEach((message) => {
    const operation = chatOperation(message);
    if (operation) operations.set(operation.key, operation);
  }));
  return [...operations.values()].sort((left, right) => left.key.localeCompare(right.key));
}

const activityPriority: Record<ChatActivity["state"], number> = {
  settled: 0, interrupted: 1, checking: 2, queued: 3, reconnecting: 4, running: 5,
};

export function chatActivity(
  messages: ChatActivityMessage[],
  observations: ChatActivityObservations,
  locallyActiveMessageIds: ReadonlySet<string>,
  now = Date.now(),
): ChatActivity | undefined {
  let activity: ChatActivity | undefined;
  for (const message of messages) {
    if (message.status !== "loading") continue;
    const operation = chatOperation(message);
    const observed = operation ? observations[operation.key] : undefined;
    let candidate: ChatActivity;
    if (observed?.state === "settled") continue;
    if (observed) {
      candidate = now - observed.observedAt > CHAT_ACTIVITY_STALE_MS
        ? { state: "reconnecting", label: "Progress has not been confirmed recently. Checking again." }
        : observed;
    } else if (locallyActiveMessageIds.has(message.id)) {
      candidate = { state: "running", label: "Request in progress." };
    } else if (operation) {
      candidate = { state: "checking", label: "Checking saved build status." };
    } else {
      candidate = { state: "interrupted", label: "This saved request has no recoverable operation ID. Its outcome is unknown." };
    }
    if (!activity || activityPriority[candidate.state] > activityPriority[activity.state]) activity = candidate;
  }
  return activity;
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** GET only: viewing history must never start or retry generation. */
export async function readChatActivity(
  apiUrl: string,
  operation: ChatOperation,
  headers: Record<string, string>,
  signal: AbortSignal,
  request: typeof fetch = fetch,
): Promise<ChatActivityObservation> {
  const now = () => Date.now();
  try {
    const response = await request(`${apiUrl}${operation.path}`, { method: "GET", headers, signal, cache: "no-store" });
    if (response.status === 404) {
      return { state: "interrupted", label: "The saved operation is no longer available. Its outcome is unknown.", observedAt: now() };
    }
    if (response.status === 401 || response.status === 403) {
      return { state: "interrupted", label: "Sign-in or permission is required to check this operation.", observedAt: now() };
    }
    if (!response.ok) throw new Error("Progress temporarily unavailable");
    const data = object(await response.json());
    const status = data?.status;
    if (status === "planned" || status === "queued" || status === "leased" || status === "pending") {
      return { state: "queued", label: "Build queued; waiting to run.", observedAt: now() };
    }
    if (status === "running") return { state: "running", label: "The server reports this build is running.", observedAt: now() };
    if (status === "succeeded" || status === "failed" || status === "partial" || status === "cancelled" || status === "canceled") {
      const summary = object(data?.result_summary);
      return {
        state: "settled", label: "Build finished.", observedAt: now(),
        outcome: status === "canceled" ? "cancelled" : status,
        projectId: operation.projectId || (typeof summary?.project_id === "string" ? summary.project_id : undefined),
      };
    }
    throw new Error("Unknown operation status");
  } catch {
    return { state: "reconnecting", label: "Could not confirm progress. Checking again; the build may still be running.", observedAt: now() };
  }
}

/** Apply only confirmed terminal outcomes; missing records are never success. */
export function settleChatActivityMessages<T extends ChatActivityMessage>(
  messages: T[],
  observations: ChatActivityObservations,
  locallyActiveMessageIds: ReadonlySet<string>,
): T[] {
  let changed = false;
  const next = messages.map((message) => {
    if (locallyActiveMessageIds.has(message.id)) return message;
    const operation = chatOperation(message);
    const result = operation ? observations[operation.key] : undefined;
    if (!result?.outcome || result.state !== "settled") return message;
    changed = true;
    const status = result.outcome === "succeeded" ? "success"
      : result.outcome === "cancelled" ? "cancelled" : "error";
    const content = result.outcome === "succeeded" ? "Build completed. Open the saved project to review the result."
      : result.outcome === "partial" ? "The build saved partial work. Review the project before retrying."
      : result.outcome === "cancelled" ? "This build was cancelled."
      : "This build failed. Your saved project history is preserved.";
    return {
      ...message, status, content,
      ...(result.projectId && (result.outcome === "succeeded" || result.outcome === "partial") ? { projectId: result.projectId } : {}),
    };
  });
  return changed ? next : messages;
}
