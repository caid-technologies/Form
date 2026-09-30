export const FORMA_AGENT_RUNTIME_STORAGE_KEY = "forma.agent.runtime.connector_id";

export function normalizeOpenCodeModel(value: string): string | null {
  const model = value.trim();
  if (!model) return null;
  if (model.length > 200 || !/^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_./:-]*$/.test(model)) {
    throw new Error("Use a model ID in provider/model format.");
  }
  return model;
}

export type OpenCodeSession = {
  session_id: string;
  connector_id: string;
  project_id: string;
  owner_user_id: string;
  status: "active" | "cancelled" | "completed" | "failed";
};

export type OpenCodeCommand = {
  model?: string | null;
  command_id: string;
  session_id: string;
  project_id: string;
  operation: "project_message" | "compile_project" | "validate_project";
  status: "queued" | "leased" | "running" | "succeeded" | "failed" | "cancelled";
};

export type OpenCodeEvent = {
  event_id: string;
  sequence: number;
  session_id: string;
  project_id: string;
  kind: "assistant_message" | "queued" | "working" | "validating" | "completed" | "failed" | "cancelled" | "connector_unavailable" | "progress";
  status: OpenCodeCommand["status"] | null;
  message: string | null;
  revision_id: string | null;
  design_outcome?: { project_readiness: "draft" | "partial" | "complete" } | null;
  error: { code: string; message: string; correlation_id: string } | null;
  created_at: string;
};

export function openCodeDesignNotice(readiness: unknown): string | null {
  if (readiness === "complete") return null;
  if (readiness === "draft") return "Draft saved only. No populated hardware design was produced. Ask Forma Agent to add components, pins, and wiring, then compile and resolve validation findings.";
  if (readiness === "partial") return "An incomplete design was saved. Review its wiring and validation findings before treating it as a completed design.";
  return "Forma Agent finished responding, but a completed design has not been verified.";
}

type OpenCodeEventPage = {
  events: OpenCodeEvent[];
  next_cursor: number;
};

export type OpenCodeTurnState = {
  assistantMessage: string | null;
  content: string;
  status: "loading" | "success" | "error" | "cancelled";
  terminalEvent: OpenCodeEvent | null;
};

export function reduceOpenCodeTurn(
  state: OpenCodeTurnState,
  event: OpenCodeEvent,
  commandId: string,
): OpenCodeTurnState {
  if (state.terminalEvent) return state;
  if (event.kind === "connector_unavailable") {
    return {
      ...state,
      content: state.assistantMessage || "Waiting for Forma Agent to reconnect. You can stop this request at any time.",
    };
  }
  if (event.kind === "assistant_message" && event.message) {
    return { ...state, assistantMessage: event.message, content: event.message };
  }
  if (event.kind === "completed" || event.kind === "failed" || event.kind === "cancelled") {
    // A prior command's terminal event must not finish the current turn.
    if (event.event_id !== `${commandId}:terminal` && event.event_id !== `cancelled_${event.session_id}`) return state;
    const status = event.kind === "completed" ? "success" : event.kind === "failed" ? "error" : "cancelled";
    let content = event.kind === "completed"
      ? state.assistantMessage || "Forma Agent finished responding."
      : event.kind === "failed"
        ? event.error?.message || "Forma Agent could not complete this request."
         : "Forma Agent was stopped.";
    if (event.kind === "failed" && event.error?.code === "connector_timeout") {
      content += `\n\nCode: ${event.error.code}`;
    }
    if (event.kind === "completed" && event.design_outcome) {
      const notice = openCodeDesignNotice(event.design_outcome.project_readiness);
      if (notice) content += `\n\n${notice}`;
    }
    return { ...state, content, status, terminalEvent: event };
  }
  return {
    ...state,
    content: state.assistantMessage || (event.kind === "validating"
      ? "Forma Agent is validating the project."
      : "Forma Agent is working on your request."),
  };
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Forma Agent returned an invalid response.");
  }
  return value as Record<string, unknown>;
}

function stringField(value: Record<string, unknown>, name: string): string {
  if (typeof value[name] !== "string" || !value[name]) throw new Error("Forma Agent returned an invalid response.");
  return value[name];
}

function nullableStringField(value: Record<string, unknown>, name: string): string | null {
  if (value[name] !== null && typeof value[name] !== "string") throw new Error("Forma Agent returned an invalid response.");
  return (value[name] as string | null | undefined) ?? null;
}

function parseSession(value: unknown): OpenCodeSession {
  const item = record(value);
  const status = stringField(item, "status");
  if (status !== "active" && status !== "cancelled" && status !== "completed" && status !== "failed") throw new Error("Forma Agent returned an invalid session status.");
  return {
    session_id: stringField(item, "session_id"),
    connector_id: stringField(item, "connector_id"),
    project_id: stringField(item, "project_id"),
    owner_user_id: stringField(item, "owner_user_id"),
    status,
  };
}

