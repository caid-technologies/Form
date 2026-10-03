export type ConversationBuildMessage = {
  status?: "idle" | "loading" | "success" | "error" | "cancelled" | "handed-off" | "interrupted";
  projectId?: string | null;
  contextProjectId?: string | null;
  buildPlanId?: string | null;
  buildJobId?: string | null;
};

export function contextBuildControls(
  message: ConversationBuildMessage,
  recoveryBlocked = false,
) {
  const hasProject = Boolean(message.contextProjectId);
  const hasPlan = Boolean(message.buildPlanId);
  const hasJob = Boolean(message.buildJobId);

  return {
    canStop: message.status === "loading" && hasProject && hasPlan,
    canReset:
      message.status === "error"
      && hasProject
      && hasPlan
      && hasJob
      && !recoveryBlocked,
  };
}

export function latestRetryableContextBuildMessage<T extends ConversationBuildMessage>(
  messages: T[],
): T | null {
  const latestBuildMessage = [...messages].reverse().find((message) => (
    Boolean(message.contextProjectId)
    && Boolean(message.buildPlanId)
    && Boolean(message.buildJobId)
  ));
  return latestBuildMessage?.status === "error" ? latestBuildMessage : null;
}

export function shouldOfferFailedBuildRetry({
  canRetryFailedBuild,
  hasInput,
  generationActive,
}: {
  canRetryFailedBuild: boolean;
  hasInput: boolean;
  generationActive: boolean;
}) {
  return canRetryFailedBuild && !hasInput && !generationActive;
}
