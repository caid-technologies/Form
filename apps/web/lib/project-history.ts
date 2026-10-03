export type ProjectRevisionSummary = {
  revision_id: string;
  revision: number;
  parent_revision: number | null;
  created_at: string;
  title: string;
  summary: string;
};

export type ProjectRevisionPage = {
  project_id: string;
  items: ProjectRevisionSummary[];
  latest_revision: number | null;
  next_before: number | null;
};

export type ProjectRevisionSnapshot = ProjectRevisionSummary & {
  project_id: string;
  project_ir: Record<string, any>;
};

export function revisionId(value: unknown): string | null {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.trim())
    ? value.trim().toLowerCase() : null;
}

export function revisionNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : null;
}

export function revisionFromProject(project: any): { revisionId: string | null } {
  return { revisionId: revisionId(project?.assembly_metadata?.canonical_revision_id) };
}

export function mergeRevisionPages(current: ProjectRevisionSummary[], incoming: ProjectRevisionSummary[]): ProjectRevisionSummary[] {
  const entries = new Map(current.map((item) => [item.revision_id, item]));
  incoming.forEach((item) => entries.set(item.revision_id, item));
  return [...entries.values()].sort((a, b) => b.revision - a.revision);
}

function isSummary(value: any): value is ProjectRevisionSummary {
  return Boolean(value && revisionId(value.revision_id) && revisionNumber(value.revision)
    && typeof value.summary === "string" && typeof value.title === "string" && typeof value.created_at === "string");
}

export async function readHistoryResponse(response: Response): Promise<any> {
  if (!response.ok) {
    if (response.status === 401) throw new Error("Sign in to view version history.");
    if (response.status === 403 || response.status === 404) throw new Error("This saved version or its project is unavailable to this account.");
    throw new Error("Version history could not be loaded. Please try again.");
  }
  return response.json();
}

export function parseRevisionPage(value: any, projectId: string): ProjectRevisionPage {
  if (value?.project_id !== projectId || !Array.isArray(value.items) || !value.items.every(isSummary)
    || (value.latest_revision !== null && !revisionNumber(value.latest_revision))
    || (value.next_before !== null && !revisionNumber(value.next_before))) {
    throw new Error("The saved version list could not be read.");
  }
  return value;
}

export function parseRevisionSnapshot(value: any, projectId: string, id: string): ProjectRevisionSnapshot {
  if (!isSummary(value) || (value as any).project_id !== projectId || value.revision_id !== id
    || !(value as any).project_ir || !Array.isArray((value as any).project_ir.components)) {
    throw new Error("The response did not contain the requested saved version.");
  }
  return value as ProjectRevisionSnapshot;
}