function parseCommand(value: unknown): OpenCodeCommand {
  const item = record(value);
  const operation = stringField(item, "operation");
  const status = stringField(item, "status");
  if (!["project_message", "compile_project", "validate_project"].includes(operation)) throw new Error("Forma Agent returned an invalid command operation.");
  if (!["queued", "leased", "running", "succeeded", "failed", "cancelled"].includes(status)) throw new Error("Forma Agent returned an invalid command status.");
  return {
    command_id: stringField(item, "command_id"),
    model: typeof item.model === "string" ? normalizeOpenCodeModel(item.model) : null,
    session_id: stringField(item, "session_id"),
    project_id: stringField(item, "project_id"),
    operation: operation as OpenCodeCommand["operation"],
    status: status as OpenCodeCommand["status"],
  };
}

function parseEvent(value: unknown): OpenCodeEvent {
  const item = record(value);
  const kind = stringField(item, "kind");
  if (!["assistant_message", "queued", "working", "validating", "completed", "failed", "cancelled", "connector_unavailable", "progress"].includes(kind)) {
    throw new Error("Forma Agent returned an invalid event kind.");
  }
  const status = item.status === null || item.status === undefined ? null : stringField(item, "status");
  if (status !== null && !["queued", "leased", "running", "succeeded", "failed", "cancelled"].includes(status)) throw new Error("Forma Agent returned an invalid event status.");
  const errorValue = item.error === null || item.error === undefined ? null : record(item.error);
  const outcome = item.design_outcome == null ? null : record(item.design_outcome);
  if (outcome && !["draft", "partial", "complete"].includes(String(outcome.project_readiness))) throw new Error("Forma Agent returned an invalid design outcome.");
  return {
    event_id: stringField(item, "event_id"),
    sequence: Number(item.sequence),
    session_id: stringField(item, "session_id"),
    project_id: stringField(item, "project_id"),
    kind: kind as OpenCodeEvent["kind"],
    status: status as OpenCodeEvent["status"],
    message: nullableStringField(item, "message"),
    revision_id: nullableStringField(item, "revision_id"),
    design_outcome: outcome ? { project_readiness: outcome.project_readiness as "draft" | "partial" | "complete" } : null,
    error: errorValue
      ? {
          code: stringField(errorValue, "code"),
          message: stringField(errorValue, "message"),
          correlation_id: stringField(errorValue, "correlation_id"),
        }
      : null,
    created_at: stringField(item, "created_at"),
  };
}

async function responseJson(response: Response): Promise<unknown> {
  if (!response.ok) {
    const body = await response.json().catch(() => null) as unknown;
    const detail = body && typeof body === "object" && body !== null && "detail" in body ? (body as { detail?: unknown }).detail : null;
    const message = detail && typeof detail === "object" && detail !== null && "message" in detail
      ? (detail as { message?: unknown }).message
      : null;
    throw new Error(typeof message === "string" ? message : "Forma Agent request failed.");
  }
  return response.json();
}

function selectedRuntimeConnectorId(defaultConnectorId: string): string {
  if (typeof window === "undefined") return defaultConnectorId;
  const selected = window.localStorage.getItem(FORMA_AGENT_RUNTIME_STORAGE_KEY)?.trim();
  return selected || defaultConnectorId;
}

export async function createOpenCodeSession(
  apiUrl: string,
  headers: Record<string, string>,
  connectorId: string,
  projectId?: string | null,
): Promise<OpenCodeSession> {
  const selectedConnectorId = selectedRuntimeConnectorId(connectorId);
  const response = await fetch(`${apiUrl}/opencode/sessions`, {
    method: "POST",
    headers,
    body: JSON.stringify({ connector_id: selectedConnectorId, ...(projectId ? { project_id: projectId } : {}) }),
  });
  return parseSession(await responseJson(response));
}

export async function submitOpenCodeCommand(
  apiUrl: string,
  headers: Record<string, string>,
  sessionId: string,
  message: string,
): Promise<OpenCodeCommand> {
  const response = await fetch(`${apiUrl}/opencode/sessions/${encodeURIComponent(sessionId)}/commands`, {
    method: "POST",
    headers,
    body: JSON.stringify({ message, idempotency_key: `web-${crypto.randomUUID()}` }),
  });
  return parseCommand(await responseJson(response));
}

export async function listOpenCodeEvents(
  apiUrl: string,
  headers: Record<string, string>,
  sessionId: string,
  cursor: number,
): Promise<OpenCodeEventPage> {
  const response = await fetch(`${apiUrl}/opencode/sessions/${encodeURIComponent(sessionId)}/events?cursor=${cursor}&limit=100`, {
    headers,
    cache: "no-store",
  });
  const value = record(await responseJson(response));
  const events = value.events;
  if (!Array.isArray(events) || !Number.isInteger(Number(value.next_cursor))) throw new Error("Forma Agent returned an invalid event page.");
  return { events: events.map(parseEvent), next_cursor: Number(value.next_cursor) };
}

export async function cancelOpenCodeSession(
  apiUrl: string,
  headers: Record<string, string>,
  sessionId: string,
): Promise<void> {
  const response = await fetch(`${apiUrl}/opencode/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
    headers,
  });
  await responseJson(response);
}
