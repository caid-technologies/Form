export type ServerConnectionStatus = "connected" | "disconnected";

export type WorkspaceStatusTone = "ok" | "authoring" | "delivered" | "error";

export type WorkspaceStatusReason =
  | "stable"
  | "authoring"
  | "delivered"
  | "disconnected"
  | "auth"
  | "run-failure"
  | "timeout";

export type AgentOperationStatus = "idle" | "loading" | "success" | "error" | "cancelled" | "handed-off" | "interrupted";

export type AgentOperationSignal = {
  status?: AgentOperationStatus;
  content?: string | null;
  startedAt?: string | null;
  lastEventAt?: string | null;
};

export type WorkspaceStatusSignals = {
  connection: ServerConnectionStatus;
  authError?: boolean;
  agent?: AgentOperationSignal | null;
  authoring?: boolean;
  delivered?: boolean;
  nowMs?: number;
  staleAfterMs?: number;
};

export type WorkspaceStatusPresentation = {
  tone: WorkspaceStatusTone;
  reason: WorkspaceStatusReason;
  label: string;
  pulse: boolean;
};

export const WORKSPACE_STATUS_STALE_AFTER_MS = 30_000;

const AUTH_OR_SECURITY_ERROR_RE =
  /\b(401|403|unauthorized|unauthenticated|forbidden|invalid (?:access )?token|authentication (?:failed|error)|auth(?:entication)? required|security (?:error|violation|policy))\b/i;

const EXECUTION_TIMEOUT_RE =
  /\b(timed? out|timeout|deadline exceeded|aborted due to timeout|execution timeout)\b/i;

const CONNECTION_STATUS_PRESENTATION = {
  connected: {
    label: "Connected",
    dotClassName: "bg-emerald-400",
  },
  disconnected: {
    label: "Disconnected",
    dotClassName: "bg-orange-400",
  },
} as const satisfies Record<ServerConnectionStatus, {
  label: string;
  dotClassName: string;
}>;

export function connectionStatusPresentation(status: ServerConnectionStatus) {
  return CONNECTION_STATUS_PRESENTATION[status];
}

export function isAuthOrSecurityHttpStatus(status: number) {
  return status === 401 || status === 403;
}

export function isAuthOrSecurityError(content?: string | null) {
  return Boolean(content && AUTH_OR_SECURITY_ERROR_RE.test(content));
}

export function isExecutionTimeout(content?: string | null) {
  return Boolean(content && EXECUTION_TIMEOUT_RE.test(content));
}

export function workspaceStatusBadge({
  connection,
  authError = false,
  agent = null,
  authoring = false,
  delivered = false,
  nowMs = Date.now(),
  staleAfterMs = WORKSPACE_STATUS_STALE_AFTER_MS,
}: WorkspaceStatusSignals): WorkspaceStatusPresentation {
  const content = agent?.content || "";
  const agentFailed = agent?.status === "error";
  const timedOut = (agentFailed && isExecutionTimeout(content))
    || agentOperationTimedOut(agent, nowMs, staleAfterMs);
  const authFailed = authError || isAuthOrSecurityError(content);

  if (authFailed) {
    return present("error", "auth", "Authentication or security error", true);
  }
  if (timedOut) {
    return present("error", "timeout", "Execution timed out", true);
  }
  if (agentFailed) {
    return present("error", "run-failure", "Run failed", true);
  }
  if (connection !== "connected") {
    return present("error", "disconnected", "Disconnected", true);
  }
  if (authoring && delivered) {
    return present("delivered", "delivered", "Delivered — agent work is on-chain", false);
  }
  if (authoring) {
    return present("authoring", "authoring", "Authoring — Forma Agent is building this project", true);
  }

  return present(
    "ok",
    "stable",
    "Ready — agent, connection, and auth are healthy",
    agent?.status === "loading",
  );
}

function present(
  tone: WorkspaceStatusTone,
  reason: WorkspaceStatusReason,
  label: string,
  pulse: boolean,
): WorkspaceStatusPresentation {
  return { tone, reason, label, pulse };
}

function agentOperationTimedOut(
  agent: AgentOperationSignal | null | undefined,
  nowMs: number,
  staleAfterMs: number,
) {
  if (agent?.status !== "loading") return false;
  const startedMs = timestampMs(agent.startedAt);
  const lastEventMs = timestampMs(agent.lastEventAt);
  if (lastEventMs !== null) return nowMs - lastEventMs >= staleAfterMs;
  return startedMs !== null && nowMs - startedMs >= staleAfterMs;
}

function timestampMs(value?: string | null) {
  if (!value?.trim()) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}
