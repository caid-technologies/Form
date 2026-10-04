/** Build a project-only link pinned to an immutable revision. */
export function sharedProjectUrl(origin: string, projectId: string, revisionId: string, token: string): string {
  const url = new URL(`/project/${encodeURIComponent(projectId)}/shared`, origin);
  url.searchParams.set("revision", revisionId);
  // Keep the capability out of HTTP request URLs and Referer headers.
  url.hash = new URLSearchParams({ share: token }).toString();
  return url.href;
}
