type HistoryMessage = { id?: string; role?: string; content?: string; projectId?: string | null };

export function hasConversationHistory(messages: unknown[]): boolean {
  return messages.some((value) => {
    if (!value || typeof value !== "object") return false;
    const message = value as HistoryMessage;
    return typeof message.content === "string"
      && (message.role === "user" || message.role === "assistant")
      && message.id !== "assistant-welcome"
      && !(typeof message.id === "string" && message.id.startsWith("project-context:"))
      && Boolean(message.content.trim())
      && message.content !== "OpenCode project"
      && !(message.role === "assistant" && message.projectId
        && message.content.endsWith(" is the active project for this chat."));
  });
}

// Loading a project must never PUT a synthetic conversation over saved history.
// The OpenCode archive recovers turns created before browser persistence worked.
export async function loadProjectChatHistory({
  apiUrl, chatId, projectId, headers, recoverOpenCode, signal,
}: {
  apiUrl: string;
  chatId: string;
  projectId: string;
  headers: Record<string, string>;
  recoverOpenCode: boolean;
  signal?: AbortSignal;
}): Promise<HistoryMessage[]> {
  const response = await fetch(`${apiUrl}/chats/${encodeURIComponent(chatId)}`, {
    headers, signal, cache: "no-store",
  });
  if (!response.ok && response.status !== 404) throw new Error("Saved chat history could not be loaded.");
  if (response.ok) {
    const chat = await response.json();
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    if (hasConversationHistory(messages)) return messages;
  }
  if (!recoverOpenCode) return [];
  const history = await fetch(`${apiUrl}/opencode/projects/${encodeURIComponent(projectId)}/history`, {
    headers, signal, cache: "no-store",
  });
  if (!history.ok) throw new Error("Recorded Forma Agent history could not be loaded.");
  const result = await history.json();
  return Array.isArray(result?.messages) ? result.messages : [];
}
