/** Small UI state only. Never put project payloads or mesh buffers in this state. */
export type ChatProjectLayoutState = {
  desktopOpen: boolean;
  narrowPane: "chat" | "project";
  fullScreen: boolean;
  chatFraction: number;
};

export const CHAT_PROJECT_SPLIT_MIN_WIDTH = 960;
export const MIN_CHAT_FRACTION = 0.32;
export const MAX_CHAT_FRACTION = 0.60;
export const DEFAULT_CHAT_FRACTION = 0.42;

export function initialChatProjectLayout(): ChatProjectLayoutState {
  return { desktopOpen: true, narrowPane: "chat", fullScreen: false, chatFraction: DEFAULT_CHAT_FRACTION };
}

export function clampChatFraction(value: number): number {
  return Number.isFinite(value)
    ? Math.min(MAX_CHAT_FRACTION, Math.max(MIN_CHAT_FRACTION, value))
    : DEFAULT_CHAT_FRACTION;
}

export function projectPaneVisible(state: ChatProjectLayoutState, hasProject: boolean, wide: boolean): boolean {
  return hasProject && (state.fullScreen || (wide ? state.desktopOpen : state.narrowPane === "project"));
}

export type ProjectMessageReference = {
  role: string;
  status?: string;
  projectId?: string | null;
  revisionId?: string | null;
};

export function completedProjectReference(message: ProjectMessageReference): string | null {
  if (message.role !== "assistant") return null;
  // Older saved messages may omit status, but in-flight/failed turns are never artifacts.
  if (message.status && message.status !== "success" && message.status !== "idle") return null;
  const id = message.projectId?.trim();
  return id || null;
}

export function linkedProjectPath(projectId: string, revisionId?: string | null): string {
  return `/project/${encodeURIComponent(projectId)}${revisionId ? `?revision=${encodeURIComponent(revisionId)}` : ""}`;
}
